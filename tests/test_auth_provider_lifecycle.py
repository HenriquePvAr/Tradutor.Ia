"""Covers the auth-provider lifecycle contract app_ui.py and community_auth.py must keep.

Root cause this covers: `app_ui.py` executed `AUTH = build_auth_provider()` at module
scope, so any import of `app_ui` (including `pytest --collect-only`) built the real
provider immediately. With no Supabase configuration present -- which is the case on
the "Hermetic Tests" CI runner, see .github/workflows/tests.yml, which sets no
`env:` and references no `secrets.*`/`vars.*` for auth -- that raised
`AuthConfigurationError` and aborted collection.

The fix defers construction to `community_auth.get_auth_provider()`, a cached factory
called from `app_ui._build_auth_provider_at_startup()` (an `@app.on_startup` hook) or
lazily via `app_ui._LazyAuthProvider.__getattr__` on first real use. Import now only
needs a stable, reassignable `app_ui.AUTH` reference; misconfiguration still fails
closed, just at startup/first-use instead of at import.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import community_auth

ROOT = Path(__file__).resolve().parent

_SCRUBBED_PREFIXES = ("SUPABASE_", "AUTH_PROVIDER", "COMMUNITY_AUTH_PROVIDER",
                      "ALLOW_LOCAL_TEST_IDENTITIES", "LOCAL_TEST_AUTH", "BETTER_AUTH_")


def _clean_env(**overrides: str) -> dict[str, str]:
    """A subprocess environment with every auth-related variable removed."""
    env = {k: v for k, v in os.environ.items()
           if not any(k.startswith(prefix) for prefix in _SCRUBBED_PREFIXES)}
    env.update(overrides)
    return env


def _run(code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
        timeout=60, env=env if env is not None else _clean_env())


def _local_test_env(tmp_dir: str, **overrides: str) -> dict[str, str]:
    """Synthetic, hermetic config for the explicit local_test auth provider.

    No real Supabase project, no network, no GitHub secret is ever needed here.
    """
    values = {
        "AUTH_PROVIDER": "local_test",
        "APP_ENV": "test",
        "ALLOW_LOCAL_TEST_IDENTITIES": "1",
        "TRADUTOR_UI_HOST": "127.0.0.1",
        "LOCAL_TEST_AUTH_DB": str(Path(tmp_dir) / "local_test_auth.sqlite3"),
        "LOCAL_TEST_AUTH_SESSION_SECRET": "synthetic-test-only-" + ("x" * 40),
        "SUPABASE_URL": "",
        "SUPABASE_JWKS_URL": "",
        "BETTER_AUTH_INTERNAL_URL": "",
    }
    values.update(overrides)
    return values


class ImportIsHermeticTests(unittest.TestCase):
    """Item 1/2: app_ui must import and collect with zero Supabase configuration."""

    def test_import_app_ui_without_supabase_config_succeeds(self):
        result = _run("import app_ui")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_collect_only_owner_and_provider_modules_without_supabase_config_succeeds(self):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "test_owner_scoped_ui.py", "test_provider_selection.py",
             "test_ui_runtime_dependency.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=120, env=_clean_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ImportHasNoSideEffectsTests(unittest.TestCase):
    """Items 3-6: import builds nothing, opens no socket, starts no server/session."""

    def test_import_does_not_call_build_auth_provider(self):
        code = (
            "import community_auth\n"
            "def _boom(*a, **k):\n"
            "    raise AssertionError('build_auth_provider called at import time')\n"
            "community_auth.build_auth_provider = _boom\n"
            "import app_ui\n"
            "print('OK')\n"
        )
        result = _run(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_import_does_not_open_a_socket(self):
        code = (
            "import socket\n"
            "def _blocked(self, *a, **k):\n"
            "    raise AssertionError('network connection attempted at import time')\n"
            "socket.socket.connect = _blocked\n"
            "import app_ui\n"
            "print('OK')\n"
        )
        result = _run(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_import_does_not_start_the_nicegui_server(self):
        code = "import app_ui\nassert not app_ui.app.is_started\nprint('OK')\n"
        result = _run(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_import_does_not_build_or_cache_a_provider_session(self):
        code = (
            "import app_ui\n"
            "import community_auth\n"
            "assert community_auth._AUTH_PROVIDER_CACHE is None, "
            "'a provider (and any session it holds) was built at import time'\n"
            "print('OK')\n"
        )
        result = _run(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)


class AuthProviderFactoryLifecycleTests(unittest.TestCase):
    """Items 7, 8, 9, 10, 12, 14: the cached factory's real behaviour, in-process."""

    def setUp(self):
        community_auth.reset_auth_provider_cache()
        self.addCleanup(community_auth.reset_auth_provider_cache)
        self._tmp = tempfile.mkdtemp()
        # Windows can still hold a lock on the sqlite file from a provider's open
        # connection when the test ends; best-effort cleanup, never fail the test on it.
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))

    def test_get_auth_provider_builds_once_and_caches(self):
        calls = []
        orig = community_auth.build_auth_provider

        def counting(*a, **k):
            calls.append(1)
            return orig(*a, **k)

        community_auth.build_auth_provider = counting
        self.addCleanup(lambda: setattr(community_auth, "build_auth_provider", orig))
        env = _local_test_env(self._tmp)
        first = community_auth.get_auth_provider(env)
        second = community_auth.get_auth_provider(env)
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1, "get_auth_provider must not rebuild on repeat calls")

    def test_missing_config_fails_closed_never_falls_back_to_local(self):
        with self.assertRaises(community_auth.AuthConfigurationError):
            community_auth.get_auth_provider({})

    def test_local_provider_is_never_chosen_implicitly(self):
        # An empty/unset env defaults to "supabase", not "local" -- silently downgrading
        # to a weaker provider on missing config would be exactly the fallback this
        # mission forbids.
        with self.assertRaises(community_auth.AuthConfigurationError) as ctx:
            community_auth.build_auth_provider({})
        self.assertIn("supabase", str(ctx.exception).lower())

    def test_config_error_is_sanitized(self):
        try:
            community_auth.get_auth_provider({"SUPABASE_URL": "https://example.invalid"})
        except community_auth.AuthConfigurationError as exc:
            message = str(exc)
            for leak in ("service_role", "SERVICE_ROLE", "eyJ", "supabase.co"):
                self.assertNotIn(leak, message)
        else:
            self.fail("expected AuthConfigurationError for an incomplete Supabase config")

    def test_repeated_calls_do_not_create_duplicate_providers(self):
        env = _local_test_env(self._tmp)
        providers = {id(community_auth.get_auth_provider(env)) for _ in range(5)}
        self.assertEqual(len(providers), 1)

    def test_reset_allows_a_new_provider_after_cleanup(self):
        env = _local_test_env(self._tmp)
        first = community_auth.get_auth_provider(env)
        community_auth.reset_auth_provider_cache()
        second = community_auth.get_auth_provider(env)
        self.assertIsNot(first, second)


