"""#84F6R: source-analysis routing, pre-job failure visibility and optional Paddle.

Three separate contracts, all hermetic:

* a supported VortexScans URL is dispatched to its own adapter and never needs the
  Webtoons canonicalizer or any Webtoons request, even while Webtoons is timing out;
* a source analysis that fails *before* a job exists reaches the user as a classified,
  recoverable error instead of an unclassified 500, and creates no job;
* ``quality_optimized`` runs on RapidOCR with Paddle absent - Paddle stays an optional
  fallback whose empty result is rejected by the #84F5 guard.
"""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import requests

import chapter_source
import down
import ocr_engine
import run_webtoon

ROOT = Path(__file__).resolve().parent

VORTEX_URL = "https://vortexscans.org/series/shadow-slave/chapter-1.5"
WEBTOONS_URL = (
    "https://www.webtoons.com/en/fantasy/shadow-slave/ep-1/viewer"
    "?title_no=6555&episode_no=1"
)
PUBLIC_DNS = [(2, 1, 6, "", ("93.184.216.34", 0))]


class _QuitDriver:
    current_url = VORTEX_URL

    def get(self, _url):
        return None

    def execute_script(self, *_args):
        return None

    def quit(self):
        return None


def _webtoons_is_down(*_args, **_kwargs):
    raise requests.exceptions.ReadTimeout("webtoons metadata timed out")


# ---------------------------------------------------------------- Phase A
class SourceDispatchIsolationTests(unittest.TestCase):
    """A Webtoons outage must not be able to reach a Vortex source analysis."""

    def test_vortex_url_selects_only_its_own_adapter(self):
        adapter = chapter_source.select_adapter(VORTEX_URL)
        self.assertEqual(adapter.name, "vortexscans")
        # The Webtoons canonicalizer is an adapter-owned method, not a shared step.
        self.assertIsNone(getattr(adapter, "resolve_canonical_url", None))

    def test_vortex_analysis_succeeds_while_webtoons_canonicalizer_times_out(self):
        sentinel = SimpleNamespace(outcome="supported_specific_adapter", accepted=[])
        with (
            patch("canonical_source_identity.canonicalize_webtoons_url", _webtoons_is_down),
            patch.object(requests.Session, "get", _webtoons_is_down),
            patch.object(chapter_source.socket, "getaddrinfo", return_value=PUBLIC_DNS),
            patch("down.inspect_source_preflight", side_effect=lambda _a, value, **_k: value),
            patch("down._create_driver", return_value=_QuitDriver()),
            patch("down._capture_driver_ownership", return_value={}),
            patch("down._refresh_driver_ownership"),
            patch("down._bounded_driver_teardown", return_value={}),
            patch("down._scroll_incrementally",
                  return_value={"reached_document_end": True, "stabilized": True}),
            patch("down._load_source_profile", return_value=None),
            patch("down.time.sleep"),
            patch.object(chapter_source.VortexScansAdapter, "analyze", return_value=sentinel),
        ):
            self.assertIs(down.analyze_chapter_source(VORTEX_URL), sentinel)

    def test_webtoons_url_still_routes_through_the_webtoons_canonicalizer(self):
        adapter = chapter_source.select_adapter(WEBTOONS_URL)
        self.assertEqual(adapter.name, "webtoons")
        with patch("canonical_source_identity.canonicalize_webtoons_url") as resolver:
            down._resolve_canonical_source(adapter, WEBTOONS_URL)
        self.assertEqual(resolver.call_args.args[0], WEBTOONS_URL)

    def test_unsupported_host_is_never_silently_treated_as_a_known_source(self):
        adapter = chapter_source.select_adapter("https://unknown.example.test/ch/1")
        self.assertEqual(adapter.name, "universal")
        self.assertNotIn(adapter.name, {"vortexscans", "webtoons"})


