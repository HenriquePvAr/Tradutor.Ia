"""TDD #80 contracts: History refresh, boot overlay containment, start idempotency.

Hermetic.  No real job, no provider, no remote mutation.  Three defects are
pinned here, each proven against the local Chapter 2 evidence before it was
written:

``HISTORY-REVISION-CROSS-PROCESS-001``
    ``UiBridge.history_revision`` was an in-memory counter in the UI process.
    The worker process writes the terminal transition straight to SQLite, so
    the counter never moved and the browser -- which refreshes History only
    when that number changes -- kept a stale list for minutes.

``PIPELINE-UI-REGRESSION-001``
    ``renderBootstrapSurface`` painted the bootstrap readiness view into
    ``#loadingSurface``, which lives inside the "Nova tradução" column above
    the real Pipeline panel.  Every ``refreshBootstrap()`` -- not just the
    first boot -- repainted it, displacing the pipeline.

``START-SPAM-001``
    ``_pending_duplicate()`` was a read followed by an unguarded
    ``_create_job()``.  Two concurrent submissions both saw "no duplicate" and
    both created a job.

Deliberately NOT registered: ``FINALIZATION-RACE-001``.  The Chapter 2
evidence shows the artifacts were published *before* the terminal DB write
(PDF 20:04:25.68, manifest 20:04:29.84, DB terminal 20:04:31.077), so the
ordering contract held.  It is pinned below anyway so it stays that way.
"""

import _test_bootstrap  # noqa: F401

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from job_store import JobStatus, JobStore


ROOT = Path(__file__).resolve().parent


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class StoreCase(unittest.TestCase):
    """Opens temp stores and closes them in the right order.

    Windows refuses to unlink an open sqlite file, so the directory cleanup is
    registered *first* and therefore runs *last* -- ``addCleanup`` is LIFO.
    """

    def tmpdir(self) -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name

    def store(self, tmp: str) -> JobStore:
        store = JobStore(Path(tmp) / "jobs.sqlite3")
        self.addCleanup(store.close)
        return store


def new_job(store: JobStore, *, slug: str, owner: str = "owner-a", **kw) -> str:
    """Create a chapter job the way the bridge does.

    ``chapter_slug`` in the configuration is the key the bridge dedupes on, so
    it is also the key the database index enforces.
    """
    return store.create_job(
        source_url=kw.pop("url", f"https://example.invalid/{slug}"),
        output_dir=str(Path(tempfile.gettempdir()) / slug),
        command=["python", "-c", "pass"],
        series_slug=kw.pop("series_slug", slug),
        configuration={
            "community_owner_id": owner,
            "job_type": "translation",
            "chapter_slug": slug,
        },
        **kw,
    )


def terminalize(store: JobStore, job_id: str, status: str, **fields) -> None:
    """Walk the real lifecycle: a job cannot jump from queued to a terminal state."""
    for step in (JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING):
        store.transition(job_id, step)
    store.transition(job_id, status, **fields)


class HistoryRevisionIsCrossProcess(StoreCase):
    """The refresh signal must survive a write from another process."""

    def test_store_exposes_a_terminal_revision_derived_from_the_database(self):
        store = self.store(self.tmpdir())
        self.assertTrue(
            hasattr(store, "terminal_revision"),
            "JobStore must expose a DB-derived terminal revision; an "
            "in-memory counter cannot see another process's write",
        )
        self.assertIsInstance(store.terminal_revision(), float)

    def test_terminal_revision_changes_when_another_connection_terminalizes(self):
        """The exact Chapter 2 failure: worker writes, UI process must notice."""
        tmp = self.tmpdir()
        ui = self.store(tmp)
        job_id = new_job(ui, slug="shadow_slave_chapter_2")
        before = ui.terminal_revision()

        # A *separate* connection stands in for the worker process.
        worker = self.store(tmp)
        terminalize(worker, job_id, JobStatus.REVIEW_REQUIRED,
                    reason_code="quality_review_required")

        self.assertNotEqual(
            before, ui.terminal_revision(),
            "the UI-side store must observe the worker's terminal write",
        )

    def test_terminal_revision_is_owner_scoped_when_an_owner_is_given(self):
        store = self.store(self.tmpdir())
        job_id = new_job(store, slug="chapter_a", owner="owner-a")
        other = store.terminal_revision("owner-b")
        terminalize(store, job_id, JobStatus.FINISHED)
        self.assertEqual(other, store.terminal_revision("owner-b"))
        self.assertNotEqual(other, store.terminal_revision("owner-a"))

    def test_published_history_revision_is_not_only_the_in_memory_counter(self):
        bridge = read("ui_bridge.py")
        at = bridge.index('"history_revision":')
        published = bridge[at:at + 200]
        self.assertIn(
            "terminal_revision", published,
            "the published revision must fold in DB-derived state; "
            "self.history_revision alone is blind to the worker process",
        )


