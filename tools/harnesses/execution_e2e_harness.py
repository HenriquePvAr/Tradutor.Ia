"""Run isolated UI-bridge -> real worker -> real local runner execution checks."""
from __future__ import annotations

import asyncio
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
URL = "https://comix.to/title/qk31-i-became-the-genius-bastard-of-a-noble-dark-clan/11378175-chapter-70"


def _token() -> str:
    enc = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    payload = json.dumps({"exp": int(time.time()) + 3600}).encode()
    return enc(b'{"alg":"none"}') + "." + enc(payload) + ".isolated-e2e"


def _fixture_images(root: Path) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    result = []
    for i, color in enumerate(("#f1d4b0", "#b6d8f0", "#e2c0df"), 1):
        image = Image.new("RGB", (1024, 1600), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 60, 960, 260), outline="#111111", width=5)
        draw.text((90, 120), f"PAGE {i} UNIQUE FIXTURE", fill="#111111")
        path = root / f"{i:03}.png"
        image.save(path, format="PNG")
        result.append(path)
    return result


async def _start(bridge, payload, *, local_folder_allowed=True):
    bridge.ensure_worker = lambda: {"status": "isolated_e2e_manual_dispatch"}
    prior = os.environ.get("TRADUTOR_IA_HERMETIC_TEST_ENV")
    if not local_folder_allowed:
        os.environ["TRADUTOR_IA_HERMETIC_TEST_ENV"] = "1"
    try:
        return await bridge.start(payload, local_folder_allowed=local_folder_allowed,
                                  license_access_token=_token())
    finally:
        if not local_folder_allowed:
            if prior is None:
                os.environ.pop("TRADUTOR_IA_HERMETIC_TEST_ENV", None)
            else:
                os.environ["TRADUTOR_IA_HERMETIC_TEST_ENV"] = prior


