from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import dataclasses
import hashlib
import json
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from legacy_publication_migration_executor import (
    ConflictError,
    FakeLegacyArtifactStorage,
    FakeLegacyTargetBackend,
    LegacyMigrationExecutor,
    ResponseLostError,
    TerminalBackendError,
    TransientBackendError,
    ValidationError,
)
from legacy_publication_migrator import load_migration_input, plan_migration
from legacy_publication_postgres_adapter import (
    ALLOWED_RUNTIME_FUNCTIONS,
    FORBIDDEN_OPERATIONAL_FUNCTIONS,
    LocalPostgresConnectionSettings,
    PostgresLegacyProvenanceBackend,
    RefuseRemotePostgresHostError,
    map_postgres_error,
)
from test_legacy_publication_migrator_dry_run import _draft_fixture, _published_fixture


POSTGRES_IMAGE = "postgres:15-alpine"
MIGRATIONS = [
    "20260716120000_social_schema.sql",
    "20260716120100_social_rls.sql",
    "20260717120000_fix_chapter_read_policy.sql",
    "20260722120000_public_profile_identity.sql",
    "20260806120000_legacy_publication_provenance.sql",
    "20260806130000_legacy_publication_migration_runtime.sql",
]


def _docker_available() -> bool:
    return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True).returncode == 0


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _postgres_container():
    if not _docker_available():
        pytest.skip("Docker daemon is not available for local PostgreSQL adapter integration tests")
    port = _free_local_port()
    name = f"tradutor-legacy-adapter-{uuid.uuid4().hex[:12]}"
    local_password = f"local-{uuid.uuid4().hex}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={local_password}",
            "-e",
            "POSTGRES_DB=tradutor_local_adapter",
            "-p",
            f"127.0.0.1:{port}:5432",
            POSTGRES_IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"local postgres container unavailable: {run.stderr.strip()}")
    try:
        settings = LocalPostgresConnectionSettings(
            host="127.0.0.1",
            port=port,
            dbname="tradutor_local_adapter",
            user="postgres",
            password=local_password,
        )
        deadline = time.time() + 45
        while True:
            try:
                with psycopg.connect(settings.dsn, autocommit=True) as conn:
                    conn.execute("select 1")
                    break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        yield settings
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


@pytest.fixture(scope="session")
def local_pg_settings():
    with _postgres_container() as settings:
        _bootstrap_database(settings)
        yield settings


@pytest.fixture()
def db(local_pg_settings):
    _truncate_runtime_tables(local_pg_settings)
    yield local_pg_settings
    _truncate_runtime_tables(local_pg_settings)


def admin(settings: LocalPostgresConnectionSettings):
    return psycopg.connect(settings.dsn, autocommit=True)


def _bootstrap_database(settings: LocalPostgresConnectionSettings) -> None:
    with admin(settings) as conn:
        conn.execute("create extension if not exists pgcrypto")
        conn.execute("create schema if not exists auth")
        conn.execute(
            """
            create or replace function auth.uid()
            returns uuid
            language sql
            stable
            as $$
                select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
            $$
            """
        )
        conn.execute(
            """
            create table if not exists auth.users (
                id uuid primary key,
                email text,
                created_at timestamptz not null default now()
            )
            """
        )
        for role in ("anon", "authenticated", "service_role"):
            conn.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{role}') then create role {role} noinherit nologin; end if; end $$")
        for migration in MIGRATIONS:
            conn.execute(Path("supabase/migrations", migration).read_text(encoding="utf-8"))


def _truncate_runtime_tables(settings: LocalPostgresConnectionSettings) -> None:
    with admin(settings) as conn:
        conn.execute(
            """
            truncate table
                private.legacy_publication_migration_events,
                private.legacy_publication_migrations,
                private.legacy_migration_sources,
                private.chapter_assets,
                public.chapters,
                public.works,
                public.profiles,
                auth.users
            cascade
            """
        )


def _input_and_plan(root: Path, fixture: dict[str, object]):
    root.mkdir(parents=True, exist_ok=True)
    fixture_path = root / "pg-fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    migration_input = load_migration_input(fixture_path, artifact_root=root)
    return migration_input, plan_migration(migration_input)


def _published_input_and_plan(root: Path, **overrides: object):
    overrides.setdefault("legacy_publication_id", "TEST-PUB-PG-001")
    return _input_and_plan(root, _published_fixture(root, **overrides))


