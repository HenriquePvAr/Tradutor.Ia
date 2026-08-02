"""Isolated worker child for one targeted quality-review rerun operation."""

from __future__ import annotations

import argparse
import json
import signal
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from chapter_quality_revision import ChapterQualityRevision
from job_store import JobStatus, JobStore
from runner_start_gate import wait_for_start_gate
from ui_helpers import sanitize_diagnostic_text

_STOP_REQUESTED = False


def _install_stop_handlers() -> None:
    def handler(_signum, _frame):
        global _STOP_REQUESTED
        _STOP_REQUESTED = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is not None:
            try:
                signal.signal(value, handler)
            except (OSError, ValueError):
                pass


def _cancel(jobs: JobStore, job_id: str, worker_id: str) -> None:
    current = jobs.get_job(job_id)
    if not current or current["status"] in JobStatus.TERMINAL:
        return
    config = current.get("configuration") or {}
    targets = [item for item in (config.get("targets") or []) if isinstance(item, dict)]
    prior = jobs.review_actions(job_id)
    try:
        processed = max(0, min(len(targets), int(prior.get("processed_regions") or 0)))
    except (TypeError, ValueError):
        processed = 0
    prior.update({
        "schema_version": 2,
        "target_regions": len(targets),
        "processed_regions": processed,
        "remaining_regions": 0,
        "cancelled_regions": max(0, len(targets) - processed),
        "current_step": "cancelled",
    })
    jobs.update_fields(
        job_id,
        review_actions_json=json.dumps(prior, ensure_ascii=False, sort_keys=True),
    )
    if current["status"] in {JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING}:
        jobs.transition(
            job_id,
            JobStatus.CANCELLING,
            expected_worker=worker_id,
            stage="cancelling",
            reason_code="user_cancelled",
        )
    jobs.transition(
        job_id,
        JobStatus.CANCELLED,
        expected_worker=worker_id,
        stage="cancelled",
        reason_code="user_cancelled",
        recoverable=0,
    )


def _write_log(path: str, message: str) -> None:
    if not path:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    safe = sanitize_diagnostic_text(str(message or ""))[:500]
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%H:%M:%S')} {safe}\n")


def _is_safe_draft(manifest: dict[str, Any], expected_regions: int) -> bool:
    if str(manifest.get("status") or "") != "draft_ready":
        return False
    if int(manifest.get("safe_changes_applied") or 0) < max(1, expected_regions):
        return False
    if int(manifest.get("manual_review") or 0) > 0:
        return False
    summary = manifest.get("visual_state_summary")
    if not isinstance(summary, dict):
        return False
    return not any(
        int(summary.get(key) or 0) > 0
        for key in ("rejected_visual_regression", "manual_review", "pending", "failed")
    )


def _manifest_region_outcomes(
    manifest: dict[str, Any], expected_regions: int
) -> tuple[int, int]:
    """Return disjoint approved/rejected counts for one processed target set."""
    summary = manifest.get("visual_state_summary")
    summary = summary if isinstance(summary, dict) else {}
    approved = max(0, min(expected_regions, int(summary.get("applied") or 0)))
    explicitly_rejected = sum(
        max(0, int(summary.get(key) or 0))
        for key in ("rejected_visual_regression", "manual_review", "pending", "failed")
    )
    rejected = max(explicitly_rejected, expected_regions - approved)
    return approved, min(expected_regions, rejected)


