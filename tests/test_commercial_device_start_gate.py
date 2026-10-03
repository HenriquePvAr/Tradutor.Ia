"""Fail-closed contract for the commercial device UUID at Quality job creation.

Root cause fixed: a Quality job used to be persisted without configuration["device_uuid"]
when commercial device authorization did not resolve (e.g. the beta provider is not
Supabase).  It then ran the whole OCR pass and only died at the translation provider with
``commercial_device_id_missing``.  ``_enforce_translation_device_uuid`` now refuses such a
job up front.  Download-only jobs never translate and stay exempt.

The runner→child→provider half of the chain (env["TRADUTOR_DEVICE_UUID"] from the config)
is already covered by test_wallet_reserve_error_mapping.py; this file covers creation.
"""
import os
import pathlib
import unittest
from unittest import mock

import ui_bridge


def _env_without_hermetic():
    # Force the production branch of the gate regardless of how the suite is launched.
    return mock.patch.dict(
        os.environ,
        {k: v for k, v in os.environ.items() if k != "TRADUTOR_IA_HERMETIC_TEST_ENV"},
        clear=True,
    )


class DeviceUuidCreationGateTest(unittest.TestCase):
    def test_download_only_is_exempt(self):
        cfg = {"download_only": True}
        with _env_without_hermetic():
            ui_bridge._enforce_translation_device_uuid(cfg)  # must not raise
        self.assertNotIn("device_uuid", cfg)

    def test_quality_without_device_uuid_is_blocked(self):
        with _env_without_hermetic():
            with self.assertRaises(ValueError) as ctx:
                ui_bridge._enforce_translation_device_uuid({"download_only": False})
        self.assertEqual(str(ctx.exception), "commercial_device_id_missing")

    def test_quality_with_invalid_uuid_is_blocked(self):
        with _env_without_hermetic():
            with self.assertRaises(ValueError) as ctx:
                ui_bridge._enforce_translation_device_uuid(
                    {"download_only": False, "device_uuid": "not-a-uuid"})
        self.assertEqual(str(ctx.exception), "invalid_license_device_uuid")

    def test_quality_with_valid_uuid_passes(self):
        good = "cad49839-f9f1-4d01-ad0d-314c2b919518"
        cfg = {"download_only": False, "device_uuid": good}
        with _env_without_hermetic():
            ui_bridge._enforce_translation_device_uuid(cfg)  # must not raise
        self.assertEqual(cfg["device_uuid"], good)

    def test_hermetic_env_stamps_a_valid_device_uuid(self):
        cfg = {"download_only": False}
        with mock.patch.dict(os.environ, {"TRADUTOR_IA_HERMETIC_TEST_ENV": "1"}):
            ui_bridge._enforce_translation_device_uuid(cfg)  # must not raise
        # A syntactically valid UUID so downstream validation stays exercised.
        import uuid
        uuid.UUID(cfg["device_uuid"])

    def test_beta_metadata_cannot_substitute_device_uuid(self):
        # A beta license authorization block must never satisfy the commercial gate.
        cfg = {"download_only": False, "beta_license_authorization": {"anything": True}}
        with _env_without_hermetic():
            with self.assertRaises(ValueError):
                ui_bridge._enforce_translation_device_uuid(cfg)


class StartButtonActiveJobIdentityTest(unittest.TestCase):
    """The start button must gate on a real active job, not a stale terminal status."""

    def test_pipeline_busy_uses_active_job_identity(self):
        js = pathlib.Path("static/tradutor_ui.js").read_text(encoding="utf-8")
        # Both start-gate call sites must OR in the canonical active-job identity so a
        # previous failed job's status can never re-enable start while a job is active.
        occurrences = js.count(
            "activeOperationStatuses.has(appState.status) || Boolean(appState.activeJobId)")
        self.assertGreaterEqual(occurrences, 2, "both start-gate sites must use activeJobId")


if __name__ == "__main__":
    unittest.main()
