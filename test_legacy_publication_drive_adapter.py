from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import dataclasses
import hashlib
import json
import re
import socket
from pathlib import Path
from typing import Any

import pytest

from legacy_publication_migrator import load_migration_input, plan_migration
from legacy_publication_migration_executor import (
    ConflictingStorageObjectError,
    FakeLegacyProvenanceBackend,
    FakeLegacyTargetBackend,
    LegacyMigrationExecutor,
    ResponseLostError,
    TerminalBackendError,
    TransientBackendError,
    ValidationError,
)
from legacy_publication_drive_adapter import (
    APP_PROP_IDEMPOTENCY,
    APP_PROP_SCHEMA,
    APP_PROP_SHA256,
    MAX_CREATE_DEADLINE_SECONDS,
    MIN_CREATE_DEADLINE_SECONDS,
    DriveClientError,
    GoogleDriveLegacyArtifactStorage,
    build_idempotency_query,
    derive_idempotency_key,
    escape_drive_query_literal,
    map_drive_error,
    minimum_safe_create_lease_ttl,
    validate_create_deadline_seconds,
)


FOLDER_ID = "TEST-DRIVE-FOLDER-001"
TEST_CREATE_DEADLINE_SECONDS = 60.0
PDF_A = b"%PDF-1.4\n% synthetic fixture\nTEST DRIVE PUBLICATION A\n%%EOF\n"
PDF_B = b"%PDF-1.4\n% synthetic fixture\nTEST DRIVE PUBLICATION B\n%%EOF\n"


# ---------------------------------------------------------------------------
# Fake, in-memory Drive client -- no network, no credentials, no real API.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _DriveObject:
    file_id: str
    name: str
    mime_type: str
    size: int
    parent_id: str
    trashed: bool
    md5_checksum: str
    app_properties: dict[str, str]


_QUERY_RE = re.compile(
    r"key='(?P<key_prop>(?:[^'\\]|\\.)*)' and value='(?P<value>(?:[^'\\]|\\.)*)' \} "
    r"and '(?P<folder>(?:[^'\\]|\\.)*)' in parents"
)


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def parse_idempotency_query(query: str) -> tuple[str, str]:
    """Test-only parser mirroring what a real Drive files.list call would receive."""
    match = _QUERY_RE.search(query)
    if not match:
        raise AssertionError(f"unparseable synthetic query: {query!r}")
    return _unescape(match.group("value")), _unescape(match.group("folder"))


