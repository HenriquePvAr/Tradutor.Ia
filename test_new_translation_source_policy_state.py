"""Regression contracts for source validation and workspace policy UI state."""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from chapter_source import SUPPORTED_SPECIFIC_ADAPTER
from job_store import JobStore
from source_readiness import SourceReadinessStore, default_workspace_id
from test_translation_start import WEBTOON_URL, _Bridge, drive


ROOT = Path(__file__).resolve().parent
UI = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
LOADING_VIEW = (ROOT / "static" / "loading_view.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


SOURCE_REHYDRATION_HARNESS = r"""
'use strict';
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing function ${name}`);
  const brace = source.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

const names = [
  'normalizeTranslationProvider',
  'syncSourceFormState',
  'minimumSourceInputIsValid',
  'workspacePolicyAllowsProcessing',
  'sourceValidationMatchesForm',
  'translationStartDisabledReasons',
  'updateTranslationStartControls',
  'invalidateSourceValidation',
  'setSourceType',
  'currentSourceExecutionDraft',
  'readSourceValidationDraft',
  'writeSourceValidationDraft',
  'clearSourceValidationDraft',
  'persistSourceValidationDraft',
  'refreshStoredSourceExecutionDraft',
  'applyStoredExecutionDraft',
  'rehydrateSourceValidationFromReadyRecord',
  'formPayload',
  'renderSourceAnalysisReady',
];

const elements = {};
function makeElement(selector, value) {
  const element = {
    value: '',
    checked: false,
    disabled: false,
    hidden: false,
    textContent: '',
    innerHTML: '',
    dataset: {},
    classList: {toggle() {}, add() {}, remove() {}},
    setAttribute() {},
  };
  if (value && typeof value === 'object' && !Array.isArray(value)) Object.assign(element, value);
  else if (value !== undefined) element.value = String(value);
  return element;
}
for (const [selector, value] of Object.entries(scenario.elements || {})) {
  elements[selector] = makeElement(selector, value);
}
for (const selector of [
  '#urlInput', '#localFolderInput', '#nameInput', '#outputInput', '#providerSelect',
  '#cacheToggle', '#forceToggle', '#ctxToggle', '#openToggle', '#sourceProfileToggle',
  '#scopeCustomInput', '#validateSourceBtn', '#startBtn', '#sourceReadyPanel',
  '#sourceReadyMeta', '#sourceReadyPolicyState', '#openSourcePolicySettings',
  '#urlSourceField', '#localFolderSourceField', '#urlError', '#localFolderError',
  '#scopeCustom',
]) {
  if (!elements[selector]) elements[selector] = makeElement(selector);
}
const sourceTypeCards = [{dataset: {sourceType: 'url'}, classList: elements['#urlInput'].classList, setAttribute() {}},
                         {dataset: {sourceType: 'local_folder'}, classList: elements['#urlInput'].classList, setAttribute() {}}];
const scopeCards = ['full', '3', '5', '20', '50', 'custom'].map(scope => ({
  dataset: {scope}, classList: {toggle() {}}, setAttribute() {},
}));
const choiceCards = ['fast', 'quality', 'download_only'].map(mode => ({
  dataset: {mode}, classList: {toggle() {}},
}));
function $(selector) {
  const scopeMatch = String(selector).match(/^\.scope-card\[data-scope="([^"]+)"\]$/);
  if (scopeMatch) return scopeCards.find(card => card.dataset.scope === scopeMatch[1]) || null;
  if (!elements[selector]) elements[selector] = makeElement(selector);
  return elements[selector];
}
function $$(selector) {
  if (selector === '.source-type-card') return sourceTypeCards;
  if (selector === '.scope-card') return scopeCards;
  if (selector === '.choice-card') return choiceCards;
  if (selector === '.stage-item') return [];
  return [];
}
const storage = new Map(Object.entries(scenario.sessionStorage || {}));
const sessionStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, String(value)); },
  removeItem(key) { storage.delete(key); },
};
const document = {createElement: () => ({textContent: '', innerHTML: ''})};
const window = {TradutorI18n: {t: () => ''}};
const SOURCE_VALIDATION_DRAFT_STORAGE_KEY = 'tradutor.sourceValidationDraft.v1';
const appState = Object.assign({
  selectedScope: 'full',
  selectedMode: 'fast',
  selectedSourceType: 'url',
  providerDirty: false,
  programmingFields: false,
  settings: {workspace_source_policy: {status: 'active', all_submitted_sources_authorized: true}},
  sourceValidation: {status: 'idle', analysisResultId: '', sourceUrl: '', reasonCode: ''},
  sourceForm: {url: '', localFolder: '', chapterName: '', outputSlug: '', translationProvider: ''},
  lastStartDisabledReasons: [],
  sourceReady: null,
  newTranslationDraft: false,
  currentSourceUrl: '',
}, scenario.appState || {});
const trace = [];
function uiTrace(event, payload) { trace.push({event, payload}); }
function escapeHtml(value) { return String(value ?? ''); }
function escapeAttr(value) { return String(value ?? '').replace(/"/g, '&quot;'); }
function slugify(value) { return String(value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, ''); }
function guessFromUrl() { return {title: 'Fixture chapter', slug: 'fixture_chapter'}; }
function shake() {}
function showToast() {}

const body = `const inFlightStatuses = new Set(['staging', 'queued', 'claiming', 'starting', 'running', 'cancelling', 'awaiting_source_review']);
${names.map(extractFunction).join('\n')}
return {appState, elements, storage, trace,
  persistSourceValidationDraft, renderSourceAnalysisReady, rehydrateSourceValidationFromReadyRecord,
  refreshStoredSourceExecutionDraft, formPayload, updateTranslationStartControls};`;
const ui = new Function('$', '$$', 'elements', 'storage', 'trace', 'sessionStorage', 'document', 'window',
  'SOURCE_VALIDATION_DRAFT_STORAGE_KEY', 'appState', 'uiTrace', 'escapeHtml',
  'escapeAttr', 'slugify', 'guessFromUrl', 'shake', 'showToast', body)(
  $, $$, elements, storage, trace, sessionStorage, document, window, SOURCE_VALIDATION_DRAFT_STORAGE_KEY,
  appState, uiTrace, escapeHtml, escapeAttr, slugify, guessFromUrl, shake, showToast);

let payload = null;
if (scenario.action === 'persist') {
  ui.persistSourceValidationDraft(scenario.validationResult, scenario.sourceUrl);
} else if (scenario.action === 'render') {
  ui.renderSourceAnalysisReady(scenario.record);
  if (scenario.buildPayload) payload = ui.formPayload();
} else if (scenario.action === 'render_then_edit') {
  ui.renderSourceAnalysisReady(scenario.record);
  ui.elements['#urlInput'].value = scenario.editedUrl;
  ui.appState.sourceForm.url = scenario.sourceUrl;
  ui.appState.sourceValidation = {status: 'ready', analysisResultId: scenario.analysisId,
    sourceUrl: scenario.sourceUrl, reasonCode: ''};
  ui.appState.sourceForm.url = scenario.sourceUrl;
  const state = {sourceChanged: true};
  if (state.sourceChanged) { ui.storage.delete(SOURCE_VALIDATION_DRAFT_STORAGE_KEY); }
  ui.appState.sourceValidation = {status: 'idle', analysisResultId: '', sourceUrl: '', reasonCode: '', analysis: null};
  ui.updateTranslationStartControls();
}

process.stdout.write(JSON.stringify({
  appState: ui.appState,
  elements: Object.fromEntries(Object.entries(ui.elements).map(([key, value]) => [key, {
    value: value.value, checked: value.checked, disabled: value.disabled, hidden: value.hidden,
    textContent: value.textContent, innerHTML: value.innerHTML,
  }])),
  storage: Object.fromEntries(ui.storage.entries()),
  trace: ui.trace,
  payload,
}));
"""


def source_payload(**extra):
    payload = {
        "source_type": "url",
        "url": WEBTOON_URL,
        "chapter_name": "Fixture chapter",
        "slug": "fixture_chapter",
        "mode": "fast",
        "full": True,
        "use_cache": False,
        "force": True,
        "pipeline_intent": {"requested": True, "mode": "fast", "scope": "full"},
    }
    payload.update(extra)
    return payload


class SourceStateBridge(_Bridge):
    def _analyze_source(self, _url, *, cancel_check=None, on_progress=None):
        page = SimpleNamespace(id="page-1")
        return SimpleNamespace(
            outcome=SUPPORTED_SPECIFIC_ADAPTER,
            accepted=[page],
            public=lambda: {
                "adapter": "fixture_adapter",
                "adapter_version": "test-v1",
                "final_host": "reader.example.test",
                "title": "Fixture chapter",
                "outcome": SUPPORTED_SPECIFIC_ADAPTER,
                "confidence": 1.0,
                "candidate_count": 1,
                "accepted_count": 1,
                "discarded_count": 0,
                "accepted": [{"id": "page-1", "order": 1}],
            },
        )


class JoblessSourceAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite3"
        self.bridge = SourceStateBridge(self.db)

    def tearDown(self):
        self.bridge.store.close()
        self.tmp.cleanup()

    def activate_policy(self):
        ready = SourceReadinessStore(self.db)
        try:
            return ready.activate_workspace_policy(
                owner="local",
                workspace_id=default_workspace_id(self.db),
                created_by="local",
                authorization_statement="Only authorized fixture sources are submitted.",
            )
        finally:
            ready.close()

    def test_source_analysis_persists_result_without_creating_job_or_queue_item(self):
        result = drive(self.bridge.analyze_source_candidate(source_payload()))

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "source_analysis_ready")
        self.assertTrue(result["analysis_result_id"].startswith("sa_"))
        self.assertEqual(self.bridge.store.list_jobs(limit=None), [])
        self.assertEqual(self.bridge.worker_calls, 0)

    def test_jobless_analysis_remains_visible_when_workspace_policy_is_active(self):
        self.activate_policy()
        result = drive(self.bridge.analyze_source_candidate(source_payload()))

        restored = self.bridge.latest_source_analysis("local")
        self.assertEqual(restored["analysis_result_id"], result["analysis_result_id"])
        self.assertEqual(restored["job_id"], "")

    def test_real_start_requires_matching_analysis_and_active_policy_before_job_creation(self):
        analysis = drive(self.bridge.analyze_source_candidate(source_payload()))
        guarded = source_payload(
            source_validation_required=True,
            source_analysis_result_id=analysis["analysis_result_id"],
        )

        with self.assertRaisesRegex(ValueError, "workspace_source_authorization_required"):
            drive(self.bridge.start(guarded))
        self.assertEqual(self.bridge.store.list_jobs(limit=None), [])

        self.activate_policy()
        result = drive(self.bridge.start(guarded))
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.bridge.store.list_jobs(limit=None)), 1)

    def test_real_start_rejects_missing_or_wrong_source_analysis_before_job_creation(self):
        self.activate_policy()
        for analysis_id in ("", "sa_" + "0" * 32):
            with self.subTest(analysis_id=analysis_id):
                with self.assertRaisesRegex(ValueError, "source_validation_required"):
                    drive(self.bridge.start(source_payload(
                        source_validation_required=True,
                        source_analysis_result_id=analysis_id,
                    )))
                self.assertEqual(self.bridge.store.list_jobs(limit=None), [])

    def test_real_start_rejects_analysis_for_a_different_url(self):
        self.activate_policy()
        analysis = drive(self.bridge.analyze_source_candidate(source_payload()))
        payload = source_payload(
            url="https://example.org/another-chapter",
            source_validation_required=True,
            source_analysis_result_id=analysis["analysis_result_id"],
        )
        with self.assertRaisesRegex(ValueError, "source_validation_required"):
            drive(self.bridge.start(payload))
        self.assertEqual(self.bridge.store.list_jobs(limit=None), [])

    def test_analysis_consumed_by_a_real_job_is_not_restored_as_standalone(self):
        self.activate_policy()
        analysis = drive(self.bridge.analyze_source_candidate(source_payload()))
        drive(self.bridge.start(source_payload(
            source_validation_required=True,
            source_analysis_result_id=analysis["analysis_result_id"],
        )))

        self.assertIsNone(self.bridge.latest_source_analysis("local"))

    def test_standalone_source_analysis_survives_bridge_process_restart(self):
        self.activate_policy()
        result = drive(self.bridge.analyze_source_candidate(source_payload()))
        self.bridge.store.close()

        restarted = SourceStateBridge(self.db)
        try:
            restored = restarted.latest_source_analysis("local")
            self.assertIsNotNone(restored)
            self.assertEqual(restored["analysis_result_id"], result["analysis_result_id"])
            self.assertEqual(restored["source_analysis_result"]["status"], "source_analysis_ready")
        finally:
            restarted.store.close()