def _draft_input_and_plan(root: Path, **overrides: object):
    overrides.setdefault("legacy_publication_id", "TEST-PUB-PG-002")
    return _input_and_plan(root, _draft_fixture(root, **overrides))


def _adapter(settings: LocalPostgresConnectionSettings, *, response_lost: set[str] | None = None):
    return PostgresLegacyProvenanceBackend(settings, response_lost_after_commit=response_lost or set())


def _operational_session_settings(settings: LocalPostgresConnectionSettings) -> LocalPostgresConnectionSettings:
    password = f"audit-{uuid.uuid4().hex}"
    with admin(settings) as conn:
        conn.execute("drop role if exists migration_adapter_session")
        conn.execute(
            sql.SQL(
                "create role migration_adapter_session login password {} "
                "nosuperuser nocreatedb nocreaterole nobypassrls noinherit"
            ).format(sql.Literal(password))
        )
        conn.execute("grant migration_operator to migration_adapter_session")
    return dataclasses.replace(settings, user="migration_adapter_session", password=password)


def _seed_targets(settings: LocalPostgresConnectionSettings, migration_input, plan, *, include_asset: bool = True) -> tuple[str, str]:
    work_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{plan.plan_digest}:work"))
    chapter_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{plan.plan_digest}:chapter"))
    owner_id = migration_input.publication.owner_id
    status = "draft" if migration_input.publication.migration_mode == "draft_recovery" else migration_input.publication.requested_target_status
    with admin(settings) as conn:
        conn.execute("insert into auth.users (id, email) values (%s, %s) on conflict do nothing", (owner_id, "local-adapter@example.test"))
        conn.execute("insert into public.profiles (id, username) values (%s, %s) on conflict do nothing", (owner_id, f"u{owner_id[:8]}"))
        conn.execute(
            "insert into public.works (id, owner_id, title, slug, status) values (%s, %s, %s, %s, %s) on conflict do nothing",
            (work_id, owner_id, "Synthetic Local PG Work", f"w-{work_id[:8]}", status),
        )
        conn.execute(
            "insert into public.chapters (id, work_id, chapter_number, title, status) values (%s, %s, %s, %s, %s) on conflict do nothing",
            (chapter_id, work_id, 1, "Synthetic Local PG Chapter", status),
        )
        if include_asset:
            artifact = migration_input.publication.artifact
            remote_asset = migration_input.publication.remote_asset or {}
            storage_provider = str(remote_asset.get("storage_provider") or "google_drive")
            storage_file_id = str(remote_asset.get("storage_file_id") or f"TEST-STORAGE-{plan.plan_digest[:16]}")
            conn.execute(
                """
                insert into private.chapter_assets (chapter_id, storage_provider, storage_file_id, byte_size, checksum_sha256)
                values (%s, %s, %s, %s, %s)
                on conflict (chapter_id) do nothing
                """,
                (chapter_id, storage_provider, storage_file_id, artifact.pdf_size, artifact.pdf_sha256),
            )
    return work_id, chapter_id


def _seed_owner(settings: LocalPostgresConnectionSettings, migration_input) -> None:
    owner_id = migration_input.publication.owner_id
    with admin(settings) as conn:
        conn.execute("insert into auth.users (id, email) values (%s, %s) on conflict do nothing", (owner_id, "local-adapter@example.test"))


def _counts(settings: LocalPostgresConnectionSettings) -> dict[str, int]:
    with admin(settings) as conn:
        row = conn.execute(
            """
            select
                (select count(*) from private.legacy_migration_sources),
                (select count(*) from private.legacy_publication_migrations),
                (select count(*) from private.legacy_publication_migration_events),
                (select count(*) from public.works),
                (select count(*) from public.chapters),
                (select count(*) from private.chapter_assets)
            """
        ).fetchone()
    return {"sources": row[0], "migrations": row[1], "events": row[2], "works": row[3], "chapters": row[4], "assets": row[5]}


def _migration_row(settings: LocalPostgresConnectionSettings) -> dict[str, Any]:
    with admin(settings) as conn:
        row = conn.execute(
            """
            select id::text, migration_state, migration_mode, target_work_id::text,
                   target_chapter_id::text, target_chapter_asset_id::text
            from private.legacy_publication_migrations
            order by created_at desc
            limit 1
            """
        ).fetchone()
    return {"migration_id": row[0], "migration_state": row[1], "migration_mode": row[2], "target_work_id": row[3], "target_chapter_id": row[4], "target_chapter_asset_id": row[5]}


