"""Isolated worker child for re-rendering a persisted Review snapshot."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from job_store import JobStatus, JobStore
from runner_start_gate import wait_for_start_gate
from ui_helpers import sanitize_diagnostic_text


def _safe_child(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_output(final_dir: Path, fmt: str, expected_pages: int) -> dict[str, Any]:
    if not final_dir.is_dir():
        raise ValueError("review_output_missing")
    pages = sorted(final_dir.glob("page_*.png"))
    if len(pages) != expected_pages:
        raise ValueError("review_output_page_count_mismatch")
    from PIL import Image
    dimensions: list[tuple[int, int]] = []
    for path in pages:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.width <= 0 or image.height <= 0:
                raise ValueError("review_png_dimensions_invalid")
            dimensions.append(image.size)
    manifest_path = final_dir / "reconstruction_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = final_dir if fmt == "png" else final_dir / str(manifest.get("artifact") or "")
    if fmt == "pdf":
        from pdf_reader import parse_document
        parsed = parse_document(artifact)
        if len(parsed.pages) != expected_pages or any(page.width <= 0 or page.height <= 0 for page in parsed.pages):
            raise ValueError("review_pdf_page_count_mismatch")
    elif fmt == "psd":
        from psd_tools import PSDImage
        psd_files = sorted(artifact.glob("*.psd")) if artifact.is_dir() else [artifact]
        if len(psd_files) != expected_pages:
            raise ValueError("review_psd_page_count_mismatch")
        if any(not PSDImage.open(str(path)).width or not PSDImage.open(str(path)).height for path in psd_files):
            raise ValueError("review_psd_dimensions_invalid")
    elif fmt != "png":
        raise ValueError("review_output_format_unsupported")
    return {"pages": pages, "artifact": artifact, "dimensions": dimensions, "manifest": manifest}


def _validate_lineage(verified: dict[str, Any], job: dict[str, Any], config: dict[str, Any], *, final: bool) -> None:
    manifest = verified["manifest"]
    expected = {
        "source_job_id": str(config.get("source_job_id") or ""),
        "source_run_id": str(config.get("source_run_id") or ""),
        "review_snapshot_id": str(config.get("review_snapshot_id") or ""),
        "output_format": str(config.get("output_format") or "").casefold(),
    }
    if any(str(manifest.get(key) or "") != value for key, value in expected.items()):
        raise ValueError("review_output_lineage_mismatch")
    if final and str(manifest.get("review_generation_id") or "") != str(config.get("review_generation_id") or ""):
        raise ValueError("review_output_generation_mismatch")


def _fail_once(stage: str, generation_root: Path) -> bool:
    if os.getenv("APP_ENV", "").casefold() not in {"test", "testing"}:
        return False
    if os.getenv("YOMU_TEST_REVIEW_REEXPORT_FAIL_ONCE", "").casefold() != stage.casefold():
        return False
    marker = generation_root / "faults" / f"{stage}.once"
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="ascii") as handle:
            handle.write("injected\n")
        return True
    except FileExistsError:
        return False


def execute_review_reexport(job_id: str, db_path: str, worker_id: str, *, log_path: str = "") -> int:
    store = JobStore(db_path)
    attempt_root: Path | None = None
    try:
        job = store.get_job(job_id)
        if not job or job.get("status") not in {JobStatus.CLAIMING, JobStatus.STARTING}:
            return 2
        config = job.get("configuration") or {}
        if config.get("job_type") != "review_reexport" or job.get("operation_kind") != "artifact_reconstruction":
            raise ValueError("review_generation_contract_invalid")
        parent = store.get_job(str(config.get("source_job_id") or ""))
        if (not parent or str(parent.get("run_id") or "") != str(config.get("source_run_id") or "")
                or str(parent.get("owner_id") or "") != str(job.get("owner_id") or "")):
            raise ValueError("review_generation_parent_mismatch")
        root = Path(str(config.get("generation_root") or "")).resolve()
        expected_root = Path(str(parent.get("output_dir") or "")).resolve() / "review_generations" / str(config.get("review_generation_id") or "")
        snapshot_path = Path(str(config.get("snapshot_path") or "")).resolve()
        if root != expected_root or snapshot_path != root / "review_snapshot.json" or not snapshot_path.is_file():
            raise ValueError("review_generation_path_invalid")
        if store.cancel_requested(job_id):
            store.transition(job_id, JobStatus.CANCELLED, expected_worker=worker_id,
                             stage="cancelled", reason_code="user_cancelled", recoverable=0)
            return 0
        if job["status"] == JobStatus.CLAIMING:
            job = store.transition(job_id, JobStatus.STARTING, expected_worker=worker_id)
        job = store.transition(job_id, JobStatus.RUNNING, expected_worker=worker_id,
                               stage="loading_artifacts", progress_current=0,
                               progress_total=0, progress_message="Carregando snapshot e artefatos persistidos")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if (snapshot.get("snapshot_id") != config.get("review_snapshot_id")
                or snapshot.get("job_id") != parent.get("id")
                or snapshot.get("run_id") != parent.get("run_id")):
            raise ValueError("review_snapshot_lineage_mismatch")
        store.update_progress(job_id, stage="loading_artifacts", current=0,
                              total=len(snapshot.get("pages") or []), message="Carregando estado da geração")
        final_dir = root / "final"
        if final_dir.exists():
            store.update_progress(job_id, stage="validating_output", message="Validando saída existente para recovery")
            verified = _validate_output(final_dir, str(config["output_format"]), len(snapshot["pages"]))
            _validate_lineage(verified, job, config, final=True)
            store.update_fields(job_id, output_dir=str(final_dir), exit_code=0, recoverable=0,
                                progress_current=len(verified["pages"]))
            store.transition(job_id, JobStatus.FINISHED, expected_worker=worker_id,
                             stage="finished", reason_code="completed", progress_current=len(snapshot["pages"]),
                             progress_total=len(snapshot["pages"]), progress_message="Saída recuperada e validada")
            return 0

        staging_parent = root / "staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        # A retry only becomes claimable after the previous child is terminal/reconciled.
        # Thus stale attempts in this generation are safe to remove, and no other
        # generation's data is touched.
        for stale in staging_parent.iterdir():
            if stale.is_dir() and _safe_child(stale, staging_parent):
                shutil.rmtree(stale, ignore_errors=True)
        attempt_root = staging_parent / f"attempt-{int(config.get('attempt') or job.get('attempt') or 1):03d}"
        attempt_root.mkdir(parents=True, exist_ok=False)
        if _fail_once("loading_artifacts", root):
            os._exit(75)  # test-only hard process boundary; worker reconciles RUNNING -> INTERRUPTED
        store.update_progress(job_id, stage="validating_review", current=0,
                              total=len(snapshot["pages"]), message="Validando revisão efetiva")
        from review_reexport import ReviewReexportError, render_review_snapshot, validate_review_snapshot
        blockers = validate_review_snapshot(snapshot)
        if blockers:
            raise ReviewReexportError("review_blocked", blockers)
        fmt = str(config.get("output_format") or "").casefold()
        store.update_progress(job_id, stage="exporting", current=0, total=len(snapshot["pages"]),
                              message="Preparando export final")
        if _fail_once("export", root):
            os._exit(75)  # test-only hard process boundary; leaves staging for retry cleanup

        def report_progress(stage: str, current: int, total: int, page: int) -> None:
            mapped = "reconstructing" if stage == "reconstructing" else "exporting"
            message = f"Página {page} de {total}" if page else "Exportando arquivo final"
            store.update_progress(job_id, stage=mapped, current=current, total=total,
                                  message=message, counter_stage="review_reexport_pages")

        result = render_review_snapshot(
            snapshot=snapshot, output_root=str(parent["output_dir"]), output_format=fmt,
            chapter_name=str(config.get("chapter_name") or parent.get("series_title") or "chapter"),
            staging_root=attempt_root / "render", progress=report_progress,
            typesetting_mode=str(config.get("typesetting_mode") or "on").casefold(),
        )
        rendered_dir = Path(result["output_dir"]).resolve()
        if not _safe_child(rendered_dir, attempt_root) or final_dir.exists():
            raise ValueError("review_generation_publish_path_invalid")
        store.update_progress(job_id, stage="validating_output", current=0,
                              total=len(snapshot["pages"]), message="Validando PNG/PDF/PSD antes de publicar")
        verified = _validate_output(rendered_dir, fmt, len(snapshot["pages"]))
        _validate_lineage(verified, job, config, final=False)
        manifest = dict(verified["manifest"])
        manifest.update({"review_generation_id": config["review_generation_id"],
                         "review_snapshot_id": config["review_snapshot_id"],
                         "source_job_id": parent["id"], "source_run_id": parent["run_id"],
                         "attempt": int(config.get("attempt") or 1),
                         "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        (rendered_dir / "reconstruction_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(rendered_dir, final_dir)
        artifact = final_dir if fmt == "png" else final_dir / str(manifest["artifact"])
        store.update_fields(job_id, output_dir=str(final_dir), exit_code=0, recoverable=0,
                            progress_current=len(verified["pages"]))
        store.update_progress(job_id, stage="finished", current=len(snapshot["pages"]),
                              total=len(snapshot["pages"]), message="Geração revisada concluída",
                              counter_stage="review_reexport_pages")
        store.transition(job_id, JobStatus.FINISHED, expected_worker=worker_id,
                         stage="finished", reason_code="completed")
        return 0
    except Exception as exc:  # noqa: BLE001 - process boundary must settle state
        current = store.get_job(job_id)
        if current and current["status"] in JobStatus.IN_FLIGHT:
            store.transition(job_id, JobStatus.FAILED, expected_worker=worker_id,
                             stage="failed", reason_code="review_reexport_failed",
                             error_type=type(exc).__name__[:80],
                             error_message=sanitize_diagnostic_text(str(exc))[:400],
                             recoverable=1, exit_code=1)
        if log_path:
            try:
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                Path(log_path).write_text(f"review_reexport_failed type={type(exc).__name__}\n", encoding="utf-8")
            except OSError:
                pass
        return 0
    finally:
        if attempt_root is not None and attempt_root.exists() and _safe_child(attempt_root, attempt_root.parent):
            shutil.rmtree(attempt_root, ignore_errors=True)
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--log", default="")
    parser.add_argument("--start-gate", default="")
    args = parser.parse_args(argv)
    if not wait_for_start_gate(args.start_gate):
        return 2
    return execute_review_reexport(args.job_id, args.db, args.worker_id, log_path=args.log)


if __name__ == "__main__":
    raise SystemExit(main())
