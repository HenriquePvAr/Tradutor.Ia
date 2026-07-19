"""Webtoons Selenium bridge to the lazy-slot resolver, without a real browser."""

import _test_bootstrap  # noqa: F401

import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import chapter_source
import down
import ui_bridge
from chapter_source import SLOT_PENDING, SourceError, UniversalChapterAdapter, select_adapter
from lazy_slot_resolver import ResolverLimits
from webtoons_reader_bridge import WebtoonsReaderBridge

PAGE = "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1"
PAGE_HOST = "webtoon-phinf.pstatic.net"
PLACEHOLDER_HOST = "webtoons-static.pstatic.net"
SLOT_HEIGHT = 1280
REPO = Path(__file__).resolve().parent


def public_dns(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


class FakeWebtoonsDriver:
    def __init__(
        self,
        *,
        total=20,
        resolved=3,
        resolve_on_scroll=True,
        include_extra=False,
        invalid_final_host=False,
    ):
        self.total = total
        self.resolved = set(range(resolved))
        self.resolve_on_scroll = resolve_on_scroll
        self.include_extra = include_extra
        self.extra_active = False
        self.invalid_final_host = invalid_final_host
        self.current_url = PAGE
        self.scrolls = []
        self.quit_calls = 0

    def get(self, url):
        self.current_url = url

    def quit(self):
        self.quit_calls += 1

    def execute_script(self, script, *args):
        text = str(script)
        if "reader_bottom" in text:
            return {
                "found": True,
                "reader_top": 100,
                "reader_bottom": 100 + self.total * SLOT_HEIGHT,
                "viewport_height": SLOT_HEIGHT,
                "document_height": 100 + self.total * SLOT_HEIGHT + 50_000,
            }
        if "window.scrollTo" in text:
            requested = int(args[1])
            top = 100
            bottom = 100 + self.total * SLOT_HEIGHT
            target = min(max(top, requested), max(top, bottom - SLOT_HEIGHT))
            self.scrolls.append(target)
            if self.resolve_on_scroll:
                for index in range(self.total):
                    if target + SLOT_HEIGHT >= 100 + index * SLOT_HEIGHT:
                        self.resolved.add(index)
                # Simulate a lazy callback resolving a later image before an earlier one.
                if len(self.resolved) < self.total:
                    self.resolved.add(min(self.total - 1, len(self.resolved) + 2))
            if self.include_extra:
                self.extra_active = True
            return True
        if "slots: images.map" in text:
            return {"found": True, "slots": self._slots()}
        if "candidates: out" in text:
            return {"found": True, "candidates": self._slots()}
        raise AssertionError("unexpected Selenium script")

    def _slots(self):
        slots = []
        for index in range(self.total):
            done = index in self.resolved
            host = PAGE_HOST if done else PLACEHOLDER_HOST
            if self.invalid_final_host and index == self.total - 1:
                host = "webtoons-static.pstatic.net"
                done = True
            slots.append({
                "tag": "img",
                "url": f"https://{host}/page-{index:03}.jpg",
                "currentSrc": f"https://{host}/page-{index:03}.jpg",
                "src": f"https://{host}/page-{index:03}.jpg",
                "data_url": "",
                "data_src": "",
                "source": "currentSrc",
                "order": index,
                "width": 800 if done else 1,
                "height": SLOT_HEIGHT if done else 1,
                "naturalWidth": 800 if done else 1,
                "naturalHeight": SLOT_HEIGHT if done else 1,
                "complete": done,
                "inContainer": True,
                "isChapterCandidate": True,
                "container": "_imageList",
                "className": "_images",
                "id": "",
                "alt": "",
                "context": "_imageList",
                "y": 100 + index * SLOT_HEIGHT,
            })
        if self.extra_active:
            slots.append({
                "tag": "img",
                "url": f"https://{PAGE_HOST}/extra.jpg",
                "currentSrc": f"https://{PAGE_HOST}/extra.jpg",
                "src": f"https://{PAGE_HOST}/extra.jpg",
                "order": self.total,
                "width": 800,
                "height": SLOT_HEIGHT,
                "naturalWidth": 800,
                "naturalHeight": SLOT_HEIGHT,
                "complete": True,
                "inContainer": True,
                "isChapterCandidate": True,
                "container": "_imageList",
                "className": "_images",
                "id": "",
                "alt": "",
                "context": "_imageList",
                "y": 100 + self.total * SLOT_HEIGHT,
            })
        return slots


def analyse_with_bridge(driver, *, cancel_check=None, limits=None, events=None):
    adapter = select_adapter(PAGE)
    resolver = down._webtoons_lazy_resolver(
        cancel_check=cancel_check,
        on_progress=events.append if events is not None else None,
        limits=limits or ResolverLimits(max_rounds=60, stable_rounds=2, settle_seconds=0),
        sleep=lambda _seconds: None,
    )
    with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
        return adapter.analyze(
            {"driver": driver, "page_url": PAGE, "lazy_slot_resolver": resolver},
            profile=None,
        )


class BridgeOperationTests(unittest.TestCase):
    def test_read_slots_preserves_reader_dom_index_and_placeholder_state(self):
        adapter = select_adapter(PAGE)
        bridge = WebtoonsReaderBridge(FakeWebtoonsDriver(), adapter)
        slots = bridge.read_slots()
        self.assertEqual(len(slots), 20)
        self.assertEqual([item["order"] for item in slots[:4]], [0, 1, 2, 3])
        classified = chapter_source.classify_reader_slots(slots, adapter=adapter)
        self.assertEqual(classified[3]["state"], SLOT_PENDING)

    def test_scroll_is_limited_to_reader_bounds_not_document_end(self):
        adapter = select_adapter(PAGE)
        driver = FakeWebtoonsDriver()
        bridge = WebtoonsReaderBridge(driver, adapter)
        bridge.scroll_to(999_999_999)
        self.assertLess(driver.scrolls[-1], 100 + driver.total * SLOT_HEIGHT + 50_000)


class LazyIntegrationTests(unittest.TestCase):
    def test_webtoons_adapter_resolves_all_slots_before_selection(self):
        events = []
        analysis = analyse_with_bridge(FakeWebtoonsDriver(), events=events)
        self.assertEqual(len(analysis.accepted), 20)
        self.assertEqual([candidate.order for candidate in analysis.accepted], list(range(20)))
        self.assertEqual(analysis.reader_diagnostics["slots_pending"], 0)
        self.assertNotIn(PLACEHOLDER_HOST, str(analysis.public()))
        self.assertTrue(any(event.get("counter_stage") == "source_lazy_resolution"
                            for event in events))

    def test_timeout_keeps_pending_and_marks_source_coverage_incomplete(self):
        analysis = analyse_with_bridge(
            FakeWebtoonsDriver(resolve_on_scroll=False),
            limits=ResolverLimits(max_rounds=2, stable_rounds=1, settle_seconds=0,
                                  timeout_seconds=999),
        )
        self.assertGreater(analysis.reader_diagnostics["slots_pending"], 0)
        self.assertIn("scroll_incomplete", analysis.warnings)
        from source_analysis_phase import source_analysis_is_incomplete

        self.assertTrue(source_analysis_is_incomplete(analysis.public()))

    def test_cancelled_resolution_does_not_produce_a_complete_selection(self):
        calls = {"count": 0}

        def cancel():
            calls["count"] += 1
            return calls["count"] > 3

        with self.assertRaises(SourceError) as ctx:
            analyse_with_bridge(FakeWebtoonsDriver(), cancel_check=cancel)
        self.assertEqual(ctx.exception.code, "cancelled")

    def test_changed_dom_is_reported(self):
        analysis = analyse_with_bridge(FakeWebtoonsDriver(include_extra=True))
        self.assertIn("reader_dom_changed", analysis.warnings)

    def test_invalid_final_host_is_rejected_not_selected(self):
        analysis = analyse_with_bridge(FakeWebtoonsDriver(invalid_final_host=True))
        public = analysis.public()
        self.assertEqual(public["reader_diagnostics"]["slots_rejected"], 1)
        self.assertNotIn("webtoons-static.pstatic.net", str(public.get("accepted", [])))


class AdapterBoundaryTests(unittest.TestCase):
    def test_universal_does_not_use_the_webtoons_bridge(self):
        adapter = UniversalChapterAdapter("https://example.org/chapter/1")
        called = False

        def resolver(*_args):
            nonlocal called
            called = True
            return {}

        self.assertNotEqual(adapter.collection_strategy, "adapter_specific")
        self.assertFalse(called)
        payload = {}
        self.assertIs(down._webtoons_lazy_resolver()(adapter, object(), "", payload), payload)

    def test_vortex_and_local_folder_do_not_use_the_bridge(self):
        vortex = select_adapter("https://vortexscans.org/series/demo-series/chapter-42")
        payload = {}
        self.assertIs(down._webtoons_lazy_resolver()(vortex, object(), "", payload), payload)
        from local_folder_source import LocalFolderChapterAdapter

        self.assertFalse(hasattr(LocalFolderChapterAdapter(), "reader_selectors"))


class WorkerProgressTests(unittest.TestCase):
    def test_worker_persists_lazy_resolution_progress_and_allows_runner_after_selection(self):
        from job_store import JobStatus, JobStore
        from worker_service import Worker

        class WorkerProbe(Worker):
            def __init__(self, store):
                self.store = store
                self.worker_id = "w-test"
                self.pid = 1234
                self._active = None
                self._stop_requested = False

            def _analyze_source(self, _url, *, cancel_check=None, on_progress=None):
                if on_progress:
                    on_progress({
                        "stage": "source_lazy_resolution",
                        "counter_stage": "source_lazy_resolution",
                        "current": 20,
                        "total": 20,
                        "message": "Carregando páginas do leitor: 20/20",
                    })
                return analyse_with_bridge(FakeWebtoonsDriver(), events=[], limits=None)

        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            try:
                job_id = store.create_job(
                    source_url=PAGE,
                    output_dir=str(Path(folder) / "out"),
                    configuration={"job_type": "translation"},
                    command=["python", "-c", "pass"],
                )
                store.update_fields(job_id, source_type="url")
                store.transition(job_id, JobStatus.CLAIMING, worker_id="w-test")
                worker = WorkerProbe(store)
                prepared = worker._prepare_source(store.get_job(job_id))
                self.assertIsNotNone(prepared)
                row = store.get_job(job_id)
                self.assertEqual(row["status"], JobStatus.QUEUED)
                self.assertTrue(row["source_selection"]["candidate_ids"])
                self.assertEqual(row["stage"], "created")
                self.assertEqual(row["progress_counter_stage"], "source_lazy_resolution")
            finally:
                store.close()


class DriverLifecycleTests(unittest.TestCase):
    def test_source_analysis_closes_driver_for_success_incomplete_and_cancel(self):
        cases = [
            ("success", FakeWebtoonsDriver(), None),
            ("incomplete", FakeWebtoonsDriver(resolve_on_scroll=False), None),
            ("cancelled", FakeWebtoonsDriver(), self._cancel_during_lazy()),
        ]
        for _name, driver, cancel_check in cases:
            created = []
            closed = []

            def teardown(current, _ownership):
                closed.append(current)
                current.quit()
                return {"status": "success", "timeout_occurred": False}

            with (
                mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns),
                mock.patch("down.preflight_browser_navigation", side_effect=lambda _adapter, value: value),
                mock.patch("down._create_driver", side_effect=lambda: created.append(driver) or driver),
                mock.patch("down._capture_driver_ownership", return_value={}),
                mock.patch("down._refresh_driver_ownership"),
                mock.patch("down._bounded_driver_teardown", side_effect=teardown),
                mock.patch("down._scroll_incrementally", return_value={
                    "reached_document_end": True,
                    "stabilized": True,
                }),
                mock.patch("down.time.sleep"),
            ):
                try:
                    down.analyze_chapter_source(PAGE, cancel_check=cancel_check)
                except SourceError as exc:
                    self.assertEqual(exc.code, "cancelled")

            self.assertEqual(len(created), 1)
            self.assertEqual(created, closed)
            self.assertEqual(driver.quit_calls, 1)

    @staticmethod
    def _cancel_during_lazy():
        calls = {"count": 0}

        def cancel():
            calls["count"] += 1
            return calls["count"] > 3

        return cancel