def test_sql_allowlist_and_no_admin_authorization_surface():
    assert set(ALLOWED_RUNTIME_FUNCTIONS) == {
        "register_legacy_source",
        "claim_legacy_publication",
        "attach_migration_work",
        "attach_migration_chapter",
        "attach_migration_asset",
        "mark_migration_failure",
        "complete_legacy_migration",
        "append_legacy_migration_event",
    }
    assert "authorize_draft_recovery_publication" in FORBIDDEN_OPERATIONAL_FUNCTIONS
    assert "authorize_draft_recovery_publication" not in ALLOWED_RUNTIME_FUNCTIONS
    assert not hasattr(PostgresLegacyProvenanceBackend, "authorize_draft_recovery_publication")


def test_local_host_guard_rejects_remote_hosts():
    for host in (
        "db.example.supabase.co",
        "aws-0-us-east-1.pooler.supabase.com",
        "8.8.8.8",
        "example.com",
        "127.0.0.2",
        "127.1.2.3",
        "0.0.0.0",
        "::ffff:127.0.0.1",
        "localhost.evil.com",
        "foo.localhost.evil.com",
        "evil-localhost",
        "2606:4700:4700::1111",
    ):
        with pytest.raises(RefuseRemotePostgresHostError):
            LocalPostgresConnectionSettings(host=host, port=5432, dbname="postgres", user="postgres", password=None)


def test_local_host_guard_accepts_only_phase_allowlist():
    for host in ("localhost", "127.0.0.1", "::1"):
        settings = LocalPostgresConnectionSettings(host=host, port=5432, dbname="postgres", user="postgres", password=None)
        assert settings.host == host


def test_parameter_binding_and_zero_direct_dml_are_static():
    assert all("%s" in sql for sql in ALLOWED_RUNTIME_FUNCTIONS.values())
    production = Path("legacy_publication_postgres_adapter.py").read_text(encoding="utf-8")
    forbidden = [
        "insert into private.legacy_migration_sources",
        "insert into private.legacy_publication_migrations",
        "insert into private.legacy_publication_migration_events",
        "insert into public.works",
        "insert into public.chapters",
        "insert into private.chapter_assets",
        "update private.",
        "delete from private.",
        "authorize_draft_recovery_publication(",
    ]
    lowered = production.lower()
    assert not any(fragment in lowered for fragment in forbidden)
    assert "_admin_read" not in production
    assert "_migration_by_id" not in production
    assert "getattr(" not in production
    assert ".format(" not in production


