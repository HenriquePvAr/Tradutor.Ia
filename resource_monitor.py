"""Optional lightweight resource telemetry for pipeline benchmarks.

The monitor is deliberately best-effort: if psutil is unavailable or a metric
cannot be read on the current platform, the pipeline keeps running normally.
"""

from __future__ import annotations

import csv
import html
import json
import os
import statistics
import threading
import time
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised through the no-psutil unit test path.
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


MB = 1024 * 1024


class ResourceMonitor:
    def __init__(
        self,
        output_folder: str | Path,
        *,
        enabled: bool = True,
        interval_seconds: float = 1.0,
        total_pages: int = 0,
    ) -> None:
        self.output_folder = Path(output_folder)
        self.enabled = bool(enabled and psutil is not None)
        self.interval_seconds = max(0.25, float(interval_seconds or 1.0))
        self.total_pages = int(total_pages or 0)
        self.stage = "preparing"
        self.pages_done = 0
        self.queue_depth = 0
        self.active_workers = 0
        self.worker_roles: dict[int, str] = {}
        self.samples: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._started = 0.0
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._parent = psutil.Process(os.getpid()) if self.enabled else None

    @property
    def unavailable_reason(self) -> str:
        return "" if self.enabled else "psutil_unavailable_or_disabled"

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self.output_folder.mkdir(parents=True, exist_ok=True)
        self._started = time.monotonic()
        self._prime_cpu_counters()
        self._thread = threading.Thread(
            target=self._run,
            name="resource-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "reason": self.unavailable_reason,
                "resource_report_json": "",
                "resource_report_html": "",
                "resource_timeline_csv": "",
            }
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.interval_seconds * 2))
        self._sample_once()
        return self.write_reports()

    def set_stage(self, stage: str) -> None:
        with self._lock:
            if stage != self.stage:
                self.stage = str(stage)
                self.events.append(self._event("stage", {"stage": self.stage}))

    def set_progress(
        self,
        *,
        pages_done: int | None = None,
        pages_total: int | None = None,
        queue_depth: int | None = None,
        active_workers: int | None = None,
    ) -> None:
        with self._lock:
            if pages_done is not None:
                self.pages_done = max(0, int(pages_done))
            if pages_total is not None:
                self.total_pages = max(0, int(pages_total))
            if queue_depth is not None:
                self.queue_depth = max(0, int(queue_depth))
            if active_workers is not None:
                self.active_workers = max(0, int(active_workers))

    def register_worker_roles(self, pids: list[int] | tuple[int, ...], role: str) -> None:
        with self._lock:
            for pid in pids:
                try:
                    self.worker_roles[int(pid)] = role
                except (TypeError, ValueError):
                    continue

    def record_event(self, name: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.events.append(self._event(name, payload or {}))

    def _event(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "elapsed_s": round(time.monotonic() - self._started, 3)
            if self._started
            else 0.0,
            "name": name,
            **payload,
        }

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._sample_once()

    def _prime_cpu_counters(self) -> None:
        try:
            psutil.cpu_percent(interval=None, percpu=False)
            psutil.cpu_percent(interval=None, percpu=True)
            if self._parent:
                self._parent.cpu_percent(interval=None)
                for child in self._parent.children(recursive=True):
                    child.cpu_percent(interval=None)
        except Exception:
            pass

    def _sample_once(self) -> None:
        if not self.enabled or not self._parent:
            return
        try:
            with self._lock:
                stage = self.stage
                pages_done = self.pages_done
                total_pages = self.total_pages
                queue_depth = self.queue_depth
                active_workers = self.active_workers
                worker_roles = dict(self.worker_roles)
            system = self._system_metrics()
            parent = self._process_metrics(self._parent)
            children = []
            for child in self._parent.children(recursive=True):
                metrics = self._process_metrics(child)
                if metrics:
                    metrics["role"] = worker_roles.get(metrics["pid"], "child")
                    children.append(metrics)
            children_rss_mb = sum(float(item.get("rss_mb") or 0.0) for item in children)
            sample = {
                "timestamp": time.time(),
                "elapsed_s": round(time.monotonic() - self._started, 3),
                "stage": stage,
                "pages_done": pages_done,
                "pages_total": total_pages,
                "queue_depth": queue_depth,
                "active_workers": active_workers or len(children),
                "parent_pid": parent.get("pid"),
                "parent_rss_mb": parent.get("rss_mb", 0.0),
                "parent_vms_mb": parent.get("vms_mb", 0.0),
                "parent_cpu_percent": parent.get("cpu_percent", 0.0),
                "parent_threads": parent.get("threads", 0),
                "child_count": len(children),
                "children_rss_mb": round(children_rss_mb, 3),
                "total_pipeline_rss_mb": round(
                    float(parent.get("rss_mb") or 0.0) + children_rss_mb,
                    3,
                ),
                "children": children,
                **system,
            }
            with self._lock:
                self.samples.append(sample)
        except Exception as exc:
            self.record_event("sample_error", {"error": str(exc)})

    def _system_metrics(self) -> dict[str, Any]:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        return {
            "system_total_mb": round(virtual.total / MB, 3),
            "system_available_mb": round(virtual.available / MB, 3),
            "system_used_mb": round(virtual.used / MB, 3),
            "system_ram_percent": round(float(virtual.percent), 3),
            "swap_total_mb": round(swap.total / MB, 3),
            "swap_used_mb": round(swap.used / MB, 3),
            "swap_percent": round(float(swap.percent), 3),
            "cpu_percent": round(float(psutil.cpu_percent(interval=None)), 3),
            "per_core_cpu": [round(float(item), 3) for item in per_core],
            "physical_cores": psutil.cpu_count(logical=False) or 0,
            "logical_cores": psutil.cpu_count(logical=True) or 0,
        }

    def _process_metrics(self, process: Any) -> dict[str, Any]:
        try:
            with process.oneshot():
                mem = process.memory_info()
                return {
                    "pid": int(process.pid),
                    "name": process.name(),
                    "status": process.status(),
                    "rss_mb": round(mem.rss / MB, 3),
                    "vms_mb": round(mem.vms / MB, 3),
                    "cpu_percent": round(float(process.cpu_percent(interval=None)), 3),
                    "threads": int(process.num_threads()),
                }
        except Exception:
            return {}

    def write_reports(self) -> dict[str, Any]:
        self.output_folder.mkdir(parents=True, exist_ok=True)
        report_path = self.output_folder / "resource_report.json"
        html_path = self.output_folder / "resource_report.html"
        csv_path = self.output_folder / "resource_timeline.csv"
        summary = self._summary()
        payload = {
            "enabled": True,
            "interval_seconds": self.interval_seconds,
            "summary": summary,
            "events": list(self.events),
            "samples": list(self.samples),
            "resource_report_json": str(report_path),
            "resource_report_html": str(html_path),
            "resource_timeline_csv": str(csv_path),
        }
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._write_csv(csv_path)
        html_path.write_text(self._html(payload), encoding="utf-8")
        return {
            "enabled": True,
            **summary,
            "resource_report_json": str(report_path),
            "resource_report_html": str(html_path),
            "resource_timeline_csv": str(csv_path),
        }

    def _summary(self) -> dict[str, Any]:
        samples = list(self.samples)
        if not samples:
            return {"sample_count": 0}
        fields = [
            "total_pipeline_rss_mb",
            "parent_rss_mb",
            "children_rss_mb",
            "system_available_mb",
            "system_ram_percent",
            "swap_used_mb",
            "cpu_percent",
        ]
        summary: dict[str, Any] = {"sample_count": len(samples)}
        for field in fields:
            values = [float(sample.get(field) or 0.0) for sample in samples]
            summary[f"{field}_avg"] = round(statistics.fmean(values), 3)
            summary[f"{field}_peak"] = round(max(values), 3)
        child_peaks: dict[int, float] = {}
        child_roles: dict[int, str] = dict(self.worker_roles)
        for sample in samples:
            for child in sample.get("children", []):
                pid = int(child.get("pid"))
                child_peaks[pid] = max(
                    child_peaks.get(pid, 0.0),
                    float(child.get("rss_mb") or 0.0),
                )
                child_roles.setdefault(pid, child.get("role", "child"))
        summary["child_peak_rss_mb"] = {
            str(pid): round(value, 3) for pid, value in sorted(child_peaks.items())
        }
        summary["child_roles"] = {str(pid): role for pid, role in sorted(child_roles.items())}
        ocr_worker_peaks = [
            value
            for pid, value in child_peaks.items()
            if child_roles.get(pid) == "ocr-worker"
        ]
        if ocr_worker_peaks:
            summary["ocr_worker_rss_mb_avg"] = round(
                statistics.fmean(ocr_worker_peaks),
                3,
            )
            summary["ocr_worker_rss_mb_peak"] = round(max(ocr_worker_peaks), 3)
        return summary

    def _write_csv(self, path: Path) -> None:
        fields = [
            "timestamp",
            "elapsed_s",
            "stage",
            "pages_done",
            "pages_total",
            "queue_depth",
            "active_workers",
            "parent_rss_mb",
            "children_rss_mb",
            "total_pipeline_rss_mb",
            "system_available_mb",
            "system_ram_percent",
            "cpu_percent",
            "swap_used_mb",
            "child_count",
        ]
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            for sample in self.samples:
                writer.writerow({field: sample.get(field, "") for field in fields})

    def _html(self, payload: dict[str, Any]) -> str:
        summary = payload.get("summary", {})
        rows = "\n".join(
            f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
            for key, value in summary.items()
            if key not in {"child_peak_rss_mb", "child_roles"}
        )
        timeline_rows = "\n".join(
            "<tr>"
            f"<td>{sample.get('elapsed_s')}</td>"
            f"<td>{html.escape(str(sample.get('stage')))}</td>"
            f"<td>{sample.get('pages_done')}/{sample.get('pages_total')}</td>"
            f"<td>{sample.get('total_pipeline_rss_mb')}</td>"
            f"<td>{sample.get('system_available_mb')}</td>"
            f"<td>{sample.get('cpu_percent')}</td>"
            f"<td>{sample.get('active_workers')}</td>"
            "</tr>"
            for sample in self.samples[:: max(1, len(self.samples) // 200 or 1)]
        )
        return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Resource report</title>
<style>
body {{ background:#11131a; color:#eceff7; font-family:Inter,Segoe UI,sans-serif; margin:32px; }}
.card {{ background:#181b24; border:1px solid #2b3140; border-radius:16px; padding:20px; margin:18px 0; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ border-bottom:1px solid #2b3140; padding:8px 10px; text-align:left; }}
th {{ color:#9fd9ff; }}
code {{ color:#d5f7a1; }}
</style>
</head>
<body>
<h1>Resource report</h1>
<div class="card"><h2>Resumo</h2><table>{rows}</table></div>
<div class="card"><h2>Timeline amostrada</h2>
<table><tr><th>elapsed_s</th><th>stage</th><th>pages</th><th>pipeline RSS MB</th><th>available MB</th><th>CPU %</th><th>workers</th></tr>
{timeline_rows}</table></div>
<div class="card"><p>CSV: <code>{html.escape(str(payload.get('resource_timeline_csv')))}</code></p></div>
</body></html>"""


def detect_gpu_basic() -> dict[str, Any]:
    """Return a safe, non-invasive GPU diagnostic."""

    try:
        import subprocess

        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {"available": False, "reason": "gpu_query_unavailable"}
        data = json.loads(result.stdout)
        devices = data if isinstance(data, list) else [data]
        normalized = []
        for item in devices:
            ram = item.get("AdapterRAM")
            normalized.append(
                {
                    "name": item.get("Name", ""),
                    "adapter_ram_mb": round(float(ram) / MB, 3)
                    if isinstance(ram, (int, float)) and ram > 0
                    else None,
                    "driver_version": item.get("DriverVersion", ""),
                }
            )
        return {
            "available": bool(normalized),
            "devices": normalized,
            "usage_monitoring": "not_available",
            "pipeline_gpu_usage_detected": False,
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