def _pdf_page_count(path: Path) -> int | None:
    try:
        from yomu.export.pdf_reader import read_pdf_page_count
        return int(read_pdf_page_count(path))
    except Exception:
        try:
            from pypdf import PdfReader
            return len(PdfReader(str(path)).pages)
        except Exception:
            data = path.read_bytes()
            return len(re.findall(rb"/Type\s*/Page(?!s)\b", data))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", nargs="+",
                        choices=("local_images", "local_folder", "comix_url"),
                        default=("local_images", "local_folder"))
    parser.add_argument("--formats", nargs="+", choices=("png", "pdf", "psd"),
                        default=("png", "pdf", "psd"))
    parser.add_argument("--source-url", default=URL)
    args = parser.parse_args()
    runtime = Path(tempfile.mkdtemp(prefix="yomu-execution-e2e-"))
    env = os.environ.copy()
    env.update({
        "TRADUTOR_TEST_RUNTIME_ROOT": str(runtime),
        "TRADUTOR_RUNTIME_ROOT": str(runtime),
        "TRADUTOR_OUTPUT_ROOT": str(runtime / "output"),
        "TRADUTOR_IA_RUNTIME_ISOLATION_GUARD": "1",
        "TRADUTOR_IA_HERMETIC_TEST_ENV": "1",
        "YOMU_ENV": "test",
        "YOMU_TEST_BACKEND": "mock",
        "YOMU_EXECUTION_E2E": "1",
        "BETA_LICENSE_PROVIDER": "local_development",
        # Keep every provider path local. MockBackendClient handles successful
        # translation/wallet settlement; empty URLs prevent any HTTP settlement fallback.
        "YOMU_BACKEND_URL": "",
        "SUPABASE_URL": "",
        "TRADUTOR_DEVICE_UUID": "550e8400-e29b-41d4-a716-446655440000",
        "YOMU_CANCEL_TEST_MARKER_DIR": str(runtime / "markers"),
        "LOCAL_INPUT_ROOTS": str(runtime / "fixtures"),
        "PYTHONPATH": str(ROOT / "e2e_test_runtime") + os.pathsep +
                       str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "TRANSLATION_ENABLED": "true",
        "PIPELINE_PAGE_WORKERS": "1",
    })
    if "comix_url" in args.sources:
        env.pop("TRADUTOR_IA_HERMETIC_TEST_ENV", None)
        env["TRADUTOR_ALLOW_DRIVER_DOWNLOAD"] = "1"
        env["SE_CACHE_PATH"] = str(runtime / "selenium-cache")
    os.environ.update(env)

    import yomu.ui.ui_bridge as ui_bridge
    from yomu.runtime.job_store import JobStatus
    from yomu.download.local.local_folder_input import snapshot_workspace_root
    from yomu.ui.ui_helpers import find_output_artifacts

    bridge = ui_bridge.UiBridge()
    if "comix_url" in args.sources:
        from yomu.review.source_readiness import SourceReadinessStore, default_workspace_id
        readiness = SourceReadinessStore(bridge.store.db_path)
        try:
            readiness.activate_workspace_policy(
                owner="local", workspace_id=default_workspace_id(bridge.store.db_path),
                created_by="isolated-e2e",
                authorization_statement=(
                    "Single isolated Quality E2E for the owner-authorized public Comix URL; "
                    "three-page scope, mock provider/wallet, no publishing."
                ),
            )
        finally:
            readiness.close()
    input_images = _fixture_images(runtime / "fixtures" / "images")
    folder = runtime / "fixtures" / "folder"
    folder.mkdir(parents=True, exist_ok=True)
    for src in input_images:
        Image.open(src).save(folder / src.name)

    cases = []
    for source in args.sources:
        formats = args.formats
        for fmt in formats:
            payload = {
                "source_type": "url" if source == "comix_url" else source,
                "mode": "quality",
                "full": True,
                "output_format": fmt,
                # The UI-facing engine is deepl; YOMU_ENV=test routes it to the
                # internal MockBackendClient (never the real DeepL service).
                "translation_provider": "deepl",
                "translation_enabled": True,
                "chapter_name": f"Execution E2E {source} {fmt}",
                "slug": f"execution-e2e-{source}-{fmt}",
                "id": str(uuid.uuid4()),
                "force": True,
                "use_cache": False,
                "use_context": False,
            }
            if source == "comix_url":
                payload.update({"url": args.source_url, "full": False, "max_images": 3,
                                "chapter_name": "Comix Quality 3-page E2E"})
            elif source == "local_images":
                payload["image_paths"] = [str(p) for p in input_images]
            else:
                payload["local_folder"] = str(folder)
            result = asyncio.run(_start(
                bridge, payload, local_folder_allowed=source != "comix_url"))
            if not result.get("job_id"):
                cases.append({"source": source, "format": fmt, "created": False,
                              "error": result.get("reason_code") or result.get("error")})
                continue
            job_id = result["job_id"]
            job = bridge.store.get_job(job_id)
            config = job.get("configuration") or {}
            command = json.loads(json.dumps(job.get("command") or []))
            command_fmt = "pdf"
            if "--output-format" in command:
                command_fmt = str(command[command.index("--output-format") + 1])
            else:
                command_fmt = next((str(command[i + 1]) for i, a in enumerate(command[:-1])
                                    if a == "--output-format"), "pdf")
            transitions = [job.get("status")]

            worker_cmd = [sys.executable, "-u", str(ROOT / "worker_service.py"),
                          "--db", str(runtime / "jobs.sqlite3"), "--once"]
            worker = subprocess.Popen(worker_cmd, cwd=ROOT, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                time.sleep(.5)
                current = bridge.store.get_job(job_id)
                status = str((current or {}).get("status") or "unknown")
                if not transitions or transitions[-1] != status:
                    transitions.append(status)
                if status in (set(JobStatus.TERMINAL) | {JobStatus.INTERRUPTED}):
                    break
            else:
                current = bridge.store.get_job(job_id)
                status = str((current or {}).get("status") or "unknown")
                transitions.append("timeout:" + status)
            try:
                worker_output, _ = worker.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker_output, _ = worker.communicate()
            current = bridge.store.get_job(job_id) or {}
            output = Path(str(current.get("output_dir") or ""))
            artifacts = find_output_artifacts(output)
            png_root = Path(artifacts["png_path"]) if artifacts.get("png_path") else None
            psd_root = Path(artifacts["psd_path"]) if artifacts.get("psd_path") else None
            pngs = ([p for p in png_root.rglob("*.png")
                     if p.is_file() and p.stat().st_size] if png_root else [])
            psds = ([p for p in psd_root.rglob("*.psd")
                     if p.is_file() and p.stat().st_size] if psd_root else [])
            psd_details = []
            if psds:
                from psd_tools import PSDImage
                for psd_path in sorted(psds):
                    parsed = PSDImage.open(psd_path)
                    psd_details.append({
                        "name": psd_path.name,
                        "size_bytes": psd_path.stat().st_size,
                        "dimensions": list(parsed.size),
                        "layers": [layer.name for layer in parsed],
                    })
            pdf_path = Path(artifacts["pdf_path"]) if artifacts.get("pdf_path") else None
            report = {}
            report_path = output / "timing_report.json"
            if report_path.is_file():
                try: report = json.loads(report_path.read_text(encoding="utf-8"))
                except Exception: pass
            cases.append({
                "source": source, "format": fmt, "created": True, "job_id": job_id,
                "persisted_source_type": current.get("source_type"),
                "status_transitions": transitions, "terminal": current.get("status"),
                "reason_code": current.get("reason_code"),
                "ui_output_format": fmt, "job_output_format": config.get("output_format"),
                "worker_output_format": command_fmt,
                "actual_output_format": report.get("output_format"),
                "output_dir": str(output), "png_export_root": str(png_root) if png_root else None,
                "png_count": len(pngs), "psd_export_root": str(psd_root) if psd_root else None,
                "psd_count": len(psds), "pdf_path": str(pdf_path) if pdf_path else None,
                "psd_details": psd_details,
                "pdf_count": int(bool(pdf_path and pdf_path.is_file() and pdf_path.stat().st_size)),
                "pdf_pages": _pdf_page_count(pdf_path) if pdf_path else None,
                "translation_result_files": len(list((output / ".translation_results").glob("*.json")))
                    if (output / ".translation_results").is_dir() else 0,
                "simulated_yk_state": (
                    json.loads((output / "yk_reservation.json").read_text(encoding="utf-8")).get("state")
                    if (output / "yk_reservation.json").is_file() else None
                ),
                "pipeline_entered": (output / "downloaded_images.json").is_file(),
                "worker_exit": worker.returncode,
                "worker_output_tail": (worker_output or "")[-1000:],
            })
            (runtime / "matrix_results.json").write_text(
                json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"runtime": str(runtime),
                      "snapshot_root": str(snapshot_workspace_root()),
                      "cases": cases}, ensure_ascii=False, indent=2))
    bridge.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