def drive(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("unexpectedly awaited")


class SyntheticWebtoonsE2ETests(unittest.TestCase):
    def test_submit_worker_lazy_selection_fake_pipeline_pdf(self):
        from job_store import JobStatus, JobStore
        from worker_service import Worker

        class BridgeProbe(ui_bridge.UiBridge):
            def __init__(self, db_path):
                self.store = JobStore(db_path)
                self.history_revision = 1
                self.worker_calls = 0
                self.browser_calls = 0

            def _refresh_history(self):
                return None

            def ensure_worker(self):
                self.worker_calls += 1
                return {"online": True, "started": False}

            def _analyze_source(self, *_args, **_kwargs):
                self.browser_calls += 1
                raise AssertionError("submit must not open a browser")

        class WorkerProbe(Worker):
            def __init__(self, store):
                self.store = store
                self.worker_id = "w-test"
                self.pid = 4321
                self._active = None
                self._stop_requested = False
                self.runner_starts = 0

            def _analyze_source(self, _url, *, cancel_check=None, on_progress=None):
                return analyse_with_bridge(
                    FakeWebtoonsDriver(),
                    events=[],
                    limits=ResolverLimits(max_rounds=60, stable_rounds=2, settle_seconds=0),
                )

            def run_fake_runner(self, job):
                self.runner_starts += 1
                output_dir = Path(job["output_dir"])
                command = [
                    sys.executable,
                    "-u",
                    str(REPO / "fake_pipeline.py"),
                    "--output-dir",
                    str(output_dir),
                    "--outcome",
                    "finished",
                    "--steps",
                    "1",
                    "--sleep",
                    "0",
                ]
                proc = subprocess.run(
                    command,
                    cwd=str(REPO),
                    capture_output=True,
                    timeout=60,
                )
                for target in (JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING):
                    self.store.transition(
                        job["id"],
                        target,
                        **({"worker_id": self.worker_id} if target == JobStatus.CLAIMING else {}),
                    )
                self.store.update_fields(job["id"], exit_code=proc.returncode)
                self.store.transition(job["id"], JobStatus.FINISHED)
                return proc, output_dir

        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "jobs.sqlite3"
            bridge = BridgeProbe(db_path)
            try:
                with (
                    mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns),
                    mock.patch.object(ui_bridge, "OUTPUT_ROOT", Path(folder) / "output"),
                ):
                    submitted = drive(bridge.start({
                        "url": PAGE,
                        "slug": "synthetic_webtoons_lazy",
                        "mode": "fast",
                        "full": True,
                        "use_cache": False,
                        "force": True,
                        "use_context": False,
                    }))
                self.assertEqual(submitted["status"], JobStatus.QUEUED)
                self.assertEqual(bridge.worker_calls, 1)
                self.assertEqual(bridge.browser_calls, 0)

                worker = WorkerProbe(bridge.store)
                bridge.store.transition(submitted["job_id"], JobStatus.CLAIMING,
                                        worker_id=worker.worker_id)
                prepared = worker._prepare_source(bridge.store.get_job(submitted["job_id"]))
                self.assertIsNotNone(prepared)
                self.assertEqual(len(prepared["source_selection"]["candidate_ids"]), 20)
                proc, output_dir = worker.run_fake_runner(prepared)

                self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
                self.assertEqual(worker.runner_starts, 1)
                pdf = output_dir / "the_fake_chapter.pdf"
                self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
                self.assertTrue((output_dir / "timing_report.json").is_file())
                self.assertEqual(
                    bridge.store.get_job(submitted["job_id"])["status"],
                    JobStatus.FINISHED,
                )
            finally:
                bridge.store.close()


if __name__ == "__main__":
    unittest.main()
