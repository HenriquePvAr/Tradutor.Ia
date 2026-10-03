"""Isolated real UiBridge -> worker -> runner tall-page acceptance harness.

All files/jobs/providers are rooted in a fresh temporary runtime. No remote
provider, wallet, Supabase, or production application state is used.
"""
from __future__ import annotations

import asyncio
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

import cv2
import numpy as np
import psutil
from PIL import Image

ROOT = Path(__file__).resolve().parent


def _token() -> str:
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    payload = json.dumps({"exp": int(time.time()) + 7200}).encode()
    return encode(b'{"alg":"none"}') + "." + encode(payload) + ".isolated-tall-e2e"


def _fixture(path: Path, height: int, label: str) -> Path:
    image = np.full((height, 1080, 3), 255, dtype=np.uint8)
    # Keep the synthetic manga page non-blank under the product's 32x32
    # image-validity thumbnail check, which otherwise reduces sparse text on a
    # 20k/30k white canvas to near-uniform pixels. This neutral page-edge rail
    # is visual fixture content only; production OCR/render code is untouched.
    image[:, 940:1080] = (180, 180, 180)
    for text, y in (
        ("A new story begins", 360),
        ("We are not alone", height // 2),
        ("Have a nice day", height - 420),
    ):
        cv2.putText(image, text, (90, y), cv2.FONT_HERSHEY_SIMPLEX, 2.0,
                    (0, 0, 0), 5, cv2.LINE_AA)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image):
        raise RuntimeError("fixture_write_failed")
    return path


def _env(runtime: Path, *, recovery=False) -> dict[str, str]:
    env = os.environ.copy()
    hook_path = ROOT / "large_strip_e2e_runtime"
    env.update({
        "TRADUTOR_TEST_RUNTIME_ROOT": str(runtime),
        "TRADUTOR_RUNTIME_ROOT": str(runtime),
        "TRADUTOR_OUTPUT_ROOT": str(runtime / "output"),
        "TRADUTOR_IA_RUNTIME_ISOLATION_GUARD": "1",
        "TRADUTOR_IA_HERMETIC_TEST_ENV": "1",
        # local_folder_input.snapshot_workspace_root only relocates snapshots
        # when this test marker is present; keep UI intake and worker on one root.
        "YOMU_CANCEL_TEST_MARKER_DIR": str(runtime / "markers"),
        "YOMU_ENV": "test",
        "YOMU_TEST_BACKEND": "mock",
        "YOMU_EXECUTION_E2E": "0",
        "YOMU_TALL_STRIP_E2E": "1",
        "YOMU_TALL_STRIP_FAIL_EXPORT": "1" if recovery else "0",
        "YOMU_TALL_STRIP_FAILURE_MARKER": str(runtime / "fault-once.marker"),
        "YOMU_TEST_WALLET_STATE": str(runtime / "wallet_mock.pkl"),
        "BETA_LICENSE_PROVIDER": "local_development",
        "YOMU_BACKEND_URL": "",
        "SUPABASE_URL": "",
        "TRADUTOR_DEVICE_UUID": "550e8400-e29b-41d4-a716-446655440000",
        "LOCAL_INPUT_ROOTS": str(runtime / "fixtures"),
        "OCR_ENGINE": "rapidocr",
        "RAPIDOCR_ENABLED": "true",
        "OCR_FALLBACK_ENGINE": "",
        "TRANSLATION_ENABLED": "true",
        "PIPELINE_PAGE_WORKERS": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(hook_path), str(ROOT), env.get("PYTHONPATH", "")]
        ),
    })
    (runtime / "fixtures").mkdir(parents=True, exist_ok=True)
    return env


def _status_wait(store, job_id: str, worker: subprocess.Popen, timeout=240):
    from job_store import JobStatus

    transitions = []
    terminal = set(JobStatus.TERMINAL) | {JobStatus.INTERRUPTED}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = store.get_job(job_id) or {}
        status = str(row.get("status") or "unknown")
        if not transitions or transitions[-1] != status:
            transitions.append(status)
        if status in terminal:
            return row, transitions
        if worker.poll() is not None:
            time.sleep(0.25)
        else:
            time.sleep(0.25)
    raise TimeoutError(f"job_timeout:{job_id}")


def _log_runner_pid(job: dict) -> int | None:
    path = Path(str(job.get("log_path") or ""))
    if not path.is_file():
        return None
    matches = re.findall(r"PROCESS_CREATED pid=(\d+)", path.read_text(
        encoding="utf-8", errors="replace"))
    return int(matches[-1]) if matches else None


