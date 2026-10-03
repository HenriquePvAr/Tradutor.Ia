"""Publish attempt isolation proven at the real browser -> HTTP entry boundary.

The store already refuses to revive a terminal failed pre-Drive attempt, but the
production entry path is ``POST /api/community/publish`` with the exact payload the
browser sends -- which never carries a retry/versioning flag.  These tests pin the
whole chain (HTTP -> CommunityApi.publish -> CommunityService -> CommunityStore) so a
later change cannot move attempt-lifecycle authority to the client or re-open
in-place reuse of a failed attempt.
"""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

from pathlib import Path
import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from community_api import CommunityApi
from community_auth import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    LocalSessionAuthProvider,
    SESSION_COOKIE_NAME,
)
from community_http import CommunityNetworkBoundaryMiddleware, create_community_router
from community_store import FileStatus, PostStatus
from job_store import JobStore
from offline_test_guard import (
    _loopback_test_connect,
    _loopback_test_connect_ex,
    _loopback_test_create_connection,
)
from test_community_storage_core import _canonical_identity, _finished_translation_job


OWNER_ID = "owner-a"


class PublishHttpHarness:
    """CommunityApi behind the real router, with canonical identity required."""

    def __init__(self, tmp_path: Path):
        self.output_root = tmp_path / "output"
        self.jobs = JobStore(tmp_path / "jobs.sqlite3")
        self.api = CommunityApi(
            self.jobs,
            community_db_path=tmp_path / "community.sqlite3",
            output_root=self.output_root,
            publication_metadata_config={"provider": "fake"},
        )
        self.auth = LocalSessionAuthProvider()
        self.session = self.auth.issue_session(user_id=OWNER_ID)
        app = FastAPI()
        app.add_middleware(CommunityNetworkBoundaryMiddleware, auth=self.auth)
        app.include_router(create_community_router(self.api, self.auth))
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def close(self) -> None:
        self.client.close()
        self.api.close()
        self.jobs.close()

    def source_job(self, *, name: str = "chapter", canonical: bool = True,
                   data: bytes = b"%PDF-1.4\nattempt-isolation\n%%EOF\n") -> str:
        job_id, _ = _finished_translation_job(
            self.jobs,
            self.output_root,
            owner=OWNER_ID,
            name=name,
            data=data,
            canonical_identity=_canonical_identity() if canonical else None,
        )
        return job_id

    def browser_publish(self, source_job_id: str, **extra):
        """Exactly the payload static/tradutor_ui.js sends on the Publish click."""
        payload = {
            "slug": "",
            "series_title": "Serie",
            "series_slug": "serie",
            "episode_number": "1",
            "title": "Capitulo",
            "description": "",
            "tags": [],
            "visibility": "public",
            "allow_comments": True,
            "publish_consent": True,
            "source_job_id": source_job_id,
        }
        payload.update(extra)
        cookie = (
            f"{SESSION_COOKIE_NAME}={self.session.session_token}; "
            f"{CSRF_COOKIE_NAME}={self.session.csrf_token}"
        )
        return self.client.post(
            "/api/community/publish",
            json=payload,
            headers={"Cookie": cookie, CSRF_HEADER_NAME: self.session.csrf_token},
        )

    def publish_jobs(self) -> list[dict]:
        return [
            job for job in self.jobs.list_jobs(limit=None)
            if (job.get("configuration") or {}).get("job_type") == "community_publish"
        ]

    def fail_pre_drive(self, post_id: str, file_id: str) -> None:
        self.api.store.update_file(
            file_id, upload_status=FileStatus.FAILED, bytes_uploaded=0)
        self.api.store.set_post_status(post_id, PostStatus.FAILED)


@pytest.fixture
def harness(tmp_path):
    value = PublishHttpHarness(tmp_path)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture(autouse=True)
def allow_only_asgi_loopback(monkeypatch):
    """TestClient needs a loopback self-pipe; all non-loopback stays denied."""
    monkeypatch.setattr(socket.socket, "connect", _loopback_test_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _loopback_test_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _loopback_test_create_connection)


def _snapshot(row: dict | None) -> dict:
    assert row is not None
    return dict(row)


def test_browser_payload_after_failed_pre_drive_creates_an_isolated_attempt(harness):
    source_job_id = harness.source_job()
    first = harness.browser_publish(source_job_id)
    assert first.status_code == 200, first.text
    attempt_a = first.json()
    harness.fail_pre_drive(attempt_a["post_id"], attempt_a["file_id"])
    post_a = _snapshot(harness.api.store.get_post(attempt_a["post_id"]))
    file_a = _snapshot(harness.api.store.get_file(attempt_a["file_id"]))

    second = harness.browser_publish(source_job_id)

    assert second.status_code == 200, second.text
    attempt_b = second.json()
    assert attempt_b["post_id"] != attempt_a["post_id"]
    assert attempt_b["file_id"] != attempt_a["file_id"]
    assert attempt_b["job_id"] != attempt_a["job_id"]
    # Attempt ownership dominates artifact equality: the identical SHA must not make
    # the new attempt adopt the frozen file of the old one.
    file_b = harness.api.store.get_file(attempt_b["file_id"])
    assert file_b["post_id"] == attempt_b["post_id"]
    assert file_b["sha256"] == file_a["sha256"]
    # The terminal failed attempt is immutable history.
    assert _snapshot(harness.api.store.get_post(attempt_a["post_id"])) == post_a
    assert _snapshot(harness.api.store.get_file(attempt_a["file_id"])) == file_a
    assert file_a["upload_job_id"] == attempt_a["job_id"]


