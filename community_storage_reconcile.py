"""Administrative reconciliation between the community DB, the job store and the provider.

Detects inconsistencies without listing the whole Drive: it walks the database's own
records and checks each against the job store and, only where a remote file id exists,
the provider. ``scan`` and ``report`` never change anything; ``repair-safe`` applies only
non-destructive fixes (closing out a publish that has no job, flagging a failed upload).
Destructive actions (deleting a remote file) are never automatic and require an explicit
separate admin action.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from community_storage import StorageError
from community_store import CommunityStore, FileStatus, PostStatus
from job_store import JobStatus, JobStore


def reconcile(community: CommunityStore, jobs: JobStore, provider=None, *,
              mode: str = "scan") -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    def flag(kind: str, post_id: str = "", file_id: str = "", **extra):
        findings.append({"kind": kind, "post_id": post_id, "file_id": file_id, **extra})

    posts = community._rows(community._conn.execute("SELECT * FROM community_posts"))  # noqa: SLF001
    files = community._rows(community._conn.execute("SELECT * FROM community_files"))  # noqa: SLF001
    files_by_post: dict[str, list[dict]] = {}
    for f in files:
        files_by_post.setdefault(f["post_id"], []).append(f)

    repaired = 0
    for post in posts:
        post_files = files_by_post.get(post["id"], [])
        verified = [f for f in post_files if f["upload_status"] == FileStatus.VERIFIED]
        if post["status"] == PostStatus.PUBLISHED and not verified:
            flag("published_without_verified_file", post["id"])
        if post["status"] == PostStatus.PUBLISHING:
            job_id = next((f.get("upload_job_id") for f in post_files if f.get("upload_job_id")), "")
            job = jobs.get_job(job_id) if job_id else None
            active = job and job["status"] not in JobStatus.TERMINAL
            if not active:
                flag("publishing_without_active_job", post["id"], job_id=job_id)
                if mode == "repair-safe":
                    community.set_post_status(post["id"], PostStatus.FAILED, actor_id="reconcile")
                    repaired += 1

    for f in files:
        if f["upload_status"] == FileStatus.UPLOADING and f.get("upload_job_id"):
            job = jobs.get_job(f["upload_job_id"])
            if job and job["status"] in {JobStatus.FAILED, JobStatus.CANCELLED}:
                flag("uploading_but_job_terminal", f["post_id"], f["id"], job_status=job["status"])
                if mode == "repair-safe":
                    community.update_file(f["id"], upload_status=FileStatus.FAILED)
                    repaired += 1
        if f["upload_status"] == FileStatus.VERIFIED and f.get("storage_file_id") and provider is not None:
            try:
                meta = provider.stat_file(f["storage_file_id"])
                if meta.trashed:
                    flag("verified_file_trashed", f["post_id"], f["id"])
                elif int(meta.size) != int(f["size_bytes"]):
                    flag("size_mismatch", f["post_id"], f["id"],
                         local=f["size_bytes"], remote=meta.size)
            except StorageError as exc:
                if exc.status == 404:
                    flag("verified_file_missing_remote", f["post_id"], f["id"])
                else:
                    flag("provider_unavailable", f["post_id"], f["id"], status=exc.status)

    # Duplicate verified sha256 across published posts.
    seen: dict[str, str] = {}
    for f in files:
        if f["upload_status"] != FileStatus.VERIFIED:
            continue
        sha = f["sha256"]
        if sha in seen and seen[sha] != f["post_id"]:
            flag("duplicate_sha_published", f["post_id"], f["id"], other_post=seen[sha])
        seen.setdefault(sha, f["post_id"])

    return {"mode": mode, "findings": findings, "repaired": repaired,
            "counts": _counts(findings)}


def _counts(findings: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--community-db", required=True)
    parser.add_argument("--jobs-db", required=True)
    parser.add_argument("--mode", choices=["scan", "report", "repair-safe"], default="scan")
    args = parser.parse_args(argv)
    community = CommunityStore(args.community_db)
    jobs = JobStore(args.jobs_db)
    try:
        result = reconcile(community, jobs, None, mode=args.mode)
    finally:
        community.close()
        jobs.close()
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
