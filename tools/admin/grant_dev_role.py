"""Dry-run-only scaffold; deployment credential must be supplied externally."""
import argparse
p = argparse.ArgumentParser(); p.add_argument("user"); p.add_argument("--apply", action="store_true")
a = p.parse_args()
print(f"DEV_GRANT user={a.user} mode={'APPLY' if a.apply else 'DRY_RUN'}")
if a.apply: raise SystemExit("remote_mutation_not_enabled_in_workspace")
