import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _test_bootstrap  # noqa: F401

import ui_bridge
from job_store import JobStore


class LocalTranslationContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = object.__new__(ui_bridge.UiBridge)
        self.bridge.output_root = self.tmp / "output"
        self.bridge.history_revision = 1
        self.bridge.store = JobStore(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def payload(self, **over):
        value = {
            "local_folder": str(self.tmp / "input"),
            "chapter_name": "Local chapter",
            "slug": "local_chapter",
            "mode": "quality",
            "download_only": False,
            "full": True,
            "use_cache": False,
            "force": True,
            "use_context": True,
            "translation_enabled": True,
            "translation_provider": "deepl",
            "translation_request_id": "translation:local-test",
        }
        value.update(over)
        return value

    def test_local_folder_translation_contract_is_persisted(self):
        normalized = self.bridge._normalize_local_payload(self.payload())
        job = self.bridge._create_local_folder_staging_job(
            normalized, principal=None,
        )
        config = self.bridge.store.get_job(job["id"])["configuration"]
        self.assertTrue(config["translation_enabled"])
        self.assertEqual(config["translation_provider"], "deepl")
        self.assertEqual(config["translation_request_id"], "translation:local-test")
        self.assertEqual(config["provider_provenance"]["provider_requested"], "deepl")

    def test_local_folder_sealing_reuses_url_mechanism_without_plaintext_secret(self):
        normalized = self.bridge._normalize_local_payload(self.payload())
        config = self.bridge._translation_configuration_snapshot(self.payload(), normalized)
        job = {"id": "local-contract-job"}
        envelope = self.tmp / "auth" / "local-contract-job.auth"
        with mock.patch("secure_auth_context.AuthEnvelopeStore.seal", return_value=envelope) as seal:
            sealed = self.bridge._seal_translation_context(
                job, config, principal=None, license_access_token="secret-token",
            )
        seal.assert_called_once_with("local-contract-job", "secret-token", user_id="")
        self.assertEqual(sealed["auth_context_ref"], "auth/local-contract-job.auth")
        serialized = json.dumps(sealed, ensure_ascii=False)
        self.assertNotIn("secret-token", serialized)

    def test_local_command_carries_provider_but_not_credentials(self):
        from local_folder_job import build_local_job_command

        command = build_local_job_command(
            snapshot_ref="snap", output="chapter", mode="quality",
            logical_pages=True, use_cache=False, force=True, use_context=True,
            python_executable="YomuSekai.exe", frozen=True,
            translation_provider="deepl",
        )
        self.assertIn("--translation-provider", command)
        self.assertEqual(command[command.index("--translation-provider") + 1], "deepl")
        self.assertNotIn("secret-token", command)

    def test_download_only_is_disabled_and_skips_sealing(self):
        payload = self.payload(download_only=True, translation_enabled=True)
        normalized = self.bridge._normalize_local_payload(payload)
        self.assertFalse(normalized["translation_enabled"])
        config = self.bridge._translation_configuration_snapshot(payload, normalized)
        with mock.patch("secure_auth_context.AuthEnvelopeStore.seal") as seal:
            sealed = self.bridge._seal_translation_context(
                {"id": "download-only-job"}, config,
                principal=None, license_access_token="secret-token",
            )
        seal.assert_not_called()
        self.assertNotIn("auth_context_ref", sealed)


if __name__ == "__main__":
    unittest.main()
