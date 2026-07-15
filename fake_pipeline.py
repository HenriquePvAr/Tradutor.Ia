"""Test-only pipeline stand-in for the worker/runner.

Simulates the real chapter pipeline's observable behaviour - staged progress lines,
per-stage checkpoints, a tiny PDF and timing report - without any network, NVIDIA, OCR
or heavy rendering. Used by the job-store integration tests and the operational
survival test. Never invoked for a real chapter.

Progress lines are printed in the same "Stage current/total" shape the real pipeline
emits so the runner's existing parser understands them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

STAGES = [
    ("Baixando imagens", "download"),
    ("Reconstrução/smart split", "smart_split"),
    ("OCR", "ocr"),
    ("Tradução NVIDIA", "translate"),
    ("Renderização", "render"),
    ("Geração de PDF", "pdf"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--outcome", choices=["finished", "review", "fail"], default="finished")
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--stop-after-stage", default="")
    parser.add_argument("--hang", action="store_true")
    parser.add_argument("--fail-at-stage", default="")
    args = parser.parse_args(argv)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    checkpoints = out / "checkpoints"
    checkpoints.mkdir(exist_ok=True)

    for label, key in STAGES:
        # A completed prior stage may be reused on resume; skip re-doing it.
        checkpoint = checkpoints / f"{key}.done"
        if checkpoint.is_file():
            print(f"{label} 0/0 (reaproveitado do checkpoint)", flush=True)
            continue
        for step in range(1, args.steps + 1):
            print(f"{label} {step}/{args.steps}", flush=True)
            if args.hang:
                # Simulate an interrupted run: block until killed.
                while True:
                    time.sleep(0.1)
            time.sleep(max(0.0, args.sleep))
        if args.fail_at_stage == key:
            print(f"Falha simulada no estágio {key}", file=sys.stderr, flush=True)
            return 1
        checkpoint.write_text(json.dumps({"stage": key, "at": time.time()}), encoding="utf-8")
        if args.stop_after_stage == key:
            print(f"Parada controlada após {key}", flush=True)
            return 3

    if args.outcome == "fail":
        print("Falha simulada final", file=sys.stderr, flush=True)
        return 1

    # Produce the artifacts the runner reads to derive the terminal status.
    passed = args.outcome == "finished"
    (out / "timing_report.json").write_text(
        json.dumps(
            {
                "total_seconds": 1.0,
                "processed_images": args.steps,
                "groups_translated": args.steps,
                "quality_validation": {"passed": passed, "manual_review_required_groups": 0},
            }
        ),
        encoding="utf-8",
    )
    (out / "the_fake_chapter.pdf").write_bytes(b"%PDF-1.4\n% fake\n%%EOF\n")
    print("Geração de PDF 1/1", flush=True)
    print("Finalizado", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