# ---------------------------------------------------------------- Phase B
class PreJobSourceFailureVisibilityTests(unittest.TestCase):
    """SOURCE-ANALYSIS-OBSERVABILITY-001: a pre-job failure must not be silent."""

    def setUp(self):
        import app_ui
        from ui_bridge import UiBridge

        self.app_ui = app_ui
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict("os.environ", {
            "APP_ENV": "test",
            "ALLOW_LOCAL_TEST_IDENTITIES": "1",
            "TRADUTOR_TEST_RUNTIME_ROOT": self.tmp.name,
        })
        self.env.start()
        self.bridge = UiBridge()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.bridge.close)

    def _analyze_raising(self, exc):
        async def failing(_payload, **_kwargs):
            raise exc

        stub = SimpleNamespace(
            require_authenticated=lambda _r: SimpleNamespace(
                user_id="user-a", owner_id="user-a", authenticated=True),
            require_csrf=lambda _r, _p: None,
        )
        bridge = SimpleNamespace(analyze_source_candidate=failing)
        with patch.multiple(self.app_ui, AUTH=stub, BRIDGE=bridge):
            with self.assertRaises(self.app_ui.HTTPException) as caught:
                _drive(self.app_ui.api_source_analyze(SimpleNamespace(), payload={}))
        return caught.exception

    def test_source_timeout_reaches_the_user_as_a_recoverable_failure(self):
        exc = self._analyze_raising(requests.exceptions.ReadTimeout("read timed out"))
        self.assertEqual(exc.status_code, 502)
        self.assertEqual(exc.detail["code"], "source_timeout")
        self.assertEqual(exc.detail["stage"], "analise_da_fonte")
        self.assertTrue(exc.detail["message"])
        self.assertTrue(exc.detail["action"])

    def test_network_error_keeps_its_own_class_internally(self):
        exc = self._analyze_raising(requests.exceptions.ConnectionError("refused"))
        self.assertEqual(exc.detail["code"], "source_network_error")

    def test_contextual_timeout_error_is_a_source_timeout_at_the_ui_boundary(self):
        exc = self._analyze_raising(TimeoutError("timed out"))
        self.assertEqual(exc.detail["code"], "source_timeout")

    def test_unclassified_failure_still_produces_an_actionable_state(self):
        exc = self._analyze_raising(RuntimeError("boom"))
        self.assertEqual(exc.detail["code"], "source_analysis_failed")
        self.assertEqual(exc.status_code, 502)

    def test_no_traceback_or_raw_exception_text_reaches_the_user(self):
        exc = self._analyze_raising(RuntimeError("C:/secret/path.py token=abc123"))
        rendered = " ".join(str(value) for value in exc.detail.values())
        for leaked in ("Traceback", "token=abc123", "secret/path.py", "RuntimeError"):
            self.assertNotIn(leaked, rendered)

    def test_pre_job_failure_creates_no_job_and_no_run(self):
        before = self.bridge.store.list_jobs(limit=None)
        self._analyze_raising(requests.exceptions.ReadTimeout("read timed out"))
        self.assertEqual(self.bridge.store.list_jobs(limit=None), before)

    def test_source_network_failures_are_classified_apart_from_browser_startup(self):
        code = down._pipeline_exception_code
        self.assertEqual(code(requests.exceptions.ReadTimeout("read timed out")), "source_timeout")
        self.assertEqual(code(requests.exceptions.ConnectTimeout("t")), "source_timeout")
        self.assertEqual(code(TimeoutError("timed out")), "browser_startup_timeout")
        self.assertEqual(
            code(requests.exceptions.ConnectionError("conn")), "source_network_error")
        # The browser taxonomy is unchanged.
        self.assertEqual(
            code(RuntimeError("session not created")), "browser_launch_failed")


class StartFailureUiContractTests(unittest.TestCase):
    """The Start button must unlock and show the recoverable state, with no blue surface."""

    def setUp(self):
        self.js = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")

    def test_start_single_flight_lock_always_releases(self):
        start_block = self.js[self.js.index("startInFlight = runStartTranslation"):]
        start_block = start_block[:start_block.index("async function runStartTranslation")]
        self.assertIn(".catch(error =>", start_block)
        self.assertIn(".finally(() =>", start_block)
        self.assertIn("startInFlight = null;", start_block)
        self.assertIn("updateTranslationStartControls();", start_block)
        block = self.js[self.js.index("analysisResult = await api('/api/ui/source/analyze'"):]
        block = block[:block.index("const ready =")]
        self.assertIn("showSourceValidationError(error)", block)
        self.assertIn("delete button.dataset.busy", block)

    def test_pre_job_failure_clears_the_pipeline_surface(self):
        block = self.js[self.js.index("function showSourceValidationError"):]
        block = block[:block.index("\n  async function validateSource")]
        self.assertIn("clearLoadingSurface()", block)
        self.assertIn("sourceValidationRetryBtn", block)

    def test_recoverable_source_classes_map_to_the_retry_message(self):
        block = self.js[self.js.index("function sourceErrorCategory"):]
        block = block[:block.index("function sourceReportPayload")]
        for code in ("source_timeout", "source_network_error"):
            self.assertIn(code, block)
        self.assertIn(
            "source_temporarily_unavailable: 'Não foi possível acessar essa fonte agora."
            " Tente novamente.'",
            self.js)