class WorkspacePolicyScopeTests(unittest.TestCase):
    def test_workspace_policy_reader_uses_the_same_machine_scope_as_the_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = SourceStateBridge(Path(tmp) / "jobs.sqlite3")
            try:
                activated = bridge.set_workspace_source_policy(active=True)["policy"]
                restored = bridge.workspace_source_policy()
                self.assertEqual(restored["policy_id"], activated["policy_id"])
                self.assertEqual(restored["status"], "active")
            finally:
                bridge.store.close()


class FrontendSourceStateContracts(unittest.TestCase):
    def _function_body(self, name: str) -> str:
        match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", UI)
        self.assertIsNotNone(match, f"{name} function not found")
        start = match.start()
        depth = 0
        for index in range(match.end() - 1, len(UI)):
            char = UI[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return UI[start:index + 1]
        self.fail(f"{name} function body was not closed")

    def _ready_record(self, analysis_id: str = "sa_fixture") -> dict:
        return {
            "id": analysis_id,
            "analysis_result_id": analysis_id,
            "job_id": "",
            "status": "source_analysis_ready",
            "stage": "source_analysis_ready",
            "source_type": "public_url",
            "source_analysis_result": {
                "analysis_id": analysis_id,
                "status": "source_analysis_ready",
                "normalized_url_hash": hashlib.sha256(WEBTOON_URL.encode("utf-8")).hexdigest(),
                "result_hash": "result-hash",
                "adapter": "webtoons",
                "estimated_asset_count": 171,
                "browser_inspection_performed": True,
                "browser_engine": "chrome",
                "public_structure_indicators_present": True,
                "completed_at": 1786733617,
                "reason_code": "source_structure_compatible",
            },
            "download_authorization": {},
        }

    def _stored_draft(self, analysis_id: str = "sa_fixture", *, url: str = WEBTOON_URL) -> dict:
        return {
            "schema_version": 1,
            "status": "source_analysis_ready",
            "analysis_result_id": analysis_id,
            "source_url": url,
            "normalized_url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "result_hash": "result-hash",
            "reason_code": "workspace_policy_authorized",
            "execution": {
                "source_url": url,
                "chapter_name": "The Returned C-Rank Tank - Episode 51",
                "output_slug": "the_returned_c_rank_tank_episode_51",
                "translation_provider": "riva",
                "selected_mode": "fast",
                "selected_scope": "full",
                "custom_scope": "",
                "use_cache": False,
                "force": True,
                "use_context": True,
                "open_output": False,
                "create_source_profile": False,
            },
            "updated_at": 1786733617000,
        }

    def _run_rehydration_harness(self, scenario: dict) -> dict:
        if NODE is None:
            self.skipTest("node is required for source rehydration UI harness")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "source_rehydration.cjs"
            payload = Path(tmp) / "scenario.json"
            harness.write_text(SOURCE_REHYDRATION_HARNESS, encoding="utf-8")
            payload.write_text(json.dumps(scenario), encoding="utf-8")
            result = subprocess.run(
                [NODE, str(harness), str(ROOT / "static" / "tradutor_ui.js"), str(payload)],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_bootstrap_completion_never_uses_translation_terminal_copy(self):
        self.assertIn("mode === MODE_PIPELINE ? TERMINAL_COPY[status] : null", LOADING_VIEW)

    def test_terminal_pipeline_surface_requires_a_real_job_identity(self):
        surface = UI[UI.index("function renderLoadingSurface"):]
        surface = surface[:surface.index("\n  function renderProgress")]
        self.assertIn("terminalRunStatuses.has(state.status) && !state.jobId", surface)
        self.assertIn("clearLoadingSurface();", surface)

    def test_validation_and_processing_are_separate_actions(self):
        shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        self.assertIn('class="btn-ghost show" type="button" id="validateSourceBtn"', shell)
        self.assertIn("async function validateSource", UI)
        self.assertIn("await api('/api/ui/source/analyze'", UI)
        start = UI[UI.index("async function startTranslation"):]
        start = start[:start.index("\n  async function cancelTranslation")]
        self.assertIn("source_analysis_result_id", start)
        self.assertNotIn("validating_source", start)

    def test_start_button_is_derived_from_validated_source_and_policy(self):
        self.assertIn("function updateTranslationStartControls", UI)
        controls = UI[UI.index("function updateTranslationStartControls"):]
        controls = controls[:controls.index("\n  function", 20)]
        self.assertIn("sourceValidationMatchesForm()", controls)
        self.assertIn("workspacePolicyAllowsProcessing", controls)
        self.assertIn("start.disabled = !canStart", controls)
        matching = UI[UI.index("function sourceValidationMatchesForm"):]
        matching = matching[:matching.index("\n  function", 20)]
        self.assertIn("sourceValidation.status === 'ready'", matching)

    def test_bootstrap_rehydrates_authorized_source_state_after_runtime_restart(self):
        key = "tradutor.sourceValidationDraft.v1"
        result = self._run_rehydration_harness({
            "action": "render",
            "record": self._ready_record(),
            "buildPayload": True,
            "sessionStorage": {key: json.dumps(self._stored_draft())},
            "elements": {
                "#urlInput": {"value": ""},
                "#providerSelect": {"value": "nemotron"},
                "#cacheToggle": {"checked": True},
                "#forceToggle": {"checked": False},
                "#ctxToggle": {"checked": True},
                "#openToggle": {"checked": False},
                "#sourceProfileToggle": {"checked": False},
            },
        })

        self.assertEqual(result["appState"]["sourceValidation"]["status"], "ready")
        self.assertEqual(result["appState"]["sourceValidation"]["analysisResultId"], "sa_fixture")
        self.assertEqual(result["elements"]["#urlInput"]["value"], WEBTOON_URL)
        self.assertEqual(result["elements"]["#providerSelect"]["value"], "riva")
        self.assertFalse(result["elements"]["#cacheToggle"]["checked"])
        self.assertTrue(result["elements"]["#forceToggle"]["checked"])
        self.assertFalse(result["elements"]["#startBtn"]["disabled"])
        self.assertIn("Fonte autorizada. Pronta para processamento.",
                      result["elements"]["#sourceReadyPolicyState"]["textContent"])
        self.assertNotIn("source_analysis_result_id", result["payload"])
        self.assertEqual(result["payload"]["translation_provider"], "riva")
        self.assertFalse(result["payload"]["use_cache"])
        self.assertTrue(result["payload"]["force"])

    def test_rehydrated_start_payload_keeps_source_analysis_and_execution_config(self):
        key = "tradutor.sourceValidationDraft.v1"
        result = self._run_rehydration_harness({
            "action": "render",
            "record": self._ready_record(),
            "buildPayload": True,
            "sessionStorage": {key: json.dumps(self._stored_draft())},
        })
        # The startTranslation boundary appends source_analysis_result_id from
        # appState.sourceValidation; formPayload must preserve the current operator config.
        self.assertEqual(result["appState"]["sourceValidation"]["analysisResultId"], "sa_fixture")
        self.assertEqual(result["payload"]["url"], WEBTOON_URL)
        self.assertEqual(result["payload"]["translation_provider"], "riva")
        self.assertTrue(result["payload"]["full"])

    def test_bootstrap_does_not_auto_call_source_analyze_to_recover_state(self):
        bootstrap = UI[UI.index("async function runBootstrapRefresh"):]
        bootstrap = bootstrap[:bootstrap.index("\n  async function pollState")]
        self.assertNotIn("/api/ui/source/analyze", bootstrap)

    def test_source_changed_after_restart_does_not_reuse_previous_validation(self):
        key = "tradutor.sourceValidationDraft.v1"
        result = self._run_rehydration_harness({
            "action": "render",
            "record": self._ready_record(),
            "sessionStorage": {key: json.dumps(self._stored_draft())},
            "elements": {"#urlInput": {"value": "https://example.org/other"}},
        })

        self.assertEqual(result["appState"]["sourceValidation"]["status"], "idle")
        self.assertTrue(result["elements"]["#startBtn"]["disabled"])
        self.assertTrue(result["elements"]["#sourceReadyPanel"]["hidden"])

    def test_incomplete_ready_evidence_fails_closed_and_does_not_display_authorized(self):
        key = "tradutor.sourceValidationDraft.v1"
        record = self._ready_record()
        record["source_analysis_result"].pop("normalized_url_hash")
        result = self._run_rehydration_harness({
            "action": "render",
            "record": record,
            "sessionStorage": {key: json.dumps(self._stored_draft())},
        })

        self.assertEqual(result["appState"]["sourceValidation"]["status"], "idle")
        self.assertTrue(result["elements"]["#startBtn"]["disabled"])
        self.assertTrue(result["elements"]["#sourceReadyPanel"]["hidden"])

    def test_display_and_canonical_source_ready_are_consistent_after_bootstrap(self):
        key = "tradutor.sourceValidationDraft.v1"
        ready = self._run_rehydration_harness({
            "action": "render",
            "record": self._ready_record(),
            "sessionStorage": {key: json.dumps(self._stored_draft())},
        })
        self.assertFalse(ready["elements"]["#sourceReadyPanel"]["hidden"])
        self.assertEqual(ready["appState"]["sourceValidation"]["status"], "ready")

        blocked = self._run_rehydration_harness({
            "action": "render",
            "record": self._ready_record("sa_other"),
            "sessionStorage": {key: json.dumps(self._stored_draft("sa_fixture"))},
        })
        self.assertTrue(blocked["elements"]["#sourceReadyPanel"]["hidden"])
        self.assertNotEqual(blocked["appState"]["sourceValidation"]["status"], "ready")

    def test_source_url_form_state_is_canonicalized_before_validation_and_payload(self):
        shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        self.assertEqual(shell.count('id="urlInput"'), 1)
        self.assertIn("sourceForm: {url: '', localFolder: '', chapterName: '', outputSlug: ''}", UI)

        sync = self._function_body("syncSourceFormState")
        self.assertIn("url: $('#urlInput')?.value?.trim() || ''", sync)
        self.assertIn("appState.sourceForm = next", sync)

        validate = self._function_body("validateForm")
        self.assertIn("const form = syncSourceFormState();", validate)
        self.assertIn("const url = form.url;", validate)
        self.assertIn("const folder = form.localFolder;", validate)
        self.assertNotIn("$('#urlInput').value.trim()", validate)
        self.assertNotIn("$('#localFolderInput').value.trim()", validate)

        payload = self._function_body("formPayload")
        self.assertIn("const form = syncSourceFormState();", payload)
        self.assertIn("guessFromUrl(form.url)", payload)
        self.assertIn("chapter_name: form.chapterName || guess.title", payload)
        self.assertIn("slug: form.outputSlug || slugify(guess.slug)", payload)
        self.assertIn("payload.url = form.url", payload)
        self.assertIn("payload.local_folder = form.localFolder", payload)

    def test_source_form_state_survives_non_input_browser_writes_and_navigation_lifecycle(self):
        self.assertIn("bindSourceFormInput('#urlInput', handleSourceUrlInput)", UI)
        binder = self._function_body("bindSourceFormInput")
        for event_name in ("input", "change", "keyup", "paste", "compositionend"):
            self.assertIn(f"'{event_name}'", binder)

        minimum = self._function_body("minimumSourceInputIsValid")
        self.assertIn("const form = syncSourceFormState();", minimum)
        self.assertIn("const value = form.url;", minimum)
        self.assertIn("return Boolean(form.localFolder);", minimum)

        matching = self._function_body("sourceValidationMatchesForm")
        self.assertIn("const form = syncSourceFormState();", matching)
        self.assertIn("appState.sourceValidation.sourceUrl === form.url", matching)

        programmatic = self._function_body("programField")
        self.assertIn("syncSourceFormState", programmatic)
        source_type = self._function_body("setSourceType")
        self.assertIn("syncSourceFormState();", source_type)
        exit_review = self._function_body("exitReviewMode")
        self.assertIn("syncSourceFormState();", exit_review)
        load_history = self._function_body("loadRecordIntoForm")
        self.assertIn("syncSourceFormState();", load_history)

    def test_validate_source_click_submits_one_payload_after_form_sync(self):
        validate_source = self._function_body("validateSource")
        validate_index = validate_source.index("validateForm()")
        payload_index = validate_source.index("const payload = formPayload();")
        api_index = validate_source.index("api('/api/ui/source/analyze'")
        self.assertLess(validate_index, payload_index)
        self.assertLess(payload_index, api_index)
        self.assertEqual(validate_source.count("api('/api/ui/source/analyze'"), 1)
        self.assertIn("const sourceUrl = String(payload.url || '')", validate_source)
        self.assertIn("sourceUrl", validate_source)

    def test_policy_copy_is_explicit_and_settings_action_is_immediate(self):
        for text in (
            "Fontes externas estão bloqueadas até ativar a política.",
            "Fonte autorizada. Pronta para processamento.",
            "Desativar política",
        ):
            self.assertIn(text, UI)
        self.assertIn(
            "$('#openSourcePolicySettings')?.addEventListener('click', () => activateTab('cfg'))",
            UI,
        )

    def test_queued_latest_job_remains_targetable_by_cancel_action(self):
        render = UI[UI.index("function renderRuntime(runtime)"):]
        render = render[:render.index("\n  function renderRunStatus")]
        self.assertIn("const latestStatus = String(runtime.latest?.status", render)
        self.assertIn("const queuedRecord = appState.queue.find", render)
        self.assertIn("const activeRecord =", render)
        self.assertIn("inFlightStatuses.has(latestStatus)", render)
        self.assertIn("|| queuedRecord", render)
        self.assertIn("runtime.latest", render)
        self.assertIn(
            "appState.activeJobId = String(activeRecord?.id || activeRecord?.job_id || '')",
            render,
        )

    def test_new_draft_can_be_validated_and_queued_while_another_job_runs(self):
        controls = UI[UI.index("function updateTranslationStartControls"):]
        controls = controls[:controls.index("\n  function", 20)]
        self.assertIn("const editingFreshDraft = appState.newTranslationDraft", controls)
        self.assertIn("const busyBlocksDraft = pipelineBusy && !editingFreshDraft", controls)
        self.assertIn("!busyBlocksDraft", controls)

        activation = UI[UI.index("function activateTab"):]
        activation = activation[:activation.index("\n  $$('.rail-tab')")]
        self.assertIn("if (name === 'nova' && !appState.reviewMode)", activation)
        self.assertNotIn("name === 'nova' && !inFlightStatuses.has(appState.status)", activation)

        runtime = UI[UI.index("function renderRuntime(runtime)"):]
        runtime = runtime[:runtime.index("\n  function renderRunStatus")]
        self.assertIn("const draftOnly = appState.newTranslationDraft && !appState.reviewMode", runtime)


if __name__ == "__main__":
    unittest.main()
