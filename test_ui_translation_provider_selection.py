"""Explicit translation-provider selection in the "Nova tradução" form.

The backend has accepted ``translation_provider`` for a while, but the browser had no
control for it: ``formPayload`` copied whatever runtime/env default the settings endpoint
reported, so a Riva run could only be obtained by editing ``.env``.  Provider choice is
per-job execution configuration, so it belongs in the form.

These tests execute the real ``syncSourceFormState`` / ``formPayload`` sources from
``static/tradutor_ui.js`` under Node with stubbed boundaries, and drive the real bridge
hermetically.  No provider is ever contacted.
"""

import _test_bootstrap  # noqa: F401

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from source_readiness import SourceReadinessStore, default_workspace_id
from ui_helpers import build_run_command
from test_new_translation_source_policy_state import SourceStateBridge, source_payload
from test_translation_start import WEBTOON_URL, drive

ROOT = Path(__file__).resolve().parent
UI_SOURCE = ROOT / "static" / "tradutor_ui.js"
UI_SHELL = ROOT / "ui" / "ui_shell.html"
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

const SYMBOLS = ['normalizeTranslationProvider', 'syncSourceFormState', 'formPayload'];

const body = `
  const appState = scenario.appState;
  const elements = {};
  const $ = selector => {
    if (!(selector in elements)) {
      const value = scenario.form[selector];
      if (value === undefined) { elements[selector] = null; }
      else { elements[selector] = {value: String(value), checked: value === true}; }
    }
    return elements[selector];
  };
  const slugify = value => String(value || '').trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  const guessFromUrl = () => ({title: 'stub chapter', slug: 'stub_chapter'});
  ${SYMBOLS.map(extract).join('\n')}
  return {syncSourceFormState, formPayload, appState};
`;

const scenario = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const ui = new Function('scenario', body)(scenario);

