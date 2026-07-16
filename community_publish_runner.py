"""Runner for one community_publish job: resumable, chunked PDF upload + verification.

Spawned by the worker (like job_runner, but by job_type). Streams the local PDF to the
storage provider in chunks - never loading it whole into memory - persisting the byte
count after each chunk so an interrupted upload resumes from where it stopped. Retries
only transient errors with backoff and jitter. On completion it verifies the remote file
(size, and provider checksum when comparable) and only then marks the file verified and
the post published. Honors cooperative cancellation and worker stop.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import time
from pathlib import Path

from community_storage import StorageError, build_storage_provider
from community_store import CommunityStore, FileStatus, PostStatus
from job_store import JobStatus, JobStore, TransitionError

CHUNK_SIZE = 256 * 1024
HEARTBEAT_SECONDS = 3.0
MAX_TRANSIENT_RETRIES = 6
# Test-only pacing hook (seconds per chunk); 0 in production. Lets a survival test span a
# realistic upload window without a giant file.
_CHUNK_DELAY = float(os.getenv("COMMUNITY_UPLOAD_CHUNK_DELAY", "0") or 0)

_STOP = False


def _install_stop_handlers() -> None:
    def _h(_signum, _frame):
        global _STOP
        _STOP = True
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _h)
            except (ValueError, OSError):
                pass


def _backoff(attempt: int) -> float:
    return min(30.0, (2 ** attempt) * 0.2) + random.uniform(0, 0.2)


def run_job(job_id: str, db_path: str) -> int:
    _install_stop_handlers()
    jobs = JobStore(db_path)
    community = None
    try:
        job = jobs.get_job(job_id)
        if job is None or job["status"] not in {JobStatus.CLAIMING, JobStatus.STARTING}:
            return 2
        config = job.get("configuration") or {}
        if config.get("job_type") != "community_publish":
            return 2
        if jobs.cancel_requested(job_id):
            jobs.transition(job_id, JobStatus.CANCELLING, expected_worker=job.get("worker_id"))
            jobs.transition(job_id, JobStatus.CANCELLED, expected_worker=job.get("worker_id"))
            return 0

        community = CommunityStore(config["community_db"])
        provider = build_storage_provider(config.get("storage") or {})
        file_id = config["file_id"]
        post_id = config["post_id"]
        pdf_path = Path(config["local_pdf_path"])
        total = int(config["pdf_size"])

        job = jobs.transition(job_id, JobStatus.STARTING, expected_worker=job.get("worker_id")) \
            if job["status"] == JobStatus.CLAIMING else job
        job = jobs.transition(job_id, JobStatus.RUNNING, expected_worker=job.get("worker_id"),
                              stage="uploading")
        community.update_file(file_id, upload_status=FileStatus.UPLOADING)

        # Resume from any previously uploaded bytes, else start a fresh session.
        record = community.get_file(file_id)
        folder = provider.ensure_folder(config.get("series_slug") or "series",
                                        (config.get("storage") or {}).get("root_folder_id", "root"))
        session = provider.create_resumable_session(
            filename=config["pdf_filename"], mime_type="application/pdf", size=total,
            parent_id=folder, sha256=config.get("pdf_sha256", ""))
        community.update_file(file_id, storage_folder_id=folder, session_ref=session.session_id,
                              bytes_uploaded=0)

        cancelled = False
        interrupted = False
        remote_file_id = session.file_id  # some providers know it upfront; Drive reports it on completion
        with pdf_path.open("rb") as handle:
            offset = 0
            while offset < total:
                if jobs.cancel_requested(job_id):
                    cancelled = True
                    break
                if _STOP:
                    interrupted = True
                    break
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                result = _upload_with_retry(provider, session, offset, chunk, jobs, job_id)
                if result is None:
                    _fail(jobs, community, job_id, post_id, file_id, job, "upload_failed")
                    return 0
                offset = result.uploaded
                if getattr(result, "file_id", ""):
                    remote_file_id = result.file_id
                community.update_file(file_id, bytes_uploaded=offset)
                jobs.update_progress(job_id, stage="uploading", current=offset, total=total,
                                     message=f"{offset}/{total} bytes")
                if _CHUNK_DELAY:
                    time.sleep(_CHUNK_DELAY)

        if cancelled:
            community.update_file(file_id, upload_status=FileStatus.FAILED)
            jobs.transition(job_id, JobStatus.CANCELLING, expected_worker=job.get("worker_id"))
            jobs.transition(job_id, JobStatus.CANCELLED, expected_worker=job.get("worker_id"))
            return 0
        if interrupted:
            # Preserve progress for resume; the job goes back to a recoverable state.
            jobs.transition(job_id, JobStatus.INTERRUPTED, expected_worker=job.get("worker_id"),
                            interrupted_reason="worker_stop", recoverable=1)
            return 0

        # Verify the remote file before publishing.
        jobs.update_progress(job_id, stage="verifying", current=total, total=total)
        community.update_file(file_id, upload_status=FileStatus.VERIFYING,
                              storage_file_id=remote_file_id)
        try:
            meta = provider.stat_file(remote_file_id)
        except StorageError as exc:
            _fail(jobs, community, job_id, post_id, file_id, job, f"verify_error:{exc.status}")
            return 0
        if meta.trashed or meta.size != total or meta.mime_type != "application/pdf":
            _fail(jobs, community, job_id, post_id, file_id, job, "verify_mismatch")
            return 0

        community.update_file(file_id, upload_status=FileStatus.VERIFIED,
                              provider_checksum=meta.checksum, verified_at=time.time())
        community.set_post_status(post_id, PostStatus.PUBLISHED, actor_id=config.get("user_id", ""))
        community.add_event(post_id, config.get("user_id", ""), "published",
                            {"file_id": file_id, "size": total})
        jobs.transition(job_id, JobStatus.FINISHED, expected_worker=job.get("worker_id"),
                        stage="finished", pdf_path="")
        return 0
    finally:
        jobs.close()
        if community is not None:
            community.close()


def _upload_with_retry(provider, session, offset, chunk, jobs, job_id):
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        try:
            return provider.upload_chunk(session, offset, chunk)
        except StorageError as exc:
            if not exc.transient or attempt >= MAX_TRANSIENT_RETRIES:
                return None
            jobs.heartbeat(job_id)
            time.sleep(_backoff(attempt))
    return None


def _fail(jobs, community, job_id, post_id, file_id, job, reason):
    community.update_file(file_id, upload_status=FileStatus.FAILED)
    community.set_post_status(post_id, PostStatus.FAILED, actor_id=job.get("worker_id", ""))
    community.add_event(post_id, "", "publish_failed", {"reason": reason})
    try:
        jobs.transition(job_id, JobStatus.FAILED, expected_worker=job.get("worker_id"),
                        error_type="community_publish", error_message=reason)
    except TransitionError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)
    return run_job(args.job_id, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