class StartupHookTests(unittest.TestCase):
    """Items 7, 8: the @app.on_startup hook builds once and fails closed."""

    def setUp(self):
        import app_ui  # noqa: F401  (import after community_auth is already loaded)
        community_auth.reset_auth_provider_cache()
        self.addCleanup(community_auth.reset_auth_provider_cache)
        self._tmp = tempfile.mkdtemp()
        # Windows can still hold a lock on the sqlite file from a provider's open
        # connection when the test ends; best-effort cleanup, never fail the test on it.
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self._env_backup = dict(os.environ)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _set_env(self, values: dict[str, str]) -> None:
        for key in list(os.environ):
            if any(key.startswith(p) for p in _SCRUBBED_PREFIXES):
                del os.environ[key]
        os.environ.update(values)

    def test_startup_hook_builds_the_provider_once(self):
        import app_ui
        self._set_env(_local_test_env(self._tmp))
        asyncio.run(app_ui._build_auth_provider_at_startup())
        built = community_auth._AUTH_PROVIDER_CACHE
        self.assertIsNotNone(built)
        asyncio.run(app_ui._build_auth_provider_at_startup())
        self.assertIs(community_auth._AUTH_PROVIDER_CACHE, built,
                      "a second startup call must not build a duplicate provider")

    def test_startup_hook_fails_closed_without_config(self):
        import app_ui
        self._set_env({})
        with self.assertRaises(community_auth.AuthConfigurationError):
            asyncio.run(app_ui._build_auth_provider_at_startup())


class FakeInjectionTests(unittest.TestCase):
    """Item 11: hermetic tests can inject an explicit fake without touching env/network."""

    def test_module_attribute_accepts_an_explicit_fake(self):
        import app_ui
        sentinel = object()
        original = app_ui.AUTH
        app_ui.AUTH = sentinel
        try:
            self.assertIs(app_ui.AUTH, sentinel)
        finally:
            app_ui.AUTH = original


if __name__ == "__main__":
    unittest.main()
