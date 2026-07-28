"""The source-analysis phase, owned by whoever runs it.

The decision that follows an analysis — incomplete coverage, manual review, unsupported
outcome, missing environment, or ready to run — used to live inside the HTTP request
handler. It belongs to neither the request nor the worker: it belongs to the phase.

This module holds one implementation of that decision, depending only on a ``JobStore`` and
explicit callables. It imports nothing from the UI, no web framework and no global bridge,
so the worker can reach it without importing the request layer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from job_store import JobStatus

# Phase outcomes. One decision, not a set of booleans that can disagree.
READY_FOR_RUNNER = "ready_for_runner"
SOURCE_READY = "source_analysis_ready"
AWAITING_REVIEW = "awaiting_source_review"
FAILED = "failed"
CANCELLED = "cancelled"


class PhaseCancelled(RuntimeError):
    """The caller asked to stop. Never propagated far enough to kill the worker."""


@dataclass
class SourceAnalysisPhaseResult:
    """What the phase decided, and whether a runner may start.

    ``should_spawn_runner`` is derived from the outcome rather than set independently, so a
    review or a terminal state can never accidentally start a child process.
    """

    outcome: str
    job_id: str
    status: str = ""
    stage: str = ""
    reason_code: str = ""
    message: str = ""
    analysis: dict[str, Any] = field(default_factory=dict)
    selection: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def should_spawn_runner(self) -> bool:
        return self.outcome == READY_FOR_RUNNER

    @property
    def requires_review(self) -> bool:
        return self.outcome == AWAITING_REVIEW

    @property
    def terminal(self) -> bool:
        return self.outcome in (FAILED, CANCELLED)

    @property
    def cancelled(self) -> bool:
        return self.outcome == CANCELLED


# ---- sanitized provenance ---------------------------------------------------
def remote_source_provenance(public_analysis: dict[str, Any]) -> dict[str, Any]:
    """Return fixed-width, URL-free provenance fields for a remote source job.

    The full public analysis stays in its JSON diagnostic column. The indexed job columns
    intentionally receive only the small adapter/count subset needed to identify a run in
    history — never a page URL, cookie, selector, source title or signed token.
    """
    analysis = public_analysis if isinstance(public_analysis, dict) else {}

    def identifier(value: Any) -> str:
        text = str(value or "").strip()
        return text if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", text) else ""

    def count(name: str) -> int:
        value = analysis.get(name, 0)
        try:
            result = int(value)
        except (TypeError, ValueError):
            return 0
        return result if 0 <= result <= 10_000 else 0

    def score() -> float:
        try:
            value = float(analysis.get("confidence", 0.0))
        except (TypeError, ValueError):
            return 0.0
        return value if 0.0 <= value <= 1.0 else 0.0

    return {
        "source_type": "url",
        "adapter_name": identifier(analysis.get("adapter")),
        "adapter_version": identifier(analysis.get("adapter_version")),
        # A browser analysis has not selected a network transport yet.  Represent that
        # explicitly; the runner replaces it with the transport actually used.
        "transport_name": "pending",
        "source_score": score(),
        "candidate_count": count("candidate_count"),
        "input_count": count("candidate_count"),
        "accepted_count": count("accepted_count"),
        "rejected_count": count("discarded_count"),
    }


def source_analysis_is_incomplete(public_analysis: dict[str, Any]) -> bool:
    """Recognise only bounded collector coverage warnings in a public analysis."""
    warnings = public_analysis.get("warnings", ()) if isinstance(public_analysis, dict) else ()
    if isinstance(warnings, (str, bytes)):
        warnings = (warnings,)
    return bool({
        str(value or "").strip() for value in (warnings or ())
    } & {
        "page_limit_exceeded",
        "scroll_incomplete",
        "pagination_incomplete",
        "lazy_resolution_timeout",
        "lazy_resolution_max_rounds",
        "reader_dom_changed",
    })


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def command_with_source_selection(job: dict[str, Any], selection: dict[str, Any]) -> list[str]:
    """Rebuild the runner command so the downloader receives the persisted selection.

    The submit-time command is intentionally created before browser analysis.  Once the
    worker has a concrete set of opaque candidate IDs, the command must carry them to
    ``run_webtoon.py``; otherwise a bounded selected run looks like an unconfirmed generic
    smart-split run and can expand back to the whole chapter.
    """
    from pathlib import Path
    import sys

    from ui_helpers import build_run_command

    config = job.get("configuration") if isinstance(job.get("configuration"), dict) else {}
    command_selection = _bounded_command_selection(config, selection)
    return build_run_command(
        url=str(job.get("source_url") or ""),
        mode=str(config.get("mode") or "fast"),
        output=Path(str(job.get("output_dir") or "chapter")).name,
        full=bool(config.get("full", True)),
        max_images=config.get("max_images"),
        use_cache=bool(config.get("use_cache")),
        force=bool(config.get("force")),
        use_context=bool(config.get("use_context", True)),
        source_candidate_ids=command_selection,
        open_output=bool(config.get("open_output", False)),
        download_only=bool(config.get("download_only", False)),
        python_executable=sys.executable,
    )


def _bounded_command_selection(config: dict[str, Any], selection: dict[str, Any]) -> list[str]:
    candidate_ids = [
        str(value or "")
        for value in list(selection.get("candidate_ids") or [])
        if str(value or "")
    ]
    if bool(config.get("full", True)):
        return candidate_ids
    try:
        max_images = int(config.get("max_images") or 0)
    except (TypeError, ValueError):
        max_images = 0
    if max_images > 0:
        return candidate_ids[:max_images]
    return candidate_ids


def ensure_command_has_source_selection(store: Any, job: dict[str, Any]) -> dict[str, Any]:
    raw = job.get("source_selection_json") if isinstance(job, dict) else None
    try:
        selection = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw or {})
    except (TypeError, ValueError):
        return job
    if not selection.get("candidate_ids"):
        return job
    command = command_with_source_selection(job, selection)
    existing = list(job.get("command") or [])
    if existing == command:
        return job
    store.update_fields(job["id"], command_json=json.dumps(command, ensure_ascii=False))
    return store.get_job(job["id"]) or job


def apply_source_analysis(
    store: Any,
    job: dict[str, Any],
    analysis: Any,
    *,
    environment_ready: Callable[[], bool] | None = None,
    public_provenance: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> SourceAnalysisPhaseResult:
    """Persist the analysis and decide the job's next state.

    ``environment_ready`` is injected because the request layer refuses a job whose .env is
    unconfigured; the worker asks the same question at the same point.
    """
    from universal_chapter_adapter import (
        REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
        SUPPORTED_GENERIC_HIGH_CONFIDENCE,
        SUPPORTED_SPECIFIC_ADAPTER,
    )

    public_analysis = analysis.public()
    provenance = remote_source_provenance(public_analysis)
    job_id = job["id"]

    if source_analysis_is_incomplete(public_analysis):
        # Manual review may choose among an uncertain *complete* reader manifest; it is
        # never an override for evidence the collector already knows is partial. This is
        # analysis coverage, not download coverage — no download has been attempted.
        from chapter_source import INCOMPLETE_SOURCE_COVERAGE

        row = store.transition(
            job_id, JobStatus.FAILED, reason_code=INCOMPLETE_SOURCE_COVERAGE,
            source_analysis_json=_dump(public_analysis), stage="source_analysis",
            error_type="source_analysis", error_message="source_coverage_incomplete",
            **provenance)
        return SourceAnalysisPhaseResult(
            outcome=FAILED, job_id=job_id, status=JobStatus.FAILED, stage="source_analysis",
            reason_code=INCOMPLETE_SOURCE_COVERAGE, analysis=public_analysis,
            payload={"ok": False, "run_id": row["run_id"], "job_id": job_id,
                     "analysis": public_analysis, "reason_code": INCOMPLETE_SOURCE_COVERAGE})

    selected_ids = [candidate.id for candidate in analysis.accepted]

    if analysis.outcome == REVIEW_REQUIRED_MEDIUM_CONFIDENCE:
        row = store.transition(
            job_id, JobStatus.AWAITING_SOURCE_REVIEW, reason_code=analysis.outcome,
            source_analysis_json=_dump(public_analysis), stage="awaiting_source_review",
            **provenance)
        payload = {"ok": True, "awaiting_source_review": True, "run_id": row["run_id"],
                   "job_id": job_id, "analysis": public_analysis}
        if public_provenance is not None:
            payload["source_provenance"] = public_provenance(row)
        return SourceAnalysisPhaseResult(
            outcome=AWAITING_REVIEW, job_id=job_id,
            status=JobStatus.AWAITING_SOURCE_REVIEW, stage="awaiting_source_review",
            reason_code=analysis.outcome, analysis=public_analysis, payload=payload)

    if analysis.outcome not in {SUPPORTED_SPECIFIC_ADAPTER, SUPPORTED_GENERIC_HIGH_CONFIDENCE}:
        # A terminal, sanitised outcome so a source failure is never a silently abandoned
        # submission. No runner or child process starts.
        row = store.transition(
            job_id, JobStatus.FAILED, reason_code=analysis.outcome,
            source_analysis_json=_dump(public_analysis), stage="source_analysis",
            error_type="source_analysis", error_message=analysis.outcome, **provenance)
        return SourceAnalysisPhaseResult(
            outcome=FAILED, job_id=job_id, status=JobStatus.FAILED, stage="source_analysis",
            reason_code=analysis.outcome, analysis=public_analysis,
            payload={"ok": False, "run_id": row["run_id"], "job_id": job_id,
                     "analysis": public_analysis, "reason_code": analysis.outcome})

    if not bool((job.get("configuration") or {}).get("download_only")) and environment_ready is not None and not environment_ready():
        reason_code = "environment_not_configured"
        row = store.transition(
            job_id, JobStatus.FAILED, reason_code=reason_code,
            source_analysis_json=_dump(public_analysis), stage="source_analysis",
            error_type="configuration", error_message=reason_code, **provenance)
        return SourceAnalysisPhaseResult(
            outcome=FAILED, job_id=job_id, status=JobStatus.FAILED, stage="source_analysis",
            reason_code=reason_code, analysis=public_analysis,
            message="Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.",
            payload={"ok": False, "run_id": row["run_id"], "job_id": job_id,
                     "analysis": public_analysis, "reason_code": reason_code,
                     "message": "Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.",
                     "stage": "configuração",
                     "action": "Configure o ambiente e envie a fonte novamente."})

    # High-confidence extraction is re-analysed by the runner. Storing the opaque ids gives
    # auditability without pinning a volatile DOM order into the command.
    selection = {
        "candidate_ids": selected_ids,
        "automatic": True,
        "accepted_candidate_count": len(selected_ids),
        "selected_candidate_count": len(selected_ids),
        "manual_subset": False,
    }
    configuration = dict(job.get("configuration") or {})
    configuration["source_analysis"] = public_analysis
    configuration["source_selection"] = selection
    command = command_with_source_selection(job, selection)
    from source_readiness import SourceReadinessStore, source_result_from_analysis

    readiness = SourceReadinessStore(store.db_path)
    try:
        persisted = readiness.persist_analysis(source_result_from_analysis(job, analysis))
    finally:
        readiness.close()
    configuration["source_analysis_result_id"] = persisted.analysis_id
    configuration["source_analysis_result_hash"] = persisted.result_hash
    row = store.transition(
        job_id, JobStatus.SOURCE_ANALYSIS_READY, source_analysis_json=_dump(public_analysis),
        source_selection_json=_dump(selection),
        configuration_json=_dump(configuration),
        command_json=json.dumps(command, ensure_ascii=False),
        stage="source_analysis_ready", reason_code="download_authorization_required",
        **provenance)
    return SourceAnalysisPhaseResult(
        outcome=SOURCE_READY, job_id=job_id, status=JobStatus.SOURCE_ANALYSIS_READY,
        stage="source_analysis_ready", reason_code="download_authorization_required",
        analysis=public_analysis, selection=selection,
        payload={"ok": True, "run_id": row["run_id"], "job_id": job_id,
                 "analysis": public_analysis,
                 "source_analysis_result": persisted.public()})


def has_usable_selection(job: dict[str, Any]) -> bool:
    """True when a previous run already produced a selection worth reusing.

    Guards the restart case: a worker that died after persisting the selection must not
    reopen a browser and analyse the source a second time.
    """
    raw = job.get("source_selection_json") if isinstance(job, dict) else None
    if not raw:
        return False
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
    except (TypeError, ValueError):
        return False
    return bool(payload.get("candidate_ids"))


def should_spawn_runner(job: dict[str, Any],
                        result: SourceAnalysisPhaseResult | None = None) -> bool:
    """Single gate for starting a child process.

    A runner must never start for a review, a terminal state, or a job this worker no longer
    owns — those are exactly the cases where an unwanted child would keep running after the
    job stopped.
    """
    if result is not None and not result.should_spawn_runner:
        return False
    status = (job or {}).get("status")
    if status in JobStatus.TERMINAL or status in {
        JobStatus.AWAITING_SOURCE_REVIEW, JobStatus.SOURCE_ANALYSIS_READY,
    }:
        return False
    if (job or {}).get("cancel_requested"):
        return False
    return True
