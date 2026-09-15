from pathlib import Path


EDGE = Path("supabase/functions/wallet-reserve/index.ts").read_text(encoding="utf-8")
RUNNER = Path("job_runner.py").read_text(encoding="utf-8")
PROVIDER = Path("yomu_backend_provider.py").read_text(encoding="utf-8")


def test_wallet_reserve_preserves_known_codes_and_normalizes_unknowns():
    assert "insufficient_yk" in EDGE
    assert "device_not_found" in EDGE
    assert "license_not_found" in EDGE
    assert "pricing_unavailable" in EDGE
    assert "wallet_reserve_rpc_failed" in EDGE
    assert "normalized === \"insufficient_yk\" ? 409 : 503" in EDGE
    assert "json({code: normalized}" in EDGE


def test_job_runner_propagates_verified_device_uuid_to_child_environment():
    assert 'env["TRADUTOR_DEVICE_UUID"]' in RUNNER
    assert 'configuration.get("device_uuid")' in RUNNER


def test_translation_execute_payload_contains_device_uuid_and_reservation():
    assert '"device_id": device_id' in PROVIDER
    assert '"reservation_id": reservation_id' in PROVIDER


def test_reservation_failure_policy_is_explicit_and_fail_safe():
    from yomu_backend_provider import ReservationOutcomeClass, classify_reservation_outcome

    assert classify_reservation_outcome("invalid_translation_request") is ReservationOutcomeClass.PRE_PROVIDER_DEFINITE_FAILURE
    assert classify_reservation_outcome("provider_outcome_unknown", outcome_unknown=True) is ReservationOutcomeClass.PROVIDER_STARTED_OR_OUTCOME_UNKNOWN
    assert classify_reservation_outcome("provider_timeout", provider_started=True) is ReservationOutcomeClass.PROVIDER_STARTED_OR_OUTCOME_UNKNOWN
    assert classify_reservation_outcome("transport_failed") is ReservationOutcomeClass.PROVIDER_NOT_STARTED_UNCERTAIN


def test_translation_commit_contract_is_canonical():
    """The canonical contract is chapter-level since 20260915013128.

    A provider batch commits with commit_translation_batch_success and writes no
    ledger row; the chapter settles once through finalize_translation_job.  The
    per-batch commit_translation_success path is no longer the canonical one.
    """
    from pathlib import Path
    migration = Path("supabase/migrations/20260915013128_chapter_level_translation_finalization.sql").read_text(encoding="utf-8")
    edge = Path("supabase/functions/translation-execute/index.ts").read_text(encoding="utf-8")
    assert "create or replace function public.commit_translation_batch_success(" in migration
    assert "create or replace function public.finalize_translation_job(" in migration
    assert "p_result jsonb" in migration
    assert "p_result: result" in edge
    assert "p_claim_token: claimToken" in edge
    assert '"commit_translation_batch_success"' in edge
    assert '"finalize_translation_job"' in edge
    # The batch commit must not debit: only the chapter finalizer touches yk_ledger.
    batch = migration.split("create or replace function public.commit_translation_batch_success(")[1]
    batch = batch.split("create or replace function public.finalize_translation_job(")[0]
    assert "yk_ledger" not in batch
