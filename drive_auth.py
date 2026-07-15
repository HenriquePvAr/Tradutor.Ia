"""Administrative Google OAuth helper and token source for the community storage.

The community storage belongs to the administrator's own Google account, so it uses an
OAuth 2.0 user credential (not a service account, which cannot own files in My Drive).
This module stores the refresh/access token outside the repository, with an atomic write
and restricted permissions, and refreshes the access token on demand. Credentials are
never written to logs, never returned to the frontend, and never stored in the community
database.

This task implements and unit-tests the parsing, storage and refresh with fakes. It does
not run real OAuth: ``authorize`` prints the consent URL and the manual next step but
opens no browser and exchanges no code unless one is explicitly provided offline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

from community_storage import StorageError

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE = "https://www.googleapis.com/auth/drive.file"
_REDACT = "[REDACTED]"


@dataclass
class OAuthTokens:
    access_token: str = ""
    refresh_token: str = ""
    expiry: float = 0.0

    def expired(self, *, skew: float = 60.0) -> bool:
        return not self.access_token or time.time() >= (self.expiry - skew)


def load_tokens(path: Path) -> OAuthTokens:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return OAuthTokens()
    return OAuthTokens(access_token=data.get("access_token", ""),
                       refresh_token=data.get("refresh_token", ""),
                       expiry=float(data.get("expiry", 0) or 0))


def save_tokens(path: Path, tokens: OAuthTokens) -> None:
    """Atomically write the token file with owner-only permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(tokens)), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # best-effort on filesystems without POSIX perms
    os.replace(tmp, path)


class FileTokenSource:
    """A TokenSource backed by a token file, refreshing the access token when expired.

    The HTTP transport is injected so refresh is testable without network. The client id
    and secret come from configuration, never from the token file's serialized output in
    logs.
    """

    def __init__(self, *, token_path: str | Path, client_id: str, client_secret: str,
                 transport, token_url: str = TOKEN_URL):
        self.token_path = Path(token_path)
        self.client_id = client_id
        self.client_secret = client_secret
        self._t = transport
        self.token_url = token_url
        self._tokens = load_tokens(self.token_path)

    def access_token(self) -> str:
        if self._tokens.expired():
            self.refresh()
        if not self._tokens.access_token:
            raise StorageError("no google access token; run drive_auth.py authorize")
        return self._tokens.access_token

    def refresh(self) -> str:
        if not self._tokens.refresh_token:
            raise StorageError("no refresh token; run drive_auth.py authorize", status=401)
        body = _form({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self._tokens.refresh_token, "grant_type": "refresh_token"})
        resp = self._t.request("POST", self.token_url,
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               data=body)
        if resp.status != 200:
            raise StorageError(f"token refresh failed: {resp.status}",
                               transient=resp.status >= 500, status=resp.status)
        data = resp.json()
        self._tokens.access_token = data.get("access_token", "")
        self._tokens.expiry = time.time() + float(data.get("expires_in", 3600) or 3600)
        if data.get("refresh_token"):
            self._tokens.refresh_token = data["refresh_token"]
        save_tokens(self.token_path, self._tokens)
        return self._tokens.access_token


def _form(fields: dict[str, str]) -> bytes:
    from urllib.parse import urlencode
    return urlencode(fields).encode("utf-8")


def build_authorize_url(client_id: str, redirect_uri: str) -> str:
    from urllib.parse import urlencode
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": SCOPE, "access_type": "offline", "prompt": "consent"}
    return f"{AUTH_URL}?{urlencode(params)}"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _token_path() -> Path:
    return Path(_env("GOOGLE_OAUTH_TOKEN_PATH", str(Path.home() / ".tradutor_ia" / "drive_token.json")))


# ---- CLI (no real OAuth in this task) --------------------------------------
def cmd_authorize(_args) -> int:
    client_id = _env("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        print("set GOOGLE_OAUTH_CLIENT_ID first", file=sys.stderr)
        return 2
    url = build_authorize_url(client_id, "http://localhost:8765/callback")
    print("Open this URL in your browser to authorize (not opened automatically):")
    print(url)
    print("Then exchange the code offline; this task does not perform the exchange.")
    return 0


def cmd_status(_args) -> int:
    tokens = load_tokens(_token_path())
    # Never print token values, only their presence and expiry.
    print(f"token_path: {_token_path()}")
    print(f"has_refresh_token: {bool(tokens.refresh_token)}")
    print(f"has_access_token: {bool(tokens.access_token)}")
    print(f"access_expired: {tokens.expired()}")
    return 0


def cmd_revoke(_args) -> int:
    path = _token_path()
    if path.exists():
        path.unlink()
        print("local token removed")
    else:
        print("no local token to remove")
    return 0


def cmd_test_access(_args) -> int:
    # Would build the provider and call health_check; skipped here to avoid real network.
    print("test-access requires a configured transport; not run in offline mode")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Administrative Google Drive OAuth helper")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("authorize").set_defaults(func=cmd_authorize)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("revoke").set_defaults(func=cmd_revoke)
    sub.add_parser("test-access").set_defaults(func=cmd_test_access)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
