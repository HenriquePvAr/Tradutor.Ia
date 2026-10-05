"""Worker wrapper that records phases without changing the clean pipeline."""
from __future__ import annotations

import time
from pathlib import Path

from research.gemini_vps_web_worker.worker import Worker
from research.gemini_vps_web_worker.models import JobStatus
from research.gemini_vps_web_worker.validation import validate_and_composite, validate_input
from research.gemini_vps_web_worker.browser import AuthInteractionRequired
from perf_timing import PhaseTimer


class PerformanceWorker(Worker):
    def run_once(self) -> bool:
        job = self.store.claim_next()
        if not job:
            return False
        timer = PhaseTimer()
        timer.mark("slot_acquire_start_at")
        started = time.monotonic()
        try:
            source = Path(job.original_path)
            _, normalized = validate_input(source, source.parent / "normalized")
            timer.mark("page_ready_at")
            self.store.update(job, JobStatus.UPLOADING, normalized_path=normalized.path)
            raw = source.parent / "gemini_raw.png"
            timer.mark("upload_start_at")
            result = self.provider.submit_and_recover(normalized.path, raw, job_id=job.job_id)
            timer.mark("raw_recovery_end_at")
            provider_timing = result.pop("performance_timing", {})
            timer.mark("upload_end_at")
            self.store.update(job, JobStatus.RECOVERING_OUTPUT, raw_path=str(raw))
            timer.mark("alignment_start_at")
            self.store.update(job, JobStatus.ALIGNING)
            timer.mark("alignment_end_at")
            final = source.parent / "final.png"
            timer.mark("validation_start_at")
            self.store.update(job, JobStatus.COMPOSITING)
            validation = validate_and_composite(source, raw, final, job.authorized_mask_path,
                                                metadata=job.metadata)
            timer.mark("safe_composite_end_at")
            timer.mark("validation_end_at")
            self.store.update(job, JobStatus.VALIDATING)
            status = JobStatus.COMPLETED if validation["quality_status"] == "ACCEPTABLE" else JobStatus.REVIEW_REQUIRED
            job.metadata.update({"output": result, "validation": validation,
                                 "performance_timing": {"worker": timer.export(), "provider": provider_timing}})
            self.store.update(job, status, final_path=str(final) if final.exists() else "",
                              timings={"total_seconds": time.monotonic() - started})
            self.store.move_marker(job.job_id, "completed" if status == JobStatus.COMPLETED else "review_required")
        except AuthInteractionRequired as exc:
            self.store.update(job, JobStatus.AUTH_INTERACTION_REQUIRED, error=str(exc))
            self.store.move_marker(job.job_id, "failed")
        except ValueError as exc:
            job.metadata.update({"validation": {"quality_status": "REVIEW_REQUIRED_INVALID_AUTHORIZATION",
                                                 "reason": str(exc)}, "performance_timing": {"worker": timer.export()}})
            self.store.update(job, JobStatus.REVIEW_REQUIRED, error=str(exc))
            self.store.move_marker(job.job_id, "review_required")
        except Exception as exc:
            self.store.update(job, JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            self.store.move_marker(job.job_id, "failed")
        return True
