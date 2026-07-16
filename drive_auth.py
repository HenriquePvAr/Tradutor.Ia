"""Administrative Google OAuth helper and token source for the community storage.

The community storage belongs to the administrator's own Google account, so it uses an
OAuth 2.0 user credential (not a service account, which cannot own files in My Drive).
This module stores the refresh/access token outside the repository, with an atomic write
and restricted permissions, and refreshes the access token on demand. Credentials are
never written to logs, never returned to the frontend, and never stored in the community
database.

``authorize`` runs the official Installed-App OAuth flow: a loopback redirect on
127.0.0.1 with an ephemeral port, PKCE and offline access, opening the system browser -
never an out-of-band copy/paste flow, a fixed port, or an embedded webview. It only runs
when an administrator invokes it directly; the backend never triggers OAuth during a web
request. Tests mock the flow seam, so no browser opens and no real Google endpoint is hit.
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
from local_environment import load_local_environment_for_entrypoint

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


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _token_path() -> Path:
    return Path(_env("GOOGLE_OAUTH_TOKEN_PATH", str(Path.home() / ".tradutor_ia" / "drive_token.json")))


def _config_scopes() -> list[str]:
    raw = _env("GOOGLE_OAUTH_SCOPES", "")
    return [s.strip() for s in raw.split(",") if s.strip()] or [SCOPE]


def run_installed_app_flow(*, client_id: str, client_secret: str, scopes: list[str]) -> OAuthTokens:
    """Run the official Installed-App OAuth flow on a loopback redirect.

    Isolated so tests mock exactly this seam - no browser opens and no Google endpoint is
    hit under test. Uses an ephemeral loopback port (127.0.0.1:0), offline access for a
    refresh token, and the library's built-in PKCE; never OOB, a fixed port, or a webview.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": AUTH_URL,
            "token_uri": TOKEN_URL,
            "redirect_uris": ["http://127.0.0.1"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True,
                                  access_type="offline", prompt="consent",
                                  authorization_prompt_message="",
                                  success_message="Autorizado. Pode fechar esta aba.")
    expiry = creds.expiry.timestamp() if getattr(creds, "expiry", None) else time.time() + 3600
    return OAuthTokens(access_token=creds.token or "",
                       refresh_token=creds.refresh_token or "",
                       expiry=float(expiry))


# ---- CLI --------------------------------------------------------------------
def cmd_authorize(_args) -> int:
    client_id = _env("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = _env("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET first", file=sys.stderr)
        return 2
    tokens = run_installed_app_flow(client_id=client_id, client_secret=client_secret,
                                    scopes=_config_scopes())
    if not tokens.refresh_token:
        print("no refresh token returned; re-run authorize with consent", file=sys.stderr)
        return 1
    save_tokens(_token_path(), tokens)
    # Never print token values.
    print("authorized; token saved to", _token_path())
    return 0


def cmd_status(_args) -> int:
    tokens = load_tokens(_token_path())
    print(f"token_path: {_token_path()}")
    print(f"client_configured: {bool(_env('GOOGLE_OAUTH_CLIENT_ID') and _env('GOOGLE_OAUTH_CLIENT_SECRET'))}")
    print(f"root_folder_configured: {bool(_env('COMMUNITY_DRIVE_ROOT_FOLDER_ID'))}")
    print(f"provider: {_env('COMMUNITY_STORAGE_PROVIDER', 'filesystem')}")
    print(f"has_refresh_token: {bool(tokens.refresh_token)}")
    print(f"has_access_token: {bool(tokens.access_token)}")
    print(f"access_expired: {tokens.expired()}")
    print(f"refresh_possible: {bool(tokens.refresh_token)}")
    print(f"scopes: {','.join(_config_scopes())}")
    return 0


def cmd_revoke(args) -> int:
    if not getattr(args, "yes", False):
        print("this removes the local token (and revokes it online when possible).")
        print("re-run with --yes to confirm.")
        return 1
    path = _token_path()
    tokens = load_tokens(path)
    if tokens.refresh_token:
        try:
            from google_drive_transport import RequestsHttpTransport
            RequestsHttpTransport().request(
                "POST", REVOKE_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=_form({"token": tokens.refresh_token}))
            print("online revoke attempted")
        except Exception:  # noqa: BLE001 - local removal proceeds regardless
            print("online revoke failed; removing local token anyway")
    if path.exists():
        path.unlink()
        print("local token removed")
    else:
        print("no local token to remove")
    return 0


def cmd_test_access(_args) -> int:
    """Build the real provider and confirm the root folder is reachable. No upload."""
    from google_drive_factory import GoogleDriveConfig, build_google_drive_provider
    try:
        config = GoogleDriveConfig.from_env()
        provider = build_google_drive_provider(config)
        meta = provider.stat_file(config.root_folder_id)
    except StorageError as exc:
        print(f"test-access failed: {exc}", file=sys.stderr)
        return 1
    is_folder = meta.mime_type == "application/vnd.google-apps.folder"
    print(f"account_reachable: True")
    print(f"root_folder_exists: {bool(meta.file_id)}")
    print(f"root_is_folder: {is_folder}")
    print(f"root_trashed: {meta.trashed}")
    print(f"provider_healthy: {provider.health_check()}")
    return 0 if (meta.file_id and is_folder and not meta.trashed) else 1


def cmd_create_root_folder(args) -> int:
    """Create a private app-owned folder (drive.file scope) and print only its ID."""
    from google_drive_factory import GoogleDriveConfig, build_google_drive_provider
    name = getattr(args, "name", "") or "TradutorIA-Comunidade"
    try:
        # Root folder id is not required to create a top-level app folder.
        config = GoogleDriveConfig.from_env(root_folder_id_override="root")
        provider = build_google_drive_provider(config)
        folder_id = provider.ensure_folder(name, "root")
    except StorageError as exc:
        print(f"create-root-folder failed: {exc}", file=sys.stderr)
        return 1
    print("created private folder; set COMMUNITY_DRIVE_ROOT_FOLDER_ID to its id")
    print(f"folder_id: {folder_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if not load_local_environment_for_entrypoint():
        return 2
    parser = argparse.ArgumentParser(description="Administrative Google Drive OAuth helper")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("authorize").set_defaults(func=cmd_authorize)
    sub.add_parser("status").set_defaults(func=cmd_status)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--yes", action="store_true")
    revoke.set_defaults(func=cmd_revoke)
    sub.add_parser("test-access").set_defaults(func=cmd_test_access)
    crf = sub.add_parser("create-root-folder")
    crf.add_argument("--name", default="TradutorIA-Comunidade")
    crf.set_defaults(func=cmd_create_root_folder)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
