"""Lightweight micro-profiler for the classification/grouping stage.

The profiler is deliberately optional and deterministic: when disabled, the
helpers below are no-ops.  It measures internal time slices, counters, slow
pages, and slow groups without changing OCR, grouping, quality, or rendering
decisions.
"""

from __future__ import annotations

import csv
import html
import json
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_ACTIVE_PROFILER: "ClassificationProfiler | None" = None


def set_active_profiler(profiler: "ClassificationProfiler | None") -> None:
    global _ACTIVE_PROFILER
    _ACTIVE_PROFILER = profiler


def get_active_profiler() -> "ClassificationProfiler | None":
    return _ACTIVE_PROFILER


def profiling_enabled() -> bool:
    return bool(_ACTIVE_PROFILER and _ACTIVE_PROFILER.enabled)


@contextmanager
def profile_step(name: str, *, page_index: int | None = None, items: int = 0, metadata: dict | None = None):
    profiler = _ACTIVE_PROFILER
    if not profiler or not profiler.enabled:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        profiler.record_step(
            name,
            time.perf_counter() - started,
            page_index=page_index,
            items=items,
            metadata=metadata,
        )


def record_count(name: str, count: int = 1, *, page_index: int | None = None) -> None:
    profiler = _ACTIVE_PROFILER
    if profiler and profiler.enabled:
        profiler.record_count(name, count=count, page_index=page_index)


def record_group(
    *,
    page_index: int | None,
    group_id: str,
    elapsed_seconds: float,
    line_count: int,
    classification: str,
    fallback_used: bool = False,
    dominant_step: str = "",
) -> None:
    profiler = _ACTIVE_PROFILER
    if profiler and profiler.enabled:
        profiler.record_group(
            page_index=page_index,
            group_id=group_id,
            elapsed_seconds=elapsed_seconds,
            line_count=line_count,
            classification=classification,
            fallback_used=fallback_used,
            dominant_step=dominant_step,
        )


@dataclass
class StepStats:
    calls: int = 0
    total_seconds: float = 0.0
    durations: list[float] = field(default_factory=list)
    items: int = 0
    max_seconds: float = 0.0
    max_metadata: dict[str, Any] = field(default_factory=dict)

    def add(self, elapsed_seconds: float, *, items: int = 0, metadata: dict | None = None) -> None:
        elapsed_seconds = max(0.0, float(elapsed_seconds or 0.0))
        self.calls += 1
        self.total_seconds += elapsed_seconds
        self.durations.append(elapsed_seconds)
        self.items += int(items or 0)
        if elapsed_seconds >= self.max_seconds:
            self.max_seconds = elapsed_seconds
            self.max_metadata = dict(metadata or {})

    def as_dict(self, classification_total_seconds: float) -> dict[str, Any]:
        durations_ms = [value * 1000.0 for value in self.durations]
        total_ms = self.total_seconds * 1000.0
        mean_ms = total_ms / self.calls if self.calls else 0.0
        median_ms = statistics.median(durations_ms) if durations_ms else 0.0
        p95_ms = _percentile(durations_ms, 95.0)
        max_ms = self.max_seconds * 1000.0
        percent = (
            (self.total_seconds / classification_total_seconds) * 100.0
            if classification_total_seconds > 0
            else 0.0
        )
        return {
            "total_calls": self.calls,
            "total_time_ms": round(total_ms, 3),
            "percent_of_classification_grouping": round(percent, 3),
            "mean_time_ms": round(mean_ms, 3),
            "median_time_ms": round(median_ms, 3),
            "p95_time_ms": round(p95_ms, 3),
            "max_time_ms": round(max_ms, 3),
            "items_processed": self.items,
            "time_per_item_ms": round(total_ms / self.items, 6) if self.items else 0.0,
            "max_metadata": self.max_metadata,
        }


