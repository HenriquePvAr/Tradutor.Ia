"""Start-translation submit path: source selection, queue visibility, idempotency, errors."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ui_bridge
from chapter_source import (
    UnsupportedSource, host_of, select_adapter, supported_hosts,
)
from job_store import JobStatus, JobStore
from ui_helpers import build_run_command

WEBTOON_URL = "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer?title_no=1&episode_no=1"


class SourceSelectionTests(unittest.TestCase):
    def test_known_host_selects_its_adapter(self):
        self.assertEqual(select_adapter(WEBTOON_URL).name, "webtoons")
        self.assertEqual(select_adapter("https://webtoons.com/x").name, "webtoons")

    def test_unknown_host_is_rejected_with_a_stable_code(self):
        with self.assertRaises(UnsupportedSource) as ctx:
            select_adapter("https://example.org/series/x/chapter-1")
        self.assertEqual(ctx.exception.code, "unsupported_source")
        self.assertEqual(ctx.exception.host, "example.org")

    def test_lookalike_host_cannot_impersonate_an_allowed_one(self):
        # Suffix matching on the raw string would let this through.
        for impostor in ("https://evil-webtoons.com/x", "https://webtoons.com.evil.net/x"):
            with self.assertRaises(UnsupportedSource, msg=impostor):
                select_adapter(impostor)

    def test_subdomains_of_allowed_hosts_are_accepted(self):
        self.assertEqual(select_adapter("https://m.webtoons.com/x").name, "webtoons")

    def test_host_parsing_is_defensive(self):
        self.assertEqual(host_of("https://WWW.Webtoons.COM/x"), "webtoons.com")
        self.assertEqual(host_of("not a url"), "")
        self.assertEqual(host_of(""), "")

    def test_credentials_in_url_are_refused(self):
        adapter = select_adapter(WEBTOON_URL)
        with self.assertRaises(ValueError):
            adapter.validate_url("https://user:pass@webtoons.com/x")

    def test_normalize_drops_fragment_and_lowercases_host(self):
        got = select_adapter(WEBTOON_URL).normalize_url("https://WWW.Webtoons.com/en/x?a=1#frag")
        self.assertEqual(got, "https://www.webtoons.com/en/x?a=1")

    def test_supported_hosts_listed_for_the_ui(self):
        self.assertIn("webtoons.com", supported_hosts())

    def test_command_uses_the_adapter_runner_and_never_a_shell_string(self):
        command = build_run_command(
            url=WEBTOON_URL, mode="fast", output="cap", full=True, max_images=None,
            use_cache=False, force=True, use_context=True)
        self.assertIsInstance(command, list)          # never a shell string
        self.assertTrue(command[1].endswith("run_webtoon.py"))
        self.assertIn("--force", command)

    def test_unsupported_host_fails_before_a_command_exists(self):
        with self.assertRaises(UnsupportedSource):
            build_run_command(
                url="https://example.org/series/x/chapter-1", mode="fast", output="cap",
                full=True, max_images=None, use_cache=False, force=True, use_context=True)


class _Bridge(ui_bridge.UiBridge):
    def __init__(self, db_path):
        self.store = JobStore(db_path)
        self.history_revision = 1
        self.worker_calls = 0

    def _refresh_history(self):
        pass

    def ensure_worker(self):
        self.worker_calls += 1
        return {"online": False, "started": False}


def drive(coro):
    """Run a coroutine without an event loop (the offline guard blocks the self-pipe)."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("unexpectedly awaited")


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def payload(self, **over):
        base = {"url": WEBTOON_URL, "chapter_name": "Serie - Ep 1", "slug": "serie_ep_1",
                "mode": "fast", "full": True, "use_cache": False, "force": True,
                "use_context": True, "open_output": False}
        base.update(over)
        return base

    def start(self, **over):
        with mock.patch.object(ui_bridge, "env_status",
                               return_value={"env_exists": True, "nvidia_configured": True}):
            return drive(self.bridge.start(self.payload(**over)))

    def test_submit_persists_a_job_and_returns_its_id(self):
        result = self.start()
        self.assertTrue(result["ok"])
        self.assertTrue(result["job_id"])
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["status"], JobStatus.QUEUED)

    def test_submit_ensures_a_consumer_exists(self):
        # The original bug: a job was persisted with nobody to claim it.
        self.start()
        self.assertEqual(self.bridge.worker_calls, 1)

    def test_worker_offline_is_reported_not_hidden(self):
        self.assertIs(self.start()["worker"]["online"], False)

    def test_double_submit_creates_only_one_job(self):
        first = self.start()
        second = self.start()
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["job_id"], first["job_id"])
        queued = self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED])
        self.assertEqual(len(queued), 1)

    def test_unsupported_source_raises_before_persisting_anything(self):
        with self.assertRaises(UnsupportedSource):
            self.start(url="https://example.org/series/x/chapter-1")
        self.assertEqual(self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED]), [])

    def test_invalid_url_raises_and_persists_nothing(self):
        with self.assertRaises(ValueError):
            self.start(url="not-a-url")
        self.assertEqual(self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED]), [])

    def test_conflicting_cache_and_force_is_rejected(self):
        with self.assertRaises(ValueError):
            self.start(use_cache=True, force=True)


class QueueVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def queue_one(self):
        with mock.patch.object(ui_bridge, "env_status",
                               return_value={"env_exists": True, "nvidia_configured": True}):
            return drive(self.bridge.start(
                {"url": WEBTOON_URL, "slug": "serie_ep_1", "mode": "fast", "full": True,
                 "use_cache": False, "force": True}))

    def test_queued_job_is_not_reported_as_ready(self):
        self.queue_one()
        state = self.bridge.runtime_state()
        self.assertEqual(state["status"], JobStatus.QUEUED)
        self.assertTrue(state["pending"])
        self.assertTrue(state["queue_running"])       # the UI must not fall back to "pronto"

    def test_offline_worker_surfaces_a_blocked_reason(self):
        self.queue_one()
        state = self.bridge.runtime_state()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["blocked_reason"], "worker_offline")

    def test_empty_queue_is_ready_and_not_blocked(self):
        state = self.bridge.runtime_state()
        self.assertEqual(state["status"], "ready")
        self.assertFalse(state["pending"])
        self.assertFalse(state["blocked"])


class FixtureFilteringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def terminal_job(self, *, output_dir, configuration=None):
        job_id = self.bridge.store.create_job(
            source_url=WEBTOON_URL, output_dir=str(output_dir),
            configuration={"job_type": "translation", **(configuration or {})},
            command=["python", "run_webtoon.py"])
        self.bridge.store.transition(job_id, JobStatus.CLAIMING, worker_id="w1")
        self.bridge.store.transition(job_id, JobStatus.STARTING)
        self.bridge.store.transition(job_id, JobStatus.RUNNING)
        self.bridge.store.transition(job_id, JobStatus.FINISHED)
        return job_id

    def test_smoke_fixture_is_never_presented_as_the_last_result(self):
        # The AUTH SMOKE TEST symptom: a terminal translation row whose output is gone.
        self.terminal_job(output_dir=self.tmp / "auth_smoke_gone")
        self.assertIsNone(self.bridge._latest_terminal_job())
        self.assertIsNone(self.bridge.runtime_state()["latest"])

    def test_explicitly_flagged_fixture_is_filtered_even_with_output(self):
        real = self.tmp / "flagged"
        real.mkdir()
        self.terminal_job(output_dir=real, configuration={"fixture": True})
        self.assertIsNone(self.bridge._latest_terminal_job())

    def test_a_real_result_is_still_shown(self):
        real = self.tmp / "serie_ep_1"
        real.mkdir()
        job_id = self.terminal_job(output_dir=real)
        self.assertEqual(self.bridge._latest_terminal_job()["id"], job_id)


class FrontendContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        self.js = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.html = (root / "ui" / "ui_shell.html").read_text(encoding="utf-8")

    def test_in_flight_label_shown_while_the_request_runs(self):
        self.assertIn("Iniciando processamento…", self.js)

    def test_controls_are_not_flipped_before_the_backend_accepts(self):
        # setRunControls(true) must come after the awaited call, not before it.
        start = self.js[self.js.index("async function startTranslation"):]
        body = start[:start.index("\n  async function cancelTranslation")]
        self.assertLess(body.index("await api('/api/ui/run'"), body.index("setRunControls(true)"))

    def test_double_click_guard(self):
        self.assertIn("button.dataset.busy", self.js)

    def test_error_panel_exists_and_is_rendered(self):
        self.assertIn('id="startError"', self.html)
        self.assertIn("showStartError", self.js)
        self.assertIn("Tentar novamente", self.js)

    def test_preview_is_reset_on_a_new_run(self):
        self.assertIn("resetRunPreview", self.js)

    def test_error_panel_never_prints_secrets_or_tracebacks(self):
        panel = self.js[self.js.index("function showStartError"):]
        panel = panel[:panel.index("async function startTranslation")]
        for bad in ("traceback", "stack", "NVIDIA_API_KEY", "Authorization", "token", ".env"):
            self.assertNotIn(bad, panel, bad)


if __name__ == "__main__":
    unittest.main()
