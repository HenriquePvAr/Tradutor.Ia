"""Static safeguards for the local-only community publication artifact migration."""

import _test_bootstrap  # noqa: F401

from pathlib import Path


ROOT = Path(__file__).resolve().parent
MIGRATION = ROOT / "supabase" / "migrations" / "20260811120000_community_publication_artifacts.sql"
BACKEND_ACCESS_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260811130000_community_publication_metadata_backend_access.sql"
)
STRUCTURE_TEST = ROOT / "supabase" / "tests" / "database" / "00_structure_test.sql"
RLS_TEST = ROOT / "supabase" / "tests" / "database" / "01_rls_policies_test.sql"


def test_publication_artifact_migration_is_private_and_fail_closed():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table if not exists private.community_publication_artifacts" in sql
    assert "storage_reference text" in sql
    assert "references public.chapters (id)" in sql
    assert "references public.profiles (id)" in sql
    assert "alter table private.community_publication_artifacts enable row level security" in sql
    assert "revoke all on private.community_publication_artifacts from anon, authenticated" in sql
    assert "create policy" not in sql
    assert "security definer" not in sql
    assert "storage_provider in ('google_drive')" in sql


def test_publication_artifact_pgtap_contract_covers_structure_and_rls_denial():
    structure = STRUCTURE_TEST.read_text(encoding="utf-8")
    rls = RLS_TEST.read_text(encoding="utf-8")
    assert "private.community_publication_artifacts exists" in structure
    assert "private.community_publication_artifacts exposes no policy" in structure
    assert "community_pub_artifacts_owner_hash_idx" in structure
    assert "community_pub_artifacts_storage_ref_idx" in structure
    assert "storage_reference is not exposed in public schema" in structure
    assert "authenticated cannot read private publication artifact storage_reference" in rls
    assert "authenticated cannot mutate private publication artifact state" in rls
    assert "anon cannot read private publication artifact storage_reference" in rls


def test_backend_access_migration_exposes_only_hardened_service_role_rpcs():
    sql = BACKEND_ACCESS_MIGRATION.read_text(encoding="utf-8").lower()
    for function_name in (
        "public.reserve_community_publication_artifact",
        "public.record_community_publication_storage",
        "public.finalize_community_publication_artifact",
        "public.mark_community_publication_artifact_failure",
    ):
        assert f"create or replace function {function_name}" in sql
        assert "security definer" in sql
        assert "set search_path = ''" in sql
        assert f"revoke all on function {function_name}" in sql
        assert f"revoke execute on function {function_name}" in sql
        assert f"grant execute on function {function_name}" in sql
    assert "grant select" not in sql
    assert "grant insert" not in sql
    assert "grant update" not in sql
    assert "grant delete" not in sql
    assert "on private.community_publication_artifacts to service_role" not in sql
    assert "execute format" not in sql
    assert "execute immediate" not in sql
    assert "from public" in sql
    assert "from anon" in sql
    assert "from authenticated" in sql
    assert "to service_role" in sql
    assert "join public.works" in sql
    assert "for update" in sql