class ClassificationProfiler:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self.steps: dict[str, StepStats] = {}
        self.counts: dict[str, int] = {}
        self.page_counts: dict[int, dict[str, int]] = {}
        self.page_records: dict[int, dict[str, Any]] = {}
        self.group_records: list[dict[str, Any]] = []
        self.classification_total_seconds: float = 0.0

    def start_page(self, page_index: int, *, raw_line_count: int = 0) -> None:
        if not self.enabled:
            return
        record = self.page_records.setdefault(
            int(page_index),
            {
                "page_index": int(page_index),
                "raw_lines": int(raw_line_count or 0),
                "group_count": 0,
                "translatable_groups": 0,
                "fallback_count": 0,
                "repair_count": 0,
                "classification_grouping_seconds": 0.0,
                "steps": {},
                "counts": {},
            },
        )
        record["raw_lines"] = int(raw_line_count or record.get("raw_lines") or 0)

    def finish_page(
        self,
        page_index: int,
        *,
        group_count: int = 0,
        translatable_groups: int = 0,
        fallback_count: int = 0,
        repair_count: int = 0,
        classification_grouping_seconds: float = 0.0,
    ) -> None:
        if not self.enabled:
            return
        record = self.page_records.setdefault(int(page_index), {"page_index": int(page_index), "steps": {}, "counts": {}})
        record.update(
            {
                "group_count": int(group_count or 0),
                "translatable_groups": int(translatable_groups or 0),
                "fallback_count": int(fallback_count or 0),
                "repair_count": int(repair_count or 0),
                "classification_grouping_seconds": round(float(classification_grouping_seconds or 0.0), 6),
            }
        )

    def record_step(
        self,
        name: str,
        elapsed_seconds: float,
        *,
        page_index: int | None = None,
        items: int = 0,
        metadata: dict | None = None,
    ) -> None:
        if not self.enabled:
            return
        self.steps.setdefault(name, StepStats()).add(
            elapsed_seconds,
            items=items,
            metadata=metadata,
        )
        if name == "classification_grouping.page_total":
            self.classification_total_seconds += max(0.0, float(elapsed_seconds or 0.0))
        if page_index is not None:
            page = self.page_records.setdefault(int(page_index), {"page_index": int(page_index), "steps": {}, "counts": {}})
            page_steps = page.setdefault("steps", {})
            page_steps[name] = round(float(page_steps.get(name, 0.0)) + float(elapsed_seconds or 0.0), 6)

    def record_count(self, name: str, *, count: int = 1, page_index: int | None = None) -> None:
        if not self.enabled:
            return
        self.counts[name] = int(self.counts.get(name, 0)) + int(count or 0)
        if page_index is not None:
            page = self.page_records.setdefault(int(page_index), {"page_index": int(page_index), "steps": {}, "counts": {}})
            counts = page.setdefault("counts", {})
            counts[name] = int(counts.get(name, 0)) + int(count or 0)

    def record_group(
        self,
        *,
        page_index: int | None,
        group_id: str,
        elapsed_seconds: float,
        line_count: int,
        classification: str,
        fallback_used: bool = False,
        dominant_step: str = "",
    ) -> None:
        if not self.enabled:
            return
        self.group_records.append(
            {
                "page_index": page_index,
                "group_id": group_id,
                "elapsed_ms": round(float(elapsed_seconds or 0.0) * 1000.0, 3),
                "line_count": int(line_count or 0),
                "classification": classification,
                "fallback_used": bool(fallback_used),
                "dominant_step": dominant_step,
            }
        )

    def summary(self) -> dict[str, Any]:
        classification_total = self.classification_total_seconds or sum(
            stats.total_seconds for name, stats in self.steps.items()
            if not name.endswith(".page_total")
        )
        steps = {
            name: stats.as_dict(classification_total)
            for name, stats in sorted(
                self.steps.items(),
                key=lambda item: item[1].total_seconds,
                reverse=True,
            )
        }
        page_records = []
        for record in self.page_records.values():
            steps_for_page = record.get("steps", {})
            top_steps = sorted(
                (
                    {
                        "name": name,
                        "seconds": seconds,
                    }
                    for name, seconds in steps_for_page.items()
                    if name != "classification_grouping.page_total"
                ),
                key=lambda item: item["seconds"],
                reverse=True,
            )[:3]
            enriched = dict(record)
            enriched["top_steps"] = top_steps
            enriched["dominant_step"] = top_steps[0]["name"] if top_steps else ""
            page_records.append(enriched)
        page_records.sort(
            key=lambda item: float(item.get("classification_grouping_seconds") or 0.0),
            reverse=True,
        )
        groups = sorted(
            self.group_records,
            key=lambda item: float(item.get("elapsed_ms") or 0.0),
            reverse=True,
        )
        return {
            "enabled": self.enabled,
            "classification_grouping_total_ms": round(classification_total * 1000.0, 3),
            "steps": steps,
            "counts": dict(sorted(self.counts.items())),
            "top_steps": [
                {"name": name, **stats}
                for name, stats in list(steps.items())[:20]
            ],
            "slowest_pages": page_records[:20],
            "slowest_groups": groups[:50],
        }

    def write_reports(self, output_folder: str | Path) -> dict[str, str]:
        if not self.enabled:
            return {}
        output = Path(output_folder)
        output.mkdir(parents=True, exist_ok=True)
        payload = self.summary()
        json_path = output / "classification_profile.json"
        csv_path = output / "classification_profile.csv"
        html_path = output / "classification_profile.html"
        slow_pages_path = output / "classification_slowest_pages.csv"
        slow_groups_path = output / "classification_slowest_groups.csv"

        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_steps_csv(csv_path, payload)
        self._write_pages_csv(slow_pages_path, payload)
        self._write_groups_csv(slow_groups_path, payload)
        html_path.write_text(self._html(payload), encoding="utf-8")
        return {
            "classification_profile_json": str(json_path),
            "classification_profile_html": str(html_path),
            "classification_profile_csv": str(csv_path),
            "classification_slowest_pages_csv": str(slow_pages_path),
            "classification_slowest_groups_csv": str(slow_groups_path),
        }

    def _write_steps_csv(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow([
                "step",
                "total_calls",
                "total_time_ms",
                "percent",
                "mean_time_ms",
                "median_time_ms",
                "p95_time_ms",
                "max_time_ms",
                "items_processed",
                "time_per_item_ms",
            ])
            for name, stats in payload.get("steps", {}).items():
                writer.writerow([
                    name,
                    stats.get("total_calls"),
                    stats.get("total_time_ms"),
                    stats.get("percent_of_classification_grouping"),
                    stats.get("mean_time_ms"),
                    stats.get("median_time_ms"),
                    stats.get("p95_time_ms"),
                    stats.get("max_time_ms"),
                    stats.get("items_processed"),
                    stats.get("time_per_item_ms"),
                ])

    def _write_pages_csv(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow([
                "page_index",
                "classification_grouping_seconds",
                "raw_lines",
                "group_count",
                "translatable_groups",
                "fallback_count",
                "repair_count",
                "dominant_step",
                "top_steps",
            ])
            for page in payload.get("slowest_pages", []):
                writer.writerow([
                    page.get("page_index"),
                    page.get("classification_grouping_seconds"),
                    page.get("raw_lines"),
                    page.get("group_count"),
                    page.get("translatable_groups"),
                    page.get("fallback_count"),
                    page.get("repair_count"),
                    page.get("dominant_step"),
                    json.dumps(page.get("top_steps", []), ensure_ascii=False),
                ])

    def _write_groups_csv(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow([
                "page_index",
                "group_id",
                "elapsed_ms",
                "line_count",
                "classification",
                "fallback_used",
                "dominant_step",
            ])
            for group in payload.get("slowest_groups", []):
                writer.writerow([
                    group.get("page_index"),
                    group.get("group_id"),
                    group.get("elapsed_ms"),
                    group.get("line_count"),
                    group.get("classification"),
                    group.get("fallback_used"),
                    group.get("dominant_step"),
                ])

    def _html(self, payload: dict[str, Any]) -> str:
        step_rows = []
        for step in payload.get("top_steps", [])[:20]:
            step_rows.append(
                "<tr>"
                f"<td>{html.escape(step['name'])}</td>"
                f"<td>{step.get('total_time_ms')}</td>"
                f"<td>{step.get('percent_of_classification_grouping')}%</td>"
                f"<td>{step.get('total_calls')}</td>"
                f"<td>{step.get('mean_time_ms')}</td>"
                f"<td>{step.get('p95_time_ms')}</td>"
                f"<td>{step.get('max_time_ms')}</td>"
                "</tr>"
            )
        page_rows = []
        for page in payload.get("slowest_pages", [])[:10]:
            page_rows.append(
                "<tr>"
                f"<td>{page.get('page_index')}</td>"
                f"<td>{page.get('classification_grouping_seconds')}</td>"
                f"<td>{page.get('raw_lines')}</td>"
                f"<td>{page.get('group_count')}</td>"
                f"<td>{page.get('fallback_count')}</td>"
                f"<td>{html.escape(page.get('dominant_step') or '')}</td>"
                "</tr>"
            )
        group_rows = []
        for group in payload.get("slowest_groups", [])[:10]:
            group_rows.append(
                "<tr>"
                f"<td>{group.get('page_index')}</td>"
                f"<td>{html.escape(str(group.get('group_id') or ''))}</td>"
                f"<td>{group.get('elapsed_ms')}</td>"
                f"<td>{group.get('line_count')}</td>"
                f"<td>{html.escape(str(group.get('classification') or ''))}</td>"
                f"<td>{group.get('fallback_used')}</td>"
                "</tr>"
            )
        return f"""<!doctype html>
<html lang=\"pt-BR\">
<head>
  <meta charset=\"utf-8\">
  <title>Classification profile</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; background: #0f141b; color: #e8eef7; margin: 24px; }}
    h1, h2 {{ color: #7dd3fc; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px; background: #151d27; }}
    th, td {{ border: 1px solid #2b3a4a; padding: 8px 10px; font-size: 13px; text-align: left; }}
    th {{ background: #203044; color: #bfdbfe; }}
    .card {{ background: #151d27; border: 1px solid #2b3a4a; border-radius: 14px; padding: 16px; margin-bottom: 18px; }}
  </style>
</head>
<body>
  <h1>Classification/grouping profile</h1>
  <div class=\"card\">Total medido: <strong>{payload.get('classification_grouping_total_ms')} ms</strong></div>
  <h2>Top subetapas</h2>
  <table><thead><tr><th>Subetapa</th><th>Total ms</th><th>%</th><th>Calls</th><th>Média ms</th><th>P95 ms</th><th>Max ms</th></tr></thead><tbody>{''.join(step_rows)}</tbody></table>
  <h2>Top páginas lentas</h2>
  <table><thead><tr><th>Página</th><th>Segundos</th><th>Linhas</th><th>Grupos</th><th>Fallbacks</th><th>Dominante</th></tr></thead><tbody>{''.join(page_rows)}</tbody></table>
  <h2>Top grupos lentos</h2>
  <table><thead><tr><th>Página</th><th>Grupo</th><th>ms</th><th>Linhas</th><th>Classe</th><th>Fallback</th></tr></thead><tbody>{''.join(group_rows)}</tbody></table>
</body>
</html>"""


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * (percentile / 100.0)
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    weight = rank - low
    return values[low] * (1.0 - weight) + values[high] * weight
