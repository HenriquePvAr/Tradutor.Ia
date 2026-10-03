"""Behavioural contract for the browser-side publication eligibility decision.

Canonical publication identity is resolved exclusively by the server from the trusted
source/reconstruction lineage (``canonical_social_identity`` +
``community_api._CLIENT_IDENTITY_FIELDS``).  The browser never holds, chooses or sends a
canonical/remote chapter id, so the UI must not require one before offering Publish.

These tests execute the real ``publicationEligibility`` / ``publicationAction`` /
``publishToCommunity`` sources from ``static/tradutor_ui.js`` under Node with stubbed
boundaries -- no DOM, no network, no Supabase, no Drive.
"""

import _test_bootstrap  # noqa: F401

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI_SOURCE = ROOT / "static" / "tradutor_ui.js"
NODE = shutil.which("node")

HARNESS = r"""
'use strict';
const fs = require('fs');
const lines = fs.readFileSync(process.argv[2], 'utf8').split(/\r?\n/);

function balanced(text) {
  const open = (text.match(/[({[]/g) || []).length;
  const close = (text.match(/[)}\]]/g) || []).length;
  return open === close;
}

function extract(name) {
  const heads = [`  function ${name}(`, `  async function ${name}(`, `  const ${name} =`];
  const start = lines.findIndex(line => heads.some(head => line.startsWith(head)));
  if (start < 0) throw new Error(`symbol not found in tradutor_ui.js: ${name}`);
  if (/;\s*$/.test(lines[start]) && balanced(lines[start])) return lines[start];
  const out = [lines[start]];
  for (let i = start + 1; i < lines.length; i += 1) {
    out.push(lines[i]);
    if (/^  \};?$/.test(lines[i])) return out.join('\n');
  }
  throw new Error(`unterminated symbol in tradutor_ui.js: ${name}`);
}

const SYMBOLS = ['escapeHtml', 'escapeAttr', 'boolish', 'terminalRunStatuses',
  'publicationEligibility', 'publicationAction', 'validatePublicationForm',
  'publishToCommunity'];

const body = `
  const requests = [];
  const appState = {bootstrap: scenario.bootstrap, publicationBusy: false,
    publicationDrafts: {}, publicationCorrelation: 'test-correlation'};
  const elements = {};
  const $ = selector => {
    if (!elements[selector]) {
      elements[selector] = {value: scenario.form[selector] ?? '',
        checked: scenario.form[selector] !== false, textContent: '', hidden: true,
        disabled: false, focus() {}};
    }
    return elements[selector];
  };
  const isCanonicalCommunityAuthenticated = () => scenario.authenticated === true;
  const uiTrace = () => {};
  const correlationId = () => 'test-correlation';
  const guessFromUrl = () => ({slug: 'stub-slug', title: 'stub'});
  const showToast = () => {};
  const renderHistory = () => {};
  const closePublicationModal = () => {};
  const updatePublicationSubmitState = () => {};
  const refreshBootstrap = async () => {};
  const loadCommunityFeed = async () => {};
  const reconcileCommunityPublication = async () => ({status: 'published'});
  const publicationError = () => {};
  const api = async (url, options = {}) => {
    requests.push({url, body: JSON.parse(options.body || '{}')});
    return {post_id: 'stub-post'};
  };
${SYMBOLS.map(extract).join('\n')}
  return {publicationEligibility, publicationAction, publishToCommunity, requests, elements};
`;

const scenario = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const ui = new Function('scenario', body)(scenario);

(async () => {
  const eligibility = ui.publicationEligibility(scenario.record);
  const action = ui.publicationAction(scenario.record);
  if (scenario.publish === true) await ui.publishToCommunity(scenario.record);
  process.stdout.write(JSON.stringify({
    eligibility, action, requests: ui.requests,
  }));
})().catch(error => { process.stderr.write(String(error && error.stack || error)); process.exit(1); });
"""

CANONICAL_CLIENT_FIELDS = (
    "canonical_publication_id", "canonical_chapter_id", "social_chapter_id",
    "remote_chapter_id", "chapter_id",
)


def _valid_reconstruction_record(**overrides):
    """A terminal, current, quality-approved reconstruction owned by the caller.

    It deliberately carries **no** canonical/remote chapter identifier: that is exactly
    what a real browser sees, because no production writer ever produces those fields.
    """
    record = {
        "id": "local-1",
        "job_id": "a" * 32,
        "run_id": "run-1",
        "job_type": "artifact_reconstruction",
        "status": "finished",
        "pdf_path": "/outputs/stub/chapter.pdf",
        "pdf_sha256": "b" * 64,
        "output_verification": "manifest_verified",
        "publication_manifest_ready": True,
        "quality_gate": True,
        "review_status": "completed",
        "community_ownership": "owned",
        "publication_status": "",
        "slug": "stub-slug",
        "chapter_name": "Stub Chapter",
        "url": "https://example.invalid/stub/episode-1",
    }
    record.update(overrides)
    for field in CANONICAL_CLIENT_FIELDS:
        assert field not in record, f"synthetic record must not carry {field}"
    return record