def _spawn_worker(runtime: Path, env: dict[str, str]):
    return subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "worker_service.py"), "--db",
         str(runtime / "jobs.sqlite3"), "--once"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )


def _payload(source: str, name: str, source_path: Path) -> dict:
    payload = {
        "source_type": source,
        "mode": "quality",
        "full": True,
        "output_format": "png",
        "translation_provider": "deepl",  # server test mode resolves the local mock
        "translation_enabled": True,
        "chapter_name": name,
        "slug": f"tall-{source}-{uuid.uuid4().hex[:8]}",
        "id": str(uuid.uuid4()),
        "force": True,
        "use_cache": False,
        "use_context": False,
    }
    if source == "local_images":
        payload["image_paths"] = [str(source_path)]
    else:
        payload["local_folder"] = str(source_path)
    return payload


async def _create(bridge, source: str, name: str, path: Path):
    return await bridge.start(
        _payload(source, name, path), local_folder_allowed=True,
        license_access_token=_token(),
    )


def _png_output(job: dict) -> tuple[Path, list[Path]]:
    output = Path(str(job.get("output_dir") or ""))
    report_path = output / "timing_report.json"
    if not report_path.is_file():
        return output, []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    png_root = Path(str(report.get("png_path") or ""))
    if not png_root.is_absolute():
        png_root = output / png_root
    return output, sorted(p for p in png_root.glob("*.png") if p.is_file() and p.stat().st_size)


def _verify_regions(path: Path, *, height: int) -> dict:
    from ocr_engine import OCREngine

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return {"found": [False, False, False], "centers": []}
    engine = OCREngine("en", engine="rapidocr", fallback_engine="")
    lines = engine.detect_lines(image, page=1)
    centers = [int(line.box[1] + line.box[3] / 2) for line in lines]
    buckets = [
        any(center < height * 0.2 for center in centers),
        any(height * 0.35 < center < height * 0.65 for center in centers),
        any(center > height * 0.8 for center in centers),
    ]
    return {"found": buckets, "centers": centers,
            "duplicate_region_count": max(0, len(lines) - 3)}


def _run_job(bridge, runtime: Path, env: dict[str, str], source: str,
             name: str, source_path: Path):
    from job_store import JobStatus

    result = asyncio.run(_create(bridge, source, name, source_path))
    job_id = str(result.get("job_id") or "")
    if not job_id:
        raise RuntimeError(f"job_creation_failed:{result.get('reason_code') or result.get('error')}")
    initial = bridge.store.get_job(job_id) or {}
    worker = _spawn_worker(runtime, env)
    row, transitions = _status_wait(bridge.store, job_id, worker)
    worker_output, _ = worker.communicate(timeout=10)
    runner_pid = _log_runner_pid(row)
    output, pngs = _png_output(row)
    sizes = []
    for file in pngs:
        with Image.open(file) as image:
            sizes.append(list(image.size))
    check = _verify_regions(pngs[0], height=sizes[0][1]) if len(pngs) == 1 else {}
    return {
        "job_id": job_id, "initial_status": initial.get("status"),
        "transitions": transitions, "status": row.get("status"),
        "recoverable": row.get("recoverable"), "reason_code": row.get("reason_code"),
        "app_pid": os.getpid(), "worker_pid": worker.pid,
        "runner_pid": runner_pid,
        "worker_separate": worker.pid != os.getpid() and runner_pid not in (None, worker.pid),
        "worker_exit": worker.returncode, "worker_log_tail": (worker_output or "")[-1200:],
        "output_dir": str(output), "png_paths": [str(path) for path in pngs],
        "png_sizes": [path.stat().st_size for path in pngs], "dimensions": sizes,
        "regions": check,
    }


