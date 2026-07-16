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
import hashlib
import hmac
import os
import random
import signal
import sys
import tempfile
import time
from pathlib import Path

from community_storage import StorageError, build_storage_provider
from community_store import CommunityStore, FileStatus, PostStatus
from job_store import JobStatus, JobStore, TransitionError
from local_environment import load_local_environment_for_entrypoint
from runner_start_gate import wait_for_start_gate

CHUNK_SIZE = 256 * 1024
HEARTBEAT_SECONDS = 3.0
MAX_TRANSIENT_RETRIES = 6
# Test-only pacing hook (seconds per chunk); 0 in production. Lets a survival test span a
# realistic upload window without a giant file.
_CHUNK_DELAY = 0.0

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
    global _STOP
    _STOP = False
    _install_stop_handlers()
    jobs = JobStore(db_path)
    community = None
    job = None
    post_id = ""
    file_id = ""
    snapshot = None
    try:
        job = jobs.get_job(job_id)
        if job is None or job["status"] not in {JobStatus.CLAIMING, JobStatus.STARTING}:
            return 2
        config = job.get("configuration") or {}
        if config.get("job_type") != "community_publish":
            return 2

        community = CommunityStore(config["community_db"])
        file_id = config["file_id"]
        post_id = config["post_id"]
        pdf_path = Path(config["local_pdf_path"])
        total = int(config["pdf_size"])
        expected_sha256 = str(config.get("pdf_sha256") or "").strip().lower()
        if len(expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_sha256
        ):
            _fail(
                jobs, community, job_id, post_id, file_id, job,
                "invalid_expected_sha256",
            )
            return 0
        if (
            jobs.cancel_requested(job_id)
            or not community.publish_attempt_is_current(post_id, file_id, job_id)
        ):
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        job = jobs.transition(job_id, JobStatus.STARTING, expected_worker=job.get("worker_id")) \
            if job["status"] == JobStatus.CLAIMING else job
        job = jobs.transition(job_id, JobStatus.RUNNING, expected_worker=job.get("worker_id"),
                              stage="snapshotting")
        if (
            jobs.cancel_requested(job_id)
            or not community.publish_attempt_is_current(post_id, file_id, job_id)
        ):
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0

        # Copy and authenticate the selected local artifact before constructing a
        # provider or sending a byte.  Uploading directly from the mutable output path
        # would discover a same-size substitution only after the foreign bytes had
        # already reached storage.  The anonymous temporary snapshot is streamed to
        # disk, not memory, and remains the immutable source for this attempt.
        snapshot = tempfile.TemporaryFile(mode="w+b")
        snapshot_sha256 = hashlib.sha256()
        snapshot_size = 0
        cancelled = False
        interrupted = False
        with pdf_path.open("rb") as source:
            while True:
                if (
                    jobs.cancel_requested(job_id)
                    or not community.publish_attempt_is_current(post_id, file_id, job_id)
                ):
                    cancelled = True
                    break
                if _STOP:
                    interrupted = True
                    break
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                snapshot.write(chunk)
                snapshot_sha256.update(chunk)
                snapshot_size += len(chunk)
        if cancelled:
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        if interrupted:
            jobs.transition(
                job_id,
                JobStatus.INTERRUPTED,
                expected_worker=job.get("worker_id"),
                interrupted_reason="worker_stop",
                recoverable=1,
            )
            return 0
        if snapshot_size != total or not hmac.compare_digest(
            snapshot_sha256.hexdigest(), expected_sha256
        ):
            _fail(jobs, community, job_id, post_id, file_id, job, "local_pdf_changed")
            return 0
        snapshot.seek(0)

        if (
            jobs.cancel_requested(job_id)
            or not community.publish_attempt_is_current(post_id, file_id, job_id)
        ):
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        provider = build_storage_provider(config.get("storage") or {})
        if (
            jobs.cancel_requested(job_id)
            or not community.publish_attempt_is_current(post_id, file_id, job_id)
        ):
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        previous_file = community.get_file(file_id) or {}
        previous_remote_id = str(previous_file.get("storage_file_id") or "")
        previous_session_ref = str(previous_file.get("session_ref") or "")
        if previous_remote_id:
            try:
                provider.delete_file(previous_remote_id)
            except StorageError as exc:
                if exc.status != 404:
                    raise
        if previous_session_ref:
            provider.abandon_resumable_session(previous_session_ref)
        if previous_remote_id or previous_session_ref:
            if not community.update_current_publish_file(
                post_id,
                file_id,
                job_id,
                storage_file_id=None,
                session_ref=None,
                bytes_uploaded=0,
            ):
                _cancel_job(jobs, job_id)
                return 0
        jobs.update_progress(job_id, stage="uploading", current=0, total=total)
        if not community.update_current_publish_file(
            post_id,
            file_id,
            job_id,
            upload_status=FileStatus.UPLOADING,
        ):
            _cancel_job(jobs, job_id)
            return 0

        # Resume from any previously uploaded bytes, else start a fresh session.
        if (
            jobs.cancel_requested(job_id)
            or not community.publish_attempt_is_current(post_id, file_id, job_id)
        ):
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        folder = provider.ensure_folder(config.get("series_slug") or "series",
                                        (config.get("storage") or {}).get("root_folder_id", "root"))
        if (
            jobs.cancel_requested(job_id)
            or not community.publish_attempt_is_current(post_id, file_id, job_id)
        ):
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        session = provider.create_resumable_session(
            filename=config["pdf_filename"], mime_type="application/pdf", size=total,
            parent_id=folder, sha256=config.get("pdf_sha256", ""))
        if session.file_id:
            community.record_publish_remote_reference(file_id, job_id, session.file_id)
        if (
            jobs.cancel_requested(job_id)
            or not community.update_current_publish_file(
                post_id,
                file_id,
                job_id,
                storage_folder_id=folder,
                session_ref=session.session_id,
                storage_file_id=session.file_id or None,
                bytes_uploaded=0,
            )
        ):
            _discard_remote_file(provider, session.file_id, session.session_id)
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0

        uploaded_sha256 = hashlib.sha256()
        remote_file_id = session.file_id  # some providers know it upfront; Drive reports it on completion
        offset = 0
        while offset < total:
            if (
                jobs.cancel_requested(job_id)
                or not community.publish_attempt_is_current(post_id, file_id, job_id)
            ):
                cancelled = True
                break
            if _STOP:
                interrupted = True
                break
            chunk = snapshot.read(CHUNK_SIZE)
            if not chunk:
                break
            result = _upload_with_retry(provider, session, offset, chunk, jobs, job_id)
            if result is None:
                _fail(jobs, community, job_id, post_id, file_id, job, "upload_failed")
                return 0
            expected_uploaded = offset + len(chunk)
            if result.uploaded != expected_uploaded:
                _fail(
                    jobs, community, job_id, post_id, file_id, job,
                    "partial_chunk_upload",
                )
                return 0
            uploaded_sha256.update(chunk)
            offset = result.uploaded
            if getattr(result, "file_id", ""):
                remote_file_id = result.file_id
                community.record_publish_remote_reference(file_id, job_id, remote_file_id)
            file_fields = {"bytes_uploaded": offset}
            if remote_file_id:
                file_fields["storage_file_id"] = remote_file_id
            if not community.update_current_publish_file(
                post_id,
                file_id,
                job_id,
                **file_fields,
            ):
                _discard_remote_file(provider, remote_file_id, session.session_id)
                _cancel_job(jobs, job_id)
                return 0
            jobs.update_progress(job_id, stage="uploading", current=offset, total=total,
                                 message=f"{offset}/{total} bytes")
            if _CHUNK_DELAY:
                time.sleep(_CHUNK_DELAY)

        if cancelled:
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        if interrupted:
            # Preserve progress for resume; the job goes back to a recoverable state.
            jobs.transition(job_id, JobStatus.INTERRUPTED, expected_worker=job.get("worker_id"),
                            interrupted_reason="worker_stop", recoverable=1)
            return 0

        if (
            jobs.cancel_requested(job_id)
            or not community.publish_attempt_is_current(post_id, file_id, job_id)
        ):
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        if offset != total or not hmac.compare_digest(
            uploaded_sha256.hexdigest(), expected_sha256
        ):
            _fail(jobs, community, job_id, post_id, file_id, job, "local_pdf_changed")
            return 0

        # Verify the remote file before publishing.
        jobs.update_progress(job_id, stage="verifying", current=total, total=total)
        if not community.update_current_publish_file(
            post_id,
            file_id,
            job_id,
            upload_status=FileStatus.VERIFYING,
            storage_file_id=remote_file_id,
        ):
            _discard_remote_file(provider, remote_file_id, session.session_id)
            _cancel_job(jobs, job_id)
            return 0
        try:
            meta = provider.stat_file(remote_file_id)
        except StorageError as exc:
            _fail(jobs, community, job_id, post_id, file_id, job, f"verify_error:{exc.status}")
            return 0
        if meta.trashed or meta.size != total or meta.mime_type != "application/pdf":
            _fail(jobs, community, job_id, post_id, file_id, job, "verify_mismatch")
            return 0

        published = community.complete_publish_attempt(
            post_id=post_id,
            file_id=file_id,
            upload_job_id=job_id,
            provider_checksum=meta.checksum,
            actor_id=config.get("user_id", ""),
            size=total,
        )
        if not published:
            community.invalidate_publish_file(file_id, job_id)
            _cancel_job(jobs, job_id)
            return 0
        jobs.transition(job_id, JobStatus.FINISHED, expected_worker=job.get("worker_id"),
                        stage="finished", pdf_path="")
        return 0
    except Exception as exc:  # noqa: BLE001 - the runner boundary must terminalize jobs
        _fail_unexpected(
            jobs,
            community,
            job_id,
            post_id,
            file_id,
            job,
            exc,
        )
        return 0
    finally:
        if snapshot is not None:
            snapshot.close()
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


