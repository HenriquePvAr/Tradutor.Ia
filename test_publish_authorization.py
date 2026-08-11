"""Artifact-scoped Community publish authorization contracts."""

import _test_bootstrap  # noqa: F401

from pathlib import Path

from publish_authorization import (
    REASON_ARTIFACT_MISMATCH,
    REASON_CONSENT_GRANTED,
    REASON_CONSENT_REQUIRED,
    REASON_OWNER_MISMATCH,
    REASON_UNAUTHENTICATED,
    PublishArtifactIdentity,
    PublishAuthorizationStore,
)


def _identity(**overrides):
    values = {
        "owner_id": "owner-a",
        "job_id": "job-1",
        "run_id": "run-1",
        "source_url": "https://example.test/chapter/1",
        "artifact_sha256": "a" * 64,
        "artifact_size_bytes": 1234,
    }
    values.update(overrides)
    return PublishArtifactIdentity(**values)


def test_publish_consent_must_be_explicit_and_artifact_scoped(tmp_path: Path):
    store = PublishAuthorizationStore(tmp_path / "jobs.sqlite3")
    try:
        missing = store.grant(_identity(), attested=False)
        assert not missing.allowed
        assert missing.reason_code == REASON_CONSENT_REQUIRED

        granted = store.grant(_identity(), attested=True)
        assert granted.allowed
        assert granted.reason_code == REASON_CONSENT_GRANTED

        current = store.require_current(_identity(), owner_id="owner-a")
        assert current.allowed

        stale = store.require_current(
            _identity(artifact_sha256="b" * 64),
            owner_id="owner-a",
        )
        assert not stale.allowed
        assert stale.reason_code == REASON_ARTIFACT_MISMATCH
    finally:
        store.close()


def test_publish_consent_is_bound_to_authenticated_owner_and_job(tmp_path: Path):
    store = PublishAuthorizationStore(tmp_path / "jobs.sqlite3")
    try:
        assert store.grant(_identity(owner_id=""), attested=True).reason_code == REASON_UNAUTHENTICATED

        granted = store.grant(_identity(), attested=True)
        assert granted.allowed

        wrong_owner = store.require_current(_identity(), owner_id="owner-b")
        assert not wrong_owner.allowed
        assert wrong_owner.reason_code == REASON_OWNER_MISMATCH

        wrong_job = store.require_current(_identity(job_id="job-2"), owner_id="owner-a")
        assert not wrong_job.allowed
    finally:
        store.close()
