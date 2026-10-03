"""Runtime root for a job must follow the trusted runtime, never ``output_dir``.

Physically proven root cause (job 63c463…): the auth envelope was sealed under the
job's runtime root (``…\\yomu-worker-contract-v2\\auth``) but ``job.output_dir`` lived
under the DEFAULT root (``…\\TradutorIA\\output``).  ``job_runner`` derived the runner's
runtime root FROM ``output_dir`` (an ``…/output`` lineage override), so the runner looked
for the envelope in ``TradutorIA\\auth`` (empty) → ``FileNotFoundError`` →
``auth_context_missing_or_corrupt``.  Download 105/105 and OCR 105/105 had already passed.

``resolve_runtime_root_for_job`` now makes the runtime root the source of truth:
explicit ``TRADUTOR_RUNTIME_ROOT`` → DB parent → (legacy) output lineage.  ``output_dir``
can never relocate ``auth/``, the queue DB, or the secure auth context.
"""
import os
import tempfile
import unittest
from pathlib import Path

from runtime_paths import resolve_runtime_root_for_job
from secure_auth_context import AuthEnvelopeStore


class _Scenario:
    """A temp layout: an isolated runtime (db+auth) and a default-root output_dir."""
    def __init__(self, stack):
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.iso = tmp / "Temp" / "yomu-worker-contract-v2"
        (self.iso / "auth").mkdir(parents=True)
        self.db = self.iso / "jobs.sqlite3"
        self.db.write_text("x", encoding="utf-8")
        self.default = tmp / "Local" / "TradutorIA"
        self.output_dir = self.default / "output" / "chapter" / "job"
        self.output_dir.mkdir(parents=True)


class RuntimeRootResolutionTests(unittest.TestCase):
    def _scn(self):
        import contextlib
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        return _Scenario(stack)

    # --- item 7 / 10: the exact bug — explicit env + cross-root output ---
    def test_explicit_env_wins_over_cross_root_output(self):
        s = self._scn()
        root = resolve_runtime_root_for_job(
            str(s.db), str(s.output_dir), env={"TRADUTOR_RUNTIME_ROOT": str(s.iso)})
        self.assertEqual(root, s.iso)
        self.assertNotIn("TradutorIA", root.parts)

    # --- item 8: no env — DB parent wins over output ---
    def test_db_parent_wins_over_output_when_no_env(self):
        s = self._scn()
        root = resolve_runtime_root_for_job(str(s.db), str(s.output_dir), env={})
        self.assertEqual(root, s.iso)

    # --- item 2: output can NEVER override a known runtime root ---
    def test_output_never_overrides_explicit_env(self):
        s = self._scn()
        # even a bogus, non-existent output on another root cannot move it
        root = resolve_runtime_root_for_job(
            str(s.db), r"Z:\Elsewhere\output\x", env={"TRADUTOR_RUNTIME_ROOT": str(s.iso)})
        self.assertEqual(root, s.iso)

    def test_output_never_overrides_db_parent(self):
        s = self._scn()
        root = resolve_runtime_root_for_job(str(s.db), str(s.output_dir), env={})
        self.assertEqual(root, s.iso)

    # --- item 9: legacy fallback ONLY when neither env nor db is usable ---
    def test_legacy_output_lineage_is_last_resort(self):
        s = self._scn()
        root = resolve_runtime_root_for_job("", str(s.output_dir), env={})
        self.assertEqual(root, s.default)  # <root>/output/... → <root> = TradutorIA

    def test_invalid_explicit_env_falls_through_to_db_parent(self):
        s = self._scn()
        root = resolve_runtime_root_for_job(
            str(s.db), str(s.output_dir), env={"TRADUTOR_RUNTIME_ROOT": r"Z:\does\not\exist"})
        self.assertEqual(root, s.iso)

    # --- legacy contract preserved: DB one level ABOVE runtime, output UNDER it ---
    def test_db_above_runtime_with_same_tree_output_uses_output_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "jobs.sqlite3"; db.write_text("x", encoding="utf-8")  # one level above
            runtime = base / "runtime"; (runtime / "auth").mkdir(parents=True)
            out = runtime / "output" / "chapter"; out.mkdir(parents=True)
            # no explicit env: the <runtime>/output lineage (a subdir of db.parent) wins
            root = resolve_runtime_root_for_job(str(db), str(out), env={})
            self.assertEqual(root, runtime)  # NOT base (db.parent)

    def test_foreign_root_output_never_wins_over_db_parent(self):
        # The bug: output on a completely different tree must not relocate the runtime.
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            iso = Path(a) / "yomu-worker-contract-v2"; iso.mkdir()
            db = iso / "jobs.sqlite3"; db.write_text("x", encoding="utf-8")
            foreign_out = Path(b) / "TradutorIA" / "output" / "job"; foreign_out.mkdir(parents=True)
            root = resolve_runtime_root_for_job(str(db), str(foreign_out), env={})
            self.assertEqual(root, iso)  # db.parent, not the foreign TradutorIA

    # --- item 13: default runtime still works (no isolation, no divergence) ---
    def test_default_runtime_consistent_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "TradutorIA"
            (base / "auth").mkdir(parents=True)
            db = base / "jobs.sqlite3"; db.write_text("x", encoding="utf-8")
            out = base / "output" / "chapter"; out.mkdir(parents=True)
            # no env: db.parent == base, output lineage == base → both agree
            root = resolve_runtime_root_for_job(str(db), str(out), env={})
            self.assertEqual(root, base)