def _persist_rerun_state(
    jobs: JobStore,
    job_id: str,
    *,
    parent: dict[str, Any],
    child: dict[str, Any],
    targets: list[dict[str, Any]],
    manifests: list[dict[str, Any]],
    processed_regions: int,
    approved_regions: int,
    rejected_regions: int,
    provider_requests: int,
    allow_provider: bool,
    current_page: int = 0,
    current_region_id: str = "",
    current_step: str = "",
    cancelled: bool = False,
) -> dict[str, Any]:
    total = len(targets)
    cancelled_regions = max(0, total - processed_regions) if cancelled else 0
    remaining_regions = 0 if cancelled else max(0, total - processed_regions)
    result = {
        "schema_version": 2,
        "parent_job_id": str(parent["id"]),
        "parent_run_id": str(parent.get("run_id") or ""),
        "run_id": str(child.get("run_id") or ""),
        "target_regions": total,
        "target_pages": len({int(item.get("page") or 0) for item in targets}),
        "processed_regions": processed_regions,
        "approved_regions": approved_regions,
        "rejected_regions": rejected_regions,
        "remaining_regions": remaining_regions,
        "cancelled_regions": cancelled_regions,
        "provider_requests_planned": provider_requests,
        "provider_authorized": allow_provider,
        "current_page": int(current_page or 0),
        "current_region_id": str(current_region_id or "")[:160],
        "current_step": str(current_step or "")[:80],
        "page_revisions": [
            {
                "page": int(item.get("page") or 0),
                "page_revision_id": str(item.get("page_revision_id") or ""),
                "status": str(item.get("status") or ""),
                "safe_changes_applied": int(item.get("safe_changes_applied") or 0),
                "visual_state_summary": item.get("visual_state_summary") or {},
            }
            for item in manifests
        ],
    }
    jobs.update_fields(
        job_id,
        review_actions_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
    )
    return result


