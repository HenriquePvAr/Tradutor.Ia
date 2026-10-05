"""Small psutil/proc sampler for controlled VPS benchmark runs."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class ResourceSampler:
    def __init__(self, root: str | Path, interval_seconds: float = 2.0):
        self.root = Path(root)
        self.interval_seconds = interval_seconds
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_cpu: tuple[int, int] | None = None

    def _sample(self) -> dict:
        now = time.time()
        sample = {"wall_time": now, "loadavg": None, "swap": None, "processes": []}
        try:
            import psutil
            sample["cpu_percent"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            sample["ram_total_bytes"] = vm.total
            sample["ram_used_bytes"] = vm.used
            sample["ram_percent"] = vm.percent
            sw = psutil.swap_memory()
            sample["swap"] = {"used_bytes": sw.used, "percent": sw.percent, "sin": sw.sin, "sout": sw.sout}
            sample["loadavg"] = list(psutil.getloadavg())
            for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "cpu_percent", "memory_info"]):
                try:
                    info = proc.info
                    cmd = " ".join(info.get("cmdline") or [])
                    if any(token in cmd for token in ("run_worker_service", "uvicorn", "chrome", "chromium")):
                        sample["processes"].append({"pid": info["pid"], "ppid": info["ppid"], "name": info["name"],
                            "cpu_percent": info.get("cpu_percent"), "rss_bytes": (info.get("memory_info").rss if info.get("memory_info") else None)})
                except Exception:
                    continue
        except Exception as exc:
            sample["sampler_backend"] = "proc"
            try:
                cpu = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
                total, idle = sum(cpu), cpu[3] + (cpu[4] if len(cpu) > 4 else 0)
                if self._previous_cpu:
                    previous_total, previous_idle = self._previous_cpu
                    delta_total, delta_idle = total - previous_total, idle - previous_idle
                    sample["cpu_percent"] = round((1.0 - delta_idle / delta_total) * 100.0, 3) if delta_total else 0.0
                self._previous_cpu = (total, idle)
                meminfo = {line.split(":", 1)[0]: int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line}
                total_mem = meminfo.get("MemTotal", 0)
                available = meminfo.get("MemAvailable", 0)
                sample.update({"ram_total_bytes": total_mem, "ram_used_bytes": total_mem - available,
                               "ram_percent": round((total_mem - available) * 100.0 / total_mem, 3) if total_mem else None})
                load = Path("/proc/loadavg").read_text().split()
                sample["loadavg"] = [float(load[0]), float(load[1]), float(load[2])]
                swap_total = meminfo.get("SwapTotal", 0)
                swap_free = meminfo.get("SwapFree", 0)
                sample["swap"] = {"used_bytes": swap_total - swap_free, "percent": (100.0 * (swap_total - swap_free) / swap_total if swap_total else 0.0), "sin": 0, "sout": 0}
                for proc_dir in Path("/proc").glob("[0-9]*"):
                    try:
                        pid = int(proc_dir.name)
                        cmd = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").strip()
                        if any(token in cmd for token in ("run_worker_service", "uvicorn", "chrome", "chromium")):
                            status = (proc_dir / "status").read_text(errors="ignore")
                            rss = next((int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith("VmRSS:")), None)
                            sample["processes"].append({"pid": pid, "cmd": cmd[:200], "rss_bytes": rss})
                    except (OSError, ValueError):
                        continue
            except Exception as proc_exc:
                sample["unavailable"] = type(proc_exc).__name__
        return sample

    def _run(self):
        while not self._stop.is_set():
            self.samples.append(self._sample())
            self._stop.wait(self.interval_seconds)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="yomu-perf-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_seconds + 1.0)
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.write_text("\n".join(json.dumps(item, sort_keys=True) for item in self.samples), encoding="utf-8")
        return self.summary()

    def summary(self) -> dict:
        numeric = [x.get("cpu_percent") for x in self.samples if isinstance(x.get("cpu_percent"), (int, float))]
        ram = [x.get("ram_used_bytes") for x in self.samples if isinstance(x.get("ram_used_bytes"), (int, float))]
        loads = [x.get("loadavg", [None])[0] for x in self.samples if x.get("loadavg")]
        return {"samples": len(self.samples), "cpu_mean": sum(numeric) / len(numeric) if numeric else None,
                "cpu_peak": max(numeric) if numeric else None, "ram_mean_bytes": sum(ram) / len(ram) if ram else None,
                "ram_peak_bytes": max(ram) if ram else None, "load_mean_1m": sum(loads) / len(loads) if loads else None,
                "load_peak_1m": max(loads) if loads else None,
                "swap_activity": any((x.get("swap") or {}).get("sin", 0) or (x.get("swap") or {}).get("sout", 0) for x in self.samples)}