# ---------------------------------------------------------------- Phase C
class QualityModeOcrPolicyTests(unittest.TestCase):
    """quality_optimized is RapidOCR-primary; Paddle is an optional fallback."""

    def setUp(self):
        import config

        env = patch.dict("os.environ", {"TRADUTOR_OCR_ENGINE_OVERRIDE": ""})
        env.start()
        self.addCleanup(env.stop)
        for name in (
            "OCR_ENGINE", "OCR_FALLBACK_ENGINE", "OCR_HYBRID_FALLBACK",
            "RAPIDOCR_ENABLED", "RAPIDOCR_PAGE_FALLBACK",
            "OCR_REGION_SELECTIVE_FALLBACK", "FAST_OCR_MODE",
            "POST_RENDER_OCR_VALIDATION",
        ):
            attr = patch.object(config, name, getattr(config, name, None))
            attr.start()
            self.addCleanup(attr.stop)

    @staticmethod
    def _only_rapidocr(name):
        return object() if str(name).startswith("rapidocr") else None

    def test_quality_mode_configures_rapidocr_when_paddle_is_not_installed(self):
        import config

        with patch.object(
            ocr_engine.importlib.util, "find_spec", side_effect=self._only_rapidocr
        ):
            self.assertEqual(run_webtoon._configure_mode("quality"), "rapidocr")

        self.assertEqual(config.OCR_ENGINE, "rapidocr")
        self.assertTrue(config.RAPIDOCR_ENABLED)
        self.assertFalse(config.FAST_OCR_MODE)

    def test_beta_quality_has_no_cross_engine_fallback(self):
        import config

        with patch.object(
            ocr_engine.importlib.util, "find_spec", side_effect=self._only_rapidocr
        ):
            run_webtoon._configure_mode("quality")

        self.assertEqual(config.OCR_FALLBACK_ENGINE, "")
        self.assertFalse(config.OCR_HYBRID_FALLBACK)

    def test_missing_paddle_is_skipped_not_fatal_when_rapidocr_answered(self):
        engine = ocr_engine.OCREngine("en", engine="rapidocr", fallback_engine="paddle")
        rapid_line = _ocr_line("AND THEN...", (20, 10, 70, 20))
        with (
            patch.object(ocr_engine.config, "OCR_HYBRID_FALLBACK", True),
            patch.object(ocr_engine.config, "OCR_LEGACY_PADDLE_FALLBACK", True),
            patch.object(ocr_engine.config, "FAST_OCR_MODE", False),
            patch.object(ocr_engine.OCREngine, "_get_paddle",
                         side_effect=ModuleNotFoundError("paddleocr")),
        ):
            lines = engine._fallback_from_rapidocr(
                _blank_image(),
                1,
                "suspect_region",
                rapid_lines=[rapid_line],
            )

        self.assertEqual([line.text for line in lines], ["AND THEN..."])
        self.assertFalse(engine.last_run_metadata.get("engine_unavailable"))
        self.assertEqual(engine.last_run_metadata.get("final_engine"), "rapidocr")
        self.assertEqual(
            engine.last_run_metadata.get("fallback_rejected_reason"),
            "optional_paddle_unavailable",
        )

    def test_rapidocr_page_suspicion_preserves_good_story_line_without_paddle(self):
        engine = ocr_engine.OCREngine("en", engine="rapidocr", fallback_engine="paddle")
        story_line = _ocr_line("AND THEN...", (30, 25, 90, 24))
        with (
            patch.object(ocr_engine.config, "RAPIDOCR_ENABLED", True),
            patch.object(ocr_engine.config, "RAPIDOCR_PAGE_FALLBACK", True),
            patch.object(ocr_engine.config, "OCR_HYBRID_FALLBACK", True),
            patch.object(ocr_engine.config, "FAST_OCR_MODE", False),
            patch.object(ocr_engine, "_estimate_text_regions", return_value=13),
            patch.object(ocr_engine, "_detect_story_text_region_boxes",
                         return_value=[(20, 10, 120, 60)]),
            patch.object(ocr_engine.OCREngine, "_detect_with_rapidocr",
                         return_value=[story_line]),
            patch.object(ocr_engine.OCREngine, "_get_paddle",
                         side_effect=AssertionError("Paddle must not be required")),
        ):
            lines = engine.detect_lines(_blank_image(), page=32)

        self.assertEqual([line.text for line in lines], ["AND THEN..."])
        self.assertFalse(engine.last_run_metadata.get("engine_unavailable"))
        self.assertEqual(engine.last_run_metadata.get("final_engine"), "rapidocr")
        self.assertEqual(engine.last_run_metadata.get("ocr_sufficiency"), ocr_engine.OCR_REVIEW)

    def test_zero_line_decorative_page_does_not_become_engine_failure(self):
        engine = ocr_engine.OCREngine("en", engine="rapidocr", fallback_engine="paddle")
        with (
            patch.object(ocr_engine.config, "RAPIDOCR_ENABLED", True),
            patch.object(ocr_engine.config, "RAPIDOCR_PAGE_FALLBACK", True),
            patch.object(ocr_engine, "_estimate_text_regions", return_value=20),
            patch.object(ocr_engine, "_detect_story_text_region_boxes", return_value=[]),
            patch.object(ocr_engine.OCREngine, "_detect_with_rapidocr", return_value=[]),
            patch.object(ocr_engine.OCREngine, "_get_paddle",
                         side_effect=AssertionError("Paddle must not be required")),
        ):
            lines = engine.detect_lines(_blank_image(), page=8)

        self.assertEqual(lines, [])
        self.assertFalse(engine.last_run_metadata.get("engine_unavailable"))
        self.assertEqual(engine.last_run_metadata.get("ocr_sufficiency"), ocr_engine.OCR_REVIEW)

    def test_zero_line_story_box_still_fails_closed_after_regional_retry_fails(self):
        import ocr_parallel

        engine = ocr_engine.OCREngine("en", engine="rapidocr", fallback_engine="paddle")
        with (
            patch.object(ocr_engine.config, "RAPIDOCR_ENABLED", True),
            patch.object(ocr_engine.config, "RAPIDOCR_PAGE_FALLBACK", True),
            patch.object(ocr_engine, "_estimate_text_regions", return_value=8),
            patch.object(ocr_engine, "_detect_story_text_region_boxes",
                         return_value=[(20, 10, 120, 60)]),
            patch.object(ocr_engine.OCREngine, "_detect_with_rapidocr", return_value=[]),
        ):
            lines = engine.detect_lines(_blank_image(), page=19)

        self.assertEqual(lines, [])
        self.assertEqual(engine.last_run_metadata.get("ocr_sufficiency"), ocr_engine.OCR_INSUFFICIENT)
        self.assertEqual(
            ocr_parallel._ocr_error_from_metadata(engine.last_run_metadata),
            "ocr_insufficient:zero_lines_on_story_like_page",
        )

    def test_missing_primary_rapidocr_fails_closed_without_downgrading(self):
        def only_paddle(name):
            return None if str(name).startswith("rapidocr") else object()

        with patch.object(ocr_engine.importlib.util, "find_spec", side_effect=only_paddle):
            with self.assertRaises(ocr_engine.OCREngineUnavailableError) as caught:
                run_webtoon._configure_mode("quality")

        self.assertEqual(caught.exception.engine, "rapidocr")
        self.assertEqual(caught.exception.reason_class, "dependency_unavailable")

    def test_explicit_paddle_override_is_ignored(self):
        with patch.dict("os.environ", {"TRADUTOR_OCR_ENGINE_OVERRIDE": "paddle"}):
            with patch.object(
                ocr_engine.importlib.util, "find_spec", side_effect=self._only_rapidocr
            ):
                self.assertEqual(run_webtoon._configure_mode("quality"), "rapidocr")


