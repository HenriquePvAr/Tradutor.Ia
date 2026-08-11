"""Community publication metadata + published-PDF storage core contracts.

Hermetic: no Supabase network, no Google Drive network, no Webtoon/NVIDIA.
"""

import _test_bootstrap  # noqa: F401

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

import community_api
import community_publish_runner
from community_api import ArtifactBindingError, CommunityApi, CommunityError
from community_auth import RequestPrincipal
from community_publication_metadata import FakePublicationMetadataRepository
from community_storage import FakeStorageProvider, StorageError
from community_store import FileStatus, PostStatus
from job_store import JobStatus, JobStore


OWNER = RequestPrincipal("owner-a", True, auth_source="test", session_id="owner")
OTHER = RequestPrincipal("owner-b", True, auth_source="test", session_id="other")


def _write_pdf(path: Path, data: bytes = b"%PDF-1.4\nstorage-core\n%%EOF\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _finished_translation_job(
    jobs: JobStore,
    output_root: Path,
    *,
    owner: str = OWNER.user_id,
    name: str = "chapter",
    status: str = JobStatus.FINISHED,
    data: bytes = b"%PDF-1.4\nstorage-core\n%%EOF\n",
) -> tuple[str, Path]:
    output_dir = output_root / name
    pdf = _write_pdf(output_dir / "chapter.pdf", data)
    job_id = jobs.create_job(
        source_url="https://example.invalid/offline",
        output_dir=str(output_dir),
        command=["offline"],
        configuration={"job_type": "translation", "community_owner_id": owner},
    )
    claimed = jobs.claim_next_job(f"worker-{name}", 1)
    assert claimed and claimed["id"] == job_id
    worker = claimed["worker_id"]
    jobs.transition(job_id, JobStatus.STARTING, expected_worker=worker)
    jobs.transition(job_id, JobStatus.RUNNING, expected_worker=worker)
    job = jobs.transition(
        job_id,
        status,
        expected_worker=worker,
        exit_code=0,
        pdf_path=str(pdf),
    )
    (output_dir / "job_manifest.json").write_text(
        json.dumps({
            "job_id": job_id,
            "run_id": job["run_id"],
            "status": status,
            "exit_code": 0,
            "pdf_path": str(pdf),
        }),
        encoding="utf-8",
    )
    return job_id, pdf


class ResponseLostAfterCreateProvider(FakeStorageProvider):
    def __init__(self):
        super().__init__()
        self._lost_once = False

    def upload_chunk(self, session, offset, data):
        result = super().upload_chunk(session, offset, data)
        if result.completed and not self._lost_once:
            self._lost_once = True
            raise StorageError("response lost after remote create", transient=True, status=503)
        return result


class StorageCoreHarness:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.output_root = tmp_path / "output"
        self.jobs = JobStore(tmp_path / "jobs.sqlite3")
        self.api = CommunityApi(
            self.jobs,
            community_db_path=tmp_path / "community.sqlite3",
            output_root=self.output_root,
            publication_metadata_config={"provider": "fake"},
        )

    def close(self):
        self.api.close()
        self.jobs.close()

    def publish(self, job_id: str, *, principal=OWNER, consent=True) -> dict:
        return self.api.publish(
            {"source_job_id": job_id, "series_slug": "series", "publish_consent": consent},
            principal=principal,
        )

    def run_publish_job(self, job_id: str, provider, metadata_repo) -> int:
        claimed = self.jobs.claim_next_job("publisher", 1)
        assert claimed and claimed["id"] == job_id
        self.jobs.transition(job_id, JobStatus.STARTING, expected_worker="publisher")
        with patch.object(community_publish_runner, "build_storage_provider", lambda _config: provider), \
             patch.object(
                 community_publish_runner,
                 "build_publication_metadata_repository",
                 lambda _config: metadata_repo,
             ):
            return community_publish_runner.run_job(job_id, str(self.tmp / "jobs.sqlite3"))


@pytest.fixture()
def harness(tmp_path):
    h = StorageCoreHarness(tmp_path)
    try:
        yield h
    finally:
        h.close()


def test_local_pdf_without_publish_has_no_drive_or_supabase_publication(harness):
    provider = FakeStorageProvider()
    metadata = FakePublicationMetadataRepository()
    _finished_translation_job(harness.jobs, harness.output_root)

    assert provider.create_session_calls == 0
    assert provider.upload_chunk_calls == 0
    assert metadata.reserve_calls == 0
    assert metadata.finalize_calls == 0
    assert harness.api.store.list_user_posts(OWNER.user_id) == []


def test_missing_consent_creates_no_drive_or_publication(harness):
    provider = FakeStorageProvider()
    metadata = FakePublicationMetadataRepository()
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)

    with pytest.raises(CommunityError, match="publish_consent_required"):
        harness.publish(job_id, consent=False)

    assert provider.upload_chunk_calls == 0
    assert metadata.finalize_calls == 0
    assert harness.api.store.list_user_posts(OWNER.user_id) == []


def test_quality_fail_creates_no_drive_or_publication(harness):
    provider = FakeStorageProvider()
    job_id, _ = _finished_translation_job(
        harness.jobs,
        harness.output_root,
        status=JobStatus.REVIEW_REQUIRED,
    )

    with pytest.raises(ArtifactBindingError) as caught:
        harness.publish(job_id)

    assert caught.value.code == "quality_gate_required"
    assert provider.upload_chunk_calls == 0
    assert harness.api.store.list_user_posts(OWNER.user_id) == []