def test_non_superuser_session_executes_full_flow_without_private_table_reads(db, tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    work_id, chapter_id = _seed_targets(db, migration_input, plan, include_asset=True)
    op_settings = _operational_session_settings(db)
    adapter = _adapter(op_settings)

    with psycopg.connect(op_settings.dsn, autocommit=True) as conn:
        before = conn.execute("select session_user, current_user").fetchone()
        conn.execute("set role migration_operator")
        after = conn.execute("select session_user, current_user").fetchone()
    assert before == ("migration_adapter_session", "migration_adapter_session")
    assert after == ("migration_adapter_session", "migration_operator")

    source = adapter.register_legacy_source(migration_input, plan)
    assert source["source_instance_id"] == migration_input.source.source_instance_id
    migration = adapter.claim_legacy_publication(migration_input, plan)
    assert migration["migration_id"]
    assert migration["result"] == "created"
    adapter.attach_migration_work(migration["migration_id"], work_id, plan)
    adapter.attach_migration_chapter(migration["migration_id"], chapter_id, plan)
    asset = {
        "storage_provider": "google_drive",
        "storage_file_id": migration_input.publication.remote_asset["storage_file_id"],
        "size": migration_input.publication.artifact.pdf_size,
        "sha256": migration_input.publication.artifact.pdf_sha256,
    }
    adapter.attach_migration_asset(migration["migration_id"], chapter_id, asset, plan)
    event_id = adapter.append_legacy_migration_event(migration["migration_id"], "migration_planned", {"safe": True})
    assert uuid.UUID(event_id)
    completed = adapter.complete_legacy_migration(migration["migration_id"], "local-test", plan)
    assert completed["migration_state"] == "completed"

    with psycopg.connect(op_settings.dsn, autocommit=True) as conn:
        conn.execute("set role migration_operator")
        with pytest.raises(psycopg.Error) as denied:
            conn.execute("select count(*) from private.legacy_publication_migrations")
        assert denied.value.sqlstate == "42501"


@pytest.mark.parametrize(
    "sqlstate,expected",
    [
        ("23505", ConflictError),
        ("23503", ValidationError),
        ("23514", ValidationError),
        ("22023", ValidationError),
        ("42501", ValidationError),
        ("08006", TransientBackendError),
        ("40001", TransientBackendError),
        ("XX999", TerminalBackendError),
    ],
)
def test_error_mapping(sqlstate: str, expected: type[Exception]):
    class FakeError(Exception):
        pass

    err = FakeError("synthetic")
    err.sqlstate = sqlstate
    mapped = map_postgres_error(err)
    assert isinstance(mapped, expected)


def test_db_locality_proof_and_migrations_applied(db):
    proof = _adapter(db).locality_proof()
    assert proof["host"] in {"127.0.0.1", "localhost", "::1"}
    assert proof["current_database"] == "tradutor_local_adapter"
    assert proof["pg_is_in_recovery"] is False
    contract = _adapter(db).runtime_contract()
    assert {"migration_operator", "migration_publication_admin"} <= set(contract["roles"])
    assert set(contract["runtime_functions"]) >= set(ALLOWED_RUNTIME_FUNCTIONS)


def test_role_contract_operator_has_only_operational_execute_and_no_direct_dml(db):
    contract = _adapter(db).runtime_contract()
    assert set(contract["operator_executable_functions"]) == set(ALLOWED_RUNTIME_FUNCTIONS)
    assert "authorize_draft_recovery_publication" not in contract["operator_executable_functions"]
    with admin(db) as conn:
        with pytest.raises(psycopg.Error) as denied:
            conn.execute("set role migration_operator")
            conn.execute("insert into private.legacy_migration_sources (source_instance_id, source_system, source_schema_version, initial_snapshot_sha256, initial_logical_fingerprint, registered_by, manifest_id) values (gen_random_uuid(), 'x', 1, repeat('a',64), repeat('b',64), 'operator-direct-dml-test', gen_random_uuid())")
    assert denied.value.sqlstate == "42501"


def test_operator_cannot_execute_admin_authorization_function(db, tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    _seed_targets(db, migration_input, plan, include_asset=True)
    adapter = _adapter(db)
    adapter.register_legacy_source(migration_input, plan)
    migration = adapter.claim_legacy_publication(migration_input, plan)
    with admin(db) as conn:
        with pytest.raises(psycopg.Error) as denied:
            conn.execute("set role migration_operator")
            conn.execute(
                "select private.authorize_draft_recovery_publication(%s,%s,%s,%s,%s,%s)",
                (migration["migration_id"], migration["migration_id"], "a" * 64, None, "recovery_completed", "nope"),
            )
    assert denied.value.sqlstate == "42501"


def test_register_source_idempotent_response_lost_and_conflict(db, tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    adapter = _adapter(db)
    first = adapter.register_legacy_source(migration_input, plan)
    second = adapter.register_legacy_source(migration_input, plan)
    assert first == second
    assert _counts(db)["sources"] == 1

    _truncate_runtime_tables(db)
    lost = _adapter(db, response_lost={"register_legacy_source"})
    with pytest.raises(ResponseLostError):
        lost.register_legacy_source(migration_input, plan)
    assert _adapter(db).register_legacy_source(migration_input, plan)["source_instance_id"] == migration_input.source.source_instance_id
    assert _counts(db)["sources"] == 1

    changed = dataclasses.replace(migration_input.source, initial_snapshot_sha256="b" * 64)
    changed_input = dataclasses.replace(migration_input, source=changed)
    with pytest.raises(ConflictError):
        _adapter(db).register_legacy_source(changed_input, plan)
    assert _counts(db)["sources"] == 1


def test_claim_publication_idempotent_response_lost_and_sql_injection_input(db, tmp_path: Path):
    payload = "TEST-PUB-PG-001'; select pg_sleep(1); --\n/* unicode ☃ */"
    migration_input, plan = _published_input_and_plan(tmp_path, legacy_publication_id=payload)
    adapter = _adapter(db)
    _seed_owner(db, migration_input)
    adapter.register_legacy_source(migration_input, plan)
    first = adapter.claim_legacy_publication(migration_input, plan)
    second = adapter.claim_legacy_publication(migration_input, plan)
    assert first["migration_id"] == second["migration_id"]
    assert _counts(db)["migrations"] == 1

    _truncate_runtime_tables(db)
    _seed_owner(db, migration_input)
    adapter.register_legacy_source(migration_input, plan)
    lost = _adapter(db, response_lost={"claim_legacy_publication"})
    with pytest.raises(ResponseLostError):
        lost.claim_legacy_publication(migration_input, plan)
    assert _adapter(db).claim_legacy_publication(migration_input, plan)["migration_id"]
    assert _counts(db)["migrations"] == 1


def test_attach_work_chapter_asset_append_event_and_response_lost(db, tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    work_id, chapter_id = _seed_targets(db, migration_input, plan, include_asset=True)
    adapter = _adapter(db)
    adapter.register_legacy_source(migration_input, plan)
    migration = adapter.claim_legacy_publication(migration_input, plan)
    adapter.attach_migration_work(migration["migration_id"], work_id, plan)
    adapter.attach_migration_work(migration["migration_id"], work_id, plan)
    assert _migration_row(db)["target_work_id"] == work_id

    lost_work = _adapter(db, response_lost={"attach_migration_work"})
    with pytest.raises(ResponseLostError):
        lost_work.attach_migration_work(migration["migration_id"], work_id, plan)
    assert _migration_row(db)["target_work_id"] == work_id

    adapter.attach_migration_chapter(migration["migration_id"], chapter_id, plan)
    lost_chapter = _adapter(db, response_lost={"attach_migration_chapter"})
    with pytest.raises(ResponseLostError):
        lost_chapter.attach_migration_chapter(migration["migration_id"], chapter_id, plan)
    assert _migration_row(db)["target_chapter_id"] == chapter_id

    asset = {
        "storage_provider": "google_drive",
        "storage_file_id": migration_input.publication.remote_asset["storage_file_id"],
        "size": migration_input.publication.artifact.pdf_size,
        "sha256": migration_input.publication.artifact.pdf_sha256,
    }
    adapter.attach_migration_asset(migration["migration_id"], chapter_id, asset, plan)
    lost_asset = _adapter(db, response_lost={"attach_migration_asset"})
    with pytest.raises(ResponseLostError):
        lost_asset.attach_migration_asset(migration["migration_id"], chapter_id, asset, plan)
    assert _migration_row(db)["target_chapter_asset_id"] == chapter_id
    event_id = adapter.append_legacy_migration_event(migration["migration_id"], "migration_planned", {"safe": True})
    assert uuid.UUID(event_id)


def test_mark_failure_and_complete_response_lost(db, tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    work_id, chapter_id = _seed_targets(db, migration_input, plan, include_asset=True)
    adapter = _adapter(db)
    adapter.register_legacy_source(migration_input, plan)
    migration = adapter.claim_legacy_publication(migration_input, plan)
    adapter.mark_migration_failure(migration["migration_id"], "synthetic_retry", True)
    assert _migration_row(db)["migration_state"] == "failed_retryable"

    _truncate_runtime_tables(db)
    work_id, chapter_id = _seed_targets(db, migration_input, plan, include_asset=True)
    adapter.register_legacy_source(migration_input, plan)
    migration = adapter.claim_legacy_publication(migration_input, plan)
    adapter.attach_migration_work(migration["migration_id"], work_id, plan)
    adapter.attach_migration_chapter(migration["migration_id"], chapter_id, plan)
    adapter.attach_migration_asset(
        migration["migration_id"],
        chapter_id,
            {
                "storage_provider": "google_drive",
                "storage_file_id": migration_input.publication.remote_asset["storage_file_id"],
            "size": migration_input.publication.artifact.pdf_size,
            "sha256": migration_input.publication.artifact.pdf_sha256,
        },
        plan,
    )
    lost = _adapter(db, response_lost={"complete_legacy_migration"})
    with pytest.raises(ResponseLostError):
        lost.complete_legacy_migration(migration["migration_id"], "local-test", plan)
    assert adapter.complete_legacy_migration(migration["migration_id"], "local-test", plan)["migration_state"] == "completed"


def test_complete_draft_recovery_stays_recovery_completed(db, tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    work_id, chapter_id = _seed_targets(db, migration_input, plan, include_asset=True)
    adapter = _adapter(db)
    adapter.register_legacy_source(migration_input, plan)
    migration = adapter.claim_legacy_publication(migration_input, plan)
    adapter.attach_migration_work(migration["migration_id"], work_id, plan)
    adapter.attach_migration_chapter(migration["migration_id"], chapter_id, plan)
    completed = adapter.complete_legacy_migration(migration["migration_id"], "local-test", plan)
    assert completed["migration_state"] == "recovery_completed"


class SeededTargetBackend(FakeLegacyTargetBackend):
    def __init__(self, settings: LocalPostgresConnectionSettings):
        super().__init__()
        self.settings = settings

    def resolve_or_create_work(self, migration_input, plan):
        work_id, _chapter_id = _seed_targets(self.settings, migration_input, plan, include_asset=True)
        self.works.setdefault(work_id, {"work_id": work_id, "status": migration_input.publication.requested_target_status})
        self.call_count += 1
        return {"work_id": work_id}

    def resolve_or_create_chapter(self, migration_input, work_id, plan):
        _work_id, chapter_id = _seed_targets(self.settings, migration_input, plan, include_asset=True)
        status = "draft" if migration_input.publication.migration_mode == "draft_recovery" else migration_input.publication.requested_target_status
        self.chapters.setdefault(chapter_id, {"chapter_id": chapter_id, "work_id": work_id, "status": status})
        self.call_count += 1
        return {"chapter_id": chapter_id, "work_id": work_id}


class SeededArtifactStorage(FakeLegacyArtifactStorage):
    def use_verified_remote_asset(self, migration_input, plan):
        asset = migration_input.publication.remote_asset or {}
        return {
            "storage_provider": asset.get("storage_provider", "google_drive"),
            "storage_file_id": asset.get("storage_file_id", f"TEST-STORAGE-{plan.plan_digest[:16]}"),
            "size": migration_input.publication.artifact.pdf_size,
            "sha256": migration_input.publication.artifact.pdf_sha256,
            "action": "reuse_verified_remote_asset",
        }

    def upload(self, migration_input, plan):
        self.upload_count += 1
        return {
            "storage_provider": "google_drive",
            "storage_file_id": f"TEST-STORAGE-{plan.plan_digest[:16]}",
            "size": migration_input.publication.artifact.pdf_size,
            "sha256": migration_input.publication.artifact.pdf_sha256,
            "action": "fake_upload",
        }


def test_executor_with_postgres_adapter_published_and_restart_resume(db, tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    _seed_owner(db, migration_input)
    op_settings = _operational_session_settings(db)
    result = LegacyMigrationExecutor(
        provenance_backend=_adapter(op_settings),
        target_backend=SeededTargetBackend(db),
        artifact_storage=SeededArtifactStorage(),
    ).execute(migration_input, plan)
    assert result.execution_status == "completed"
    assert result.final_migration_state == "completed"
    assert _counts(db)["migrations"] == 1

    restarted = LegacyMigrationExecutor(
        provenance_backend=_adapter(op_settings),
        target_backend=SeededTargetBackend(db),
        artifact_storage=SeededArtifactStorage(),
    ).execute(migration_input, plan)
    assert restarted.execution_status == "completed"
    assert _counts(db)["migrations"] == 1


def test_executor_with_postgres_adapter_draft_recovery_and_no_authorization(db, tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    _seed_owner(db, migration_input)
    op_settings = _operational_session_settings(db)
    result = LegacyMigrationExecutor(
        provenance_backend=_adapter(op_settings),
        target_backend=SeededTargetBackend(db),
        artifact_storage=SeededArtifactStorage(),
    ).execute(migration_input, plan)
    assert result.execution_status == "completed"
    assert result.final_migration_state == "recovery_completed"
    assert result.final_target_state == "draft"


def test_concurrent_register_and_claim_are_categorized(db, tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    _seed_owner(db, migration_input)
    op_settings = _operational_session_settings(db)
    adapters = [_adapter(op_settings), _adapter(op_settings)]
    for adapter in adapters:
        adapter.register_legacy_source(migration_input, plan)
    assert _counts(db)["sources"] == 1

    results = [adapter.claim_legacy_publication(migration_input, plan) for adapter in adapters]
    assert results[0]["migration_id"] == results[1]["migration_id"]
    assert _counts(db)["migrations"] == 1


def test_connection_loss_before_call_is_transient_and_has_no_effect(db, tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    settings = dataclasses.replace(db, port=1)
    with pytest.raises(TransientBackendError):
        _adapter(settings).register_legacy_source(migration_input, plan)
    assert _counts(db)["sources"] == 0
