from __future__ import annotations

import time
from pathlib import Path

from .browser import AuthInteractionRequired, GeminiWebProvider
from .models import JobStatus
from .storage import JobStore
from .validation import training_candidate_decision, validate_and_composite, validate_input


class Worker:
    def __init__(self, root: str | Path, provider: GeminiWebProvider):
        self.store = JobStore(root)
        self.provider = provider

    def run_once(self) -> bool:
        job = self.store.claim_next()
        if not job:
            return False
        started = time.monotonic()
        try:
            source = Path(job.original_path)
            _, normalized = validate_input(source, source.parent / "normalized")
            self.store.update(job, JobStatus.UPLOADING, normalized_path=normalized.path)
            raw = source.parent / "gemini_raw.png"
            result = self.provider.submit_and_recover(normalized.path, raw, job_id=job.job_id)
            self.store.update(job, JobStatus.RECOVERING_OUTPUT, raw_path=str(raw))
            self.store.update(job, JobStatus.ALIGNING)
            final = source.parent / "final.png"
            self.store.update(job, JobStatus.COMPOSITING)
            validation = validate_and_composite(source, raw, final, job.authorized_mask_path,
                                                metadata=job.metadata)
            self.store.update(job, JobStatus.VALIDATING)
            status = JobStatus.COMPLETED if validation["quality_status"] == "ACCEPTABLE" else JobStatus.REVIEW_REQUIRED
            job.metadata.update({"output": result, "validation": validation,
                                 "training_candidate": training_candidate_decision(validation, job.metadata)})
            self.store.update(job, status, final_path=str(final) if final.exists() else "",
                              timings={"total_seconds": time.monotonic() - started})
            self.store.move_marker(job.job_id, "completed" if status == JobStatus.COMPLETED else "review_required")
        except AuthInteractionRequired as exc:
            self.store.update(job, JobStatus.AUTH_INTERACTION_REQUIRED, error=str(exc))
            self.store.move_marker(job.job_id, "failed")
        except ValueError as exc:
            job.metadata.update({"validation": {"quality_status": "REVIEW_REQUIRED_INVALID_AUTHORIZATION",
                                                 "reason": str(exc)}})
            self.store.update(job, JobStatus.REVIEW_REQUIRED, error=str(exc))
            self.store.move_marker(job.job_id, "review_required")
        except Exception as exc:
            self.store.update(job, JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            self.store.move_marker(job.job_id, "failed")
        return True

    def serve_forever(self, poll_seconds: float = 1.0):
        while True:
            self.run_once()
            time.sleep(poll_seconds)