def _discard_remote_file(provider, file_id: str, session_id: str = "") -> None:
    """Best-effort cleanup of a file created by an attempt revoked mid-call."""
    try:
        if file_id:
            provider.delete_file(file_id)
        if session_id:
            provider.abandon_resumable_session(session_id)
    except Exception:  # noqa: BLE001 - the persisted remote id remains auditable
        pass


def _fail(jobs, community, job_id, post_id, file_id, job, reason):
    actor_id = str(
        ((job.get("configuration") or {}).get("user_id") if job else "") or ""
    )
    applied = community.fail_publish_attempt(
        post_id=post_id,
        file_id=file_id,
        upload_job_id=job_id,
        actor_id=actor_id,
        reason=reason,
    )
    if not applied:
        _cancel_job(jobs, job_id)
        return
    try:
        jobs.transition(job_id, JobStatus.FAILED, expected_worker=job.get("worker_id"),
                        error_type="community_publish", error_message=reason)
    except TransitionError:
        pass


def _fail_unexpected(
    jobs: JobStore,
    community: CommunityStore | None,
    job_id: str,
    post_id: str,
    file_id: str,
    job: dict | None,
    exc: Exception,
) -> None:
    """Close an unexpected runner failure without leaking provider error text.

    Provider construction, folder/session creation and local file I/O all happen
    outside the narrow retry loop.  They still need the same conditional rollback as
    an upload failure so no PUBLISHING/PENDING attempt can be stranded forever.
    """
    if isinstance(exc, StorageError):
        status = exc.status if isinstance(exc.status, int) else "unknown"
        reason = f"storage_error:{status}"
    elif isinstance(exc, OSError):
        reason = "local_pdf_io_error"
    elif isinstance(exc, (KeyError, TypeError, ValueError)):
        reason = "invalid_publish_configuration"
    else:
        reason = "unexpected_publish_error"

    if community is not None and post_id and file_id:
        try:
            state = community.publish_attempt_state(post_id, file_id, job_id)
            if state == "completed":
                try:
                    jobs.reconcile_community_publish_terminal(job_id, JobStatus.FINISHED)
                except Exception:
                    # The community database is already authoritative and terminal.
                    # If the jobs write failed, leave this row in flight so the worker
                    # can mark it interrupted and reconcile it on the next loop.
                    pass
                return
            if state == "failed":
                try:
                    jobs.reconcile_community_publish_terminal(job_id, JobStatus.FAILED)
                except Exception:
                    pass
                return
        except Exception:  # noqa: BLE001 - continue with the conditional failure path
            pass

    applied: bool | None = None
    community_failure = False
    if community is not None and post_id and file_id:
        try:
            applied = community.fail_publish_attempt(
                post_id=post_id,
                file_id=file_id,
                upload_job_id=job_id,
                actor_id=str(
                    (((job or {}).get("configuration") or {}).get("user_id") or "")
                ),
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - still terminalize the job database below
            applied = None
            community_failure = True
    if community_failure:
        # We could not commit FAILED in the authoritative community database.  Marking
        # only the jobs database terminal would strand PUBLISHING/PENDING forever, so
        # preserve a recoverable state for the worker's cross-database reconciliation.
        current = jobs.get_job(job_id)
        if current and current["status"] in JobStatus.IN_FLIGHT:
            try:
                jobs.transition(
                    job_id,
                    JobStatus.INTERRUPTED,
                    expected_worker=current.get("worker_id"),
                    interrupted_reason="community_state_unavailable",
                    recoverable=1,
                    error_type="community_publish",
                    error_message="community_state_unavailable",
                )
            except TransitionError:
                pass
        return
    if applied is False:
        # A concurrent unpublish or newer attempt won.  Preserve that state and make
        # this obsolete runner terminal without turning it into a failure.
        _cancel_job(jobs, job_id)
        return

    current = jobs.get_job(job_id)
    if not current or current["status"] in JobStatus.TERMINAL:
        return
    try:
        jobs.transition(
            job_id,
            JobStatus.FAILED,
            expected_worker=current.get("worker_id"),
            error_type="community_publish",
            error_message=reason,
        )
    except TransitionError:
        # A concurrent cancellation/finalization is authoritative.
        pass


def _cancel_job(jobs: JobStore, job_id: str) -> None:
    current = jobs.get_job(job_id)
    if not current or current["status"] in JobStatus.TERMINAL:
        return
    worker = current.get("worker_id")
    try:
        if current["status"] == JobStatus.CLAIMING:
            current = jobs.transition(job_id, JobStatus.STARTING, expected_worker=worker)
        if current["status"] in {JobStatus.STARTING, JobStatus.RUNNING}:
            current = jobs.transition(job_id, JobStatus.CANCELLING, expected_worker=worker)
        if current["status"] == JobStatus.CANCELLING:
            jobs.transition(job_id, JobStatus.CANCELLED, expected_worker=worker)
    except TransitionError:
        pass


def main(argv: list[str] | None = None) -> int:
    if not load_local_environment_for_entrypoint():
        return 2
    global _CHUNK_DELAY
    _CHUNK_DELAY = float(os.getenv("COMMUNITY_UPLOAD_CHUNK_DELAY", "0") or 0)
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--log", default="")
    parser.add_argument("--start-gate", default="")
    args = parser.parse_args(argv)
    if not wait_for_start_gate(args.start_gate):
        return 3
    return run_job(args.job_id, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
