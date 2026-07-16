"""Central, validated construction of the Google Drive provider for publish and read.

One factory builds the provider the same way for the worker's publish runner and the
API's read path, so there is a single place that reads configuration, validates it, and
wires the transport + token source + provider together. Secrets (client id/secret, token)
are read only from the environment and the token file - never from the job configuration
persisted in the database - and never appear in ``repr`` or errors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from community_storage import StorageError

# Least-privilege scope: drive.file only sees files the app created, which fits the
# app-managed folder model. A broader scope is never requested implicitly.
DEFAULT_SCOPES = ("https://www.googleapis.com/auth/drive.file",)


@dataclass
class GoogleDriveConfig:
    root_folder_id: str
    token_path: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    chunk_size: int = 8 * 1024 * 1024
    max_retries: int = 5

    def __repr__(self) -> str:  # never leak secrets in logs or tracebacks
        return (f"GoogleDriveConfig(root_folder_id={_mask(self.root_folder_id)!r}, "
                f"token_path=<{('configured' if self.token_path else 'missing')}>, "
                f"client_id=<redacted>, "
                f"client_secret=<redacted>, scopes={self.scopes})")

    @classmethod
    def from_env(cls, *, root_folder_id_override: str = "", env: dict | None = None
                 ) -> "GoogleDriveConfig":
        env = env if env is not None else os.environ
        root = root_folder_id_override or env.get("COMMUNITY_DRIVE_ROOT_FOLDER_ID", "")
        token_path = env.get("GOOGLE_OAUTH_TOKEN_PATH", "")
        client_id = env.get("GOOGLE_OAUTH_CLIENT_ID", "")
        client_secret = env.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
        missing = [name for name, value in (
            ("COMMUNITY_DRIVE_ROOT_FOLDER_ID", root),
            ("GOOGLE_OAUTH_TOKEN_PATH", token_path),
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
        ) if not value]
        if missing:
            # Fail closed with a clear message and no network.
            raise StorageError("google_drive not configured; missing: " + ", ".join(missing))
        scopes_env = env.get("GOOGLE_OAUTH_SCOPES", "")
        scopes = tuple(s.strip() for s in scopes_env.split(",") if s.strip()) or DEFAULT_SCOPES
        return cls(root_folder_id=root, token_path=token_path, client_id=client_id,
                   client_secret=client_secret, scopes=scopes)


def _mask(value: str) -> str:
    if not value:
        return ""
    return f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "..."


def build_google_drive_provider(config: GoogleDriveConfig, *, transport=None, tokens=None):
    """Build a GoogleDriveStorageProvider from validated config.

    Transport and token source are injectable so the whole wiring is exercised offline in
    tests with fakes; production leaves them None and gets the real requests transport and
    the file-backed token source.
    """
    from google_drive_storage import GoogleDriveStorageProvider
    if transport is None:
        from google_drive_transport import RequestsHttpTransport
        transport = RequestsHttpTransport(connect_timeout=config.connect_timeout,
                                          read_timeout=config.read_timeout)
    if tokens is None:
        from drive_auth import FileTokenSource
        tokens = FileTokenSource(token_path=config.token_path, client_id=config.client_id,
                                 client_secret=config.client_secret, transport=transport)
    return GoogleDriveStorageProvider(transport=transport, tokens=tokens,
                                      root_folder_id=config.root_folder_id,
                                      chunk_size=config.chunk_size)