def test_wrong_owner_creates_no_drive_or_publication(harness):
    provider = FakeStorageProvider()
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root, owner=OWNER.user_id)

    with pytest.raises(ArtifactBindingError) as caught:
        harness.publish(job_id, principal=OTHER)

    assert caught.value.code == "artifact_not_owned"
    assert provider.upload_chunk_calls == 0
    assert harness.api.store.list_user_posts(OTHER.user_id) == []


def test_stale_artifact_is_denied_before_drive_upload(harness):
    provider = FakeStorageProvider()
    metadata = FakePublicationMetadataRepository()
    job_id, pdf = _finished_translation_job(harness.jobs, harness.output_root)
    publish = harness.publish(job_id)
    pdf.write_bytes(b"%PDF-1.4\nmutated-after-reservation\n%%EOF\n")

    harness.run_publish_job(publish["job_id"], provider, metadata)

    assert provider.create_session_calls == 0
    assert provider.upload_chunk_calls == 0
    assert metadata.finalize_calls == 0
    assert harness.api.store.get_post(publish["post_id"])["status"] == PostStatus.FAILED


def test_happy_path_uploads_once_and_persists_publication_metadata(harness):
    provider = FakeStorageProvider()
    metadata = FakePublicationMetadataRepository()
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)

    publish = harness.publish(job_id)
    harness.run_publish_job(publish["job_id"], provider, metadata)

    post = harness.api.store.get_post(publish["post_id"])
    file = harness.api.store.get_file(publish["file_id"])
    row = metadata.get_publication(publish["post_id"])
    assert post["status"] == PostStatus.PUBLISHED
    assert file["upload_status"] == FileStatus.VERIFIED
    assert provider.create_session_calls == 1
    assert provider.upload_chunk_calls == 1
    assert metadata.reserve_calls == 1
    assert metadata.finalize_calls == 1
    assert row is not None
    assert row.publication_status == "published"
    assert row.storage_reference == file["storage_file_id"]
    assert "storage_reference" not in row.public()


def test_duplicate_publish_is_idempotent_one_upload_one_publication(harness):
    provider = FakeStorageProvider()
    metadata = FakePublicationMetadataRepository()
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)

    first = harness.publish(job_id)
    second = harness.publish(job_id)
    assert first == second
    harness.run_publish_job(first["job_id"], provider, metadata)

    assert provider.create_session_calls == 1
    assert metadata.finalize_calls == 1
    assert len(harness.api.store.list_user_posts(OWNER.user_id)) == 1


def test_concurrent_equivalent_publish_is_safe(harness):
    provider = FakeStorageProvider()
    metadata = FakePublicationMetadataRepository()
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _i: harness.publish(job_id), range(2)))

    assert results[0] == results[1]
    harness.run_publish_job(results[0]["job_id"], provider, metadata)
    assert provider.create_session_calls == 1
    assert len(harness.api.store.list_user_posts(OWNER.user_id)) == 1


def test_drive_failure_does_not_mark_published(harness):
    provider = FakeStorageProvider(online=False)
    metadata = FakePublicationMetadataRepository()
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)
    publish = harness.publish(job_id)

    harness.run_publish_job(publish["job_id"], provider, metadata)

    assert harness.api.store.get_post(publish["post_id"])["status"] == PostStatus.FAILED
    assert provider.create_session_calls == 0
    assert metadata.finalize_calls == 0


def test_response_lost_retry_reuses_remote_reference_without_duplicate_upload(harness):
    provider = ResponseLostAfterCreateProvider()
    metadata = FakePublicationMetadataRepository()
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)
    first = harness.publish(job_id)

    harness.run_publish_job(first["job_id"], provider, metadata)
    assert harness.api.store.get_post(first["post_id"])["status"] == PostStatus.FAILED
    assert provider.create_session_calls == 1
    failed_file = harness.api.store.get_file(first["file_id"])
    assert failed_file["storage_file_id"]

    retry = harness.publish(job_id)
    harness.run_publish_job(retry["job_id"], provider, metadata)

    assert provider.create_session_calls == 1
    assert metadata.finalize_calls == 1
    assert harness.api.store.get_post(first["post_id"])["status"] == PostStatus.PUBLISHED


def test_drive_success_and_metadata_finalization_failure_is_recoverable(harness):
    provider = FakeStorageProvider()
    metadata = FakePublicationMetadataRepository(fail_finalize_once=True)
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)
    first = harness.publish(job_id)

    harness.run_publish_job(first["job_id"], provider, metadata)
    assert harness.api.store.get_post(first["post_id"])["status"] == PostStatus.FAILED
    failed_file = harness.api.store.get_file(first["file_id"])
    assert failed_file["storage_file_id"]

    retry = harness.publish(job_id)
    harness.run_publish_job(retry["job_id"], provider, metadata)

    assert provider.create_session_calls == 1
    assert metadata.finalize_calls == 2
    assert harness.api.store.get_post(first["post_id"])["status"] == PostStatus.PUBLISHED


def test_client_cannot_choose_storage_reference(harness):
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)

    with pytest.raises(CommunityError, match="client_identity_not_allowed"):
        harness.api.publish(
            {
                "source_job_id": job_id,
                "series_slug": "series",
                "publish_consent": True,
                "storage_file_id": "client-selected-drive-id",
            },
            principal=OWNER,
        )


def test_source_processing_policy_alone_does_not_grant_publish(harness):
    job_id, _ = _finished_translation_job(harness.jobs, harness.output_root)

    with pytest.raises(CommunityError, match="publish_consent_required"):
        harness.api.publish(
            {
                "source_job_id": job_id,
                "series_slug": "series",
                "download_authorization": {"allowed": True},
            },
            principal=OWNER,
        )
