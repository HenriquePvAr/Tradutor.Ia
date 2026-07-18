"""Manual maintenance CLI for social asset retention.

Never runs on import, on startup, or from the web app — an operator must invoke it.
Every subcommand is dry-run unless ``--apply`` is passed explicitly, and no subcommand can
permanently delete a file or empty the Drive trash.

    python social_asset_maintenance_cli.py retention-scan
    python social_asset_maintenance_cli.py retention-sweep            # dry-run
    python social_asset_maintenance_cli.py retention-sweep --apply
    python social_asset_maintenance_cli.py reconcile --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chapter_asset_repository import ChapterAssetRepository
from social_asset_reconcile import SocialAssetReconciliationService, SocialAssetRetentionSweep
from social_asset_retention import SocialAssetRetentionService, retention_days, sweep_enabled


def _repo(db: str) -> ChapterAssetRepository:
    return ChapterAssetRepository(Path(db))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent
                                        / ".cache" / "runtime" / "social_assets.sqlite3"),
                        help="asset DB (defaults to the one the app uses)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("retention-scan")
    for name in ("retention-sweep", "reconcile"):
        p = sub.add_parser(name)
        p.add_argument("--apply", action="store_true",
                       help="perform the changes (default: dry-run, nothing is modified)")
        p.add_argument("--limit", type=int, default=100)

    args = parser.parse_args(argv)
    assets = _repo(args.db)
    try:
        retention = SocialAssetRetentionService(assets)
        if args.command == "retention-scan":
            rows = assets.list_retentions_for_reconcile(limit=500)
            due = assets.list_due_retentions(__import__("time").time(), limit=500)
            out = {"retention_days": retention_days(), "sweep_enabled": sweep_enabled(),
                   "live": len(rows), "due": len(due),
                   "items": [retention._dto(r) for r in rows[:50]]}
        elif args.command == "retention-sweep":
            out = SocialAssetRetentionSweep(assets, retention).run(apply=args.apply, limit=args.limit)
        else:
            out = SocialAssetReconciliationService(assets).run(apply=args.apply, limit=args.limit)
        print(json.dumps(out, indent=2, ensure_ascii=False))  # DTOs only: no ids, no paths
    finally:
        assets.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