def execute_review_rerun(
    job_id: str,
    db_path: str,
    worker_id: str,
    *,
    log_path: str = "",
    engine_factory: Callable[..., ChapterQualityRevision] = ChapterQualityRevision,
) -> int:
    """Process only persisted target regions and terminalize the child run."""
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    _install_stop_handlers()
    jobs = JobStore(db_path)
    try:
        job = jobs.get_job(job_id)
        if not job or job["status"] not in {JobStatus.CLAIMING, JobStatus.STARTING}:
            return 2
        config = job.get("configuration") or {}
        if config.get("job_type") != "review_rerun" or job.get("operation_kind") != "review_rerun":
            return 2
        if str(job.get("parent_job_id") or "") != str(config.get("parent_job_id") or ""):
            raise ValueError("rerun_parent_mismatch")
        parent = jobs.get_job(str(job.get("parent_job_id") or ""))
        if not parent or str(parent.get("run_id") or "") != str(config.get("parent_run_id") or ""):
            raise ValueError("rerun_parent_not_current")
        if str(parent.get("owner_id") or "") != str(job.get("owner_id") or ""):
            raise ValueError("rerun_owner_mismatch")
        if jobs.cancel_requested(job_id):
            _cancel(jobs, job_id, worker_id)
            return 0
        if job["status"] == JobStatus.CLAIMING:
            job = jobs.transition(job_id, JobStatus.STARTING, expected_worker=worker_id)
        job = jobs.transition(
            job_id,
            JobStatus.RUNNING,
            expected_worker=worker_id,
            stage="review_rerun",
        )
        targets = [item for item in (config.get("targets") or []) if isinstance(item, dict)]
        if not targets:
            raise ValueError("rerun_targets_required")
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for target in targets:
            grouped[int(target.get("page") or 0)].append(target)
        if any(page <= 0 for page in grouped):
            raise ValueError("invalid_rerun_target")
        engine = engine_factory(
            str(parent["output_dir"]),
            job_id=str(parent["id"]),
            run_id=str(parent.get("run_id") or ""),
        )
        manifests: list[dict[str, Any]] = []
        total = len(targets)
        allow_provider = config.get("allow_provider") is True
        provider_requests = 0
        processed_regions = 0
        approved_regions = 0
        rejected_regions = 0
        for page, page_targets in sorted(grouped.items()):
            if _STOP_REQUESTED:
                jobs.transition(
                    job_id,
                    JobStatus.INTERRUPTED,
                    expected_worker=worker_id,
                    stage="interrupted",
                    interrupted_reason="worker_stop",
                    recoverable=1,
                )
                return 0
            if jobs.cancel_requested(job_id):
                _cancel(jobs, job_id, worker_id)
                return 0
            jobs.update_progress(
                job_id,
                stage="review_rerun",
                current=processed_regions,
                total=total,
                message=f"Página {page}: preparando {len(page_targets)} região(ões)",
                counter_stage="review_rerun_regions",
            )
            _persist_rerun_state(
                jobs, job_id, parent=parent, child=job, targets=targets,
                manifests=manifests, processed_regions=processed_regions,
                approved_regions=approved_regions, rejected_regions=rejected_regions,
                provider_requests=provider_requests, allow_provider=allow_provider,
                current_page=page,
                current_region_id=str(page_targets[0]["region_id"]),
                current_step="creating_mask_and_analyzing_neighbors",
            )
            human_overrides = {
                str(target["region_id"]): str(target.get("translation_to_reuse") or "")
                for target in page_targets
                if target.get("requires_provider") is not True
                and str(target.get("translation_to_reuse") or "").strip()
            }
            provider_requests += sum(
                1 for target in page_targets if target.get("requires_provider") is True
            )
            manifest = engine.revise_page(
                page,
                region_ids=[str(target["region_id"]) for target in page_targets],
                cache_only=not allow_provider,
                human_overrides=human_overrides or None,
            )
            manifests.append(manifest)
            page_approved, page_rejected = _manifest_region_outcomes(
                manifest, len(page_targets)
            )
            processed_regions += len(page_targets)
            approved_regions += page_approved
            rejected_regions += page_rejected
            _persist_rerun_state(
                jobs, job_id, parent=parent, child=job, targets=targets,
                manifests=manifests, processed_regions=processed_regions,
                approved_regions=approved_regions, rejected_regions=rejected_regions,
                provider_requests=provider_requests, allow_provider=allow_provider,
                current_page=page,
                current_region_id=str(page_targets[-1]["region_id"]),
                current_step="validating_residual",
            )
            jobs.update_progress(
                job_id,
                stage="review_rerun",
                current=processed_regions,
                total=total,
                message=f"Página {page}: {manifest.get('status') or 'concluída'}",
                counter_stage="review_rerun_regions",
            )
            _write_log(
                log_path,
                f"review_rerun page={page} regions={len(page_targets)} status={manifest.get('status')}",
            )
        safe = all(
            _is_safe_draft(manifest, len(grouped[int(manifest.get("page") or 0)]))
            for manifest in manifests
        )
        result = _persist_rerun_state(
            jobs, job_id, parent=parent, child=job, targets=targets,
            manifests=manifests, processed_regions=processed_regions,
            approved_regions=approved_regions, rejected_regions=rejected_regions,
            provider_requests=provider_requests, allow_provider=allow_provider,
            current_step="completed",
        )
        result["safe_to_apply"] = safe
        jobs.update_fields(
            job_id,
            review_actions_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
            exit_code=0,
        )
        terminal = JobStatus.FINISHED if safe else JobStatus.REVIEW_REQUIRED
        jobs.transition(
            job_id,
            terminal,
            expected_worker=worker_id,
            stage="finished" if safe else "review_required",
            reason_code="completed" if safe else "quality_review_required",
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - runner boundary must settle the job
        current = jobs.get_job(job_id)
        if current and current["status"] in JobStatus.IN_FLIGHT:
            jobs.transition(
                job_id,
                JobStatus.FAILED,
                expected_worker=worker_id,
                stage="failed",
                reason_code="review_rerun_failed",
                error_type=type(exc).__name__[:80],
                error_message=sanitize_diagnostic_text(str(exc))[:500],
                exit_code=1,
            )
        _write_log(log_path, f"review_rerun failed type={type(exc).__name__}")
        return 0
    finally:
        jobs.close()


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
    return execute_review_rerun(
        args.job_id,
        args.db,
        args.worker_id,
        log_path=args.log,
    )


if __name__ == "__main__":
    raise SystemExit(main())
