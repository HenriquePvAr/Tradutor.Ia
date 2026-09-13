"""Sanitized beta configuration report; never prints secret values."""
from __future__ import annotations
import os
import json
from pathlib import Path

def state(*names: str) -> str:
    return "READY" if all(os.getenv(name) for name in names) else "NOT_CONFIGURED"

def public_runtime_state() -> str:
    path = Path(__file__).parents[2] / "config" / "public-runtime.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "INVALID"
    required = ("supabase_url", "publishable_key")
    return "READY" if all(str(payload.get(name, "") or "").strip() for name in required) else "INCOMPLETE"

def main() -> None:
    print("PUBLIC_RUNTIME=" + public_runtime_state())
    print("SUPABASE=" + state("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY"))
    print("AUTH=" + state("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY"))
    print("SMTP=" + state("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))
    print("DEEPL_PROXY=" + state("DEEPL_API_KEY"))
    print("BILLING=DISABLED")
    print("REWARDED_ADS=DISABLED")
    print("UPDATER_HOSTING=" + ("READY" if os.getenv("TRADUTOR_IA_UPDATE_MANIFEST_URL") else "NOT_CONFIGURED"))
    print("UPDATE_PUBLIC_KEY=" + state("UPDATE_PUBLIC_KEY"))
    print("BETA_LICENSE=" + state("BETA_LICENSE_PROVIDER"))

if __name__ == "__main__":
    main()
