"""Dry-run-only beta license grant scaffold."""
import argparse
p = argparse.ArgumentParser(); p.add_argument("user"); p.add_argument("--days", type=int, default=30); p.add_argument("--plan", default="beta"); p.add_argument("--max-devices", type=int, default=1); p.add_argument("--apply", action="store_true")
a = p.parse_args()
if a.days not in (15, 30, 60, 90): raise SystemExit("days_must_be_15_30_60_90")
print(f"LICENSE_GRANT user={a.user} plan={a.plan} days={a.days} max_devices={a.max_devices} mode={'APPLY' if a.apply else 'DRY_RUN'}")
if a.apply: raise SystemExit("remote_mutation_not_enabled_in_workspace")