class FakeDriveClient:
    """In-memory Drive Files surface. Zero network, deterministic, inspectable."""

    def __init__(self) -> None:
        self.objects: dict[str, _DriveObject] = {}
        self.create_count = 0
        self.get_count = 0
        self.list_count = 0
        self.bytes_uploaded = 0
        self._next_id = 1
        self._faults: dict[str, list[Exception]] = {}
        self.received_create_deadlines: list[float] = []

    # -- fault injection ---------------------------------------------------
    def inject_fault(self, point: str, exc: Exception, *, times: int = 1) -> None:
        self._faults.setdefault(point, []).extend([exc] * times)

    def _pop_fault(self, point: str) -> Exception | None:
        queue = self._faults.get(point)
        if queue:
            return queue.pop(0)
        return None

    # -- inspection ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "objects": {fid: dataclasses.asdict(obj) for fid, obj in self.objects.items()},
            "create_count": self.create_count,
            "get_count": self.get_count,
            "list_count": self.list_count,
            "bytes_uploaded": self.bytes_uploaded,
        }

    # -- DriveFilesClient protocol ------------------------------------------
    def get_metadata(self, file_id: str, *, fields: tuple[str, ...]) -> dict[str, Any] | None:
        self.get_count += 1
        fault = self._pop_fault("get")
        if fault:
            raise fault
        obj = self.objects.get(file_id)
        if obj is None:
            raise DriveClientError("not_found", category="not_found")
        return self._project(obj, fields)

    def find_by_idempotency(self, *, query: str, fields: tuple[str, ...], page_size: int) -> list[dict[str, Any]]:
        self.list_count += 1
        fault = self._pop_fault("list")
        if fault:
            raise fault
        key, folder_id = parse_idempotency_query(query)
        matches = [
            obj
            for obj in self.objects.values()
            if obj.app_properties.get(APP_PROP_IDEMPOTENCY) == key
            and obj.parent_id == folder_id
            and not obj.trashed
        ]
        return [self._project(obj, fields) for obj in matches[:page_size]]

    def create_pdf(
        self,
        *,
        folder_id: str,
        filename: str,
        data: bytes,
        app_properties: dict[str, str],
        create_deadline_seconds: float,
    ) -> dict[str, Any]:
        # Real client contract requirement (documented, not enforced by this
        # hermetic fake): create_deadline_seconds must be the exact,
        # already-validated value the adapter received at construction --
        # never recomputed, widened, or defaulted here.
        self.received_create_deadlines.append(create_deadline_seconds)
        # "before_create" / "timeout_before_side_effect" are the same point:
        # the provider never accepted/persisted anything -- clean, zero-object
        # failure (deadline contract case A).
        fault = self._pop_fault("before_create") or self._pop_fault("timeout_before_side_effect")
        if fault:
            raise fault
        self.create_count += 1
        self.bytes_uploaded += len(data)
        file_id = f"fake-drive-file-{self._next_id}"
        self._next_id += 1
        size = len(data)
        md5 = hashlib.md5(data).hexdigest()
        if self._pop_fault("metadata_mismatch"):
            size += 1
        obj = _DriveObject(
            file_id=file_id,
            name=filename,
            mime_type="application/pdf",
            size=size,
            parent_id=folder_id,
            trashed=False,
            md5_checksum=md5,
            app_properties=dict(app_properties),
        )
        self.objects[file_id] = obj
        # "after_create_before_response" / "timeout_after_side_effect" /
        # "late_completion_simulation" are all the same point: the object was
        # already durably persisted by the provider before the caller's
        # deadline/response outcome resolved (deadline contract case B --
        # outcome unknown, never assumed to be a clean no-op).
        after = (
            self._pop_fault("after_create_before_response")
            or self._pop_fault("timeout_after_side_effect")
            or self._pop_fault("late_completion_simulation")
        )
        if after:
            raise after
        return self._project(obj, ("id", "name", "mimeType", "size", "parents", "trashed", "md5Checksum", "appProperties"))

    @staticmethod
    def _project(obj: _DriveObject, fields: tuple[str, ...]) -> dict[str, Any]:
        full = {
            "id": obj.file_id,
            "name": obj.name,
            "mimeType": obj.mime_type,
            "size": obj.size,
            "parents": [obj.parent_id],
            "trashed": obj.trashed,
            "md5Checksum": obj.md5_checksum,
            "appProperties": dict(obj.app_properties),
        }
        if not fields:
            return full
        return {key: full[key] for key in fields if key in full}


# ---------------------------------------------------------------------------
# Migration input / plan fixtures (synthetic identities only)
# ---------------------------------------------------------------------------


def _write_pdf(root: Path, name: str, data: bytes) -> dict[str, object]:
    path = root / name
    path.write_bytes(data)
    return {
        "local_artifact_path": name,
        "pdf_sha256": hashlib.sha256(data).hexdigest(),
        "pdf_md5": hashlib.md5(data).hexdigest(),
        "pdf_size": len(data),
    }


def _source(source_instance_id: str) -> dict[str, object]:
    return {
        "source_system": "tradutor_ia_sqlite_synthetic_v1",
        "source_instance_id": source_instance_id,
        "source_schema_version": 3,
        "initial_snapshot_sha256": "a" * 64,
        "initial_logical_fingerprint": "synthetic-logical-fingerprint-drive",
        "manifest_id": "20000000-0000-4000-8000-000000000101",
        "manifest_version": 1,
        "approval_state": "approved",
        "metadata": {"fixture": "synthetic-drive"},
    }


