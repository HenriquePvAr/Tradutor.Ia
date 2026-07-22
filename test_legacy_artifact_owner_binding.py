"""Hermetic coverage for the explicit legacy-artifact owner migration API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from community_auth import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    LocalSessionAuthProvider,
    SESSION_COOKIE_NAME,
)
from community_http import (
    CommunityNetworkBoundaryMiddleware,
    create_admin_community_router,
    create_community_router,
)
from community_api import CommunityApi
from job_store import JobStatus, JobStore


JOB_ID = "a" * 32
PDF_BYTES = b"%PDF-legacy-owner-binding\n"


class BindingHarness:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.output = tmp_path / "output"
        self.output.mkdir()
        self.jobs = JobStore(tmp_path / "jobs.sqlite3")
        self.api = CommunityApi(
            self.jobs,
            community_db_path=tmp_path / "community.sqlite3",
            output_root=self.output,
        )
        self.auth = LocalSessionAuthProvider(
            bootstrap_secret="unused-bootstrap-" + "x" * 32,
            bootstrap_user_id="admin-user",
            bootstrap_roles=("admin",),
        )
        self.admin = self.auth.issue_session(user_id="admin-user", roles=("admin",))
        self.member = self.auth.issue_session(user_id="member-user")
        app = FastAPI()
        app.add_middleware(CommunityNetworkBoundaryMiddleware, auth=self.auth)
        app.include_router(create_community_router(self.api, self.auth))
        app.include_router(create_admin_community_router(self.api, self.auth))
        self.client = TestClient(app, client=("127.0.0.1", 50000))
        self.job_id, self.run_id, self.pdf, self.sha = self._create_job()

    def _create_job(self):
        folder = self.output / "legacy-owner-binding"
        folder.mkdir()
        pdf = folder / "chapter.pdf"
        pdf.write_bytes(PDF_BYTES)
        job_id = self.jobs.create_job(
            job_id=JOB_ID,
            run_id="run-legacy-owner-binding",
            source_url="https://example.invalid/offline",
            output_dir=str(folder),
            command=["offline"],
            configuration={"job_type": "translation"},
        )
        claimed = self.jobs.claim_next_job("test-worker", 1)
        worker = claimed["worker_id"]
        self.jobs.transition(job_id, JobStatus.STARTING, expected_worker=worker)
        self.jobs.transition(job_id, JobStatus.RUNNING, expected_worker=worker)
        self.jobs.transition(
            job_id,
            JobStatus.FINISHED,
            expected_worker=worker,
            exit_code=0,
            pdf_path=str(pdf),
        )
        self.jobs.update_fields(
            job_id,
            stage="review_completed",
            review_confirmed_at=1234.0,
        )
        manifest = {
            "job_id": job_id,
            "run_id": "run-legacy-owner-binding",
            "status": "finished",
            "exit_code": 0,
            "pdf_path": str(pdf),
            "quality_passed": False,
            "final_status": "review_required",
        }
        (folder / "job_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "run_manifest.json").write_text(
            json.dumps({"run_id": "evidence-run", "pdf_path": str(pdf)}), encoding="utf-8")
        return job_id, "run-legacy-owner-binding", pdf, hashlib.sha256(PDF_BYTES).hexdigest()

    @staticmethod
    def headers(issued, *, csrf=True):
        headers = {"Cookie": f"{SESSION_COOKIE_NAME}={issued.session_token}"}
        if csrf:
            headers["Cookie"] += f"; {CSRF_COOKIE_NAME}={issued.csrf_token}"
            headers[CSRF_HEADER_NAME] = issued.csrf_token
        return headers

    def close(self):
        self.client.close()
        self.api.close()
        self.jobs.close()


def payload(harness: BindingHarness, *, target="admin-user", sha=None, run=None):
    return {
        "target_user_id": target,
        "expected_run_id": run or harness.run_id,
        "expected_pdf_sha256": sha or harness.sha,
        "reason": "Vinculação administrativa autorizada para teste offline.",
        "confirm": True,
    }


def test_non_admin_is_rejected_and_audited(tmp_path):
    h = BindingHarness(tmp_path)
    try:
        response = h.client.post(
            f"/api/admin/community/artifacts/{h.job_id}/bind-owner",
            json=payload(h, target="member-user"),
            headers=h.headers(h.member),
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "admin_required"
        assert not h.jobs.get_job(h.job_id)["configuration"].get("community_owner_id")
        audit = h.api.store.events_for_post("")
        rows = h.api.store._rows(h.api.store._conn.execute(
            "SELECT * FROM community_events WHERE event_type=?", ("legacy_artifact_owner_bound",)))
        assert rows and json.loads(rows[-1]["metadata_json"])["result"] == "admin_required"
    finally:
        h.close()


def test_admin_binds_exact_artifact_and_preserves_pdf(tmp_path):
    h = BindingHarness(tmp_path)
    try:
        before = hashlib.sha256(h.pdf.read_bytes()).hexdigest()
        response = h.client.post(
            f"/api/admin/community/artifacts/{h.job_id}/bind-owner",
            json=payload(h),
            headers=h.headers(h.admin),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "owner_bound"
        job = h.jobs.get_job(h.job_id)
        config = job["configuration"]
        assert config["community_owner_id"] == "admin-user"
        assert config["owner_bound_by"] == "admin-user"
        assert config["owner_bound_at"]
        assert hashlib.sha256(h.pdf.read_bytes()).hexdigest() == before == h.sha
        rows = h.api.store._rows(h.api.store._conn.execute(
            "SELECT * FROM community_events WHERE event_type=?", ("legacy_artifact_owner_bound",)))
        metadata = json.loads(rows[-1]["metadata_json"])
        assert metadata["result"] == "owner_bound"
        assert metadata["pdf_sha256"] == h.sha
        assert len(h.jobs.list_jobs(limit=None)) == 1
    finally:
        h.close()


def test_binding_is_idempotent_without_duplicate_success_audit(tmp_path):
    h = BindingHarness(tmp_path)
    try:
        url = f"/api/admin/community/artifacts/{h.job_id}/bind-owner"
        first = h.client.post(url, json=payload(h), headers=h.headers(h.admin))
        second = h.client.post(url, json=payload(h), headers=h.headers(h.admin))
        assert first.status_code == 200 and first.json()["status"] == "owner_bound"
        assert second.status_code == 200
        assert second.json()["status"] == "already_bound_to_target"
        rows = h.api.store._rows(h.api.store._conn.execute(
            "SELECT * FROM community_events WHERE event_type=?", ("legacy_artifact_owner_bound",)))
        assert sum(json.loads(row["metadata_json"])["result"] == "owner_bound" for row in rows) == 1
    finally:
        h.close()


def test_hash_and_run_are_explicit_preconditions(tmp_path):
    h = BindingHarness(tmp_path)
    try:
        bad_hash = h.client.post(
            f"/api/admin/community/artifacts/{h.job_id}/bind-owner",
            json=payload(h, sha="0" * 64), headers=h.headers(h.admin))
        bad_run = h.client.post(
            f"/api/admin/community/artifacts/{h.job_id}/bind-owner",
            json=payload(h, run="wrong-run"), headers=h.headers(h.admin))
        assert bad_hash.status_code == 409
        assert bad_hash.json()["detail"] == "hash_mismatch"
        assert bad_run.status_code == 409
        assert bad_run.json()["detail"] == "run_mismatch"
        assert not h.jobs.get_job(h.job_id)["configuration"].get("community_owner_id")
    finally:
        h.close()


def test_existing_owner_is_never_overwritten(tmp_path):
    h = BindingHarness(tmp_path)
    try:
        h.jobs.update_fields(
            h.job_id,
            configuration_json=json.dumps({"job_type": "translation", "community_owner_id": "other-user"}),
        )
        response = h.client.post(
            f"/api/admin/community/artifacts/{h.job_id}/bind-owner",
            json=payload(h), headers=h.headers(h.admin))
        assert response.status_code == 409
        assert response.json()["detail"] == "owner_already_assigned"
        assert h.jobs.get_job(h.job_id)["configuration"]["community_owner_id"] == "other-user"
    finally:
        h.close()
