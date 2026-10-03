from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_reservation_pricing_is_derived_from_license_plan_and_legacy_amount_ignored():
    # Pricing was already applied remotely under its canonical timestamp;
    # keep the local contract test anchored to that fetched migration.
    sql = (ROOT / "supabase/migrations/20260911045108_server_authoritative_translation_pricing.sql").read_text(encoding="utf-8")
    assert "join public.plans" in sql
    assert "p.yk_cost_per_translation" in sql
    assert "p_amount" in sql and "ignored" in sql
    assert "required_yk" in sql
    assert "insufficient_yk" in sql


def test_wallet_edge_does_not_forward_client_amount():
    source = (ROOT / "supabase/functions/wallet-reserve/index.ts").read_text(encoding="utf-8")
    assert "p_amount" not in source
    assert "p_device_id" in source


def test_claimed_result_rpc_is_server_role_only():
    sql = (ROOT / "supabase/migrations/20260911140000_translation_operation_claim_recovery.sql").read_text(encoding="utf-8")
    assert "persist_translation_result(text,jsonb,uuid) from public,anon,authenticated" in sql
    assert "persist_translation_result(text,jsonb,uuid) to service_role,postgres" in sql
