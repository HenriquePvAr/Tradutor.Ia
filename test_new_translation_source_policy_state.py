"""Regression contracts for source validation and workspace policy UI state."""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import hashlib
import re
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
