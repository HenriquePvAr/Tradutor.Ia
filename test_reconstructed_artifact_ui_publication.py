import _test_bootstrap  # noqa: F401

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import community_api
import ui_bridge
from community_api import ArtifactBindingError, CommunityApi
from community_auth import RequestPrincipal
from job_store import JobStatus, JobStore
from ui_history import UIHistoryStore


OWNER_ID = "owner-recon-ui"
OWNER = RequestPrincipal(OWNER_ID, True, auth_source="test", session_id="owner")


def _write_pdf(path: Path, body: bytes) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7\n" + body + b"\n%%EOF\n")
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _write_manifests(
    output: Path,
    *,
    job_id: str,
    run_id: str,
    status: str,
    pdf: Path,
    quality_passed: bool,
) -> None:
    (output / "job_manifest.json").write_text(json.dumps({
        "job_id": job_id,
        "run_id": run_id,
        "status": status,
        "exit_code": 0,
        "output_dir": str(output),
        "pdf_path": str(pdf),
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "quality_passed": quality_passed,
    }), encoding="utf-8")
    (output / "run_manifest.json").write_text(json.dumps({
        "run_id": run_id,
        "pdf_path": str(pdf),
    }), encoding="utf-8")


def _write_quality_report(
    output: Path,
    *,
    pdf: Path,
    run_id: str,
    passed: bool,
    manual_review_count: int = 0,
) -> Path:
    digest, size = hashlib.sha256(pdf.read_bytes()).hexdigest(), pdf.stat().st_size
    report = output / "quality_report.json"
    report.write_text(json.dumps({
        "summary": {
            "pdf_path": str(pdf),
            "run_id": run_id,
            "artifact_sha256": digest,
            "artifact_size_bytes": size,
            "quality_validation": {
                "passed": passed,
                "manual_review_required_groups": manual_review_count,
                "status": "passed" if passed else "review_required",
                "artifact_sha256": digest,
                "artifact_size_bytes": size,
                "run_id": run_id,
            },
        },
        "quality_validation": {
            "passed": passed,
            "manual_review_required_groups": manual_review_count,
            "status": "passed" if passed else "review_required",
            "artifact_sha256": digest,
            "artifact_size_bytes": size,
            "run_id": run_id,
        },
    }), encoding="utf-8")
    return report


class ReconstructedArtifactUiPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output_root = self.tmp / "output"
        self.store = JobStore(self.tmp / "jobs.sqlite3")
        self.api = CommunityApi(
            self.store,
            community_db_path=self.tmp / "community.sqlite3",
            output_root=self.output_root,
        )
        self.bridge = object.__new__(ui_bridge.UiBridge)
        self.bridge.store = self.store
        self.bridge.output_root = self.output_root
        self.bridge.history_store = UIHistoryStore(self.tmp / "ui_history.json")
        self.bridge.history = []
        self.bridge.history_revision = 1
        self._patches = [
            patch.object(ui_bridge, "OUTPUT_ROOT", self.output_root),
            patch.object(community_api, "OUTPUT_ROOT", self.output_root),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self.api.close()
        self.store.close()

    def _create_source_and_reconstruction(
        self,
        *,
        child_status: str = JobStatus.FINISHED,
        child_quality_passed: bool = True,
        child_manual_review_count: int = 0,
        child_parent: str | None = None,
        child_source_run_id: str | None = None,
        child_output_name: str = "child",
        child_created_at: float | None = None,
    ) -> tuple[str, str, str]:
        source_output = self.output_root / "chapter-source"
        source_pdf = source_output / "historical.pdf"
        _write_pdf(source_pdf, b"historical source")
        source_id = self.store.create_job(
            source_url="https://example.invalid/chapter",
            output_dir=str(source_output),
            command=["python", "run_webtoon.py"],
            configuration={
                "job_type": "translation",
                "community_owner_id": OWNER_ID,
                "chapter_name": "Episode 51",
            },
            initial_status=JobStatus.QUEUED,
        )
        self.store.update_fields(
            source_id,
            status=JobStatus.REVIEW_REQUIRED,
            exit_code=0,
            pdf_path=str(source_pdf),
            manifest_path=str(source_output / "job_manifest.json"),
        )
        _write_manifests(
            source_output,
            job_id=source_id,
            run_id=self.store.get_job(source_id)["run_id"],
            status=JobStatus.REVIEW_REQUIRED,
            pdf=source_pdf,
            quality_passed=False,
        )
        _write_quality_report(
            source_output,
            pdf=source_pdf,
            run_id=self.store.get_job(source_id)["run_id"],
            passed=False,
            manual_review_count=1,
        )

        child_output = source_output / "reconstructions" / child_output_name
        child_pdf = child_output / "artifact.pdf"
        child_sha, _size = _write_pdf(child_pdf, f"fresh reconstruction {child_output_name}".encode())
        child_id = self.store.create_job(
            source_url="https://example.invalid/chapter",
            output_dir=str(child_output),
            command=["selective_artifact_reconstruction"],
            run_id="recon-run",
            configuration={
                "job_type": "artifact_reconstruction",
                "community_owner_id": OWNER_ID,
                "source_job_id": source_id,
                "source_run_id": (
                    child_source_run_id
                    if child_source_run_id is not None
                    else self.store.get_job(source_id)["run_id"]
                ),
            },
            initial_status=JobStatus.QUEUED,
            operation_kind="artifact_reconstruction",
            parent_job_id=child_parent if child_parent is not None else source_id,
        )
        self.store.update_fields(
            child_id,
            status=child_status,
            stage="artifact_reconstruction_completed",
            exit_code=0,
            pdf_path=str(child_pdf),
            manifest_path=str(child_output / "job_manifest.json"),
            quality_report_path=str(child_output / "quality_report.json"),
        )
        _write_manifests(
            child_output,
            job_id=child_id,
            run_id="recon-run",
            status=child_status,
            pdf=child_pdf,
            quality_passed=child_quality_passed,
        )
        _write_quality_report(
            child_output,
            pdf=child_pdf,
            run_id="recon-run",
            passed=child_quality_passed,
            manual_review_count=child_manual_review_count,
        )
        if child_created_at is not None:
            self.store._conn.execute(
                "UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
                (float(child_created_at), float(child_created_at), child_id),
            )
            self.store._conn.commit()
        return source_id, child_id, child_sha

    def _add_reconstruction_child(
        self,
        source_id: str,
        *,
        output_name: str,
        status: str = JobStatus.FINISHED,
        quality_passed: bool = True,
        manual_review_count: int = 0,
        source_run_id: str | None = None,
        owner_id: str = OWNER_ID,
        parent_id: str | None = None,
        created_at: float,
    ) -> tuple[str, str]:
        source = self.store.get_job(source_id)
        assert source is not None
        child_output = self.output_root / "chapter-source" / "reconstructions" / output_name
        child_pdf = child_output / "artifact.pdf"
        child_sha, _size = _write_pdf(child_pdf, f"reconstruction {output_name}".encode())
        child_id = self.store.create_job(
            source_url="https://example.invalid/chapter",
            output_dir=str(child_output),
            command=["selective_artifact_reconstruction"],
            run_id=f"recon-{output_name}",
            configuration={
                "job_type": "artifact_reconstruction",
                "community_owner_id": owner_id,
                "source_job_id": source_id,
                "source_run_id": source_run_id if source_run_id is not None else source["run_id"],
            },
            initial_status=JobStatus.QUEUED,
            operation_kind="artifact_reconstruction",
            parent_job_id=parent_id if parent_id is not None else source_id,
        )
        self.store.update_fields(
            child_id,
            status=status,
            stage="artifact_reconstruction_completed",
            exit_code=0,
            pdf_path=str(child_pdf),
            manifest_path=str(child_output / "job_manifest.json"),
            quality_report_path=str(child_output / "quality_report.json"),
        )
        _write_manifests(
            child_output,
            job_id=child_id,
            run_id=f"recon-{output_name}",
            status=status,
            pdf=child_pdf,
            quality_passed=quality_passed,
        )
        _write_quality_report(
            child_output,
            pdf=child_pdf,
            run_id=f"recon-{output_name}",
            passed=quality_passed,
            manual_review_count=manual_review_count,
        )
        self.store._conn.execute(
            "UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
            (float(created_at), float(created_at), child_id),
        )
        self.store._conn.commit()
        return child_id, child_sha

    def test_owner_history_exposes_finished_reconstruction_without_enabling_source(self):
        source_id, child_id, child_sha = self._create_source_and_reconstruction()

        records = self.bridge._history_payload_for_owner(OWNER_ID)
        by_job = {item["job_id"]: item for item in records}

        self.assertIn(source_id, by_job)
        self.assertIn(child_id, by_job)
        self.assertEqual(by_job[source_id]["status"], JobStatus.REVIEW_REQUIRED)
        self.assertFalse(by_job[source_id].get("quality_gate"))
        self.assertEqual(by_job[child_id]["operation_kind"], "artifact_reconstruction")
        self.assertEqual(by_job[child_id]["parent_job_id"], source_id)
        self.assertNotIn("manifest_path", by_job[child_id])
        self.assertTrue(by_job[child_id].get("publication_manifest_ready"))
        self.assertEqual(by_job[child_id]["pdf_sha256"], child_sha)
        self.assertTrue(by_job[child_id].get("quality_gate"))

        with self.assertRaisesRegex(ArtifactBindingError, "quality_gate_required"):
            self.api._resolve_translation_job(source_id, OWNER)
        resolved = self.api._resolve_translation_job(child_id, OWNER)
        self.assertEqual(resolved["source_job_id"], child_id)
        self.assertEqual(resolved["pdf_sha256"], child_sha)

    def test_history_does_not_expose_non_publishable_reconstruction_children(self):
        denied = [
            (JobStatus.INTERRUPTED, True, 0),
            (JobStatus.FAILED, True, 0),
            (JobStatus.CANCELLED, True, 0),
            (JobStatus.FINISHED, False, 0),
            (JobStatus.FINISHED, True, 1),
        ]
        for status, passed, manual in denied:
            tmp = Path(tempfile.mkdtemp())
            store = JobStore(tmp / "jobs.sqlite3")
            api = CommunityApi(
                store,
                community_db_path=tmp / "community.sqlite3",
                output_root=tmp / "output",
            )
            bridge = object.__new__(ui_bridge.UiBridge)
            bridge.store = store
            bridge.output_root = tmp / "output"
            bridge.history_store = UIHistoryStore(tmp / "ui_history.json")
            bridge.history = []
            bridge.history_revision = 1
            try:
                old_store, old_api, old_bridge, old_output_root = (
                    self.store, self.api, self.bridge, self.output_root
                )
                self.store, self.api, self.bridge, self.output_root = (
                    store, api, bridge, tmp / "output"
                )
                _source_id, child_id, _sha = self._create_source_and_reconstruction(
                    child_status=status,
                    child_quality_passed=passed,
                    child_manual_review_count=manual,
                )
                records = self.bridge._history_payload_for_owner(OWNER_ID)
                self.assertNotIn(child_id, {item["job_id"] for item in records})
                with self.assertRaises(ArtifactBindingError):
                    self.api._resolve_translation_job(child_id, OWNER)
            finally:
                self.store, self.api, self.bridge, self.output_root = (
                    old_store, old_api, old_bridge, old_output_root
                )
                api.close()
                store.close()

    def test_wrong_parent_child_is_not_exposed_as_ui_publication_target(self):
        source_id, child_id, _sha = self._create_source_and_reconstruction(child_parent="f" * 32)

        records = self.bridge._history_payload_for_owner(OWNER_ID)
        by_job = {item["job_id"]: item for item in records}

        self.assertIn(source_id, by_job)
        self.assertNotIn(child_id, by_job)
        with self.assertRaisesRegex(ArtifactBindingError, "quality_gate_required"):
            self.api._resolve_translation_job(source_id, OWNER)
        with self.assertRaisesRegex(ArtifactBindingError, "artifact_source_mismatch"):
            self.api._resolve_translation_job(child_id, OWNER)

    def test_newer_invalid_reconstruction_attempts_do_not_shadow_old_valid_result(self):
        denied_newer = [
            ("failed", JobStatus.FAILED, True, 0, None),
            ("interrupted", JobStatus.INTERRUPTED, True, 0, None),
            ("cancelled", JobStatus.CANCELLED, True, 0, None),
            ("quality_fail", JobStatus.FINISHED, False, 0, None),
            ("manual_review", JobStatus.FINISHED, True, 2, None),
            ("stale_run", JobStatus.FINISHED, True, 0, "stale-source-run"),
        ]
        for name, status, passed, manual, source_run_id in denied_newer:
            with self.subTest(name=name):
                tmp = Path(tempfile.mkdtemp())
                store = JobStore(tmp / "jobs.sqlite3")
                api = CommunityApi(
                    store,
                    community_db_path=tmp / "community.sqlite3",
                    output_root=tmp / "output",
                )
                bridge = object.__new__(ui_bridge.UiBridge)
                bridge.store = store
                bridge.output_root = tmp / "output"
                bridge.history_store = UIHistoryStore(tmp / "ui_history.json")
                bridge.history = []
                bridge.history_revision = 1
                try:
                    old_store, old_api, old_bridge, old_output_root = (
                        self.store, self.api, self.bridge, self.output_root
                    )
                    self.store, self.api, self.bridge, self.output_root = (
                        store, api, bridge, tmp / "output"
                    )
                    source_id, old_id, old_sha = self._create_source_and_reconstruction(
                        child_output_name=f"{name}-old",
                        child_created_at=1000,
                    )
                    newer_id, _newer_sha = self._add_reconstruction_child(
                        source_id,
                        output_name=f"{name}-new",
                        status=status,
                        quality_passed=passed,
                        manual_review_count=manual,
                        source_run_id=source_run_id,
                        created_at=2000,
                    )

                    records = self.bridge._history_payload_for_owner(OWNER_ID)
                    reconstruction_ids = {
                        item["job_id"] for item in records
                        if item.get("operation_kind") == "artifact_reconstruction"
                    }

                    self.assertIn(old_id, reconstruction_ids)
                    self.assertNotIn(newer_id, reconstruction_ids)
                    resolved = self.api._resolve_translation_job(old_id, OWNER)
                    self.assertEqual(resolved["pdf_sha256"], old_sha)
                    with self.assertRaises(ArtifactBindingError):
                        self.api._resolve_translation_job(newer_id, OWNER)
                finally:
                    self.store, self.api, self.bridge, self.output_root = (
                        old_store, old_api, old_bridge, old_output_root
                    )
                    api.close()
                    store.close()

    def test_newer_valid_reconstruction_supersedes_old_valid_deterministically(self):
        source_id, old_id, _old_sha = self._create_source_and_reconstruction(
            child_output_name="old-valid",
            child_created_at=1000,
        )
        new_id, new_sha = self._add_reconstruction_child(
            source_id,
            output_name="new-valid",
            created_at=2000,
        )

        records = self.bridge._history_payload_for_owner(OWNER_ID)
        reconstruction_ids = [
            item["job_id"] for item in records
            if item.get("operation_kind") == "artifact_reconstruction"
        ]

        self.assertEqual(reconstruction_ids, [new_id])
        self.assertNotIn(old_id, reconstruction_ids)
        resolved = self.api._resolve_translation_job(new_id, OWNER)
        self.assertEqual(resolved["pdf_sha256"], new_sha)

    def test_reconstruction_currentness_tie_breaker_is_deterministic(self):
        source_id, old_id, _old_sha = self._create_source_and_reconstruction(
            child_output_name="tie-old",
            child_created_at=1000,
        )
        new_id, _new_sha = self._add_reconstruction_child(
            source_id,
            output_name="tie-new",
            created_at=1000,
        )

        records = self.bridge._history_payload_for_owner(OWNER_ID)
        reconstruction_ids = [
            item["job_id"] for item in records
            if item.get("operation_kind") == "artifact_reconstruction"
        ]

        self.assertEqual(reconstruction_ids, [max(old_id, new_id)])

    def test_wrong_owner_reconstruction_is_not_exposed_in_owner_history(self):
        source_id, _old_id, _old_sha = self._create_source_and_reconstruction(
            child_output_name="owned-valid",
            child_created_at=1000,
        )
        wrong_owner_id, _wrong_sha = self._add_reconstruction_child(
            source_id,
            output_name="wrong-owner",
            owner_id="other-owner",
            created_at=2000,
        )

        records = self.bridge._history_payload_for_owner(OWNER_ID)
        reconstruction_ids = {
            item["job_id"] for item in records
            if item.get("operation_kind") == "artifact_reconstruction"
        }

        self.assertNotIn(wrong_owner_id, reconstruction_ids)

    def test_no_valid_reconstruction_child_leaves_no_reconstruction_publication_card(self):
        source_id, child_id, _sha = self._create_source_and_reconstruction(
            child_status=JobStatus.FAILED,
            child_output_name="failed-only",
        )

        records = self.bridge._history_payload_for_owner(OWNER_ID)
        reconstruction_ids = {
            item["job_id"] for item in records
            if item.get("operation_kind") == "artifact_reconstruction"
        }

        self.assertIn(source_id, {item["job_id"] for item in records})
        self.assertNotIn(child_id, reconstruction_ids)

    def test_reconstruction_publication_capability_does_not_leak_manifest_path(self):
        source_id, child_id, _sha = self._create_source_and_reconstruction()
        row = self.store.get_job(child_id)
        assert row is not None

        record = self.bridge._job_record(row)
        serialized = json.dumps(record)

        self.assertNotIn("manifest_path", record)
        self.assertNotIn("job_manifest.json", serialized)
        self.assertTrue(record.get("publication_manifest_ready"))

    def test_missing_manifest_yields_false_capability_without_leaking_path(self):
        _source_id, child_id, _sha = self._create_source_and_reconstruction()
        row = self.store.get_job(child_id)
        assert row is not None
        Path(str(row["manifest_path"])).unlink()

        record = self.bridge._job_record(row)
        serialized = json.dumps(record)

        self.assertNotIn("manifest_path", record)
        self.assertNotIn("job_manifest.json", serialized)
        self.assertFalse(record.get("publication_manifest_ready"))


class ReconstructedArtifactFrontendContractTests(unittest.TestCase):
    def test_publication_payload_uses_record_job_id_and_not_parent_fallback(self):
        source = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(
            encoding="utf-8"
        )
        publish = source[source.index("async function publishToCommunity"):source.index(
            "$('#publicationCancel')"
        )]
        self.assertIn("const trustedJobId", publish)
        self.assertIn("payload.source_job_id = trustedJobId", publish)
        self.assertNotIn("parent_job_id", publish)
        self.assertNotIn("effective", publish.lower())

    def test_publication_eligibility_uses_server_capability_not_manifest_path(self):
        source = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(
            encoding="utf-8"
        )
        eligibility = source[source.index("function publicationEligibility"):source.index(
            "function publicationAction"
        )]
        self.assertIn("publication_manifest_ready", eligibility)
        self.assertNotIn("manifest_path", eligibility)

    def test_history_publication_requires_server_canonical_chapter_when_configured(self):
        source = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(
            encoding="utf-8"
        )
        eligibility = source[source.index("function publicationEligibility"):source.index(
            "function publicationAction"
        )]
        action = source[source.index("function publicationAction"):source.index(
            "function claimEligibility"
        )]
        self.assertIn("requires_canonical_chapter_publication", eligibility)
        self.assertIn("canonicalChapterReady", eligibility)
        self.assertIn("record.canonical_chapter_id", eligibility)
        self.assertIn("record.social_chapter_id", eligibility)
        self.assertIn("record.remote_chapter_id", eligibility)
        self.assertIn("!eligibility.canonicalChapterReady", action)

    def test_reconstruction_status_label_is_distinct_from_historical_review_required(self):
        source = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("artifact_reconstruction", source)
        self.assertIn("Reconstrução corrigida", source)


if __name__ == "__main__":
    unittest.main()