const payload = ui.formPayload();
// A plain re-sync (validation, re-render, control refresh) must not reset the choice.
ui.syncSourceFormState();
ui.syncSourceFormState();
const payloadAfterResync = ui.formPayload();
process.stdout.write(JSON.stringify({
  payload, payloadAfterResync, sourceForm: ui.appState.sourceForm,
}));
"""


@unittest.skipIf(NODE is None, "node is required for the UI behavioural harness")
class ProviderSelectionFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._harness = Path(cls._tmp.name) / "harness.cjs"
        cls._harness.write_text(HARNESS, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run(self, *, selected, settings_provider="nemotron"):
        scenario = {
            "form": {
                "#urlInput": WEBTOON_URL,
                "#localFolderInput": "",
                "#nameInput": "Stub chapter",
                "#outputInput": "stub_chapter",
                "#providerSelect": selected,
                "#cacheToggle": True,
                "#forceToggle": False,
                "#ctxToggle": True,
                "#openToggle": False,
                "#sourceProfileToggle": False,
                "#scopeCustomInput": "0",
            },
            "appState": {
                "selectedSourceType": "url",
                "selectedScope": "full",
                "selectedMode": "fast",
                "settings": {"translation_provider": settings_provider},
                "sourceForm": {"url": "", "localFolder": "", "chapterName": "",
                               "outputSlug": "", "translationProvider": ""},
            },
        }
        path = Path(self._tmp.name) / "scenario.json"
        path.write_text(json.dumps(scenario), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(self._harness), str(UI_SOURCE), str(path)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_selecting_riva_puts_riva_in_the_start_payload(self):
        outcome = self._run(selected="riva")
        self.assertEqual(outcome["payload"]["translation_provider"], "riva")
        self.assertEqual(outcome["sourceForm"]["translationProvider"], "riva")

    def test_selecting_nemotron_puts_nemotron_in_the_start_payload(self):
        outcome = self._run(selected="nemotron", settings_provider="riva")
        self.assertEqual(outcome["payload"]["translation_provider"], "nemotron")

    def test_runtime_default_never_overwrites_the_explicit_choice(self):
        outcome = self._run(selected="riva", settings_provider="nemotron")
        self.assertEqual(outcome["payload"]["translation_provider"], "riva")

    def test_resync_does_not_silently_reset_the_selection(self):
        outcome = self._run(selected="riva", settings_provider="nemotron")
        self.assertEqual(outcome["payloadAfterResync"]["translation_provider"], "riva")

    def test_unknown_control_value_never_becomes_a_start_payload_provider(self):
        outcome = self._run(selected="foo", settings_provider="nemotron")
        self.assertIn(outcome["payload"]["translation_provider"], {"", "nemotron"})
        self.assertNotEqual(outcome["payload"]["translation_provider"], "foo")

    def test_selecting_deepl_puts_the_canonical_id_in_the_start_payload(self):
        outcome = self._run(selected="deepl", settings_provider="nemotron")
        self.assertEqual(outcome["payload"]["translation_provider"], "deepl")
        self.assertEqual(outcome["sourceForm"]["translationProvider"], "deepl")

    def test_deepl_survives_a_resync_and_the_runtime_default(self):
        outcome = self._run(selected="deepl", settings_provider="nemotron")
        self.assertEqual(outcome["payloadAfterResync"]["translation_provider"], "deepl")

    def test_the_deepl_display_label_is_never_the_payload_value(self):
        outcome = self._run(selected="DeepL (Qualidade)", settings_provider="nemotron")
        self.assertNotIn("DeepL", outcome["payload"]["translation_provider"])

    def test_provider_is_not_part_of_the_source_identity(self):
        # `sourceChanged` gates source re-validation; provider is execution config.
        source = UI_SOURCE.read_text(encoding="utf-8")
        sync = source[source.index("function syncSourceFormState"):
                      source.index("function bindSourceFormInput")]
        changed = sync[sync.index("sourceChanged:"):]
        self.assertNotIn("translationProvider", changed)


class ProviderControlMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = UI_SHELL.read_text(encoding="utf-8")
        self.form = self.html[self.html.index('id="view-nova"'):
                              self.html.index('id="startBtn"')]

    def test_new_translation_form_exposes_a_labelled_provider_control(self):
        self.assertIn('for="providerSelect"', self.form)
        self.assertIn('Motor de tradução', self.form)
        self.assertIn('<select id="providerSelect"', self.form)

    def test_all_canonical_providers_are_selectable(self):
        self.assertIn('value="riva"', self.form)
        self.assertIn('value="nemotron"', self.form)
        self.assertIn('value="deepl"', self.form)

    def test_deepl_is_offered_under_its_product_label_but_not_preselected(self):
        self.assertIn('<option value="deepl">DeepL (Qualidade)</option>', self.form)
        self.assertIn('<option value="nemotron" selected>', self.form)
        self.assertNotIn('<option value="deepl" selected', self.form)

    def test_control_never_renders_a_credential(self):
        for secret in ("api_key", "API_KEY", "Authorization", "token"):
            self.assertNotIn(secret, self.form)


class ProviderPropagationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite3"
        self.bridge = SourceStateBridge(self.db)
        ready = SourceReadinessStore(self.db)
        try:
            ready.activate_workspace_policy(
                owner="local",
                workspace_id=default_workspace_id(self.db),
                created_by="local",
                authorization_statement="Only authorized fixture sources are submitted.",
            )
        finally:
            ready.close()

    def tearDown(self):
        self.bridge.store.close()
        self.tmp.cleanup()

    def _start(self, provider):
        analysis = drive(self.bridge.analyze_source_candidate(source_payload()))
        return drive(self.bridge.start(source_payload(
            source_validation_required=True,
            source_analysis_result_id=analysis["analysis_result_id"],
            translation_provider=provider,
        )))

    def _configuration(self, job_id):
        job = self.bridge.store.get_job(job_id)
        return job.get("configuration") or job.get("configuration_json") or {}

    def test_riva_selection_is_persisted_with_its_provenance(self):
        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            result = self._start("riva")
        configuration = self._configuration(result["job_id"])
        self.assertEqual(configuration["translation_provider"], "riva")
        self.assertEqual(
            configuration["provider_provenance"]["provider_requested"], "riva")
        self.assertEqual(
            configuration["provider_provenance"]["provider_source"], "ui_payload")

    def test_deepl_selection_is_persisted_with_its_provenance(self):
        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "nemotron"}):
            result = self._start("deepl")
        configuration = self._configuration(result["job_id"])
        self.assertEqual(configuration["translation_provider"], "deepl")
        self.assertEqual(
            configuration["provider_provenance"]["provider_requested"], "deepl")
        self.assertEqual(
            configuration["provider_provenance"]["provider_source"], "ui_payload")

    def test_nemotron_selection_is_persisted(self):
        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "riva"}):
            result = self._start("nemotron")
        self.assertEqual(
            self._configuration(result["job_id"])["translation_provider"], "nemotron")

    def test_invalid_provider_blocks_job_creation(self):
        with self.assertRaisesRegex(ValueError, "nvidia_translation_provider_invalid"):
            self._start("foo")
        self.assertEqual(self.bridge.store.list_jobs(limit=None), [])

    def test_run_command_carries_the_selected_provider(self):
        for provider in ("riva", "nemotron", "deepl"):
            with self.subTest(provider=provider):
                command = build_run_command(
                    url=WEBTOON_URL, mode="fast", output="cap", full=True,
                    max_images=None, use_cache=False, force=True, use_context=True,
                    translation_provider=provider)
                self.assertIn("--translation-provider", command)
                self.assertEqual(
                    command[command.index("--translation-provider") + 1], provider)


if __name__ == "__main__":
    unittest.main()