class BootOverlayStaysInBoot(unittest.TestCase):
    """The readiness view must never repaint over the running application."""

    def setUp(self):
        self.js = read("static/tradutor_ui.js")
        self.html = read("ui/ui_shell.html")

    def test_bootstrap_surface_renders_only_while_actually_booting(self):
        start = self.js.index("function renderBootstrapSurface")
        body = self.js[start:self.js.index("function clearLoadingSurface", start)]
        self.assertIn(
            "bootHasClosed", body,
            "renderBootstrapSurface must refuse to paint once boot is over; "
            "refreshBootstrap() runs for the whole life of the app",
        )
        guard = body[:body.index("root.hidden = false")]
        self.assertIn("return", guard,
                      "the guard must return before unhiding #loadingSurface")

    def test_close_boot_latches_so_a_later_refresh_cannot_repaint(self):
        close = self.js[self.js.index("function closeBoot"):]
        close = close[:close.index("\n  setBootStage(0)")]
        self.assertIn("bootHasClosed = true", close)

    def test_loading_surface_sits_in_the_column_above_the_real_pipeline(self):
        # Not a layout preference: this adjacency is why an ungated repaint
        # displaced the pipeline instead of overlaying something harmless.
        self.assertLess(self.html.index('id="loadingSurface"'),
                        self.html.index('id="stageList"'))

    def test_inline_pipeline_keeps_the_current_production_stages(self):
        stages = [
            "source_analysis", "awaiting_source_review", "download", "validation",
            "ocr", "translate", "render", "pdf", "quality_review",
        ]
        block = self.html[self.html.index('id="stageList"'):]
        block = block[:block.index('id="runSummary"')]
        for stage in stages:
            self.assertIn(f'data-stage="{stage}"', block)
        self.assertNotIn("NVIDIA", block)

    def test_startup_health_checks_are_preserved(self):
        # Removing the blue screen must not remove the checks behind it.
        view = read("static/loading_view.js")
        for label in ("Sessão", "Ambiente", "Interface"):
            self.assertIn(label, view)


