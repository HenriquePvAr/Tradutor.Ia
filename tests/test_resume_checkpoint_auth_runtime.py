import json
from pathlib import Path
from types import SimpleNamespace


def test_resume_checkpoint_accepts_new_run_for_same_logical_job():
    from benchmark_pipeline import _checkpoint_identity_matches

    progress = {"job_id": "logical-job", "run_signature": "same"}
    assert _checkpoint_identity_matches(progress, "logical-job", resume=True)


def test_checkpoint_does_not_cross_job_boundaries():
    from benchmark_pipeline import _checkpoint_identity_matches

    progress = {"job_id": "job-a", "pages": []}
    assert not _checkpoint_identity_matches(progress, "job-b", resume=True)
    assert not _checkpoint_identity_matches(progress, "job-b", resume=False)


def test_legacy_checkpoint_is_only_eligible_for_explicit_resume():
    from benchmark_pipeline import _checkpoint_identity_matches

    progress = {"run_signature": "same", "pages": []}
    assert _checkpoint_identity_matches(progress, "logical-job", resume=True)
    assert not _checkpoint_identity_matches(progress, "logical-job", resume=False)


def test_auth_context_uses_explicit_runtime_root(tmp_path):
    from secure_auth_context import AuthEnvelopeStore

    store = AuthEnvelopeStore(Path(tmp_path))
    path = store.seal("logical-job", "a" * 40, user_id="user")
    assert path == Path(tmp_path) / "auth" / "logical-job.auth"
    assert store.acquire("logical-job").access_token == "a" * 40
