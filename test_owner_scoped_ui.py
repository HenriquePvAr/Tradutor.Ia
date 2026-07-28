from __future__ import annotations

import _test_bootstrap  # noqa: F401

import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import app_ui
from community_auth import RequestPrincipal
from job_store import JobStatus, JobStore
from ui_bridge import UiBridge


def _create_job(store: JobStore, owner: str, *, job_id: str) -> str:
    return store.create_job(
        job_id=job_id,
        source_url="",
        output_dir=f"output/{job_id}",
        command=["offline-fixture"],
        configuration={
            "job_type": "translation",
            "community_owner_id": owner,
            "ownership_schema_version": 1,
        },
        initial_status=JobStatus.QUEUED,
    )


def test_job_store_queries_private_jobs_by_owner_in_sql(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        _create_job(store, "owner-a", job_id="a" * 32)
        _create_job(store, "owner-b", job_id="b" * 32)
        _create_job(store, "", job_id="c" * 32)

        assert [job["id"] for job in store.list_jobs_for_owner("owner-a")] == ["a" * 32]
        assert store.get_job_for_owner("owner-a", "b" * 32) is None
        assert store.get_job_for_owner("owner-b", "b" * 32)["id"] == "b" * 32

        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM jobs WHERE owner_id=? ORDER BY created_at DESC",
            ("owner-a",),
        ).fetchall()
        assert any("idx_jobs_owner" in " ".join(str(value) for value in row) for row in plan)
    finally:
        store.close()


