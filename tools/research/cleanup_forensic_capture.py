"""Opt-in, failure-tolerant capture of exact cleanup intermediates.

This module is deliberately inert unless a caller supplies an explicit root.
It serializes copies of arrays and compact provenance; it never participates in
cleanup decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import cv2
import numpy as np


def _safe(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))
    return text.strip("._") or "unknown"


def _hash_array(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


class CleanupForensicCapture:
    """Capture exact cleanup arrays when explicitly enabled by a caller."""

    schema = "tradutoria-cleanup-forensic-v1"

    def __init__(self, root):
        self.root = Path(root)
        self._latest: dict[tuple[str, str], Path] = {}

    @property
    def enabled(self) -> bool:
        return True

    def _directory(self, group, strategy: str) -> Path:
        page = getattr(group.lines[0], "page", None) if getattr(group, "lines", None) else None
        capture_id = "page_%s_%s_%s" % (
            _safe(page if page is not None else "unknown"),
            _safe(getattr(group, "group_id", "group")),
            _safe(strategy),
        )
        directory = self.root / "cleanup" / capture_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _line_record(line, index: int) -> dict:
        polygon = np.asarray(getattr(line, "polygon", []), dtype=np.int32)
        return {
            "index": int(index),
            "text": str(getattr(line, "text", "")),
            "raw_text": str(getattr(line, "raw_text", "")),
            "confidence": float(getattr(line, "confidence", 0.0) or 0.0),
            "polygon": polygon.tolist(),
            "box": [int(v) for v in (getattr(line, "box", ()) or ())],
            "engine": str(getattr(line, "engine", "")),
            "page": getattr(line, "page", None),
        }

    def capture_cleanup(self, group, *, strategy, pre_cleanup, mask, post_cleanup, metrics):
        """Persist one actual cleanup invocation; never raises into production."""
        try:
            directory = self._directory(group, strategy)
            pre = np.asarray(pre_cleanup)
            actual_mask = np.asarray(mask)
            post = np.asarray(post_cleanup)
            cv2.imwrite(str(directory / "pre_cleanup.png"), pre)
            cv2.imwrite(str(directory / "mask.png"), actual_mask)
            cv2.imwrite(str(directory / "post_cleanup.png"), post)
            metadata = {
                "schema": self.schema,
                "capture_id": directory.name,
                "group_id": str(getattr(group, "group_id", "")),
                "page": getattr(group.lines[0], "page", None) if getattr(group, "lines", None) else None,
                "cleanup_strategy": str(strategy),
                "background_type": str(getattr(group, "background_type", "")),
                "art_fidelity_uncertain": bool((metrics or {}).get("art_fidelity_uncertain", False)),
                "ocr_lines": [self._line_record(line, i) for i, line in enumerate(getattr(group, "lines", []) or [])],
                "canvas": {"shape": list(pre.shape), "dtype": str(pre.dtype)},
                "mask": {
                    "shape": list(actual_mask.shape),
                    "dtype": str(actual_mask.dtype),
                    "nonzero_pixel_count": int(np.count_nonzero(actual_mask)),
                    "sha256": _hash_array(actual_mask),
                },
                "arrays": {
                    "pre_cleanup_sha256": _hash_array(pre),
                    "post_cleanup_sha256": _hash_array(post),
                },
                "cleanup_metrics": _json_value(metrics or {}),
                "pre_typography_distinct": False,
            }
            with (directory / "metadata.json").open("w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
            self._latest[(str(getattr(group, "group_id", "")), str(strategy))] = directory
        except Exception as exc:  # diagnostics must never break image behavior
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with (self.root / "capture_errors.log").open("a", encoding="utf-8") as handle:
                    handle.write(f"cleanup capture failed: {type(exc).__name__}: {exc}\n")
            except Exception:
                pass

    def record_pre_typography(self, group, *, strategy, canvas):
        """Record whether the canvas immediately before drawing is distinct."""
        try:
            directory = self._latest.get((str(getattr(group, "group_id", "")), str(strategy)))
            if directory is None:
                return
            metadata_path = directory / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            current = np.asarray(canvas)
            post = cv2.imread(str(directory / "post_cleanup.png"), cv2.IMREAD_UNCHANGED)
            distinct = post is None or not np.array_equal(post, current)
            metadata["pre_typography_distinct"] = bool(distinct)
            metadata["arrays"]["pre_typography_sha256"] = _hash_array(current)
            if distinct:
                cv2.imwrite(str(directory / "pre_typography.png"), current)
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return