@unittest.skipIf(NODE is None, "node is required for the UI behavioural harness")
class PublicationClientCanonicalGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._harness = Path(cls._tmp.name) / "harness.cjs"
        cls._harness.write_text(HARNESS, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run(self, record, *, authenticated=True, requires_canonical_chapter=True,
             publish=False, form=None):
        scenario = {
            "record": record,
            "authenticated": authenticated,
            "publish": publish,
            "form": {"#publicationTitle": "Stub Chapter", **(form or {})},
            "bootstrap": {"community": {
                "requires_canonical_chapter_publication": requires_canonical_chapter}},
        }
        scenario_path = Path(self._tmp.name) / "scenario.json"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(self._harness), str(UI_SOURCE), str(scenario_path)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_valid_reconstruction_without_client_canonical_ids_can_publish(self):
        for requires in (True, False):
            with self.subTest(requires_canonical_chapter=requires):
                outcome = self._run(_valid_reconstruction_record(),
                                    requires_canonical_chapter=requires)
                self.assertTrue(outcome["eligibility"]["eligible"])
                self.assertNotIn("disabled", outcome["action"])
                self.assertIn("Publicar na comunidade", outcome["action"])

    def test_publish_action_never_claims_a_canonical_chapter_workflow(self):
        outcome = self._run(_valid_reconstruction_record())
        self.assertNotIn("canonico", outcome["action"].lower())
        self.assertNotIn("canônico", outcome["action"].lower())

    def test_client_publish_payload_carries_no_canonical_identity(self):
        outcome = self._run(_valid_reconstruction_record(), publish=True)
        self.assertEqual(len(outcome["requests"]), 1, outcome["requests"])
        request = outcome["requests"][0]
        self.assertEqual(request["url"], "/api/community/publish")
        for field in CANONICAL_CLIENT_FIELDS:
            self.assertNotIn(field, request["body"])

    # --- removing the canonical gate must not enable anything else -----------------
    def test_stale_reconstruction_stays_unavailable(self):
        outcome = self._run(_valid_reconstruction_record(
            output_verification="stale", publication_manifest_ready=False))
        self.assertFalse(outcome["eligibility"]["eligible"])
        self.assertIn("disabled", outcome["action"])

    def test_failed_reconstruction_stays_unavailable(self):
        outcome = self._run(_valid_reconstruction_record(status="failed"))
        self.assertFalse(outcome["eligibility"]["eligible"])

    def test_nonterminal_reconstruction_stays_unavailable(self):
        outcome = self._run(_valid_reconstruction_record(status="running"))
        self.assertFalse(outcome["eligibility"]["eligible"])
        self.assertIn("disabled", outcome["action"])

    def test_quality_failure_stays_unavailable(self):
        outcome = self._run(_valid_reconstruction_record(
            quality_gate=False, review_status="review_required", review_confirmed=False))
        self.assertFalse(outcome["eligibility"]["eligible"])
        self.assertIn("disabled", outcome["action"])
        self.assertIn("Revis", outcome["action"])

    def test_unconfirmed_manual_review_stays_unavailable(self):
        outcome = self._run(_valid_reconstruction_record(
            quality_gate=False, status="review_required", review_status="pending"))
        self.assertFalse(outcome["eligibility"]["eligible"])

    def test_missing_artifact_offers_no_publish_control(self):
        outcome = self._run(_valid_reconstruction_record(pdf_path=""))
        self.assertFalse(outcome["eligibility"]["eligible"])
        self.assertEqual(outcome["action"], "")

    def test_unverified_manifest_stays_unavailable(self):
        outcome = self._run(_valid_reconstruction_record(
            output_verification="", publication_manifest_ready=False))
        self.assertFalse(outcome["eligibility"]["eligible"])
        self.assertIn("disabled", outcome["action"])

    def test_unowned_target_stays_unavailable(self):
        for ownership in ("legacy", "unowned_new"):
            with self.subTest(ownership=ownership):
                outcome = self._run(_valid_reconstruction_record(
                    community_ownership=ownership))
                self.assertFalse(outcome["eligibility"]["eligible"])
                self.assertIn("disabled", outcome["action"])

    def test_unauthenticated_session_stays_unavailable(self):
        outcome = self._run(_valid_reconstruction_record(), authenticated=False)
        self.assertFalse(outcome["eligibility"]["eligible"])
        self.assertIn("disabled", outcome["action"])


class PublicationUiEncodingTests(unittest.TestCase):
    def test_publication_action_strings_are_valid_utf8_portuguese(self):
        source = UI_SOURCE.read_text(encoding="utf-8")
        action = source[source.index("function publicationAction"):
                        source.index("function claimEligibility")]
        # Valid Portuguese never contains a bare "Ã"/"Â": their presence means
        # UTF-8 bytes were written through a latin-1 round trip (mojibake).
        for marker in ("Ã", "Â"):
            self.assertNotIn(marker, action)


if __name__ == "__main__":
    unittest.main()