def main() -> int:
    runtime = Path(tempfile.mkdtemp(prefix="yomu-large-strip-app-e2e-"))
    env = _env(runtime)
    os.environ.update(env)
    import ui_bridge

    bridge = ui_bridge.UiBridge()
    bridge.ensure_worker = lambda: {"status": "manual_isolated_dispatch"}
    fixtures = runtime / "fixtures"
    local_images_30k = _fixture(fixtures / "images" / "001.png", 30000, "L30K")
    folder_20k = fixtures / "folder20k"
    folder_20k.mkdir(parents=True)
    _fixture(folder_20k / "001.png", 20000, "F20K")
    recovery_folder = fixtures / "recovery20k"
    recovery_folder.mkdir()
    _fixture(recovery_folder / "001.png", 20000, "R20K")

    results: dict[str, object] = {"runtime": str(runtime), "app_pid": os.getpid()}
    try:
        results["local_images_30k"] = _run_job(
            bridge, runtime, env, "local_images", "Tall local image 30k", local_images_30k)
        results["local_folder_20k"] = _run_job(
            bridge, runtime, env, "local_folder", "Tall local folder 20k", folder_20k)

        # The next submission uses the exact normal Local Folder UI bridge path.
        # Its runner receives a one-shot test-only export fault via sitecustomize.
        env_recovery = _env(runtime, recovery=True)
        env_recovery["YOMU_TEST_WALLET_STATE"] = str(runtime / "wallet_mock.pkl")
        result = asyncio.run(_create(
            bridge, "local_folder", "Tall recovery 20k", recovery_folder))
        recovery_id = str(result.get("job_id") or "")
        if not recovery_id:
            raise RuntimeError(f"recovery_job_creation_failed:{result.get('reason_code') or result.get('error')}")
        first_worker = _spawn_worker(runtime, env_recovery)
        first_row, first_transitions = _status_wait(bridge.store, recovery_id, first_worker)
        first_output, _ = first_worker.communicate(timeout=10)
        first_runner_pid = _log_runner_pid(first_row)
        provider_state = runtime / "provider_calls.json"
        wallet_state = runtime / "wallet_state.json"
        first_provider_calls = (
            json.loads(provider_state.read_text(encoding="utf-8")).get("calls", 0)
            if provider_state.is_file() else 0
        )
        first_wallet = (
            json.loads(wallet_state.read_text(encoding="utf-8"))
            if wallet_state.is_file() else {}
        )
        output_dir = Path(str(first_row.get("output_dir") or ""))
        import yk_reservation
        translation_ready = yk_reservation.translation_ready(
            output_dir, job_id=recovery_id
        )
        _out, first_pngs = _png_output(first_row)
        if first_row.get("status") != "interrupted" or not first_row.get("recoverable"):
            raise RuntimeError("first_recovery_attempt_not_interrupted_recoverably")
        if first_pngs or not translation_ready or not (runtime / "fault-once.marker").is_file():
            raise RuntimeError("recovery_fault_boundary_not_proven")

        decision = type("ResumeAccess", (), {"to_safe_job_metadata": lambda _self: {}})()
        from unittest import mock
        with mock.patch.object(bridge, "_require_beta_access", return_value=decision), \
             mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            resumed = bridge.resume(recovery_id)
        second_job_id = str(resumed.get("job_id") or recovery_id)
        second_worker = _spawn_worker(runtime, env_recovery)
        second_row, second_transitions = _status_wait(bridge.store, second_job_id, second_worker)
        second_output, _ = second_worker.communicate(timeout=10)
        second_runner_pid = _log_runner_pid(second_row)
        second_provider_calls = json.loads((runtime / "provider_calls.json").read_text(
            encoding="utf-8"))["calls"]
        second_wallet = json.loads((runtime / "wallet_state.json").read_text(encoding="utf-8"))
        final_output, final_pngs = _png_output(second_row)
        dimensions = []
        for png in final_pngs:
            with Image.open(png) as image:
                dimensions.append(list(image.size))
        recovered_regions = (
            _verify_regions(final_pngs[0], height=20000) if len(final_pngs) == 1 else {}
        )
        results["recovery_20k"] = {
            "first_job_id": recovery_id, "resumed_job_id": second_job_id,
            "first_status": first_row.get("status"),
            "first_recoverable": bool(first_row.get("recoverable")),
            "first_worker_pid": first_worker.pid,
            "first_runner_pid": first_runner_pid,
            "first_worker_exited": first_worker.returncode is not None,
            "first_worker_exit": first_worker.returncode,
            "first_worker_log_tail": (first_output or "")[-1000:],
            "first_transitions": first_transitions,
            "translation_ready_persisted": translation_ready,
            "first_final_png_count": len(first_pngs),
            "provider_calls_before_recovery": first_provider_calls,
            "provider_calls_after_recovery": second_provider_calls,
            "simulated_net_yk_before_recovery": first_wallet.get("net_yk"),
            "simulated_net_yk_after_recovery": second_wallet.get("net_yk"),
            "second_status": second_row.get("status"),
            "second_transitions": second_transitions,
            "second_worker_pid": second_worker.pid,
            "second_runner_pid": second_runner_pid,
            "second_worker_exit": second_worker.returncode,
            "second_worker_log_tail": (second_output or "")[-1000:],
            "output_dir": str(final_output),
            "png_paths": [str(path) for path in final_pngs],
            "png_sizes": [path.stat().st_size for path in final_pngs],
            "dimensions": dimensions,
            "regions_after_recovery": recovered_regions,
        }
    finally:
        bridge.store.close()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
