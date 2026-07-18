"""Retention lifecycle for social PDFs that stopped being a chapter's active asset.

Nothing is ever deleted here. A replaced or unlinked PDF becomes *retained* with a
deadline; the owner can restore it during that window; only afterwards may a sweep move it
to the Google Drive **trash** (never a permanent delete, never an empty-trash). Every
transition is optimistic-locked and audited, and no file is trashed while any known
reference exists or the deadline has not passed.

The client never supplies owner/actor ids, storage ids, states or deadlines — all are
derived server-side.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from chapter_asset_repository import AssetNotFound, ChapterAssetError, ChapterAssetRepository

DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365
DAY_SECONDS = 86400

REASONS = ("replaced", "unlinked", "failed_publish_cleanup", "reconcile_orphan", "manual_recovery")

TRANSITIONS = ChapterAssetRepository.TRANSITIONS  # enforced in the repo (fail-closed)


class RetentionError(ChapterAssetError):
    """Retention could not be applied (conflict or unsafe state)."""


class RetentionConflict(RetentionError):
    """The chapter already has an incompatible active asset (HTTP 409)."""


def retention_days(env: dict | None = None) -> int:
    """Explicit config, clamped to a safe range; an invalid value falls back to the default."""
    values = os.environ if env is None else env
    raw = str(values.get("COMMUNITY_ASSET_RETENTION_DAYS", "") or "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS  # documented safe fallback, never 0
    return max(MIN_RETENTION_DAYS, min(MAX_RETENTION_DAYS, days))


def sweep_enabled(env: dict | None = None) -> bool:
    """Automatic sweeping is OFF unless explicitly enabled."""
    values = os.environ if env is None else env
    return str(values.get("COMMUNITY_ASSET_RETENTION_SWEEP_ENABLED", "")).strip() == "1"


class SocialAssetRetentionService:
    def __init__(self, asset_repo, *, provider_factory: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.time, env: dict | None = None):
        self._assets = asset_repo
        self._provider_factory = provider_factory
        self._clock = clock
        self._env = env

    def _retain_until(self) -> float:
        return self._clock() + retention_days(self._env) * DAY_SECONDS

    # ---- registering retention ----------------------------------------------
    def retain_superseded_asset(self, chapter_id: str, publication_id: str, owner_id: str) -> str:
        """Called after a replace swapped the link; the old publication is preserved."""
        return self._assets.create_retention(
            chapter_id=chapter_id, publication_id=publication_id, owner_id=owner_id,
            reason="replaced", retain_until=self._retain_until(), previous_state="active")

    def retain_unlinked_asset(self, chapter_id: str, publication_id: str, owner_id: str) -> str:
        return self._assets.create_retention(
            chapter_id=chapter_id, publication_id=publication_id, owner_id=owner_id,
            reason="unlinked", retain_until=self._retain_until(), previous_state="active")

    # ---- owner queries -------------------------------------------------------
    def get_retention_status(self, chapter_id: str, owner_id: str) -> dict[str, Any]:
        row = self._assets.latest_retention_for_chapter(chapter_id, owner_id)
        if not row or row["state"] in ("restored", "ignored"):
            return {"state": "none", "restorable": False}  # terminal: nothing is retained
        return self._dto(row)

    def list_owner_retained_assets(self, owner_id: str, *, limit: int = 100) -> dict[str, Any]:
        return {"items": [self._dto(r) for r in self._assets.list_owner_retentions(owner_id, limit=limit)]}

    def _dto(self, row: dict[str, Any]) -> dict[str, Any]:
        """Safe DTO: no storage id, Drive id, URL, path, checksum or provider."""
        now = self._clock()
        remaining = max(0, int((row["retain_until"] - now) // DAY_SECONDS))
        return {
            "chapter_id": row["chapter_id"],
            "state": row["state"],
            "reason": row["reason"],
            "retained_at": row["retained_at"],
            "retain_until": row["retain_until"],
            "days_remaining": remaining,
            "restorable": row["state"] in ("retained", "pending_trash") and row["retain_until"] > now,
        }

    # ---- restore -------------------------------------------------------------
    def restore_asset(self, chapter_id: str, owner_id: str) -> dict[str, Any]:
        """Owner-only restore during the retention window; atomic and conflict-aware."""
        row = self._assets.latest_retention_for_chapter(chapter_id, owner_id)
        if not row or row["state"] not in ("retained", "pending_trash", "trashed"):
            raise AssetNotFound()
        if row["state"] == "trashed":
            # Untrash is not supported by the current provider contract; fail safely
            # instead of pretending the file came back.
            raise RetentionError("asset_already_trashed")
        if row["retain_until"] <= self._clock():
            raise RetentionError("retention_expired")
        # A different active asset must not be replaced silently.
        current = self._assets.get_asset_status(chapter_id, is_owner=True)
        if current.get("linked") and current.get("available"):
            raise RetentionConflict("chapter_has_active_asset")
        self._assets.link_asset(chapter_id, row["publication_id"], owner_id)
        ok = self._assets.transition_retention(
            row["id"], to_state="restored", expected_version=row["version"],
            actor_id=owner_id, reason="restore", restored_at=self._clock())
        if not ok:
            raise RetentionConflict("retention_changed_concurrently")
        return {"restored": True, "chapter_id": chapter_id}

    # ---- trash evaluation (never deletes) ------------------------------------
    def evaluate_for_trash(self, retention: dict[str, Any]) -> tuple[bool, str]:
        """Decide whether a retention may be trashed. Ambiguity always blocks."""
        if retention["state"] not in ("retained", "pending_trash", "failed"):
            return False, "state_not_eligible"
        if retention["retain_until"] > self._clock():
            return False, "within_retention"
        if self._assets.publication_reference_count(retention["publication_id"]) > 0:
            return False, "still_referenced"
        if self._assets.other_live_retentions(retention["publication_id"], retention["id"]) > 0:
            return False, "referenced_by_other_retention"
        try:
            storage_id = self._assets.publication_storage_id_for_retention(retention)
        except Exception:
            return False, "publication_unresolved"
        if not storage_id:
            return False, "missing_storage_id"
        return True, "safe"

    def move_to_trash(self, retention: dict[str, Any], *, actor_id: str = "system") -> str:
        """Move the remote file to the Drive TRASH (never a permanent delete)."""
        safe, why = self.evaluate_for_trash(retention)
        if not safe:
            self._assets.audit(action="trash_blocked", retention_id=retention["id"],
                               chapter_id=retention["chapter_id"],
                               publication_id=retention["publication_id"],
                               owner_id=retention["owner_id"], actor_id=actor_id,
                               from_state=retention["state"], to_state=retention["state"],
                               reason=why)
            if why in ("publication_unresolved", "missing_storage_id"):
                self._assets.transition_retention(
                    retention["id"], to_state="reconcile_required",
                    expected_version=retention["version"], actor_id=actor_id, reason=why)
                return "reconcile_required"
            return "blocked"
        # Mark intent first so a crash mid-call is recoverable and idempotent.
        if retention["state"] != "pending_trash":
            if not self._assets.transition_retention(
                    retention["id"], to_state="pending_trash",
                    expected_version=retention["version"], actor_id=actor_id, reason="due"):
                return "blocked"
            retention = self._assets.get_retention(retention["id"])
        storage_id = self._assets.publication_storage_id_for_retention(retention)
        try:
            provider = (self._provider_factory or self._default_provider)()
            provider.move_to_trash(storage_id)
        except Exception as exc:  # sanitized: never log the Drive id or credentials
            self._assets.transition_retention(
                retention["id"], to_state="failed", expected_version=retention["version"],
                actor_id=actor_id, reason="trash_failed",
                last_attempt_at=self._clock(),
                attempt_count=int(retention.get("attempt_count") or 0) + 1,
                last_error_code=type(exc).__name__)
            return "failed"
        self._assets.transition_retention(
            retention["id"], to_state="trashed", expected_version=retention["version"],
            actor_id=actor_id, reason="swept", trashed_at=self._clock())
        return "trashed"

    def retry_pending(self, *, limit: int = 50, actor_id: str = "system") -> dict[str, int]:
        counts = {"retried": 0, "trashed": 0, "failed": 0}
        for row in self._assets.list_due_retentions(self._clock(), limit=limit):
            if row["state"] not in ("pending_trash", "failed"):
                continue
            counts["retried"] += 1
            outcome = self.move_to_trash(row, actor_id=actor_id)
            if outcome == "trashed":
                counts["trashed"] += 1
            elif outcome == "failed":
                counts["failed"] += 1
        return counts

    @staticmethod
    def _default_provider():
        from community_api import build_read_provider
        return build_read_provider()