def _published_fixture(root: Path, *, storage_file_id: str = "TEST-DRIVE-REMOTE-001", **overrides: object) -> dict[str, object]:
    artifact = _write_pdf(root, "drive-published.pdf", PDF_A)
    publication = {
        "legacy_publication_id": "TEST-DRIVE-PUB-001",
        "source_instance_id": "10000000-0000-4000-8000-000000000101",
        "source_system": "tradutor_ia_sqlite_synthetic_v1",
        "owner_id": "30000000-0000-4000-8000-000000000101",
        "owner_resolution_state": "resolved",
        "source_job_id": "synthetic-job-drive-published",
        "source_run_id": "40000000-0000-4000-8000-000000000101",
        "migration_mode": "published_copy",
        "requested_target_status": "community",
        "source_record_digest": "digest-drive-published",
        "remote_asset_verification": "strong",
        "remote_asset": {"storage_provider": "google_drive", "storage_file_id": storage_file_id, "verified": True},
        "target": {"work": {"state": "target_absent"}, "chapter": {"state": "target_absent"}},
        "metadata": {"title": "Synthetic Drive Published Title"},
    }
    publication.update(artifact)
    publication.update(overrides)
    return {"schema_version": 1, "source": _source("10000000-0000-4000-8000-000000000101"), "publications": [publication]}


def _draft_fixture(root: Path, **overrides: object) -> dict[str, object]:
    artifact = _write_pdf(root, "drive-recovery.pdf", PDF_B)
    publication = {
        "legacy_publication_id": "TEST-DRIVE-PUB-002",
        "source_instance_id": "10000000-0000-4000-8000-000000000102",
        "source_system": "tradutor_ia_sqlite_synthetic_v1",
        "owner_id": "30000000-0000-4000-8000-000000000102",
        "owner_resolution_state": "resolved",
        "source_job_id": "synthetic-job-drive-recovery",
        "source_run_id": "40000000-0000-4000-8000-000000000102",
        "migration_mode": "draft_recovery",
        "requested_target_status": "draft",
        "source_record_digest": "digest-drive-recovery",
        "remote_asset_verification": "unverified",
        "remote_asset": None,
        "target": {"work": {"state": "target_absent"}, "chapter": {"state": "target_absent"}},
        "metadata": {"title": "Synthetic Drive Recovery Title"},
    }
    publication.update(artifact)
    publication.update(overrides)
    source = _source("10000000-0000-4000-8000-000000000102")
    source["manifest_id"] = "20000000-0000-4000-8000-000000000102"
    return {"schema_version": 1, "source": source, "publications": [publication]}


def _input_and_plan(root: Path, fixture: dict[str, object]):
    fixture_path = root / "drive-fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    migration_input = load_migration_input(fixture_path, artifact_root=root)
    return migration_input, plan_migration(migration_input)


def _published_input_and_plan(root: Path, **overrides: object):
    return _input_and_plan(root, _published_fixture(root, **overrides))


def _draft_input_and_plan(root: Path, **overrides: object):
    return _input_and_plan(root, _draft_fixture(root, **overrides))


def _seed_verified_remote(client: FakeDriveClient, *, file_id: str, sha256: str, size: int, folder_id: str = FOLDER_ID) -> None:
    client.objects[file_id] = _DriveObject(
        file_id=file_id,
        name="preexisting.pdf",
        mime_type="application/pdf",
        size=size,
        parent_id=folder_id,
        trashed=False,
        md5_checksum="deadbeef",
        app_properties={APP_PROP_SCHEMA: "1", APP_PROP_SHA256: sha256},
    )


# ---------------------------------------------------------------------------
# Constructor / dependency injection contract
# ---------------------------------------------------------------------------


def test_constructor_requires_drive_client():
    with pytest.raises(ValueError):
        GoogleDriveLegacyArtifactStorage(drive_client=None, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)  # type: ignore[arg-type]


def test_constructor_requires_explicit_folder_id():
    with pytest.raises(ValueError):
        GoogleDriveLegacyArtifactStorage(drive_client=FakeDriveClient(), folder_id="", create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)


