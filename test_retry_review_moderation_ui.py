from __future__ import annotations

import _test_bootstrap  # noqa: F401

from pathlib import Path
from unittest.mock import patch

import pytest

import ui_bridge
from community_api import CommunityApi
from community_auth import AuthorizationDenied, RequestPrincipal
from community_store import Moderation, PostStatus, Visibility
from job_store import JobStatus
from job_store import JobStore, TransitionError


def _bridge(tmp_path: Path) -> ui_bridge.UiBridge:
    patches = (
        patch.object(ui_bridge, "JOBS_DB_PATH", tmp_path / "jobs.sqlite3"),
        patch.object(ui_bridge, "env_status", lambda *a, **k: {
            "env_exists": True, "nvidia_configured": True,
        }),
        patch.object(ui_bridge, "_current_commit", lambda: "offline"),
        patch.object(ui_bridge, "_current_branch", lambda: "test"),
    )
    for item in patches:
        item.start()
    bridge = ui_bridge.UiBridge()
    bridge._test_patches = patches
    return bridge


def _failed_owned_job(bridge: ui_bridge.UiBridge, tmp_path: Path) -> str:
    job_id = bridge.store.create_job(
        source_url="https://example.invalid/synthetic",
        output_dir=str(tmp_path / "failed"),
        command=["python", "fake_pipeline.py", "--output-dir", str(tmp_path / "failed")],
        configuration={
            "job_type": "translation",
            "community_owner_id": "owner-a",
            "chapter_name": "Synthetic",
            "fixture": True,
            "synthetic": True,
            "local_test_only": True,
        },
        initial_status=JobStatus.QUEUED,
    )
    claimed = bridge.store.claim_next_job("fixture-worker", 1)
    assert claimed and claimed["id"] == job_id
    bridge.store.transition(job_id, JobStatus.STARTING, expected_worker="fixture-worker")
    bridge.store.transition(job_id, JobStatus.RUNNING, expected_worker="fixture-worker")
    bridge.store.transition(
        job_id, JobStatus.FAILED, expected_worker="fixture-worker",
        error_message="synthetic_transient_failure",
    )
    return job_id


