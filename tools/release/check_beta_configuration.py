"""Sanitized beta configuration report; never prints secret values."""
from __future__ import annotations
import os

def state(*names: str) -> str:
    return "READY" if all(os.getenv(name) for name in names) else "NOT_CONFIGURED"

def main() -> None:
    print("SUPABASE=" + state("SUPABASE_URL", "SUPABASE_ANON_KEY"))
    print("AUTH=" + state("SUPABASE_URL", "SUPABASE_ANON_KEY"))
    print("SMTP=" + state("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))
    print("DEEPL_PROXY=" + state("DEEPL_API_KEY"))
    print("BILLING=DISABLED")
    print("REWARDED_ADS=DISABLED")
    print("UPDATER_HOSTING=" + ("READY" if os.getenv("UPDATE_MANIFEST_URL") else "NOT_CONFIGURED"))
    print("UPDATE_PUBLIC_KEY=" + state("UPDATE_PUBLIC_KEY"))

if __name__ == "__main__":
    main()