def test_constructor_has_no_env_or_default_fallback(monkeypatch):
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "should-never-be-read")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/should/never/be/read")
    storage = GoogleDriveLegacyArtifactStorage(drive_client=FakeDriveClient(), folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    assert storage._folder_id == FOLDER_ID  # explicit value only, never env


# ---------------------------------------------------------------------------
# No network at import or during unit tests
# ---------------------------------------------------------------------------


def test_no_real_socket_connect_during_adapter_unit_tests(tmp_path: Path):
    client = FakeDriveClient()
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    with pytest.raises(AssertionError):
        # offline_test_guard patches socket.socket.connect to fail closed;
        # this proves the guard is active for this module's test run.
        socket.socket().connect(("example.com", 443))
    storage.upload(migration_input, plan)  # must not touch the network


# ---------------------------------------------------------------------------
# Integrity: SHA-256, size, MD5, exact metadata
# ---------------------------------------------------------------------------


def test_upload_validates_pdf_signature(tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    migration_input.publication.artifact.local_artifact_path.write_bytes(b"NOT-A-PDF")
    storage = GoogleDriveLegacyArtifactStorage(drive_client=FakeDriveClient(), folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    with pytest.raises(ValidationError):
        storage.upload(migration_input, plan)


def test_upload_rehashes_and_rejects_content_changed_after_plan(tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    migration_input.publication.artifact.local_artifact_path.write_bytes(PDF_A)  # swapped after planning
    storage = GoogleDriveLegacyArtifactStorage(drive_client=FakeDriveClient(), folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    with pytest.raises(ValidationError):
        storage.upload(migration_input, plan)


def test_new_upload_persists_sha256_in_app_properties_and_verifies_metadata(tmp_path: Path):
    client = FakeDriveClient()
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    result = storage.upload(migration_input, plan)
    assert storage.upload_count == 1
    assert client.create_count == 1
    assert result["sha256"] == migration_input.publication.artifact.pdf_sha256
    assert result["size"] == migration_input.publication.artifact.pdf_size
    stored = next(iter(client.objects.values()))
    assert stored.app_properties[APP_PROP_SHA256] == migration_input.publication.artifact.pdf_sha256
    assert stored.mime_type == "application/pdf"
    assert stored.parent_id == FOLDER_ID
    assert stored.trashed is False


def test_new_upload_rejects_drive_reported_size_mismatch(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("metadata_mismatch", True)  # sentinel truthy "value", see FakeDriveClient._pop_fault
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    with pytest.raises(TerminalBackendError):
        storage.upload(migration_input, plan)


def test_missing_sha256_app_property_does_not_promote_to_strong(tmp_path: Path):
    client = FakeDriveClient()
    migration_input, plan = _published_input_and_plan(tmp_path, storage_file_id="TEST-DRIVE-LEGACY-NO-PROP")
    client.objects["TEST-DRIVE-LEGACY-NO-PROP"] = _DriveObject(
        file_id="TEST-DRIVE-LEGACY-NO-PROP", name="legacy.pdf", mime_type="application/pdf",
        size=migration_input.publication.artifact.pdf_size, parent_id=FOLDER_ID, trashed=False,
        md5_checksum="whatever", app_properties={},
    )
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    with pytest.raises(ValidationError):
        storage.use_verified_remote_asset(migration_input, plan)


# ---------------------------------------------------------------------------
# Exact metadata lookup / no search by name
# ---------------------------------------------------------------------------


def test_use_verified_remote_asset_does_exact_id_lookup_only(tmp_path: Path):
    client = FakeDriveClient()
    migration_input, plan = _published_input_and_plan(tmp_path, storage_file_id="TEST-DRIVE-REMOTE-EXACT")
    _seed_verified_remote(
        client, file_id="TEST-DRIVE-REMOTE-EXACT",
        sha256=migration_input.publication.artifact.pdf_sha256,
        size=migration_input.publication.artifact.pdf_size,
    )
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    result = storage.use_verified_remote_asset(migration_input, plan)
    assert result["action"] == "reuse_verified_remote_asset"
    assert client.get_count == 1
    assert client.list_count == 0
    assert storage.upload_count == 0


def test_published_copy_strong_asset_never_calls_upload(tmp_path: Path):
    client = FakeDriveClient()
    migration_input, plan = _published_input_and_plan(tmp_path, storage_file_id="TEST-DRIVE-REMOTE-002")
    _seed_verified_remote(
        client, file_id="TEST-DRIVE-REMOTE-002",
        sha256=migration_input.publication.artifact.pdf_sha256,
        size=migration_input.publication.artifact.pdf_size,
    )
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    executor = LegacyMigrationExecutor(
        provenance_backend=FakeLegacyProvenanceBackend(),
        target_backend=FakeLegacyTargetBackend(),
        artifact_storage=storage,
    )
    result = executor.execute(migration_input, plan)
    assert result.execution_status == "completed"
    assert storage.upload_count == 0
    assert client.create_count == 0


# ---------------------------------------------------------------------------
# Idempotency key determinism
# ---------------------------------------------------------------------------


def test_idempotency_key_is_deterministic_and_opaque(tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    key_a = derive_idempotency_key(migration_input, plan)
    key_b = derive_idempotency_key(migration_input, plan)
    assert key_a == key_b
    assert re.fullmatch(r"[0-9a-f]{64}", key_a)
    assert plan.idempotency_key not in key_a  # opaque digest, not the raw identity/title


# ---------------------------------------------------------------------------
# Narrow response-lost recovery lookup
# ---------------------------------------------------------------------------


def test_new_upload_zero_matches_allows_create(tmp_path: Path):
    client = FakeDriveClient()
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    storage.upload(migration_input, plan)
    assert client.list_count == 1
    assert client.create_count == 1


def test_existing_same_content_is_reused_without_new_upload(tmp_path: Path):
    client = FakeDriveClient()
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    storage.upload(migration_input, plan)
    second = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS).upload(migration_input, plan)
    assert second["action"] == "already_exists_same_content"
    assert client.create_count == 1
    assert storage.upload_count == 1


@pytest.mark.parametrize("mutate", ["size", "sha256", "md5", "parent", "mime"])
def test_existing_conflicting_object_fails_closed(tmp_path: Path, mutate: str):
    client = FakeDriveClient()
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    idem_key = derive_idempotency_key(migration_input, plan)
    obj = _DriveObject(
        file_id="fake-drive-file-preexisting",
        name="collide.pdf",
        mime_type="application/pdf",
        size=migration_input.publication.artifact.pdf_size,
        parent_id=FOLDER_ID,
        trashed=False,
        md5_checksum=hashlib.md5(migration_input.publication.artifact.local_artifact_path.read_bytes()).hexdigest(),
        app_properties={APP_PROP_SCHEMA: "1", APP_PROP_SHA256: migration_input.publication.artifact.pdf_sha256, APP_PROP_IDEMPOTENCY: idem_key},
    )
    if mutate == "size":
        obj.size += 1
    elif mutate == "sha256":
        obj.app_properties[APP_PROP_SHA256] = "0" * 64
    elif mutate == "md5":
        obj.md5_checksum = "0" * 32
    elif mutate == "parent":
        obj.parent_id = "OTHER-FOLDER"
    elif mutate == "mime":
        obj.mime_type = "application/octet-stream"
    client.objects[obj.file_id] = obj
    if mutate == "parent":
        # A different parent means the narrow lookup (scoped to FOLDER_ID) won't
        # find it at all, so this is a clean zero-match create, not a conflict.
        result = storage.upload(migration_input, plan)
        assert result["action"] == "drive_upload"
        return
    with pytest.raises(ConflictingStorageObjectError):
        storage.upload(migration_input, plan)


def test_multiple_lookup_matches_is_ambiguous_conflict(tmp_path: Path):
    client = FakeDriveClient()
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    idem_key = derive_idempotency_key(migration_input, plan)
    for i in range(2):
        client.objects[f"dup-{i}"] = _DriveObject(
            file_id=f"dup-{i}", name="dup.pdf", mime_type="application/pdf",
            size=migration_input.publication.artifact.pdf_size, parent_id=FOLDER_ID, trashed=False,
            md5_checksum="x", app_properties={APP_PROP_SCHEMA: "1", APP_PROP_SHA256: migration_input.publication.artifact.pdf_sha256, APP_PROP_IDEMPOTENCY: idem_key},
        )
    with pytest.raises(ConflictingStorageObjectError):
        storage.upload(migration_input, plan)


# ---------------------------------------------------------------------------
# Response lost / retry / restart
# ---------------------------------------------------------------------------


def test_response_lost_after_create_maps_to_response_lost_error(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("after_create_before_response", DriveClientError("lost", category="response_lost"))
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    with pytest.raises(ResponseLostError):
        storage.upload(migration_input, plan)
    assert client.create_count == 1  # the create really happened
    assert len(client.objects) == 1


def test_retry_after_response_lost_reuses_the_persisted_object(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("after_create_before_response", DriveClientError("lost", category="response_lost"))
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    with pytest.raises(ResponseLostError):
        storage.upload(migration_input, plan)
    result = storage.upload(migration_input, plan)  # retry, same adapter
    assert result["action"] == "already_exists_same_content"
    assert client.create_count == 1
    assert len(client.objects) == 1


def test_process_restart_after_response_lost_reuses_single_object(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("after_create_before_response", DriveClientError("lost", category="response_lost"))
    migration_input, plan = _draft_input_and_plan(tmp_path)
    first_storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    with pytest.raises(ResponseLostError):
        first_storage.upload(migration_input, plan)
    del first_storage  # simulate process restart: no adapter-local cache survives
    restarted_storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    result = restarted_storage.upload(migration_input, plan)
    assert result["action"] == "already_exists_same_content"
    assert client.create_count == 1
    assert len(client.objects) == 1


def test_executor_resume_after_response_lost_completes_recovery(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("after_create_before_response", DriveClientError("lost", category="response_lost"))
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    targets = FakeLegacyTargetBackend()
    executor = LegacyMigrationExecutor(provenance_backend=provenance, target_backend=targets, artifact_storage=storage, max_attempts=1)
    first = executor.execute(migration_input, plan)
    assert first.execution_status == "incomplete_resume_required"
    assert first.retryable is True
    second = executor.resume(migration_input, plan, first.resume_state)
    assert second.execution_status == "completed"
    # The create genuinely happened before the response was lost, so the retry
    # reuses it (upload_count only counts confirmed-new uploads); the object
    # count on the client is what proves no duplicate was made.
    assert storage.upload_count == 0
    assert client.create_count == 1
    assert len(client.objects) == 1


# ---------------------------------------------------------------------------
# Hybrid end-to-end
# ---------------------------------------------------------------------------


def test_draft_recovery_hybrid_e2e_uploads_once_and_stays_draft(tmp_path: Path):
    client = FakeDriveClient()
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    executor = LegacyMigrationExecutor(
        provenance_backend=FakeLegacyProvenanceBackend(),
        target_backend=FakeLegacyTargetBackend(),
        artifact_storage=storage,
    )
    result = executor.execute(migration_input, plan)
    assert result.execution_status == "completed"
    assert result.final_migration_state == "recovery_completed"
    assert result.final_target_state == "draft"
    assert storage.upload_count == 1
    assert client.create_count == 1
    assert len(client.objects) == 1
    stored = next(iter(client.objects.values()))
    assert stored.app_properties[APP_PROP_SHA256] == migration_input.publication.artifact.pdf_sha256


# ---------------------------------------------------------------------------
# Query escaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "plain-hex-key",
        "value'with'quotes",
        "value\\with\\backslashes",
        "value\nwith\nnewline",
        "unicode-éção",
        "and or parens (){}",
        "trailing-backslash\\",
    ],
)
def test_query_escaping_preserves_structure_for_synthetic_values(raw: str):
    query = build_idempotency_query("SOME-FOLDER", raw)
    key, folder = parse_idempotency_query(query)
    assert key == raw
    assert folder == "SOME-FOLDER"


def test_escape_helper_doubles_backslash_and_escapes_quote():
    assert escape_drive_query_literal("a'b\\c") == "a\\'b\\\\c"


# ---------------------------------------------------------------------------
# No overwrite / no delete (static contract, not just behavioral)
# ---------------------------------------------------------------------------


def test_adapter_exposes_no_update_or_delete_capability():
    for forbidden in ("update", "delete", "trash", "overwrite", "move"):
        assert not hasattr(GoogleDriveLegacyArtifactStorage, forbidden)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category,expected_type",
    [
        ("transient", TransientBackendError),
        ("response_lost", ResponseLostError),
        ("authentication", TerminalBackendError),
        ("permission", TerminalBackendError),
        ("not_found", TerminalBackendError),
        ("validation", ValidationError),
        ("unknown", TerminalBackendError),
    ],
)
def test_map_drive_error_categories(category: str, expected_type: type):
    mapped = map_drive_error(DriveClientError("boom", category=category))
    assert isinstance(mapped, expected_type)


def test_transient_error_during_lookup_is_retryable_by_executor(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("list", DriveClientError("429", category="transient"), times=1)
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    executor = LegacyMigrationExecutor(
        provenance_backend=FakeLegacyProvenanceBackend(),
        target_backend=FakeLegacyTargetBackend(),
        artifact_storage=storage,
        max_attempts=3,
    )
    result = executor.execute(migration_input, plan)
    assert result.execution_status == "completed"
    assert storage.upload_count == 1


def test_unknown_error_fails_closed_not_retried_forever(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("get", DriveClientError("weird", category="unknown"))
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _published_input_and_plan(tmp_path)
    with pytest.raises(TerminalBackendError):
        storage.use_verified_remote_asset(migration_input, plan)


# ---------------------------------------------------------------------------
# Concurrency contract audit (documented gap, not a false guarantee)
# ---------------------------------------------------------------------------


def test_concurrent_narrow_lookup_race_can_duplicate_without_external_lock(tmp_path: Path):
    """Demonstrates why Drive alone cannot provide the uniqueness guarantee.

    A single adapter instance never races itself: upload() always does its
    lookup immediately before its own create, so within one process a retry
    correctly finds what a prior attempt made. The gap is across processes:
    two workers can each independently observe zero matches (via the exact
    same narrow, folder+appProperty-scoped query) before either one's create
    is visible to the other, because neither Drive nor this adapter provides
    a check-and-set primitive spanning that gap. This test simulates that by
    calling the client directly the way two racing workers would, bypassing
    the single adapter's internal lookup-then-create coupling. This is the
    storage_concurrency_gap documented in
    .runtime/legacy-drive-storage-adapter/concurrency_contract.md: nothing in
    this adapter (or in claim_legacy_publication's autocommit connection)
    holds a lock across the network round trip to Drive.
    """
    client = FakeDriveClient()
    migration_input, plan = _draft_input_and_plan(tmp_path)
    idem_key = derive_idempotency_key(migration_input, plan)
    query = build_idempotency_query(FOLDER_ID, idem_key)
    data = migration_input.publication.artifact.local_artifact_path.read_bytes()
    sha256 = migration_input.publication.artifact.pdf_sha256
    app_properties = {APP_PROP_SCHEMA: "1", APP_PROP_SHA256: sha256, APP_PROP_IDEMPOTENCY: idem_key}

    # Both workers observe zero matches before either commits a create --
    # exactly what would happen if their two create() calls interleaved on a
    # real, unlocked Drive folder.
    assert client.find_by_idempotency(query=query, fields=(), page_size=3) == []
    assert client.find_by_idempotency(query=query, fields=(), page_size=3) == []

    client.create_pdf(
        folder_id=FOLDER_ID, filename="worker-a.pdf", data=data, app_properties=app_properties,
        create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS,
    )
    client.create_pdf(
        folder_id=FOLDER_ID, filename="worker-b.pdf", data=data, app_properties=app_properties,
        create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS,
    )

    assert client.create_count == 2  # the gap: two distinct objects, same idempotency key
    assert len({obj.file_id for obj in client.objects.values()}) == 2


# ---------------------------------------------------------------------------
# Hard create deadline contract (storage create deadline, NOT reservation
# lease TTL -- see legacy_publication_drive_adapter.py module docstring and
# .runtime/legacy-storage-reservation-design/ttl_and_timeout.md)
# ---------------------------------------------------------------------------


def test_min_max_bounds_are_finite_positive_and_ordered():
    assert 0 < MIN_CREATE_DEADLINE_SECONDS < MAX_CREATE_DEADLINE_SECONDS


@pytest.mark.parametrize(
    "invalid",
    [None, 0, 0.0, -1, float("nan"), float("inf"), float("-inf"), "30", True, MAX_CREATE_DEADLINE_SECONDS + 1, MIN_CREATE_DEADLINE_SECONDS - 0.01],
)
def test_construction_rejects_invalid_create_deadline(invalid: object):
    client = FakeDriveClient()
    with pytest.raises(ValueError):
        GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=invalid)  # type: ignore[arg-type]
    assert client.create_count == 0  # rejected before any create is ever attempted


def test_construction_requires_create_deadline_argument():
    with pytest.raises(TypeError):
        GoogleDriveLegacyArtifactStorage(drive_client=FakeDriveClient(), folder_id=FOLDER_ID)  # type: ignore[call-arg]


@pytest.mark.parametrize("valid", [MIN_CREATE_DEADLINE_SECONDS, MAX_CREATE_DEADLINE_SECONDS, (MIN_CREATE_DEADLINE_SECONDS + MAX_CREATE_DEADLINE_SECONDS) / 2])
def test_construction_accepts_boundary_and_mid_range_deadlines(tmp_path: Path, valid: float):
    client = FakeDriveClient()
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=valid)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    storage.upload(migration_input, plan)
    assert client.received_create_deadlines == [valid]


def test_create_pdf_receives_exact_validated_deadline(tmp_path: Path):
    client = FakeDriveClient()
    chosen = 77.0
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=chosen)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    storage.upload(migration_input, plan)
    assert client.received_create_deadlines == [chosen]


def test_publication_input_cannot_override_create_deadline(tmp_path: Path):
    client = FakeDriveClient()
    configured = 90.0
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=configured)
    # An attacker-controlled or merely careless legacy manifest cannot smuggle
    # its own timeout in through publication metadata: this is infra policy,
    # never publication/manifest input.
    migration_input, plan = _draft_input_and_plan(tmp_path, metadata={"title": "x", "timeout": 1, "create_deadline_seconds": 1})
    storage.upload(migration_input, plan)
    assert client.received_create_deadlines == [configured]


def test_validate_create_deadline_seconds_helper_matches_constructor_contract():
    assert validate_create_deadline_seconds(MIN_CREATE_DEADLINE_SECONDS) == MIN_CREATE_DEADLINE_SECONDS
    for invalid in (None, 0, -5, float("nan"), float("inf"), MAX_CREATE_DEADLINE_SECONDS + 1):
        with pytest.raises(ValueError):
            validate_create_deadline_seconds(invalid)


def test_timeout_before_side_effect_is_transient_and_creates_nothing(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("timeout_before_side_effect", DriveClientError("deadline_exceeded_before_accept", category="transient"))
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    with pytest.raises(TransientBackendError):
        storage.upload(migration_input, plan)
    assert client.create_count == 0
    assert len(client.objects) == 0


def test_timeout_after_side_effect_is_response_lost_and_object_persists(tmp_path: Path):
    client = FakeDriveClient()
    client.inject_fault("timeout_after_side_effect", DriveClientError("deadline_exceeded_outcome_unknown", category="response_lost"))
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    with pytest.raises(ResponseLostError):
        storage.upload(migration_input, plan)
    assert client.create_count == 1
    assert len(client.objects) == 1
    recovered = storage.upload(migration_input, plan)  # narrow recovery, no duplicate
    assert recovered["action"] == "already_exists_same_content"
    assert client.create_count == 1
    assert len(client.objects) == 1


def test_late_completion_after_caller_deadline_is_not_treated_as_safe_create(tmp_path: Path):
    """A caller-side deadline elapsing does NOT mean the provider stopped.

    This models 'late completion': the fake provider still durably persists
    the object -- exactly like a real resumable/multipart Drive upload that
    keeps running server-side after the HTTP client gives up -- and this
    adapter must surface that as an unknown/response-lost outcome (recovery
    required), never as a plain retryable no-op that would risk a duplicate
    create on blind retry.
    """
    client = FakeDriveClient()
    client.inject_fault("late_completion_simulation", DriveClientError("late_completion", category="response_lost"))
    storage = GoogleDriveLegacyArtifactStorage(drive_client=client, folder_id=FOLDER_ID, create_deadline_seconds=TEST_CREATE_DEADLINE_SECONDS)
    migration_input, plan = _draft_input_and_plan(tmp_path)
    with pytest.raises(ResponseLostError):
        storage.upload(migration_input, plan)
    # The object exists despite the caller having already received an error --
    # proof that "caller gave up" != "provider cancelled the side effect".
    assert client.create_count == 1
    assert len(client.objects) == 1


def test_minimum_safe_create_lease_ttl_exceeds_deadline_plus_margin():
    ttl = minimum_safe_create_lease_ttl(TEST_CREATE_DEADLINE_SECONDS, safety_margin_seconds=15.0)
    assert ttl > TEST_CREATE_DEADLINE_SECONDS + 15.0 - 1e-9
    assert ttl == TEST_CREATE_DEADLINE_SECONDS + 15.0
    with pytest.raises(ValueError):
        minimum_safe_create_lease_ttl(TEST_CREATE_DEADLINE_SECONDS, safety_margin_seconds=0)