class CrossProcessAuthAfterFixTests(unittest.TestCase):
    """End-to-end: seal in runtime X, resolve with output on root Y, acquire → PASS."""

    def test_auth_acquire_follows_runtime_not_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            iso = tmp / "yomu-worker-contract-v2"; (iso / "auth").mkdir(parents=True)
            db = iso / "jobs.sqlite3"; db.write_text("x", encoding="utf-8")
            output_dir = tmp / "TradutorIA" / "output" / "job"; output_dir.mkdir(parents=True)
            job_id = "63c463e49ce2469985eb8139ba0243df"

            # app-side: seal the auth envelope under the runtime root
            AuthEnvelopeStore(iso).seal(job_id, "dummy.header.body", user_id="tester")
            self.assertTrue((iso / "auth" / (job_id + ".auth")).exists())
            self.assertFalse((tmp / "TradutorIA" / "auth").exists())  # nothing under output root

            # runner-side: resolve runtime (env set as job_runner would), acquire
            resolved = resolve_runtime_root_for_job(
                str(db), str(output_dir), env={"TRADUTOR_RUNTIME_ROOT": str(iso)})
            self.assertEqual(resolved, iso)
            ctx = AuthEnvelopeStore(resolved).acquire(job_id)  # must NOT raise
            self.assertEqual(ctx.access_token, "dummy.header.body")

    def test_before_fix_output_derivation_would_have_missed_it(self):
        # Documents the old failure: deriving from output_dir points at the wrong auth dir.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            iso = tmp / "yomu-worker-contract-v2"; (iso / "auth").mkdir(parents=True)
            output_dir = tmp / "TradutorIA" / "output" / "job"; output_dir.mkdir(parents=True)
            job_id = "job0001"
            AuthEnvelopeStore(iso).seal(job_id, "dummy.header.body", user_id="t")
            # the OLD (buggy) derivation:
            parts = [p.casefold() for p in output_dir.resolve().parts]
            old_root = Path(*output_dir.resolve().parts[: parts.index("output")])
            with self.assertRaises(Exception):
                AuthEnvelopeStore(old_root).acquire(job_id)  # looked under TradutorIA → missing


class ProviderAuthAfterFixTests(unittest.TestCase):
    """The translation provider's auth store follows the resolved runtime root."""

    def test_provider_acquires_auth_when_output_on_other_root(self):
        from yomu_backend_provider import YomuBackendTranslationProvider

        class _FakeBackend:  # never called by the auth path
            pass

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            iso = tmp / "yomu-worker-contract-v2"; (iso / "auth").mkdir(parents=True)
            db = iso / "jobs.sqlite3"; db.write_text("x", encoding="utf-8")
            output_dir = tmp / "TradutorIA" / "output" / "job"; output_dir.mkdir(parents=True)
            job_id = "prov0001"
            AuthEnvelopeStore(iso).seal(job_id, "dummy.header.body", user_id="t")
            resolved = resolve_runtime_root_for_job(
                str(db), str(output_dir), env={"TRADUTOR_RUNTIME_ROOT": str(iso)})
            provider = YomuBackendTranslationProvider(runtime_root=resolved, backend=_FakeBackend())
            ctx = provider.auth.acquire(job_id)  # the exact call that raised in prod
            self.assertEqual(ctx.access_token, "dummy.header.body")


if __name__ == "__main__":
    unittest.main()