class OptionalFallbackGuardTests(unittest.TestCase):
    """#84F5 through the production decision, not a test-only copy."""

    @staticmethod
    def _lines(count):
        return [SimpleNamespace(text="palavra") for _ in range(count)]

    def test_empty_paddle_fallback_is_rejected(self):
        import benchmark_pipeline

        self.assertTrue(benchmark_pipeline._fallback_discards_source_text(
            self._lines(20), []))

    def test_slightly_different_fallback_is_not_rejected(self):
        import benchmark_pipeline

        # 20 lines of "palavra" = 140 letters; 19 lines = 133 letters (95%).
        self.assertFalse(benchmark_pipeline._fallback_discards_source_text(
            self._lines(20), self._lines(19)))

    def test_quality_mode_full_page_fallback_uses_that_same_guard(self):
        source = (ROOT / "benchmark_pipeline.py").read_text(encoding="utf-8")
        block = source[source.index("pipeline.full_page_paddle_fallback"):]
        block = block[:block.index("_boxes_substantially_overlap")]
        self.assertIn("if _fallback_discards_source_text(", block)
        self.assertIn('"fallback_rejected_reason": "fallback_discards_source_text"', block)


def _blank_image():
    import numpy as np

    return np.zeros((40, 120, 3), dtype="uint8")


def _ocr_line(text, box):
    import numpy as np

    x, y, width, height = box
    polygon = np.array(
        [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
        dtype=np.int32,
    )
    return ocr_engine.OCRLine(
        text=text,
        confidence=0.96,
        polygon=polygon,
        box=box,
        raw_text=text,
        engine="rapidocr",
    )


def _drive(coroutine):
    """Run one coroutine to completion without a running event loop."""
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coroutine)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