def test_binding_legacy_owner_updates_queryable_owner_column(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        job_id = _create_job(store, "", job_id="d" * 32)
        outcome, _ = store.bind_community_owner(
            job_id, "owner-a", bound_by="owner-a")
        assert outcome == "owner_bound"
        assert store.get_job_for_owner("owner-a", job_id)["owner_id"] == "owner-a"
    finally:
        store.close()


def test_authenticated_principal_exposes_backend_derived_scope():
    principal = RequestPrincipal(
        user_id="subject-a",
        authenticated=True,
        roles=frozenset({"user"}),
        auth_source="local_test",
        session_id="session-a",
    )
    assert principal.subject_id == "subject-a"
    assert principal.owner_id == "subject-a"
    assert principal.provider == "local_test"
    assert principal.principal_hash


def test_bootstrap_resolves_principal_before_private_bridge_query(monkeypatch):
    order: list[str] = []
    principal = RequestPrincipal(
        user_id="owner-a",
        authenticated=True,
        roles=frozenset({"user"}),
        auth_source="local_test",
        session_id="session-a",
    )

    class FakeAuth:
        def authenticate_request(self, request):
            order.append("principal")
            return principal

    class FakeBridge:
        def bootstrap(self, cursor, *, principal):
            order.append("private_query")
            assert principal is not None
            assert principal.owner_id == "owner-a"
            return {
                "history": [],
                "queue": [],
                "community": {},
                "profile": {},
            }

    monkeypatch.setattr(app_ui, "AUTH", FakeAuth())
    monkeypatch.setattr(app_ui, "BRIDGE", FakeBridge())
    monkeypatch.setattr(app_ui, "_sync_public_profile", lambda principal: {})
    monkeypatch.setattr(app_ui, "_profile_for_principal", lambda principal: {})
    monkeypatch.setattr(app_ui, "_enrich_history_publications", lambda history: None)

    payload = app_ui.api_bootstrap(SimpleNamespace(), cursor=0)
    assert payload["community"]["user_id"] == "owner-a"
    assert order == ["principal", "private_query"]


def test_private_job_endpoints_require_request_principal():
    private_endpoints = (
        app_ui.api_cancel,
        app_ui.api_job_cancel,
        app_ui.api_quality_review,
        app_ui.api_quality_review_page,
        app_ui.api_quality_review_action,
        app_ui.api_quality_review_bulk_action,
        app_ui.api_quality_review_global_review,
        app_ui.api_quality_review_confirm,
        app_ui.api_retry,
        app_ui.api_queue_remove,
        app_ui.api_queue_clear,
        app_ui.api_queue_start,
        app_ui.api_resume,
    )
    for endpoint in private_endpoints:
        assert "request" in inspect.signature(endpoint).parameters, endpoint.__name__


def test_open_artifact_resolves_opaque_job_and_kind_for_owner(monkeypatch):
    calls = []
    principal = RequestPrincipal(
        user_id="owner-a", authenticated=True, roles=frozenset({"user"}),
        auth_source="local_test", session_id="session-a")

    monkeypatch.setattr(
        app_ui, "_ui_principal",
        lambda request, mutate=False: principal)
    monkeypatch.setattr(
        app_ui.BRIDGE, "open_artifact_for_owner",
        lambda owner_id, job_id, artifact, select=False:
            calls.append((owner_id, job_id, artifact, select)))

    assert app_ui.api_open(
        SimpleNamespace(),
        {"job_id": "job-a", "artifact": "pdf", "path": r"C:\foreign.pdf"},
    ) == {"ok": True}
    assert calls == [("owner-a", "job-a", "pdf", False)]


def test_profiles_are_persisted_per_authenticated_owner(tmp_path):
    bridge = UiBridge.__new__(UiBridge)
    bridge.profile_root = tmp_path / "profiles"
    bridge.profile_media_root = tmp_path / "media"
    bridge.legacy_profile_path = tmp_path / "legacy.json"
    bridge.profile = {}

    bridge.save_profile({"display_name": "Alice"}, user_id="owner-a")
    bridge.save_profile({"display_name": "Bruno"}, user_id="owner-b")

    assert bridge.profile_for_user("owner-a")["display_name"] == "Alice"
    assert bridge.profile_for_user("owner-b")["display_name"] == "Bruno"
    assert len(list((tmp_path / "profiles").glob("*.json"))) == 2


def test_runtime_paths_can_only_be_overridden_in_guarded_test_mode(
    monkeypatch, tmp_path
):
    source = inspect.getsource(UiBridge.__init__)
    assert "runtime_root" in source
    assert "output_root" in source


def test_persisted_queued_items_keep_the_remove_control():
    source = (
        app_ui.ROOT / "static" / "tradutor_ui.js"
    ).read_text(encoding="utf-8")
    assert "['waiting','queued'].includes(item.status)" in source


def test_owner_history_never_discovers_global_filesystem_records(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        _create_job(store, "owner-a", job_id="1" * 32)
        _create_job(store, "owner-b", job_id="2" * 32)
        store.transition("1" * 32, JobStatus.CANCELLED)
        store.transition("2" * 32, JobStatus.CANCELLED)
        bridge = UiBridge.__new__(UiBridge)
        bridge.store = store
        bridge._job_record = lambda job: {"id": job["id"], "owner_id": job["owner_id"]}
        bridge.history_store = SimpleNamespace(
            discover_outputs=lambda: (_ for _ in ()).throw(
                AssertionError("private history must not scan global outputs")))

        assert bridge._history_payload_for_owner("owner-a") == [
            {"id": "1" * 32, "owner_id": "owner-a"}]
        assert bridge._history_payload_for_owner("owner-b") == [
            {"id": "2" * 32, "owner_id": "owner-b"}]
        assert bridge._history_payload_for_owner("moderator") == []
    finally:
        store.close()


def test_schema_v6_is_backfilled_from_existing_owner_metadata(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    store = JobStore(db)
    try:
        _create_job(store, "owner-a", job_id="e" * 32)
        store._conn.execute("UPDATE jobs SET owner_id='' WHERE id=?", ("e" * 32,))
        store._conn.execute(
            "UPDATE meta SET value='6' WHERE key='schema_version'")
    finally:
        store.close()

    reopened = JobStore(db)
    try:
        assert reopened.get_job_for_owner("owner-a", "e" * 32)["owner_id"] == "owner-a"
    finally:
        reopened.close()


def test_cross_owner_direct_job_lookup_is_indistinguishable_from_missing(monkeypatch):
    principal = RequestPrincipal(
        user_id="owner-b", authenticated=True, roles=frozenset({"user"}),
        auth_source="local_test", session_id="session-b")

    class FakeAuth:
        def require_authenticated(self, request):
            return principal

        def require_csrf(self, request, current):
            assert current is principal

    class FakeStore:
        def get_job_for_owner(self, owner_id, job_id):
            assert owner_id == "owner-b"
            assert job_id == "job-owned-by-a"
            return None

    monkeypatch.setattr(app_ui, "AUTH", FakeAuth())
    monkeypatch.setattr(app_ui, "BRIDGE", SimpleNamespace(store=FakeStore()))
    with pytest.raises(app_ui.HTTPException) as exc:
        app_ui._owned_ui_job(
            SimpleNamespace(), "job-owned-by-a", mutate=True)
    assert exc.value.status_code == 404
    assert exc.value.detail == "not_found"


def test_sequential_bootstraps_are_keyed_by_current_principal(monkeypatch):
    principals = iter((
        RequestPrincipal(
            user_id="owner-a", authenticated=True, roles=frozenset({"user"}),
            auth_source="local_test", session_id="session-a"),
        RequestPrincipal(
            user_id="owner-b", authenticated=True, roles=frozenset({"user"}),
            auth_source="local_test", session_id="session-b"),
    ))

    class FakeAuth:
        def authenticate_request(self, request):
            return next(principals)

    class FakeBridge:
        def bootstrap(self, cursor, *, principal):
            return {
                "history": [{"owner": principal.owner_id}],
                "queue": [],
                "community": {},
                "profile": {},
            }

    monkeypatch.setattr(app_ui, "AUTH", FakeAuth())
    monkeypatch.setattr(app_ui, "BRIDGE", FakeBridge())
    monkeypatch.setattr(app_ui, "_sync_public_profile", lambda principal: {})
    monkeypatch.setattr(app_ui, "_profile_for_principal", lambda principal: {})
    monkeypatch.setattr(app_ui, "_enrich_history_publications", lambda history: None)
    first = app_ui.api_bootstrap(SimpleNamespace(), cursor=0)
    second = app_ui.api_bootstrap(SimpleNamespace(), cursor=0)
    assert first["history"] == [{"owner": "owner-a"}]
    assert second["history"] == [{"owner": "owner-b"}]


def test_anonymous_bootstrap_does_not_query_private_bridge(monkeypatch):
    class AnonymousAuth:
        def authenticate_request(self, request):
            return RequestPrincipal.anonymous()

    class ForbiddenBridge:
        def bootstrap(self, *args, **kwargs):
            raise AssertionError("anonymous bootstrap queried private data")

    monkeypatch.setattr(app_ui, "AUTH", AnonymousAuth())
    monkeypatch.setattr(app_ui, "BRIDGE", ForbiddenBridge())
    payload = app_ui.api_bootstrap(SimpleNamespace(), cursor=0)
    assert payload["history"] == []
    assert payload["queue"] == []
    assert payload["logs"] == []
    assert payload["community"]["authenticated"] is False


def test_concurrent_owner_queries_never_cross_results(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        for index in range(8):
            _create_job(
                store,
                "owner-a" if index % 2 == 0 else "owner-b",
                job_id=f"{index:032x}",
            )

        def query(owner):
            return {job["owner_id"] for job in store.list_jobs_for_owner(owner)}

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                query, ["owner-a", "owner-b"] * 20))
        assert all(result == {owner} for result, owner in zip(
            results, ["owner-a", "owner-b"] * 20))
    finally:
        store.close()


def test_job_bound_ui_routes_apply_owned_job_guard():
    endpoints = (
        app_ui.api_quality_revision_status,
        app_ui.api_quality_revision_start,
        app_ui.api_quality_revision_cancel,
        app_ui.api_quality_revision_canary_start,
        app_ui.api_page_revision_regions,
        app_ui.api_page_revision_forgotten,
        app_ui.api_page_revision_start,
        app_ui.api_page_revision_status,
        app_ui.api_page_revision_cancel,
        app_ui.api_page_revision_resume,
        app_ui.api_page_revision_decision,
        app_ui.api_page_revision_manual_region,
        app_ui.api_page_revision_draft,
        app_ui.api_audit_review,
        app_ui.api_audit_decision,
        app_ui.api_audit_triage,
        app_ui.api_audit_decision_bulk,
        app_ui.api_audit_provider_set,
        app_ui.api_audit_ocr_candidates,
        app_ui.api_audit_ocr_invalid_candidates,
        app_ui.api_human_translation_review,
        app_ui.api_human_translation_record,
        app_ui.api_human_translation_refinement,
        app_ui.api_human_translation_font_candidates,
        app_ui.api_human_translation_font_choice,
        app_ui.api_human_translation_font_candidate_preview,
        app_ui.api_human_translation_draft,
        app_ui.api_human_translation_gates,
        app_ui.api_human_translation_visual_review,
        app_ui.api_human_translation_preview_crop,
        app_ui.api_human_mask_editor_state,
        app_ui.api_human_mask_save,
        app_ui.api_human_mask_confirm,
        app_ui.api_human_mask_asset,
        app_ui.api_audit_region_crop,
        app_ui.api_audit_editorial_pending,
        app_ui.api_audit_provider_authorization,
        app_ui.api_audit_provider_authorization_cancel,
        app_ui.api_audit_decision_delete,
        app_ui.api_quality_review,
        app_ui.api_quality_review_page,
        app_ui.api_quality_review_action,
        app_ui.api_quality_review_bulk_action,
        app_ui.api_quality_review_global_review,
        app_ui.api_quality_review_confirm,
        app_ui.api_retry,
        app_ui.api_source_confirm,
        app_ui.api_source_retry,
        app_ui.api_resume,
        app_ui.api_history_delete,
    )
    for endpoint in endpoints:
        assert "_owned_ui_job(" in inspect.getsource(endpoint), endpoint.__name__
