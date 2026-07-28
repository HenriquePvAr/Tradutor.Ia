"""Persistent source readiness and explicit content-download authorization.

This module deliberately stops before the production downloader.  A successful public
source analysis proves only that a later operation *may be requested*.  Download authority
is a separate, owner-scoped, append-only decision with an exact operation scope.

The fixture downloader below accepts only a test seam and local bytes supplied by tests.
It never relaxes the public URL/SSRF policy and is not wired to the production endpoint.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

SOURCE_ANALYSIS_SCHEMA_VERSION = 1
DOWNLOAD_AUTHORIZATION_SCHEMA_VERSION = 1
ASSET_MANIFEST_SCHEMA_VERSION = 1

SOURCE_ANALYSIS_STATES = frozenset({
    "source_analysis_pending", "source_analysis_active", "source_analysis_ready",
    "source_analysis_blocked", "source_analysis_failed",
})
RIGHTS_BASES = frozenset({
    "owned_content", "licensed_content", "explicit_permission", "public_domain",
    "local_test_fixture",
})
DOWNLOAD_OPERATIONS = frozenset({
    "analyze_metadata", "download_assets", "run_ocr", "translate", "reconstruct",
    "generate_pdf", "publish",
})
DOWNLOAD_AUTHORIZATION_REQUIRED = "download_authorization_required"


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 160 or not all(c.isalnum() or c in "._:@-" for c in text):
        raise ValueError(f"invalid_{field}")
    return text


def _owner_for_job(job: dict[str, Any]) -> str:
    config = job.get("configuration") if isinstance(job.get("configuration"), dict) else {}
    owner = str(config.get("community_owner_id") or "").strip()
    return _safe_id(owner or "local", "owner")


@dataclass(frozen=True)
class SourceAnalysisResult:
    schema_version: int
    analysis_id: str
    owner: str
    operation_id: str
    previous_operation_id: str
    attempt: int
    normalized_url_hash: str
    adapter: str
    source_kind: str
    preflight_result_id: str
    preflight_status: str
    preflight_reason_code: str
    browser_inspection_required: bool
    browser_inspection_performed: bool
    browser_runtime_id: str
    browser_engine: str
    browser_version: str
    document_ready_state: str
    public_title_present: bool
    public_structure_indicators_present: bool
    estimated_asset_count: int | None
    estimate_kind: str
    authentication_required: bool
    captcha_detected: bool
    access_restricted: bool
    status: str
    reason_code: str
    policy_hash: str
    created_at: float
    completed_at: float
    result_hash: str

    def public(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DownloadAuthorizationDecision:
    schema_version: int
    authorization_id: str
    owner: str
    analysis_result_id: str
    normalized_url_hash: str
    source_adapter: str
    authorization_kind: str
    rights_basis: str
    scope: str
    allowed_operations: tuple[str, ...]
    denied_operations: tuple[str, ...]
    reviewer: str
    authorization_source: str
    created_at: float
    status: str
    authorization_hash: str

    def public(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload["allowed_operations"] = list(self.allowed_operations)
        payload["denied_operations"] = list(self.denied_operations)
        return payload


class SourceReadinessStore:
    """Append-only, owner-scoped store sharing only the SQLite file location."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def close(self) -> None:
        self._conn.close()

    def _ensure_schema(self) -> None:
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS source_analysis_results (
            analysis_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            previous_operation_id TEXT NOT NULL,
            identity_hash TEXT NOT NULL UNIQUE,
            result_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            normalized_url_hash TEXT NOT NULL,
            adapter TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_source_analysis_owner_created
            ON source_analysis_results(owner, created_at DESC);
        CREATE TABLE IF NOT EXISTS download_authorization_decisions (
            authorization_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            analysis_result_id TEXT NOT NULL,
            identity_hash TEXT NOT NULL UNIQUE,
            authorization_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_download_auth_owner_analysis
            ON download_authorization_decisions(owner, analysis_result_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS downloaded_asset_manifests (
            manifest_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            analysis_result_id TEXT NOT NULL,
            authorization_id TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            identity_hash TEXT NOT NULL UNIQUE,
            manifest_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """)

    def persist_analysis(self, payload: dict[str, Any]) -> SourceAnalysisResult:
        body = dict(payload)
        body["schema_version"] = SOURCE_ANALYSIS_SCHEMA_VERSION
        body["owner"] = _safe_id(body.get("owner"), "owner")
        body["operation_id"] = _safe_id(body.get("operation_id"), "operation_id")
        body["previous_operation_id"] = str(body.get("previous_operation_id") or "")
        body["attempt"] = max(1, int(body.get("attempt") or 1))
        body["status"] = str(body.get("status") or "")
        if body["status"] not in SOURCE_ANALYSIS_STATES:
            raise ValueError("invalid_source_analysis_status")
        body["normalized_url_hash"] = str(body.get("normalized_url_hash") or "")
        body["adapter"] = _safe_id(body.get("adapter"), "adapter")
        body["source_kind"] = str(body.get("source_kind") or "public_url")
        body["preflight_result_id"] = str(body.get("preflight_result_id") or "")
        body["preflight_status"] = str(body.get("preflight_status") or "")
        body["preflight_reason_code"] = str(body.get("preflight_reason_code") or "")
        body["browser_inspection_required"] = bool(body.get("browser_inspection_required"))
        body["browser_inspection_performed"] = bool(body.get("browser_inspection_performed"))
        body["browser_runtime_id"] = str(body.get("browser_runtime_id") or "")
        body["browser_engine"] = str(body.get("browser_engine") or "")
        body["browser_version"] = str(body.get("browser_version") or "")
        body["document_ready_state"] = str(body.get("document_ready_state") or "")
        body["public_title_present"] = bool(body.get("public_title_present"))
        body["public_structure_indicators_present"] = bool(
            body.get("public_structure_indicators_present"))
        estimate = body.get("estimated_asset_count")
        body["estimated_asset_count"] = None if estimate is None else max(0, int(estimate))
        body["estimate_kind"] = str(body.get("estimate_kind") or "unavailable")
        body["authentication_required"] = bool(body.get("authentication_required"))
        body["captcha_detected"] = bool(body.get("captcha_detected"))
        body["access_restricted"] = bool(body.get("access_restricted"))
        body["reason_code"] = str(body.get("reason_code") or "")
        body["policy_hash"] = str(body.get("policy_hash") or "")
        now = float(body.get("completed_at") or time.time())
        body["created_at"] = float(body.get("created_at") or now)
        body["completed_at"] = now
        identity_payload = {
            key: body.get(key) for key in (
                "schema_version", "owner", "operation_id", "previous_operation_id", "attempt",
                "normalized_url_hash", "adapter", "preflight_result_id", "preflight_status",
                "preflight_reason_code", "browser_inspection_required",
                "browser_inspection_performed", "browser_runtime_id", "policy_hash",
            )
        }
        identity_hash = _hash(identity_payload)
        result_payload = {
            key: value for key, value in body.items()
            if key not in {"analysis_id", "result_hash", "created_at", "completed_at"}
        }
        result_hash = _hash(result_payload)
        analysis_id = f"sa_{identity_hash[:32]}"
        body.update(analysis_id=analysis_id, result_hash=result_hash)
        existing = self._conn.execute(
            "SELECT payload_json FROM source_analysis_results WHERE identity_hash=?",
            (identity_hash,),
        ).fetchone()
        if existing:
            return SourceAnalysisResult(**json.loads(existing["payload_json"]))
        self._conn.execute(
            """INSERT INTO source_analysis_results
               (analysis_id,owner,operation_id,previous_operation_id,identity_hash,result_hash,
                status,normalized_url_hash,adapter,policy_hash,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (analysis_id, body["owner"], body["operation_id"], body["previous_operation_id"],
             identity_hash, result_hash, body["status"], body["normalized_url_hash"],
             body["adapter"], body["policy_hash"], _canonical(body), body["created_at"]),
        )
        return SourceAnalysisResult(**body)

    def get_analysis(self, owner: str, analysis_id: str) -> SourceAnalysisResult | None:
        row = self._conn.execute(
            "SELECT payload_json FROM source_analysis_results WHERE owner=? AND analysis_id=?",
            (_safe_id(owner, "owner"), str(analysis_id or "")),
        ).fetchone()
        return SourceAnalysisResult(**json.loads(row["payload_json"])) if row else None

    def latest_analysis(self, owner: str) -> SourceAnalysisResult | None:
        row = self._conn.execute(
            "SELECT payload_json FROM source_analysis_results WHERE owner=? ORDER BY created_at DESC LIMIT 1",
            (_safe_id(owner, "owner"),),
        ).fetchone()
        return SourceAnalysisResult(**json.loads(row["payload_json"])) if row else None

    def authorize(self, *, owner: str, analysis_result_id: str, rights_basis: str,
                  allowed_operations: Iterable[str], reviewer: str,
                  authorization_source: str = "explicit_ui_review") -> DownloadAuthorizationDecision:
        owner = _safe_id(owner, "owner")
        result = self.get_analysis(owner, analysis_result_id)
        if result is None:
            raise ValueError("analysis_result_not_found")
        rights_basis = str(rights_basis or "")
        if rights_basis not in RIGHTS_BASES:
            raise ValueError("download_rights_basis_required")
        allowed = tuple(sorted(set(str(value) for value in allowed_operations)))
        if not allowed or not set(allowed).issubset(DOWNLOAD_OPERATIONS):
            raise ValueError("download_scope_denied")
        denied = tuple(sorted(DOWNLOAD_OPERATIONS - set(allowed)))
        identity_payload = {
            "schema_version": DOWNLOAD_AUTHORIZATION_SCHEMA_VERSION,
            "owner": owner,
            "analysis_result_id": result.analysis_id,
            "normalized_url_hash": result.normalized_url_hash,
            "source_adapter": result.adapter,
            "rights_basis": rights_basis,
            "allowed_operations": allowed,
            "reviewer": _safe_id(reviewer, "reviewer"),
            "authorization_source": str(authorization_source or ""),
        }
        identity_hash = _hash(identity_payload)
        existing = self._conn.execute(
            "SELECT payload_json FROM download_authorization_decisions WHERE identity_hash=?",
            (identity_hash,),
        ).fetchone()
        if existing:
            data = json.loads(existing["payload_json"])
            data["allowed_operations"] = tuple(data["allowed_operations"])
            data["denied_operations"] = tuple(data["denied_operations"])
            return DownloadAuthorizationDecision(**data)
        now = time.time()
        authorization_hash = _hash(identity_payload)
        payload = {
            **identity_payload,
            "authorization_id": f"da_{identity_hash[:32]}",
            "authorization_kind": "content_rights",
            "scope": "explicit_operations",
            "denied_operations": denied,
            "created_at": now,
            "status": "active",
            "authorization_hash": authorization_hash,
        }
        serializable = dict(payload)
        serializable["allowed_operations"] = list(allowed)
        serializable["denied_operations"] = list(denied)
        self._conn.execute(
            """INSERT INTO download_authorization_decisions
               (authorization_id,owner,analysis_result_id,identity_hash,authorization_hash,
                status,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (payload["authorization_id"], owner, result.analysis_id, identity_hash,
             authorization_hash, "active", _canonical(serializable), now),
        )
        return DownloadAuthorizationDecision(**payload)

    def require_operation(self, *, owner: str, analysis_result_id: str,
                          operation: str) -> DownloadAuthorizationDecision:
        rows = self._conn.execute(
            """SELECT payload_json FROM download_authorization_decisions
               WHERE owner=? AND analysis_result_id=? AND status='active'
               ORDER BY created_at DESC""",
            (_safe_id(owner, "owner"), str(analysis_result_id or "")),
        ).fetchall()
        for row in rows:
            data = json.loads(row["payload_json"])
            if operation in data.get("allowed_operations", []):
                data["allowed_operations"] = tuple(data["allowed_operations"])
                data["denied_operations"] = tuple(data["denied_operations"])
                return DownloadAuthorizationDecision(**data)
        raise PermissionError(DOWNLOAD_AUTHORIZATION_REQUIRED)

    def latest_authorization(self, *, owner: str,
                             analysis_result_id: str) -> DownloadAuthorizationDecision | None:
        row = self._conn.execute(
            """SELECT payload_json FROM download_authorization_decisions
               WHERE owner=? AND analysis_result_id=? ORDER BY created_at DESC LIMIT 1""",
            (_safe_id(owner, "owner"), str(analysis_result_id or "")),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["payload_json"])
        data["allowed_operations"] = tuple(data["allowed_operations"])
        data["denied_operations"] = tuple(data["denied_operations"])
        return DownloadAuthorizationDecision(**data)

    def persist_manifest(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        identity = {
            key: body.get(key) for key in (
                "schema_version", "owner", "analysis_result_id", "authorization_id",
                "operation_id", "adapter", "assets",
            )
        }
        identity_hash = _hash(identity)
        existing = self._conn.execute(
            "SELECT payload_json FROM downloaded_asset_manifests WHERE identity_hash=?",
            (identity_hash,),
        ).fetchone()
        if existing:
            return json.loads(existing["payload_json"])
        body["manifest_id"] = f"dm_{identity_hash[:32]}"
        body["manifest_hash"] = _hash({
            key: value for key, value in body.items()
            if key not in {"manifest_id", "manifest_hash", "download_started_at",
                           "download_completed_at"}
        })
        self._conn.execute(
            """INSERT INTO downloaded_asset_manifests
               (manifest_id,owner,analysis_result_id,authorization_id,operation_id,
                identity_hash,manifest_hash,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (body["manifest_id"], body["owner"], body["analysis_result_id"],
             body["authorization_id"], body["operation_id"], identity_hash,
             body["manifest_hash"], _canonical(body), time.time()),
        )
        return body

    def manifest_for_operation(self, *, owner: str, analysis_result_id: str,
                               authorization_id: str, operation_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT payload_json FROM downloaded_asset_manifests
               WHERE owner=? AND analysis_result_id=? AND authorization_id=? AND operation_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (_safe_id(owner, "owner"), str(analysis_result_id or ""),
             str(authorization_id or ""), _safe_id(operation_id, "operation_id")),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None


def source_result_from_analysis(job: dict[str, Any], analysis: Any, *,
                                preflight: dict[str, Any] | None = None,
                                browser: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a URL-free result from a successful adapter analysis."""
    public = analysis.public()
    preflight = dict(preflight or {})
    browser = dict(browser or {})
    operation_id = str(job.get("run_id") or job.get("id") or uuid.uuid4().hex)
    return {
        "owner": _owner_for_job(job),
        "operation_id": operation_id,
        "previous_operation_id": str(job.get("previous_job_id") or ""),
        "attempt": int(job.get("attempt") or 1),
        "normalized_url_hash": hashlib.sha256(
            str(job.get("source_url") or "").encode("utf-8")).hexdigest(),
        "adapter": str(public.get("adapter") or "universal"),
        "source_kind": "public_url",
        "preflight_result_id": str(preflight.get("result_id") or ""),
        "preflight_status": str(preflight.get("status") or "completed"),
        "preflight_reason_code": str(preflight.get("reason_code") or ""),
        "browser_inspection_required": bool(preflight.get("browser_inspection_required", True)),
        "browser_inspection_performed": True,
        "browser_runtime_id": str(browser.get("runtime_id") or ""),
        "browser_engine": str(browser.get("engine") or "chrome"),
        "browser_version": str(browser.get("version") or ""),
        "document_ready_state": str(browser.get("ready_state") or "complete"),
        "public_title_present": bool(public.get("title")),
        "public_structure_indicators_present": bool(public.get("candidate_count", 0)),
        "estimated_asset_count": int(public.get("accepted_count") or 0),
        "estimate_kind": "non_binding_browser_estimate",
        "authentication_required": False,
        "captcha_detected": False,
        "access_restricted": False,
        "status": "source_analysis_ready",
        "reason_code": "source_structure_compatible",
        "policy_hash": str(preflight.get("policy_hash") or ""),
    }


def download_fixture_assets(*, store: SourceReadinessStore, owner: str,
                            analysis_result_id: str, operation_id: str,
                            assets: Iterable[dict[str, Any]], output_dir: str | Path,
                            read_asset: Callable[[str], tuple[str, bytes]],
                            max_asset_bytes: int = 1_000_000,
                            max_total_bytes: int = 5_000_000,
                            max_assets: int = 32) -> dict[str, Any]:
    """Download synthetic assets through an explicit local-test seam only."""
    decision = store.require_operation(
        owner=owner, analysis_result_id=analysis_result_id, operation="download_assets")
    if decision.rights_basis != "local_test_fixture":
        raise PermissionError("fixture_authorization_required")
    existing = store.manifest_for_operation(
        owner=owner, analysis_result_id=analysis_result_id,
        authorization_id=decision.authorization_id, operation_id=operation_id)
    if existing is not None:
        return existing
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ordered = sorted(list(assets), key=lambda item: (int(item["sequence"]), str(item["reference"])))
    if len(ordered) > max_assets:
        raise ValueError("download_asset_count_limit_exceeded")
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    total = 0
    started = time.time()
    for item in ordered:
        reference = str(item["reference"])
        source_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        if source_hash in seen:
            continue
        seen.add(source_hash)
        content_type, content = read_asset(reference)
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("download_asset_content_type_invalid")
        if len(content) > max_asset_bytes:
            raise ValueError("download_asset_too_large")
        total += len(content)
        if total > max_total_bytes:
            raise ValueError("download_total_limit_exceeded")
        digest = hashlib.sha256(content).hexdigest()
        extension = {".png": ".png", ".jpg": ".jpg", ".webp": ".webp"}.get(
            mimetypes.guess_extension(content_type) or "", "")
        if not extension:
            extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[content_type]
        destination = root / f"{digest}{extension}"
        if root not in destination.resolve().parents:
            raise ValueError("download_asset_url_blocked")
        if not destination.exists():
            fd, partial = tempfile.mkstemp(prefix=".asset-", suffix=".partial", dir=root)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(partial, destination)
            finally:
                Path(partial).unlink(missing_ok=True)
        records.append({
            "asset_id": f"asset_{digest[:24]}",
            "source_reference_hash": source_hash,
            "sequence": int(item["sequence"]),
            "content_type": content_type,
            "byte_size": len(content),
            "sha256": digest,
            "local_content_address": f"sha256/{digest}{extension}",
            "status": "downloaded",
            "attempt_count": 1,
        })
    result = store.get_analysis(owner, analysis_result_id)
    manifest = {
        "schema_version": ASSET_MANIFEST_SCHEMA_VERSION,
        "owner": owner,
        "analysis_result_id": analysis_result_id,
        "authorization_id": decision.authorization_id,
        "operation_id": _safe_id(operation_id, "operation_id"),
        "adapter": result.adapter if result else "",
        "asset_count": len(records),
        "assets": records,
        "total_bytes": total,
        "download_started_at": started,
        "download_completed_at": time.time(),
        "status": "download_completed",
        "reason_code": "",
    }
    return store.persist_manifest(manifest)
