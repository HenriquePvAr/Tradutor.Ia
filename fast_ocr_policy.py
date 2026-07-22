"""Bounded native OCR fallbacks used by the predictable ``fast`` mode."""

from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass, field
from typing import Any

import config


@dataclass
class FastOCRBudget:
    """Track page/region fallback limits without changing OCR decisions globally."""

    enabled: bool = False
    heavy_fallback: bool = False
    page_timeout_seconds: float = 45.0
    region_timeout_seconds: float = 12.0
    max_full_pages: int = 0
    max_full_regions: int = 4
    total_budget_seconds: float = 60.0
    full_pages_used: int = 0
    full_regions_used: int = 0
    fallback_seconds: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_config(cls, *, fast: bool) -> "FastOCRBudget":
        return cls(
            enabled=bool(fast),
            heavy_fallback=bool(getattr(config, "FAST_OCR_HEAVY_FALLBACK", False)),
            page_timeout_seconds=float(getattr(config, "FAST_OCR_PAGE_TIMEOUT_SECONDS", 45.0)),
            region_timeout_seconds=float(getattr(config, "FAST_OCR_REGION_TIMEOUT_SECONDS", 12.0)),
            max_full_pages=int(getattr(config, "FAST_OCR_FULL_FALLBACK_MAX_PAGES", 0)),
            max_full_regions=int(getattr(config, "FAST_OCR_FULL_FALLBACK_MAX_REGIONS", 4)),
            total_budget_seconds=float(
                getattr(config, "FAST_OCR_TOTAL_FALLBACK_BUDGET_SECONDS", 60.0)
            ),
        )

    def allow(self, *, kind: str, page: int | None = None) -> tuple[bool, str]:
        if not self.enabled or not self.heavy_fallback:
            return False, "fast_ocr_heavy_fallback_disabled"
        if self.total_budget_seconds <= self.fallback_seconds:
            return False, "fast_ocr_fallback_budget_exhausted"
        if kind == "paddle_full_page" and self.full_pages_used >= self.max_full_pages:
            return False, "fast_ocr_full_page_limit"
        if kind == "paddle_full_region" and self.full_regions_used >= self.max_full_regions:
            return False, "fast_ocr_full_region_limit"
        return True, ""

    def record(self, *, kind: str, elapsed: float, page: int | None = None, timed_out: bool = False) -> None:
        elapsed = max(0.0, float(elapsed))
        self.fallback_seconds += elapsed
        if kind == "paddle_full_page":
            self.full_pages_used += 1
        elif kind == "paddle_full_region":
            self.full_regions_used += 1
        self.events.append(
            {
                "kind": kind,
                "page": page,
                "elapsed_seconds": round(elapsed, 3),
                "timed_out": bool(timed_out),
            }
        )

    def report(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "heavy_fallback": self.heavy_fallback,
            "page_timeout_seconds": self.page_timeout_seconds,
            "region_timeout_seconds": self.region_timeout_seconds,
            "max_full_pages": self.max_full_pages,
            "max_full_regions": self.max_full_regions,
            "total_budget_seconds": self.total_budget_seconds,
            "full_pages_used": self.full_pages_used,
            "full_regions_used": self.full_regions_used,
            "fallback_seconds": round(self.fallback_seconds, 3),
            "events": list(self.events),
        }


def _serialize_line(line: Any) -> dict[str, Any]:
    polygon = getattr(line, "polygon", [])
    if hasattr(polygon, "tolist"):
        polygon = polygon.tolist()
    else:
        polygon = list(polygon or [])
    return {
        "text": str(getattr(line, "text", "") or ""),
        "confidence": float(getattr(line, "confidence", 0.0) or 0.0),
        "polygon": polygon,
        "box": tuple(getattr(line, "box", (0, 0, 0, 0))),
        "raw_text": str(getattr(line, "raw_text", "") or ""),
        "engine": str(getattr(line, "engine", "") or ""),
        "page": getattr(line, "page", None),
        "metadata": getattr(line, "metadata", None),
    }


def _run_ocr_child(connection, image, lang: str, engine_name: str, page: int | None) -> None:
    """Run one native OCR attempt in an owned process that can actually be stopped."""

    try:
        from ocr_engine import OCREngine

        engine = OCREngine(lang, engine=engine_name, fallback_engine="")
        lines = engine.detect_lines(image, page=page)
        connection.send(
            {
                "ok": True,
                "lines": [_serialize_line(line) for line in lines],
                "metadata": dict(engine.last_run_metadata or {}),
            }
        )
    except BaseException as exc:  # child reports; parent owns the lifecycle
        connection.send({"ok": False, "error": type(exc).__name__})
    finally:
        connection.close()


def run_ocr_with_timeout(
    image,
    *,
    lang: str,
    engine_name: str,
    page: int | None,
    timeout_seconds: float,
    worker_target=None,
):
    """Return ``(lines, metadata)`` and terminate only the owned OCR child on timeout."""

    from ocr_engine import OCRLine
    import numpy as np

    from process_options import configure_hidden_multiprocessing

    configure_hidden_multiprocessing()
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    target = worker_target or _run_ocr_child
    target_args = (child, image, str(lang), str(engine_name), page)
    if worker_target is not None:
        target_args = (child,)
    process = ctx.Process(
        target=target,
        args=target_args,
        daemon=True,
    )
    started = time.perf_counter()
    process.start()
    child.close()
    process.join(max(0.1, float(timeout_seconds)))
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(2.0)
        if process.is_alive():
            process.kill()
            process.join(2.0)
        return [], {"timeout": True, "timeout_seconds": float(timeout_seconds)}
    payload = parent.recv() if parent.poll(0.25) else {"ok": False, "error": "ocr_child_no_result"}
    elapsed = time.perf_counter() - started
    if not payload.get("ok"):
        return [], {"error": payload.get("error", "ocr_child_failed"), "elapsed_seconds": elapsed}
    lines = []
    for item in payload.get("lines", []):
        lines.append(
            OCRLine(
                text=item.get("text", ""),
                confidence=float(item.get("confidence", 0.0)),
                polygon=np.asarray(item.get("polygon", []), dtype=np.int32),
                box=tuple(item.get("box", (0, 0, 0, 0))),
                raw_text=item.get("raw_text", ""),
                engine=item.get("engine", engine_name),
                page=item.get("page", page),
                metadata=item.get("metadata"),
            )
        )
    metadata = dict(payload.get("metadata") or {})
    metadata["elapsed_seconds"] = elapsed
    return lines, metadata