def test_new_attempt_carries_the_server_derived_canonical_chapter(harness):
    source_job_id = harness.source_job()
    attempt_a = harness.browser_publish(source_job_id).json()
    materializer = harness.api._canonical_identity_materializer
    chapter_id = next(iter(materializer.chapter_mappings.values()))["chapter_id"]
    assert harness.jobs.get_job(attempt_a["job_id"])[
        "configuration"]["canonical_publication_id"] == chapter_id
    harness.fail_pre_drive(attempt_a["post_id"], attempt_a["file_id"])

    attempt_b = harness.browser_publish(source_job_id).json()

    assert harness.jobs.get_job(attempt_b["job_id"])[
        "configuration"]["canonical_publication_id"] == chapter_id
    assert len(materializer.chapter_mappings) == 1


def test_duplicate_browser_click_while_active_starts_no_parallel_attempt(harness):
    source_job_id = harness.source_job()
    attempt_a = harness.browser_publish(source_job_id).json()

    second = harness.browser_publish(source_job_id)

    assert second.status_code == 200, second.text
    assert second.json() == attempt_a
    assert len(harness.api.store.list_user_posts(OWNER_ID)) == 1
    assert len(harness.publish_jobs()) == 1


def test_browser_click_after_success_stays_idempotent(harness):
    source_job_id = harness.source_job()
    attempt_a = harness.browser_publish(source_job_id).json()
    harness.api.store.update_file(
        attempt_a["file_id"],
        upload_status=FileStatus.VERIFIED,
        storage_file_id="storage-verified",
    )
    harness.api.store.set_post_status(attempt_a["post_id"], PostStatus.PUBLISHED)

    second = harness.browser_publish(source_job_id)

    assert second.status_code == 200, second.text
    assert second.json()["post_id"] == attempt_a["post_id"]
    assert second.json()["file_id"] == attempt_a["file_id"]
    assert len(harness.publish_jobs()) == 1


def test_browser_click_after_post_drive_failure_recovers_in_place(harness):
    source_job_id = harness.source_job()
    attempt_a = harness.browser_publish(source_job_id).json()
    harness.api.store.update_file(
        attempt_a["file_id"],
        upload_status=FileStatus.FAILED,
        storage_file_id="storage-created-remotely",
    )
    harness.api.store.set_post_status(attempt_a["post_id"], PostStatus.FAILED)

    second = harness.browser_publish(source_job_id)

    assert second.status_code == 200, second.text
    # Post-Drive ambiguity is a recovery, never a fresh isolated upload attempt.
    assert second.json()["post_id"] == attempt_a["post_id"]
    assert second.json()["file_id"] == attempt_a["file_id"]
    assert len(harness.api.store.list_user_posts(OWNER_ID)) == 1


@pytest.mark.parametrize(
    "field", ["canonical_publication_id", "chapter_id", "remote_chapter_id"])
def test_client_cannot_inject_publication_identity(harness, field):
    source_job_id = harness.source_job()

    response = harness.browser_publish(source_job_id, **{field: "attacker-chapter"})

    assert response.status_code == 400
    assert response.json()["detail"] == "client_identity_not_allowed"
    assert harness.api.store.list_user_posts(OWNER_ID) == []
    assert harness.publish_jobs() == []


def test_client_cannot_force_a_fresh_attempt_after_a_post_drive_failure(harness):
    source_job_id = harness.source_job()
    attempt_a = harness.browser_publish(source_job_id).json()
    harness.api.store.update_file(
        attempt_a["file_id"],
        upload_status=FileStatus.FAILED,
        storage_file_id="storage-created-remotely",
    )
    harness.api.store.set_post_status(attempt_a["post_id"], PostStatus.FAILED)

    response = harness.browser_publish(source_job_id, force_new_version=True)

    assert response.status_code == 400
    assert response.json()["detail"] == "client_retry_control_not_allowed"
    assert len(harness.api.store.list_user_posts(OWNER_ID)) == 1
    assert len(harness.publish_jobs()) == 1


def test_unresolvable_canonical_identity_creates_no_attempt_rows(harness):
    source_job_id = harness.source_job(canonical=False)

    response = harness.browser_publish(source_job_id)

    assert response.status_code >= 400
    assert harness.api.store.list_user_posts(OWNER_ID) == []
    assert harness.publish_jobs() == []


def test_ui_publish_payload_carries_no_retry_or_versioning_flag():
    """Retry safety is server state, not a browser-controlled flag."""
    source = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    assert "/api/community/publish" in source
    assert "force_new_version" not in source
