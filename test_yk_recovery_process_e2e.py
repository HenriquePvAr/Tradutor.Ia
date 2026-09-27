"""Process-boundary recovery proof for the local-folder quality job.

This deliberately launches the real worker_service and its runner subprocess twice.
All remote/economic edges are replaced by a disk-backed mock scoped to a temp root.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import base64
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent


class DiskWalletProvider:
    """Disk-backed wrapper around the canonical deterministic wallet mock."""

    def __init__(self, state_path: Path):
        from yomu_backend_provider import MockBackendClient
        self.backend = MockBackendClient()
        self.backend.translate_invocation_count = 0
        self.path = state_path
        if self.path.exists():
            data = pickle.loads(self.path.read_bytes())
            for key, value in data.items():
                setattr(self.backend, key, value)

    def save(self):
        fields = ("reserve_count", "provider_execution_count", "provider_chunk_count",
                  "translate_invocation_count",
                  "_reservations", "_responses", "_wallet_state", "_reservation_jobs",
                  "_reservation_owners", "_provider_requests", "_settlement_keys")
        data = {key: getattr(self.backend, key) for key in fields}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(data))
        os.replace(tmp, self.path)
        summary = {
            "provider_calls": self.backend.translate_invocation_count,
            "provider_executions": self.backend.provider_execution_count,
            "reserve_count": self.backend.reserve_count,
            "net_yk": sum(state == "consumed" for state in self.backend._wallet_state.values()),
            "wallet_states": dict(self.backend._wallet_state),
        }
        self.path.with_name("provider_calls.json").write_text(
            json.dumps({"calls": summary["provider_calls"]}), encoding="utf-8")
        self.path.with_name("wallet_state.json").write_text(
            json.dumps(summary), encoding="utf-8")

    def reserve(self, **kwargs):
        result = self.backend.reserve(**kwargs)
        self.save()
        return result

    def translate_batch(self, *args, **kwargs):
        self.backend.translate_invocation_count += 1
        result = self.backend.translate_batch(*args, **kwargs)
        self.save()
        return result

    def settle_job(self, **kwargs):
        result = self.backend.settle_job(**kwargs)
        self.save()
        return result


def _provider(runtime_root: Path):
    from yomu_backend_provider import YomuBackendTranslationProvider
    backend = DiskWalletProvider(Path(os.environ["YOMU_TEST_WALLET_STATE"]))
    return YomuBackendTranslationProvider(runtime_root=runtime_root, backend=backend)


def _token():
    enc = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    payload = json.dumps({"exp": int(time.time()) + 7200}).encode()
    return enc(b'{"alg":"none"}') + "." + enc(payload) + ".test-only"


def _run_pipeline(manifest: Path, snapshot_root: Path, output: Path):
    import benchmark_pipeline as bp
    import config
    import local_folder_input
    from local_folder_source import LocalFolderChapterAdapter, LocalFolderPolicy
    from ocr_balloon import TextCandidate, TextGroup
    from ocr_engine import OCRLine
    from png_output import PngExportError

    runtime = Path(os.environ["TRADUTOR_TEST_RUNTIME_ROOT"])
    old_snapshot_root = local_folder_input.LOCAL_SNAPSHOT_ROOT
    local_folder_input.LOCAL_SNAPSHOT_ROOT = snapshot_root
    output.mkdir(parents=True, exist_ok=True)

    def detect(jobs, _lang, **_kwargs):
        records = {}
        for job in jobs:
            index = int(job["index"])
            text = ("THE HERO", "THE FRIEND", "THE VILLAGE")[index - 1]
            polygon = np.array([[80, 80], [360, 80], [360, 180], [80, 180]], np.int32)
            line = OCRLine(text=text, raw_text=text, confidence=.99, polygon=polygon,
                           box=(80, 80, 280, 100), engine="fixture", page=index, metadata={})
            records[index] = {"index": index, "lines": [line], "ocr_metadata": {},
                              "elapsed_seconds": 0}
        return records, {"parallel": False, "worker_pids": []}

    def analyse(_image, lines, page_index=None):
        line = lines[0]
        group = TextGroup(group_id=f"G{page_index}", lines=[line], text=line.text,
                          classification="speech", inside_balloon_like_region=True,
                          source_engine="fixture", quality_score=1.0)
        return [TextCandidate(line=line)], [group]

    def render_fixture(original, _lines, _candidates, groups, **_kwargs):
        # Keep the pipeline's rendering boundary deterministic while still
        # feeding its real final-page list to the canonical PNG exporter.
        for group in groups:
            group.translation = group.translation or "translated fixture"
            group.translation_final_state = "translated"
            group.redrawn = True
        return original.copy(), {"translated_group_count": len(groups), "rendered": True}

    def test_build_provider(_requested, *, translation_enabled):
        if not translation_enabled:
            return None
        return _provider(runtime)

    original_png_export = bp.export_final_pages_png
    def fail_png_export_once(*args, **kwargs):
        marker = Path(os.environ["YOMU_TEST_EXPORT_FAIL_MARKER"])
        if not marker.exists():
            wallet = json.loads(Path(os.environ["YOMU_TEST_WALLET_STATE"])
                                .with_name("wallet_state.json").read_text(encoding="utf-8"))
            assert wallet["net_yk"] == 1
            marker.write_text("injected-after-translation-and-commit", encoding="utf-8")
            raise PngExportError("YOMU_TEST_FAIL_EXPORT_ONCE")
        return original_png_export(*args, **kwargs)

    class NoopMonitor:
        def __init__(self, *_, **__): pass
        def start(self): pass
        def set_stage(self, *_): pass
        def set_progress(self, **_): pass
        def register_worker_roles(self, *_args, **_kwargs): pass
        def stop(self): return {"enabled": False}

    args = SimpleNamespace(
        url=os.environ["YOMU_TEST_SOURCE_REF"], max_images=3, full=True,
        debug_folder=str(output / "debug"), keep_debug=False, fast=True,
        benchmark=True, force=False, force_download=False, page_indices="",
        output_folder=str(output), ocr_engine="fixture", use_context=False,
        session_context_path=str(output / "session.json"), source_candidate_ids=[],
        local_manifest_path=str(manifest), translation_provider="deepl",
        output_format="png", resume_checkpoint=os.getenv("TRADUTOR_RESUME_CHECKPOINT") == "1",
        checkpoint_job_id=os.getenv("TRADUTOR_CHECKPOINT_JOB_ID", ""),
        job_id=os.getenv("TRADUTOR_JOB_ID", ""),
    )
    try:
        with mock.patch.object(bp, "_build_translation_provider", side_effect=test_build_provider), \
             mock.patch.object(bp, "detect_ocr_jobs", side_effect=detect), \
             mock.patch.object(bp, "analyze_image_array", side_effect=analyse), \
             mock.patch.object(bp, "render_analyzed_image", side_effect=render_fixture), \
             mock.patch.object(bp, "apply_speech_container_reocr", side_effect=lambda _a, b, *_x, **_k: (b, [])), \
             mock.patch.object(bp, "apply_selective_ocr_fallbacks", side_effect=lambda _a, b, *_x, **_k: (b, [])), \
             mock.patch.object(bp, "_grouping_fallback_reason", return_value=""), \
             mock.patch.object(bp, "ResourceMonitor", NoopMonitor), \
             mock.patch.object(bp, "detect_gpu_basic", return_value={}), \
             mock.patch.object(bp, "export_final_pages_png", side_effect=fail_png_export_once), \
             mock.patch.multiple(config, OCR_ENGINE="fixture", OCR_FALLBACK_ENGINE="",
                 OCR_HYBRID_FALLBACK=False, SKIP_NO_TEXT_IMAGES=False,
                 ENABLE_DOWNLOAD_CACHE=False, ENABLE_OCR_CACHE=False,
                 ENABLE_IMAGE_PROCESS_CACHE=False, RESOURCE_MONITORING=False,
                 CLASSIFICATION_PROFILING=False, POST_RENDER_OCR_VALIDATION=False,
                 VISUAL_DIFF_VALIDATION=False, TRANSLATION_VALIDATION=False,
                 TRANSLATION_RETRY_ON_MIXED_LANGUAGE=False, TRANSLATE_SFX=False), \
             mock.patch.dict(os.environ, {"TRANSLATION_ENABLED": "true",
                 "PIPELINE_PAGE_WORKERS": "1", "YOMU_CANCEL_TEST_MARKER_DIR": str(runtime / "markers")}, clear=False):
            bp.run_benchmark(args)
        return 0
    finally:
        local_folder_input.LOCAL_SNAPSHOT_ROOT = old_snapshot_root


def _child_main(argv):
    if len(argv) < 4 or argv[0] != "--pipeline-child":
        return None
    from pathlib import Path
    return _run_pipeline(Path(argv[1]), Path(argv[2]), Path(argv[3]))


def test_local_folder_recovery_crosses_real_worker_processes(tmp_path, monkeypatch):
    from job_store import JobStatus, JobStore
    from local_folder_source import LocalFolderChapterAdapter, LocalFolderPolicy
    from local_folder_input import local_source_reference
    from secure_auth_context import AuthEnvelopeStore
    import ui_bridge
    import yk_reservation
    from PIL import Image, ImageDraw

    runtime = tmp_path / "runtime"
    source = tmp_path / "source"
    snapshot_root = runtime / "local_sources"
    output = runtime / "output" / "recovery-e2e"
    for directory in (runtime, source, snapshot_root, output.parent):
        directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, 4):
        image = Image.new("RGB", (640, 480), "white")
        draw = ImageDraw.Draw(image)
        draw.text((90, 100), f"PAGE {index} THE HERO", fill="black")
        for y in range(0, 480, 24):
            shade = 180 if (y // 24) % 2 else 235
            draw.rectangle((0, y, 639, y + 11), fill=(shade, shade, shade))
        image.save(source / f"page_{index:03}.png")
    snapshot = LocalFolderChapterAdapter(
        LocalFolderPolicy(allowed_roots=(tmp_path,))).snapshot(
            source, snapshot_root, snapshot_id="recovery-e2e")
    source_ref = local_source_reference(snapshot.analysis.source_fingerprint)

    monkeypatch.setenv("TRADUTOR_TEST_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("TRADUTOR_OUTPUT_ROOT", str(runtime / "output"))
    monkeypatch.setenv("YOMU_ENV", "test")
    monkeypatch.setenv("YOMU_TEST_BACKEND", "mock")
    monkeypatch.setenv("YOMU_RECOVERY_E2E", "1")
    monkeypatch.setenv("YOMU_TEST_WALLET_STATE", str(runtime / "wallet_mock.pkl"))
    monkeypatch.setenv("YOMU_TEST_EXPORT_FAIL_MARKER", str(runtime / "fail-once.marker"))
    monkeypatch.setenv("YOMU_TEST_SOURCE_REF", source_ref)
    monkeypatch.setenv("TRADUTOR_DEVICE_UUID", "550e8400-e29b-41d4-a716-446655440000")
    bridge = ui_bridge.UiBridge()
    store = bridge.store
    command = [sys.executable, "-u", str(Path(__file__).resolve()),
               "--pipeline-child", str(snapshot.manifest_path), str(snapshot_root), str(output),
               "--translation-provider", "deepl", "--output-format", "png"]
    job_id = store.create_job(
        source_url=source_ref, output_dir=str(output), command=command,
        configuration={"job_type": "translation", "source_type": "local_folder",
            "mode": "quality", "full": True, "max_images": 3,
            "translation_enabled": True, "translation_provider": "deepl",
            "translation_request_id": "translation:recovery-e2e",
            "logical_job_id": "recovery-e2e", "auth_context_id": "recovery-e2e",
            "device_uuid": "550e8400-e29b-41d4-a716-446655440000",
            "output_format": "png", "local_manifest_path": str(snapshot.manifest_path),
            "snapshot_root": str(snapshot_root)},
        series_title="Recovery E2E", episode_number="1")
    AuthEnvelopeStore(runtime).seal(job_id, _token())
    # Persisted through the real runtime path; each OS process receives only files/env.
    wallet = DiskWalletProvider(runtime / "wallet_mock.pkl")
    wallet.save()

    def run_worker():
        env = os.environ.copy()
        proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "worker_service.py"),
            "--db", str(runtime / "jobs.sqlite3"), "--once"], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pid = proc.pid
        out, _ = proc.communicate(timeout=180)
        return pid, proc.returncode, out

    try:
        first_pid, first_rc, first_log = run_worker()
        first = store.get_job(job_id)
        assert first_pid > 0 and first_rc == 0, first_log[-6000:]
        runner_log = Path(first.get("log_path") or "")
        runner_text = runner_log.read_text(encoding="utf-8", errors="replace") if runner_log.is_file() else ""
        diagnostics = [(str(p), p.read_text(encoding="utf-8", errors="replace")[-5000:])
                       for p in runtime.rglob("*.log") if p.is_file()]
        assert first["status"] == JobStatus.INTERRUPTED, (first, runner_text[-10000:], diagnostics, first_log[-2000:])
        assert first.get("recoverable") == 1, first
        assert yk_reservation.translation_ready(output, job_id=job_id)
        first_wallet = json.loads((runtime / "wallet_state.json").read_text(encoding="utf-8"))
        first_calls = json.loads((runtime / "provider_calls.json").read_text(encoding="utf-8"))["calls"]
        assert first_calls == 1 and first_wallet["net_yk"] == 1
        # Source/staged page PNGs are expected before chapter-level export.
        # The fault is injected at the export boundary, before the final PNG
        # directory is published.
        assert (runtime / "fail-once.marker").is_file()
        assert not (output / "output").exists()
        assert first_pid != os.getpid()

        # communicate() returned with a concrete exit code, so the worker process
        # crossed a real OS process boundary and terminated before resume.
        assert first_rc is not None

        decision = SimpleNamespace(to_safe_job_metadata=lambda: {})
        with mock.patch.object(bridge, "_require_beta_access", return_value=decision), \
             mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            resumed = bridge.resume(job_id)
        second_job_id = resumed["job_id"]
        second_pid, second_rc, second_log = run_worker()
        second = store.get_job(second_job_id)
        assert second_pid > 0 and second_pid != first_pid
        assert second_rc == 0, second_log[-6000:]
        assert second["status"] == JobStatus.FINISHED, second
        final_wallet = json.loads((runtime / "wallet_state.json").read_text(encoding="utf-8"))
        final_calls = json.loads((runtime / "provider_calls.json").read_text(encoding="utf-8"))["calls"]
        assert final_calls == 1
        assert final_wallet["net_yk"] == 1
        assert second["previous_job_id"] == job_id
        assert yk_reservation.translation_ready(output, job_id=job_id)

        report = json.loads((output / "timing_report.json").read_text(encoding="utf-8"))
        png_dir = Path(report["png_path"])
        files = sorted(png_dir.glob("*.png"))
        assert len(files) == 3
        for path in files:
            assert path.stat().st_size > 0
            with Image.open(path) as image:
                image.verify()
                assert image.width >= 480 and image.height > 0
        e2e_result = {
            "first_worker_pid": first_pid,
            "second_worker_pid": second_pid,
            "pids_different": first_pid != second_pid,
            "first_worker_exit_code": first_rc,
            "second_worker_exit_code": second_rc,
            "first_job_id": job_id,
            "resumed_job_id": second_job_id,
            "first_status": "interrupted_recoverable",
            "final_status": second["status"],
            "first_provider_calls": first_calls,
            "final_provider_calls": final_calls,
            "first_net_yk": first_wallet["net_yk"],
            "final_net_yk": final_wallet["net_yk"],
            "translation_ready": True,
            "translation_loaded_from_persisted_state": final_calls == 1,
            "png_files": [{"path": str(path), "size": path.stat().st_size} for path in files],
            "source_or_ocr_rerun": "local snapshot materialization and fixture OCR reran",
            "fault_injection": "one-shot failure at PNG export after persisted translation and mock consume",
        }
        (runtime / "e2e_result.json").write_text(json.dumps(e2e_result, indent=2), encoding="utf-8")
        print("YK_RECOVERY_E2E_RESULT=" + json.dumps(e2e_result, separators=(",", ":")))
    finally:
        store.close()


if __name__ == "__main__":
    result = _child_main(sys.argv[1:])
    if result is not None:
        raise SystemExit(result)