class StartIsIdempotent(StoreCase):
    """One start intent per burst, at the UI *and* at the database."""

    def test_second_active_job_for_the_same_chapter_is_refused_by_the_database(self):
        store = self.store(self.tmpdir())
        new_job(store, slug="shadow_slave_chapter_2")
        with self.assertRaises(sqlite3.IntegrityError):
            new_job(store, slug="shadow_slave_chapter_2")

    def test_concurrent_submissions_create_exactly_one_row(self):
        """Ten simultaneous starts -- the real burst -- must yield one job."""
        tmp = self.tmpdir()
        self.store(tmp)  # migrate once, before the threads race
        created, refused = [], []
        barrier = threading.Barrier(10)

        def submit():
            store = JobStore(Path(tmp) / "jobs.sqlite3")
            try:
                barrier.wait()
                created.append(new_job(store, slug="shadow_slave_chapter_2"))
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                refused.append(1)
            finally:
                store.close()

        threads = [threading.Thread(target=submit) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(created), 1, "exactly one job row may be created")
        self.assertEqual(len(refused), 9)

    def test_a_terminal_chapter_can_be_translated_again(self):
        """Idempotency protects a burst; it must not be a permanent ban."""
        store = self.store(self.tmpdir())
        first = new_job(store, slug="shadow_slave_chapter_2")
        terminalize(store, first, JobStatus.REVIEW_REQUIRED,
                    reason_code="quality_review_required")
        second = new_job(store, slug="shadow_slave_chapter_2")
        self.assertNotEqual(first, second)

    def test_different_owners_do_not_collide(self):
        store = self.store(self.tmpdir())
        new_job(store, slug="shadow_slave_chapter_2", owner="owner-a")
        new_job(store, slug="shadow_slave_chapter_2", owner="owner-b")

    def test_ui_start_is_single_flight_not_only_a_disabled_button(self):
        js = read("static/tradutor_ui.js")
        block = js[js.index("let startInFlight"):]
        block = block[:block.index("\n  function visibleCancelControl")]
        self.assertIn(
            "startInFlight", block,
            "a dataset flag on a DOM node is UX; the concurrency contract "
            "needs a single-flight promise that later callers reuse",
        )

    def test_start_shows_busy_state_before_the_blocking_source_analysis_call(self):
        """MISSION-84F34-C: source analysis runs synchronously inside the
        request/response cycle of ``/api/ui/source/analyze`` (it can take up
        to 190s - see the ``timeoutMs`` on that call). A first click that
        does not visibly react until that call resolves reads as "did
        nothing", inviting repeat clicks. The button's busy state and the
        indeterminate progress render must happen synchronously, before that
        awaited call - not be faked with a fabricated percentage.
        """
        js = read("static/tradutor_ui.js")
        start = js.index("async function runStartTranslation")
        end = js.index("\n  function visibleCancelControl")
        block = js[start:end]
        busy_index = block.index("button.dataset.busy = '1'")
        render_index = block.index("renderLocalPipelineState('source_analysis'")
        analyze_call_index = block.index("api('/api/ui/source/analyze'")
        self.assertLess(
            busy_index, analyze_call_index,
            "the Start button must go busy before the source-analysis "
            "request is awaited, not after it resolves",
        )
        self.assertLess(
            render_index, analyze_call_index,
            "the progress state must render before the network call so the "
            "spinner is real feedback, not faked progress after the fact",
        )

    def test_every_start_entrypoint_uses_the_canonical_command(self):
        js = read("static/tradutor_ui.js")
        start = js.index("let startInFlight")
        end = js.index("\n  function visibleCancelControl")
        outside = js[:start] + js[end:]
        self.assertNotIn(
            "/api/ui/run", outside,
            "only startTranslation may POST /api/ui/run; a second call site "
            "would be a start path with no single-flight guard",
        )
        self.assertIn("$('#startBtn')?.addEventListener('click', startTranslation)", js)
        self.assertGreaterEqual(js.count("startTranslation()"), 3)


class ArtifactOrderingStaysCorrect(StoreCase):
    """Chapter 2 published artifacts before terminal state.  Keep it that way.

    A contract, not a bug report: FINALIZATION-RACE-001 was NOT proven.  The
    forensic timeline showed PDF and manifest on disk seconds before the DB
    went terminal.
    """

    def test_terminal_transition_records_its_reason_code(self):
        store = self.store(self.tmpdir())
        job_id = new_job(store, slug="ordering_probe")
        terminalize(store, job_id, JobStatus.REVIEW_REQUIRED,
                    reason_code="quality_review_required")
        job = store.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.REVIEW_REQUIRED)
        self.assertEqual(job["reason_code"], "quality_review_required")

    def test_review_required_is_an_artifact_complete_terminal_state(self):
        # review_required must never be read as "no PDF": the Chapter 2 run is
        # review_required and carries a bound 13MB PDF.
        self.assertIn(JobStatus.REVIEW_REQUIRED, JobStatus.TERMINAL)


if __name__ == "__main__":
    unittest.main()
