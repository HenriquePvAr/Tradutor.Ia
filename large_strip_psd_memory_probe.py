"""Separate production PSD export RSS from parser/validation RSS at 1080x30000."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import psutil
from PIL import Image, ImageDraw


def _monitor(action):
    process = psutil.Process(os.getpid())
    baseline = process.memory_info().rss
    peak = [baseline]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            try:
                peak[0] = max(peak[0], process.memory_info().rss)
            except psutil.Error:
                pass
            stop.wait(0.02)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    started = time.perf_counter()
    try:
        action()
    finally:
        elapsed = time.perf_counter() - started
        stop.set()
        thread.join()
        peak[0] = max(peak[0], process.memory_info().rss)
    return {
        "baseline_rss_mb": round(baseline / 1024**2, 1),
        "peak_rss_mb": round(peak[0] / 1024**2, 1),
        "peak_additional_rss_mb": round(max(0, peak[0] - baseline) / 1024**2, 1),
        "elapsed_seconds": round(elapsed, 2),
    }


def main() -> int:
    mode, original_path, translated_path, psd_path = sys.argv[1:5]
    original_path = Path(original_path)
    translated_path = Path(translated_path)
    psd_path = Path(psd_path)
    if mode == "export":
        import psd_exporter

        # Measure the production serialization path alone. Validation is measured in a
        # separate process below, to prevent its transient composite from inflating this.
        psd_exporter._validate_psd = lambda *_args, **_kwargs: None
        result = _monitor(lambda: psd_exporter._write_one(
            original_path, translated_path, psd_path
        ))
        result.update({"stage": "production_psd_export_without_reopen_validation",
                       "file_size_bytes": psd_path.stat().st_size})
    elif mode == "validate":
        import psd_exporter

        original = Image.open(original_path).convert("RGBA")
        translated = Image.open(translated_path).convert("RGBA")
        result = _monitor(lambda: psd_exporter._validate_psd(
            psd_path, original, translated
        ))
        result.update({"stage": "psd_tools_open_layers_composite_and_pillow_reopen",
                       "file_size_bytes": psd_path.stat().st_size})
    else:
        raise ValueError("unknown_probe_stage")
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
