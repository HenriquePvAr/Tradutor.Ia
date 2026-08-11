"""Explicit, artifact-scoped authorization for Community publishing.

Processing/download authorization intentionally does not grant redistribution.  This
module records a separate user attestation bound to the concrete local artifact that is
about to be published, so a later PDF/job change cannot reuse an older consent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
ATTESTATION_VERSION = "publish-consent-v1"
SCOPE_COMMUNITY_PUBLISH = "community_publish"

REASON_CONSENT_REQUIRED = "publish_consent_required"
REASON_CONSENT_GRANTED = "publish_consent_granted"
REASON_CONSENT_MISSING = "publish_consent_missing"
REASON_OWNER_MISMATCH = "publish_identity_mismatch"
REASON_JOB_MISMATCH = "publish_job_mismatch"
REASON_ARTIFACT_MISMATCH = "publish_artifact_mismatch"
REASON_UNAUTHENTICATED = "authentication_required"


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _safe_text(value: Any, limit: int = 256) -> str:
    return str(value or "").strip()[:limit]


@dataclass(frozen=True)
class PublishArtifactIdentity:
    owner_id: str
    job_id: str
    run_id: str
    source_url: str
    artifact_sha256: str
    artifact_size_bytes: int
    scope: str = SCOPE_COMMUNITY_PUBLISH

    def normalized(self) -> dict[str, Any]:
        return {
            "owner_id": _safe_text(self.owner_id),
            "job_id": _safe_text(self.job_id, 64),
            "run_id": _safe_text(self.run_id, 128),
            "source_url_hash": hashlib.sha256(
                _safe_text(self.source_url, 4096).encode("utf-8")
            ).hexdigest(),
            "artifact_sha256": _safe_text(self.artifact_sha256, 64).lower(),
            "artifact_size_bytes": int(self.artifact_size_bytes),
            "scope": _safe_text(self.scope, 80) or SCOPE_COMMUNITY_PUBLISH,
            "attestation_version": ATTESTATION_VERSION,
        }


@dataclass(frozen=True)
class PublishAuthorizationDecision:
    decision: str
    reason_code: str
    owner_id: str
    job_id: str
    run_id: str
    artifact_sha256: str
    artifact_size_bytes: int
    scope: str
    decision_id: str = ""
    created_at: float = 0.0
    attestation_version: str = ATTESTATION_VERSION

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    def public(self) -> dict[str, Any]:
        return dict(self.__dict__)


class PublishAuthorizationStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS publish_authorization_decisions (
                decision_id TEXT PRIMARY KEY,
                identity_hash TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_publish_authorization_lookup
               ON publish_authorization_decisions(owner_id, job_id, run_id, artifact_sha256, scope, created_at DESC)"""
        )

    @staticmethod
    def deny(identity: PublishArtifactIdentity, reason_code: str) -> PublishAuthorizationDecision:
        normalized = identity.normalized()
        return PublishAuthorizationDecision(
            decision="DENY",
            reason_code=reason_code,
            owner_id=normalized["owner_id"],
            job_id=normalized["job_id"],
            run_id=normalized["run_id"],
            artifact_sha256=normalized["artifact_sha256"],
            artifact_size_bytes=normalized["artifact_size_bytes"],
            scope=normalized["scope"],
        )

    def grant(self, identity: PublishArtifactIdentity, *, attested: bool) -> PublishAuthorizationDecision:
        normalized = identity.normalized()
        if not normalized["owner_id"]:
            return self.deny(identity, REASON_UNAUTHENTICATED)
        if attested is not True:
            return self.deny(identity, REASON_CONSENT_REQUIRED)
        if len(normalized["artifact_sha256"]) != 64 or normalized["artifact_size_bytes"] <= 0:
            return self.deny(identity, REASON_ARTIFACT_MISMATCH)

        identity_hash = _hash(normalized)
        decision_id = f"pa_{identity_hash[:32]}"
        existing = self._conn.execute(
            "SELECT payload_json FROM publish_authorization_decisions WHERE identity_hash=?",
            (identity_hash,),
        ).fetchone()
        if existing:
            return PublishAuthorizationDecision(**json.loads(existing["payload_json"]))
        payload = {
            "decision": "ALLOW",
            "reason_code": REASON_CONSENT_GRANTED,
            "owner_id": normalized["owner_id"],
            "job_id": normalized["job_id"],
            "run_id": normalized["run_id"],
            "artifact_sha256": normalized["artifact_sha256"],
            "artifact_size_bytes": normalized["artifact_size_bytes"],
            "scope": normalized["scope"],
            "decision_id": decision_id,
            "created_at": time.time(),
            "attestation_version": ATTESTATION_VERSION,
        }
        self._conn.execute(
            """INSERT INTO publish_authorization_decisions
               (decision_id,identity_hash,owner_id,job_id,run_id,artifact_sha256,scope,status,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id,
                identity_hash,
                normalized["owner_id"],
                normalized["job_id"],
                normalized["run_id"],
                normalized["artifact_sha256"],
                normalized["scope"],
                "active",
                _canonical(payload),
                payload["created_at"],
            ),
        )
        return PublishAuthorizationDecision(**payload)

    def require_current(
        self,
        identity: PublishArtifactIdentity,
        *,
        owner_id: str,
    ) -> PublishAuthorizationDecision:
        normalized = identity.normalized()
        if not owner_id:
            return self.deny(identity, REASON_UNAUTHENTICATED)
        if _safe_text(owner_id) != normalized["owner_id"]:
            return self.deny(identity, REASON_OWNER_MISMATCH)
        rows = self._conn.execute(
            """SELECT payload_json FROM publish_authorization_decisions
               WHERE owner_id=? AND scope=? ORDER BY created_at DESC""",
            (normalized["owner_id"], normalized["scope"]),
        ).fetchall()
        saw_job = False
        saw_artifact = False
        for row in rows:
            data = json.loads(row["payload_json"])
            if normalized["job_id"]:
                if data.get("job_id") != normalized["job_id"]:
                    continue
                saw_job = True
                if data.get("run_id") != normalized["run_id"]:
                    return self.deny(identity, REASON_JOB_MISMATCH)
            elif data.get("job_id"):
                continue
            if (
                str(data.get("artifact_sha256") or "").lower()
                != normalized["artifact_sha256"]
                or int(data.get("artifact_size_bytes") or 0)
                != normalized["artifact_size_bytes"]
            ):
                saw_artifact = True
                return self.deny(identity, REASON_ARTIFACT_MISMATCH)
            return PublishAuthorizationDecision(**data)
        return self.deny(
            identity,
            REASON_ARTIFACT_MISMATCH if saw_artifact else (
                REASON_JOB_MISMATCH if saw_job else REASON_CONSENT_MISSING
            ),
        )