def test_retry_is_idempotent_and_persists_lineage(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_LOCAL_TEST_IDENTITIES", "1")
    bridge = _bridge(tmp_path)
    try:
        original_id = _failed_owned_job(bridge, tmp_path)
        first = bridge.retry_job_for_owner("owner-a", original_id)
        second = bridge.retry_job_for_owner("owner-a", original_id)
        assert first["id"] == second["id"]
        retry = bridge.store.get_job(first["id"])
        assert retry["previous_job_id"] == original_id
        assert retry["attempt"] == 2
        assert retry["configuration"]["retry_of"] == original_id
        assert retry["configuration"]["retry_reason"] == "user_requested"
        assert retry["configuration"]["local_test_only"] is True
        assert retry["command"][retry["command"].index("--output-dir") + 1] == retry["output_dir"]
        assert len([
            row for row in bridge.store.list_jobs(limit=None)
            if row.get("previous_job_id") == original_id
        ]) == 1
    finally:
        bridge.store.close()
        for item in bridge._test_patches:
            item.stop()


def test_retry_remains_owner_scoped(tmp_path):
    bridge = _bridge(tmp_path)
    try:
        original_id = _failed_owned_job(bridge, tmp_path)
        with pytest.raises(ValueError, match="job_not_retryable"):
            bridge.retry_job_for_owner("owner-b", original_id)
    finally:
        bridge.store.close()
        for item in bridge._test_patches:
            item.stop()


def _community(tmp_path: Path) -> tuple[CommunityApi, str]:
    from job_store import JobStore

    jobs = JobStore(tmp_path / "jobs.sqlite3")
    api = CommunityApi(
        jobs,
        community_db_path=tmp_path / "community.sqlite3",
        output_root=tmp_path / "output",
        storage_root=tmp_path / "storage",
    )
    post_id = api.store.create_post(
        user_id="owner-a",
        title="Synthetic post",
        series_title="Synthetic series",
        visibility=Visibility.PUBLIC,
    )
    api.store.set_post_status(
        post_id, PostStatus.PUBLISHED,
        moderation_status=Moderation.APPROVED,
    )
    api._test_jobs = jobs
    return api, post_id


def _principal(user: str, *roles: str) -> RequestPrincipal:
    return RequestPrincipal(
        user_id=user,
        authenticated=True,
        roles=frozenset(roles or ("user",)),
        auth_source="local_test",
        session_id=f"session-{user}",
    )


def test_moderator_can_hide_restore_and_remove_with_append_only_events(tmp_path):
    api, post_id = _community(tmp_path)
    moderator = _principal("mod-a", "moderator")
    try:
        hidden = api.moderate_post(
            post_id, "hide", {"reason": "synthetic policy review"},
            principal=moderator,
        )
        assert hidden["moderation_status"] == "hidden"
        assert api.feed(principal=_principal("owner-b"))["posts"] == []

        restored = api.moderate_post(
            post_id, "restore", {"reason": "synthetic review complete"},
            principal=moderator,
        )
        assert restored["moderation_status"] == Moderation.APPROVED

        removed = api.moderate_post(
            post_id, "remove", {"reason": "synthetic removal"},
            principal=moderator,
        )
        assert removed["status"] == PostStatus.BLOCKED
        events = api.store.events_for_post(post_id)
        assert [row["event_type"] for row in events[-3:]] == [
            "moderation_hide", "moderation_restore", "moderation_remove",
        ]
        assert all("session" not in str(row).lower() for row in events[-3:])
    finally:
        api.close()
        api._test_jobs.close()


def test_common_user_cannot_moderate_and_reason_is_required(tmp_path):
    api, post_id = _community(tmp_path)
    try:
        with pytest.raises(AuthorizationDenied, match="moderator_required"):
            api.moderate_post(
                post_id, "hide", {"reason": "not allowed"},
                principal=_principal("owner-a"),
            )
        with pytest.raises(ValueError, match="moderation_reason_required"):
            api.moderate_post(
                post_id, "hide", {},
                principal=_principal("mod-a", "moderator"),
            )
    finally:
        api.close()
        api._test_jobs.close()


def test_moderation_surface_and_retry_confirmation_are_wired():
    html = Path("ui/ui_shell.html").read_text(encoding="utf-8")
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    assert "retryConfirmDialog" in html
    assert "moderationPanel" in html
    assert "/api/community/moderation/posts/" in js
    assert "moderation_hide" in js
    assert "moderation_restore" in js
    assert "moderation_remove" in js
    assert "window.prompt" not in js
    assert "moderationReasonDialog" in html


def test_review_revisions_are_append_only_and_optimistically_locked(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        job_id = store.create_job(
            source_url="", output_dir=str(tmp_path / "review"),
            command=["offline"], configuration={"job_type": "translation"},
        )
        edited = store.record_review_item_revision(
            job_id, "p1:i1", expected_version=0, action="edited",
            translation="Tradução revisada", reason_code="human_translation_edited",
            reason="", actor_id="owner-a",
        )
        assert edited["version"] == 1
        with pytest.raises(TransitionError, match="review_version_conflict"):
            store.record_review_item_revision(
                job_id, "p1:i1", expected_version=0, action="reviewed",
                translation="Versão obsoleta", reason_code="human_translation_approved",
                reason="", actor_id="owner-a",
            )
        approved = store.record_review_item_revision(
            job_id, "p1:i1", expected_version=1, action="reviewed",
            translation="Tradução revisada", reason_code="human_translation_approved",
            reason="revisada", actor_id="owner-a",
        )
        assert approved["version"] == 2
        assert [row["action"] for row in store.review_item_revisions(job_id, "p1:i1")] == [
            "edited", "reviewed",
        ]
    finally:
        store.close()


def test_deep_review_ui_uses_versioned_endpoint():
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    assert "/api/ui/quality-review/edit" in js
    assert "expected_version" in js
    assert "data-review-deep-action=\"rejected\"" in js
    assert "data-review-deep-action=\"preserved_original\"" in js
    assert "data-review-deep-action=\"manual_review\"" in js
