"""Retention sweep + reconciliation over the app's OWN records and files.

Both are dry-run by default and never delete anything: the strongest action available is
moving a file to the Google Drive **trash**, and only when every reference check passes and
the retention deadline has expired. Ambiguity is never "fixed" — it becomes
``reconcile_required`` for human review.

Only records created by this application are inspected. Remote lookups happen by known file
id (resolved server-side from a local publication); there is no broad Drive search and no
access to personal files — the ``drive.file`` scope structurally limits visibility to files
the app itself created.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from social_asset_retention import SocialAssetRetentionService

# Reconciliation categories (see docs). Only a few are auto-fixable.
HEALTHY = "healthy"
RETAINED = "retained"
SAFE_FOR_TRASH = "safe_for_trash"
ORPHAN_CANDIDATE = "orphan_candidate"
RECONCILE_REQUIRED = "reconcile_required"


class SocialAssetRetentionSweep:
    """Move *due* retentions to the Drive trash. Dry-run unless apply=True."""

    def __init__(self, asset_repo, retention: SocialAssetRetentionService, *,
                 clock: Callable[[], float] = time.time):
        self._assets = asset_repo
        self._retention = retention
        self._clock = clock

    def run(self, *, apply: bool = False, limit: int = 100) -> dict[str, Any]:
        run_id = self._assets.start_reconcile_run("sweep-apply" if apply else "sweep-dry-run")
        counts = {"scanned": 0, "safe": 0, "ambiguous": 0, "changed": 0, "failed": 0}
        planned: list[dict[str, Any]] = []
        try:
            for row in self._assets.list_due_retentions(self._clock(), limit=limit):
                counts["scanned"] += 1
                safe, why = self._retention.evaluate_for_trash(row)
                entry = {"chapter_id": row["chapter_id"], "state": row["state"], "decision": why}
                if not safe:
                    counts["ambiguous"] += 1
                    planned.append(entry)
                    continue
                counts["safe"] += 1
                if not apply:
                    entry["would"] = "move_to_trash"   # dry-run changes nothing
                    planned.append(entry)
                    continue
                outcome = self._retention.move_to_trash(row, actor_id="sweep")
                entry["result"] = outcome
                planned.append(entry)
                if outcome == "trashed":
                    counts["changed"] += 1
                elif outcome == "failed":
                    counts["failed"] += 1
            status = "ok"
        except Exception:
            status = "error"
            raise
        finally:
            self._assets.finish_reconcile_run(run_id, status=status, counts=counts,
                                              summary={"items": planned[:50]})
        return {"run_id": run_id, "mode": "apply" if apply else "dry-run",
                "counts": counts, "items": planned}


class SocialAssetReconciliationService:
    """Classify the app's own asset records; auto-fix only unambiguous lag."""

    def __init__(self, asset_repo, *, provider_factory: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.time):
        self._assets = asset_repo
        self._provider_factory = provider_factory
        self._clock = clock

    def _remote_state(self, storage_id: str) -> str:
        """'present' | 'trashed' | 'missing' | 'unknown' — by known id only."""
        if not storage_id:
            return "unknown"
        factory = self._provider_factory
        if factory is None:
            return "unknown"
        try:
            meta = factory().stat_file(storage_id)
        except Exception:
            return "missing"
        return "trashed" if getattr(meta, "trashed", False) else "present"

    def run(self, *, apply: bool = False, limit: int = 200) -> dict[str, Any]:
        run_id = self._assets.start_reconcile_run("reconcile-apply" if apply else "reconcile-dry-run")
        counts = {"scanned": 0, "safe": 0, "ambiguous": 0, "changed": 0, "failed": 0}
        findings: list[dict[str, Any]] = []
        try:
            now = self._clock()
            for row in self._assets.list_retentions_for_reconcile(limit=limit):
                counts["scanned"] += 1
                finding = {"chapter_id": row["chapter_id"], "state": row["state"]}
                try:
                    storage_id = self._assets.publication_storage_id_for_retention(row)
                except Exception:
                    finding["category"] = RECONCILE_REQUIRED
                    finding["why"] = "publication_unresolved"
                    counts["ambiguous"] += 1
                    findings.append(finding)
                    continue
                remote = self._remote_state(storage_id)
                referenced = self._assets.publication_reference_count(row["publication_id"]) > 0

                if row["state"] == "pending_trash" and remote == "trashed":
                    # Unambiguous local lag: the Drive already confirms the trash.
                    finding["category"] = HEALTHY
                    finding["why"] = "local_state_behind"
                    if apply and self._assets.transition_retention(
                            row["id"], to_state="trashed", expected_version=row["version"],
                            actor_id="reconcile", reason="drive_confirms_trashed",
                            trashed_at=now):
                        counts["changed"] += 1
                    counts["safe"] += 1
                elif row["state"] == "trashed" and remote == "present":
                    finding["category"] = RECONCILE_REQUIRED
                    finding["why"] = "local_trashed_but_remote_active"
                    counts["ambiguous"] += 1
                elif remote == "missing":
                    finding["category"] = RECONCILE_REQUIRED
                    finding["why"] = "remote_file_missing"
                    counts["ambiguous"] += 1
                elif referenced:
                    finding["category"] = RETAINED
                    finding["why"] = "still_referenced"
                    counts["safe"] += 1
                elif row["retain_until"] > now:
                    finding["category"] = RETAINED
                    finding["why"] = "within_retention"
                    counts["safe"] += 1
                elif row["state"] in ("retained", "failed"):
                    finding["category"] = SAFE_FOR_TRASH
                    finding["why"] = "expired_and_unreferenced"
                    counts["safe"] += 1
                else:
                    finding["category"] = ORPHAN_CANDIDATE
                    finding["why"] = "no_reference_outside_retention"
                    counts["ambiguous"] += 1
                findings.append(finding)
            status = "ok"
        except Exception:
            status = "error"
            raise
        finally:
            self._assets.finish_reconcile_run(run_id, status=status, counts=counts,
                                              summary={"findings": findings[:50]})
        return {"run_id": run_id, "mode": "apply" if apply else "dry-run",
                "counts": counts, "findings": findings}
