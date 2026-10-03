"""NVIDIA OCR (second engine) + hybrid dynamic-queue scheduler.

Scope: OCR only. Nothing here calls NVIDIA for real (the hermetic guard from
_test_bootstrap blocks real sockets) and nothing here touches translation --
DeepL/quality_optimized/yomu_backend stay exactly as configured today.
"""
import _test_bootstrap  # noqa: F401

import base64
import io
import unittest
from unittest import mock

import numpy as np

import config
from ocr_contract import OCRPageResult, OCRRegion
from ocr_engine import OCRLine


def _fake_image(width=20, height=10):
    return np.zeros((height, width, 3), dtype=np.uint8)


def _fake_cv2_imread(path):
    return _fake_image()


def _detection(text, points, confidence=0.9):
    """One text_detection in the official Nemotron OCR v2 response shape."""
    return {
        "text_prediction": {"text": text, "confidence": confidence},
        "bounding_box": {"points": [{"x": x, "y": y} for x, y in points]},
    }


def _ocr_v2_payload(detections):
    return {"model": "nvidia/nemotron-ocr-v2", "data": [{"index": 0, "text_detections": detections}]}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_not_json=False):
        self.status_code = status_code
        self._payload = payload or {}
        self._raise_not_json = raise_not_json

    def json(self):
        if self._raise_not_json:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """Records outgoing requests (to assert no secret leaks) and returns a
    scripted sequence of responses/exceptions."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class NvidiaEnvConfigTests(unittest.TestCase):
    """§1: audit -- NVIDIA_API_KEY/NVIDIA_BASE_URL exist and are reused; the OCR
    model/endpoint are separate config, never the translation LLM."""

    def test_nvidia_api_key_and_base_url_config_names_exist(self):
        self.assertTrue(hasattr(config, "NVIDIA_API_KEY"))
        self.assertTrue(hasattr(config, "NVIDIA_BASE_URL"))

    def test_nvidia_ocr_has_its_own_model_config_not_the_llm(self):
        self.assertTrue(hasattr(config, "NVIDIA_OCR_MODEL"))
        self.assertNotEqual(config.NVIDIA_OCR_MODEL, config.NVIDIA_TRANSLATION_MODEL)


class DefaultUnchangedTests(unittest.TestCase):
    """TEST_RAPIDOCR_DEFAULT_UNCHANGED"""

    def test_default_execution_mode_is_rapidocr(self):
        self.assertEqual(config.OCR_EXECUTION_MODE, "rapidocr")

    def test_default_nvidia_ocr_is_disabled(self):
        self.assertFalse(config.NVIDIA_OCR_ENABLED)

    def test_rapidocr_only_mode_never_touches_nvidia(self):
        import ocr_hybrid_scheduler as sched

        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch.object(sched.OCREngine, "detect_lines", return_value=[]) as detect:
            results, telemetry = sched.run_ocr(
                [{"index": 0, "image_path": "a.png"}], "en", mode="rapidocr"
            )
        detect.assert_called_once()
        self.assertEqual(telemetry["OCR_EXECUTION_MODE"], "rapidocr")
        self.assertEqual(telemetry["NVIDIA_OCR_CALLS"], 0)
        self.assertEqual(results[0].engine, "rapidocr")


class NvidiaProviderConfigTests(unittest.TestCase):
    """TEST_NVIDIA_PROVIDER_CONFIG / TEST_NO_NEMOTRON_LLM_RUNTIME"""

    def test_provider_refuses_to_construct_over_the_translation_model(self):
        from nvidia_ocr_provider import NvidiaOCRConfigError, NvidiaOCRProvider

        with mock.patch.object(config, "NVIDIA_OCR_MODEL", config.NVIDIA_TRANSLATION_MODEL):
            with self.assertRaises(NvidiaOCRConfigError):
                NvidiaOCRProvider()

    def test_provider_is_not_configured_without_api_key(self):
        from nvidia_ocr_provider import NvidiaOCRProvider

        with mock.patch.object(config, "NVIDIA_API_KEY", ""):
            provider = NvidiaOCRProvider()
        self.assertFalse(provider.is_configured())

    def test_no_nemotron_llm_symbol_is_imported_by_the_ocr_provider(self):
        import nvidia_ocr_provider

        source = io.open(nvidia_ocr_provider.__file__, encoding="utf-8").read()
        self.assertNotIn("chat.completions", source)
        self.assertNotIn("NvidiaCredentialPool", source)


class NvidiaSecretHandlingTests(unittest.TestCase):
    """TEST_NVIDIA_ENV_SECRET_NOT_LOGGED / TEST_NO_SECRET_IN_DIAGNOSTICS"""

    def _provider(self, session):
        from nvidia_ocr_provider import NvidiaOCRProvider

        with mock.patch.object(config, "NVIDIA_API_KEY", "super-secret-key"), \
             mock.patch.object(config, "NVIDIA_OCR_MODEL", "nvidia/nemotron-ocr-v2"):
            return NvidiaOCRProvider(session=session)

    def test_secret_never_appears_in_a_raised_exception_message(self):
        session = _FakeSession([_FakeResponse(status_code=401)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception) as ctx:
                provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertNotIn("super-secret-key", str(ctx.exception))

    def test_secret_is_only_ever_placed_in_the_authorization_header(self):
        session = _FakeSession([_FakeResponse(status_code=200, payload={"data": [{"index": 0, "text_detections": []}]})])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            provider.recognize_page(_fake_image(), context={"page_id": 0})
        sent = session.requests[0]
        self.assertEqual(sent["headers"]["Authorization"], "Bearer super-secret-key")
        self.assertNotIn("super-secret-key", str(sent["json"]))


class NvidiaResponseNormalizationTests(unittest.TestCase):
    """TEST_NVIDIA_RESPONSE_NORMALIZATION / TEST_NVIDIA_BBOX_NORMALIZATION"""

    def _provider(self, session):
        from nvidia_ocr_provider import NvidiaOCRProvider

        with mock.patch.object(config, "NVIDIA_API_KEY", "k"), \
             mock.patch.object(config, "NVIDIA_OCR_MODEL", "nvidia/nemotron-ocr-v2"):
            return NvidiaOCRProvider(session=session)

    def test_pixel_bbox_regions_normalize_into_ocr_page_result(self):
        payload = _ocr_v2_payload([_detection("Hello", [(0.05, 0.2), (0.55, 0.2), (0.55, 0.8), (0.05, 0.8)])])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=20, height=10), context={"page_id": 3})
        self.assertIsInstance(page, OCRPageResult)
        self.assertEqual(page.engine, "nvidia")
        self.assertEqual(page.region_count, 1)
        region = page.regions[0]
        self.assertIsInstance(region, OCRRegion)
        self.assertEqual(region.text, "Hello")
        self.assertEqual(region.bbox, (1, 2, 11, 8))

    def test_normalized_0_to_1_bbox_is_denormalized_to_pixels(self):
        payload = _ocr_v2_payload([_detection("Hi", [(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)], confidence=0.5)])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=20, height=10), context={"page_id": 0})
        self.assertEqual(page.regions[0].bbox, (0, 0, 10, 5))

    def test_page_result_converts_back_to_ocr_lines_for_downstream(self):
        page = OCRPageResult.from_ocr_lines(
            [OCRLine(text="x", confidence=0.9, polygon=None, box=(0, 0, 1, 1), raw_text="x", engine="rapidocr")],
            page_id=1, engine="rapidocr", width=5, height=5,
        )
        lines = page.to_ocr_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].engine, "rapidocr")


class NvidiaMalformedResponseTests(unittest.TestCase):
    """TEST_NVIDIA_MALFORMED_RESPONSE"""

    def _provider(self, session):
        from nvidia_ocr_provider import NvidiaOCRProvider

        with mock.patch.object(config, "NVIDIA_API_KEY", "k"), \
             mock.patch.object(config, "NVIDIA_OCR_MODEL", "nvidia/nemotron-ocr-v2"):
            return NvidiaOCRProvider(session=session)

    def test_missing_data_key_fails_closed(self):
        session = _FakeSession([_FakeResponse(status_code=200, payload={"oops": True})])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception):
                provider.recognize_page(_fake_image(), context={"page_id": 0})

    def test_bad_bbox_shape_fails_closed(self):
        payload = _ocr_v2_payload([{"text_prediction": {"text": "x", "confidence": 0.5}, "bounding_box": {"points": [{"x": 1, "y": 2}]}}])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception):
                provider.recognize_page(_fake_image(), context={"page_id": 0})

    def test_non_json_body_fails_closed(self):
        session = _FakeSession([_FakeResponse(status_code=200, raise_not_json=True)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception):
                provider.recognize_page(_fake_image(), context={"page_id": 0})

    def test_no_result_is_ever_silently_empty_on_malformed_body(self):
        # A malformed body must raise, never return a page with zero regions
        # that downstream code could mistake for "no text on this page".
        payload = {"data": "not-a-list"}
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception):
                provider.recognize_page(_fake_image(), context={"page_id": 0})


class NvidiaTimeoutAndHttpErrorTests(unittest.TestCase):
    """TEST_NVIDIA_TIMEOUT / TEST_NVIDIA_HTTP_ERROR"""

    def _provider(self, session):
        from nvidia_ocr_provider import NvidiaOCRProvider

        with mock.patch.object(config, "NVIDIA_API_KEY", "k"), \
             mock.patch.object(config, "NVIDIA_OCR_MODEL", "nvidia/nemotron-ocr-v2"), \
             mock.patch.object(config, "NVIDIA_OCR_MAX_RETRIES", 1), \
             mock.patch.object(config, "NVIDIA_OCR_RETRY_BACKOFF_SECONDS", 0.0):
            return NvidiaOCRProvider(session=session)

    def test_timeout_raises_and_is_marked_non_retryable(self):
        import requests
        from nvidia_ocr_provider import NvidiaOCRProviderError

        session = _FakeSession([requests.exceptions.Timeout("timed out")])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(NvidiaOCRProviderError) as ctx:
                provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(session.requests), 1)  # no retry on ambiguous timeout

    def test_explicit_timeout_is_configured_on_every_request(self):
        session = _FakeSession([_FakeResponse(status_code=200, payload={"data": [{"index": 0, "text_detections": []}]})])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertEqual(
            session.requests[0]["timeout"],
            (provider.connect_timeout, provider.read_timeout),
        )

    def test_5xx_is_retried_a_bounded_number_of_times_then_raises(self):
        from nvidia_ocr_provider import NvidiaOCRProviderError

        session = _FakeSession([_FakeResponse(status_code=503), _FakeResponse(status_code=503)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(NvidiaOCRProviderError) as ctx:
                provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(session.requests), 2)  # 1 initial + 1 retry (max_retries=1)

    def test_4xx_fails_immediately_without_retry(self):
        session = _FakeSession([_FakeResponse(status_code=404)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception):
                provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertEqual(len(session.requests), 1)


class HybridDynamicQueueTests(unittest.TestCase):
    """TEST_HYBRID_DYNAMIC_QUEUE / TEST_HYBRID_EACH_PAGE_PROCESSED_ONCE /
    TEST_HYBRID_PRESERVES_PAGE_ORDER"""

    def _fake_provider(self, pages_processed):
        class _Provider:
            def is_configured(self_inner):
                return True

            def recognize_page(self_inner, image, context=None):
                page_id = context["page_id"]
                pages_processed.append(page_id)
                return OCRPageResult(page_id=page_id, engine="nvidia", width=1, height=1, regions=[])

        return _Provider()

    def test_each_page_is_processed_exactly_once_across_both_engines(self):
        import ocr_hybrid_scheduler as sched

        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(6)]
        pages_processed = []
        provider = self._fake_provider(pages_processed)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch.object(sched.OCREngine, "detect_lines", return_value=[]):
            results, telemetry = sched.run_hybrid(
                jobs, "en", provider, rapidocr_workers=2, nvidia_workers=2
            )
        self.assertEqual(sorted(results.keys()), list(range(6)))
        self.assertEqual(telemetry["RAPIDOCR_PAGES"] + telemetry["NVIDIA_OCR_PAGES"], 6)

    def test_page_order_is_deterministic_regardless_of_completion_order(self):
        import ocr_hybrid_scheduler as sched

        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(10)]
        provider = self._fake_provider([])
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch.object(sched.OCREngine, "detect_lines", return_value=[]):
            results, _ = sched.run_hybrid(jobs, "en", provider, rapidocr_workers=3, nvidia_workers=3)
        ordered = [results[i] for i in sorted(results)]
        self.assertEqual([page.page_id for page in ordered], list(range(10)))

    def test_dynamic_queue_lets_the_faster_engine_take_more_pages(self):
        import time as time_module

        import ocr_hybrid_scheduler as sched

        # RapidOCR is made artificially slow; NVIDIA is fast -- with a dynamic
        # queue NVIDIA should end up handling more than half the pages
        # (an odd/even split would give it exactly half regardless of speed).
        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(8)]

        def slow_detect(self_engine, image, page=None):
            time_module.sleep(0.02)
            return []

        pages_processed = []
        provider = self._fake_provider(pages_processed)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch.object(sched.OCREngine, "detect_lines", slow_detect):
            results, telemetry = sched.run_hybrid(
                jobs, "en", provider, rapidocr_workers=1, nvidia_workers=1
            )
        self.assertGreater(telemetry["NVIDIA_OCR_PAGES"], telemetry["RAPIDOCR_PAGES"])

    def test_rapid_and_nvidia_overlap_in_wall_clock_time(self):
        """TEST_HYBRID_RAPID_AND_NVIDIA_OVERLAP_IN_TIME"""
        import time as time_module

        import ocr_hybrid_scheduler as sched

        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(4)]

        def slow_detect(self_engine, image, page=None):
            time_module.sleep(0.05)
            return []

        class _SlowProvider:
            def is_configured(self_inner):
                return True

            def recognize_page(self_inner, image, context=None):
                time_module.sleep(0.05)
                return OCRPageResult(page_id=context["page_id"], engine="nvidia", width=1, height=1, regions=[])

        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch.object(sched.OCREngine, "detect_lines", slow_detect):
            _, telemetry = sched.run_hybrid(jobs, "en", _SlowProvider(), rapidocr_workers=1, nvidia_workers=1)
        # Sequential sum of the same work would be >= 4 * 0.05s = 200ms; a
        # scheduler that truly overlaps the two engines finishes well under that.
        self.assertLess(telemetry["OCR_WALL_CLOCK_MS"], telemetry["OCR_SEQUENTIAL_SUM_MS"])
        self.assertGreater(telemetry["OCR_PARALLEL_SPEEDUP"], 1.0)


class HybridFailureHandlingTests(unittest.TestCase):
    """TEST_NVIDIA_FAILURE_DOES_NOT_DEADLOCK_QUEUE"""

    def test_nvidia_failure_requeues_pages_to_rapidocr_instead_of_deadlocking(self):
        import ocr_hybrid_scheduler as sched
        from nvidia_ocr_provider import NvidiaOCRProviderError

        class _FailingProvider:
            def is_configured(self_inner):
                return True

            def recognize_page(self_inner, image, context=None):
                raise NvidiaOCRProviderError("nvidia_ocr_connection_error", retryable=True)

        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(5)]
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch.object(sched.OCREngine, "detect_lines", return_value=[]):
            results, telemetry = sched.run_hybrid(
                jobs, "en", _FailingProvider(), rapidocr_workers=1, nvidia_workers=1
            )
        # every page still gets a result -- no deadlock -- and none of them
        # ended up attributed to the failing NVIDIA engine.
        self.assertEqual(sorted(results.keys()), list(range(5)))
        self.assertTrue(all(page.engine == "rapidocr" for page in results.values()))
        self.assertEqual(telemetry["NVIDIA_OCR_PAGES"], 0)


class NvidiaExecutionModeTests(unittest.TestCase):
    """TEST_NVIDIA_PROVIDER_CONFIG (mode=nvidia fails closed when unconfigured)"""

    def test_nvidia_mode_fails_closed_when_unconfigured(self):
        import ocr_hybrid_scheduler as sched
        from nvidia_ocr_provider import NvidiaOCRProvider

        with mock.patch.object(config, "NVIDIA_API_KEY", ""):
            provider = NvidiaOCRProvider()
        with self.assertRaises(sched.OcrExecutionModeError):
            sched.run_nvidia_only([{"index": 0, "image_path": "a.png"}], provider)


class TranslationUntouchedTests(unittest.TestCase):
    """TEST_TRANSLATION_STILL_YOMU_BACKEND / TEST_DEEPL_STILL_QUALITY_OPTIMIZED"""

    def test_deepl_default_provider_and_model_are_unchanged(self):
        import ui_helpers

        self.assertEqual(ui_helpers.DEFAULT_TRANSLATION_PROVIDER, "deepl")
        self.assertEqual(config.DEEPL_MODEL_TYPE, "quality_optimized")

    def test_ocr_config_module_never_mentions_translation_provider_selection(self):
        source = io.open("ocr_hybrid_scheduler.py", encoding="utf-8").read()
        self.assertNotIn("yomu_backend", source)
        self.assertNotIn("DeepL", source)


class NvidiaOcrHostedContractTests(unittest.TestCase):
    """Mission: corrigir SOMENTE configuracao/contrato do endpoint NVIDIA OCR.

    Covers TEST_HOSTED_OCR_DEFAULT_URL, TEST_HOSTED_OCR_USES_FULL_INVOKE_URL,
    TEST_HOSTED_OCR_DOES_NOT_APPEND_V1_INFER, TEST_HOSTED_OCR_DOES_NOT_USE_INTEGRATE_API,
    TEST_SELF_HOSTED_OCR_USES_V1_OCR, TEST_HOSTED_AUTH_BEARER, TEST_AUTH_SECRET_NOT_LOGGED,
    TEST_OCR_V2_RESPONSE_NORMALIZATION, TEST_OCR_V2_BBOX_NORMALIZATION,
    TEST_OCR_V2_CONFIDENCE_NORMALIZATION, TEST_INVALID_HOST_FAILS_CLOSED,
    TEST_NVIDIA_OCR_HOSTED_URL_NOT_DOUBLE_APPENDED, TEST_NVIDIA_OCR_HOSTED_MODEL_IS_NEMOTRON_OCR_V2,
    TEST_NVIDIA_OCR_NEVER_USES_CHAT_COMPLETIONS.
    """

    def _provider(self, session, **overrides):
        from nvidia_ocr_provider import NvidiaOCRProvider

        patches = {
            "NVIDIA_API_KEY": "k",
            "NVIDIA_OCR_MODEL": "nvidia/nemotron-ocr-v2",
            "NVIDIA_OCR_DEPLOYMENT": "hosted",
            "NVIDIA_OCR_INVOKE_URL": "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2",
        }
        patches.update(overrides)
        stack = [mock.patch.object(config, key, value) for key, value in patches.items()]
        for patch in stack:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in stack])
        return NvidiaOCRProvider(session=session)

    def test_hosted_ocr_default_url(self):
        # TEST_HOSTED_OCR_DEFAULT_URL
        self.assertEqual(
            config.NVIDIA_OCR_INVOKE_URL,
            "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2",
        )

    def test_hosted_ocr_uses_full_invoke_url_verbatim(self):
        # TEST_HOSTED_OCR_USES_FULL_INVOKE_URL / TEST_NVIDIA_OCR_HOSTED_URL_NOT_DOUBLE_APPENDED
        session = _FakeSession([_FakeResponse(status_code=200, payload={"data": [{"index": 0, "text_detections": []}]})])
        provider = self._provider(session)
        self.assertEqual(provider.endpoint, "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2")
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertEqual(session.requests[0]["url"], provider.endpoint)

    def test_hosted_ocr_does_not_append_v1_infer(self):
        # TEST_HOSTED_OCR_DOES_NOT_APPEND_V1_INFER
        session = _FakeSession([])
        provider = self._provider(session)
        self.assertNotIn("/ocr/v1/infer", provider.endpoint)
        self.assertFalse(provider.endpoint.endswith("/v1/infer"))

    def test_hosted_ocr_does_not_use_integrate_api_host(self):
        # TEST_HOSTED_OCR_DOES_NOT_USE_INTEGRATE_API
        session = _FakeSession([])
        provider = self._provider(session)
        self.assertNotIn("integrate.api.nvidia.com", provider.endpoint)

    def test_invalid_hosted_host_fails_closed(self):
        # TEST_INVALID_HOST_FAILS_CLOSED / §7 invalid_nvidia_ocr_endpoint
        from nvidia_ocr_provider import NvidiaOCRConfigError

        session = _FakeSession([])
        with self.assertRaises(NvidiaOCRConfigError) as ctx:
            self._provider(session, NVIDIA_OCR_INVOKE_URL="https://integrate.api.nvidia.com/v1/ocr/v1/infer")
        self.assertIn("invalid_nvidia_ocr_endpoint", str(ctx.exception))

    def test_self_hosted_ocr_uses_v1_ocr_on_configured_base(self):
        # TEST_SELF_HOSTED_OCR_USES_V1_OCR
        session = _FakeSession([])
        provider = self._provider(
            session,
            NVIDIA_OCR_DEPLOYMENT="self_hosted",
            NVIDIA_OCR_ENDPOINT="",
            NVIDIA_OCR_SELF_HOSTED_BASE_URL="http://localhost:8000",
        )
        self.assertEqual(provider.endpoint, "http://localhost:8000/v1/ocr")

    def test_hosted_model_must_be_nemotron_ocr_v2(self):
        # TEST_NVIDIA_OCR_HOSTED_MODEL_IS_NEMOTRON_OCR_V2
        from nvidia_ocr_provider import NvidiaOCRConfigError

        session = _FakeSession([])
        with self.assertRaises(NvidiaOCRConfigError):
            self._provider(session, NVIDIA_OCR_MODEL="nvidia/nemoretriever-ocr-v1")

    def test_hosted_never_resolves_to_chat_completions_or_translation_llm(self):
        # TEST_NVIDIA_OCR_NEVER_USES_CHAT_COMPLETIONS
        from nvidia_ocr_provider import NvidiaOCRConfigError

        session = _FakeSession([])
        with self.assertRaises(NvidiaOCRConfigError):
            self._provider(session, NVIDIA_OCR_MODEL="nvidia/nemotron-3-super-120b-a12b")
        with self.assertRaises(NvidiaOCRConfigError):
            self._provider(session, NVIDIA_OCR_MODEL="nvidia/riva-translate-4b-instruct-v2")

    def test_hosted_auth_uses_bearer_header(self):
        # TEST_HOSTED_AUTH_BEARER
        session = _FakeSession([_FakeResponse(status_code=200, payload={"data": [{"index": 0, "text_detections": []}]})])
        provider = self._provider(session, NVIDIA_API_KEY="hosted-secret")
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertEqual(session.requests[0]["headers"]["Authorization"], "Bearer hosted-secret")

    def test_auth_secret_never_logged_or_in_exception(self):
        # TEST_AUTH_SECRET_NOT_LOGGED
        session = _FakeSession([_FakeResponse(status_code=401)])
        provider = self._provider(session, NVIDIA_API_KEY="hosted-secret")
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception) as ctx:
                provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertNotIn("hosted-secret", str(ctx.exception))

    def test_ocr_v2_response_normalization(self):
        # TEST_OCR_V2_RESPONSE_NORMALIZATION
        payload = _ocr_v2_payload([_detection("Ola", [(0.1, 0.4), (0.6, 0.4), (0.6, 0.9), (0.1, 0.9)], confidence=0.87)])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=20, height=10), context={"page_id": 1})
        self.assertEqual(page.engine, "nvidia")
        self.assertEqual(page.regions[0].text, "Ola")

    def test_ocr_v2_bbox_normalization(self):
        # TEST_OCR_V2_BBOX_NORMALIZATION
        payload = _ocr_v2_payload([_detection("Oi", [(0.1, 0.2), (0.6, 0.2), (0.6, 0.8), (0.1, 0.8)], confidence=0.5)])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=100, height=100), context={"page_id": 0})
        self.assertEqual(page.regions[0].bbox, (10, 20, 60, 80))

    def test_ocr_v2_confidence_normalization(self):
        # TEST_OCR_V2_CONFIDENCE_NORMALIZATION
        payload = _ocr_v2_payload([_detection("x", [(0, 0), (1, 0), (1, 1), (0, 1)], confidence=0.42)])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=1, height=1), context={"page_id": 0})
        self.assertEqual(page.regions[0].confidence, 0.42)


class NvidiaOfficialContractTests(unittest.TestCase):
    """Mission: corrigir SOMENTE request/response contract do NvidiaOCRProvider
    para o Nemotron OCR v2 oficial. Exact test names required by the brief."""

    def _provider(self, session, **overrides):
        from nvidia_ocr_provider import NvidiaOCRProvider

        patches = {
            "NVIDIA_API_KEY": "k",
            "NVIDIA_OCR_MODEL": "nvidia/nemotron-ocr-v2",
            "NVIDIA_OCR_DEPLOYMENT": "hosted",
            "NVIDIA_OCR_INVOKE_URL": "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2",
            "NVIDIA_OCR_MERGE_LEVEL": "sentence",
        }
        patches.update(overrides)
        stack = [mock.patch.object(config, key, value) for key, value in patches.items()]
        for patch in stack:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in stack])
        return NvidiaOCRProvider(session=session)

    def _send(self, provider):
        session = provider._session
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            provider.recognize_page(_fake_image(), context={"page_id": 0})
        return session.requests[0]["json"]

    def test_nvidia_request_has_input_array(self):
        # TEST_NVIDIA_REQUEST_HAS_INPUT_ARRAY
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session)
        sent = self._send(provider)
        self.assertIsInstance(sent["input"], list)
        self.assertEqual(len(sent["input"]), 1)

    def test_nvidia_request_type_image_url(self):
        # TEST_NVIDIA_REQUEST_TYPE_IMAGE_URL
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session)
        sent = self._send(provider)
        self.assertEqual(sent["input"][0]["type"], "image_url")

    def test_nvidia_request_uses_data_url(self):
        # TEST_NVIDIA_REQUEST_USES_DATA_URL
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session)
        sent = self._send(provider)
        url = sent["input"][0]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        b64_part = url.split(",", 1)[1]
        self.assertNotIn("\n", b64_part)
        raw = base64.b64decode(b64_part)
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")  # real PNG bytes, matches the declared mime

    def test_nvidia_request_no_model_root_field(self):
        # TEST_NVIDIA_REQUEST_NO_MODEL_ROOT_FIELD
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session)
        sent = self._send(provider)
        self.assertNotIn("model", sent)

    def test_nvidia_request_no_image_root_field(self):
        # TEST_NVIDIA_REQUEST_NO_IMAGE_ROOT_FIELD
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session)
        sent = self._send(provider)
        self.assertNotIn("image", sent)

    def test_nvidia_request_no_image_format_root_field(self):
        # TEST_NVIDIA_REQUEST_NO_IMAGE_FORMAT_ROOT_FIELD
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session)
        sent = self._send(provider)
        self.assertNotIn("image_format", sent)

    def test_nvidia_merge_level_word(self):
        # TEST_NVIDIA_MERGE_LEVEL_WORD
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session, NVIDIA_OCR_MERGE_LEVEL="word")
        sent = self._send(provider)
        self.assertEqual(sent["merge_levels"], ["word"])

    def test_nvidia_merge_level_sentence(self):
        # TEST_NVIDIA_MERGE_LEVEL_SENTENCE
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session, NVIDIA_OCR_MERGE_LEVEL="sentence")
        sent = self._send(provider)
        self.assertEqual(sent["merge_levels"], ["sentence"])

    def test_nvidia_merge_level_paragraph(self):
        # TEST_NVIDIA_MERGE_LEVEL_PARAGRAPH
        session = _FakeSession([_FakeResponse(status_code=200, payload=_ocr_v2_payload([]))])
        provider = self._provider(session, NVIDIA_OCR_MERGE_LEVEL="paragraph")
        sent = self._send(provider)
        self.assertEqual(sent["merge_levels"], ["paragraph"])

    def test_nvidia_invalid_merge_level_fails(self):
        # TEST_NVIDIA_INVALID_MERGE_LEVEL_FAILS
        from nvidia_ocr_provider import NvidiaOCRConfigError

        session = _FakeSession([])
        with self.assertRaises(NvidiaOCRConfigError):
            self._provider(session, NVIDIA_OCR_MERGE_LEVEL="paragraphs")  # not a valid level

    def test_nvidia_response_data_parse(self):
        # TEST_NVIDIA_RESPONSE_DATA_PARSE
        payload = _ocr_v2_payload([_detection("Hello", [(0, 0), (1, 0), (1, 1), (0, 1)])])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=10, height=10), context={"page_id": 0})
        self.assertEqual(page.region_count, 1)
        self.assertEqual(page.regions[0].engine, "nvidia")

    def test_nvidia_text_prediction_parse(self):
        # TEST_NVIDIA_TEXT_PREDICTION_PARSE
        payload = _ocr_v2_payload([_detection("Bonjour", [(0, 0), (1, 0), (1, 1), (0, 1)])])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=10, height=10), context={"page_id": 0})
        self.assertEqual(page.regions[0].text, "Bonjour")

    def test_nvidia_confidence_parse(self):
        # TEST_NVIDIA_CONFIDENCE_PARSE
        payload = _ocr_v2_payload([_detection("x", [(0, 0), (1, 0), (1, 1), (0, 1)], confidence=0.77)])
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=10, height=10), context={"page_id": 0})
        self.assertEqual(page.regions[0].confidence, 0.77)

    def test_nvidia_normalized_points_to_pixels(self):
        # TEST_NVIDIA_NORMALIZED_POINTS_TO_PIXELS
        payload = _ocr_v2_payload(
            [_detection("x", [(0.25, 0.1), (0.75, 0.1), (0.75, 0.9), (0.25, 0.9)])]
        )
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=200, height=100), context={"page_id": 0})
        self.assertEqual(page.regions[0].bbox, (50, 10, 150, 90))

    def test_nvidia_bbox_clamp(self):
        # TEST_NVIDIA_BBOX_CLAMP -- out-of-range normalized coords clamp to the page bounds
        payload = _ocr_v2_payload(
            [_detection("x", [(-0.2, -0.5), (1.3, -0.5), (1.3, 1.4), (-0.2, 1.4)])]
        )
        session = _FakeSession([_FakeResponse(status_code=200, payload=payload)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            page = provider.recognize_page(_fake_image(width=50, height=20), context={"page_id": 0})
        x0, y0, x1, y1 = page.regions[0].bbox
        self.assertGreaterEqual(x0, 0)
        self.assertGreaterEqual(y0, 0)
        self.assertLessEqual(x1, 50)
        self.assertLessEqual(y1, 20)
        self.assertEqual((x0, y0, x1, y1), (0, 0, 50, 20))

    def test_nvidia_422_sanitized(self):
        # TEST_NVIDIA_422_SANITIZED -- only status/reason code surface, never the body
        from nvidia_ocr_provider import NvidiaOCRProviderError

        session = _FakeSession([_FakeResponse(status_code=422, payload={"detail": "invalid data URL: <secret leak>"})])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(NvidiaOCRProviderError) as ctx:
                provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertFalse(ctx.exception.retryable)
        self.assertNotIn("secret leak", str(ctx.exception))
        self.assertNotIn("detail", str(ctx.exception))

    def test_nvidia_base64_not_logged(self):
        # TEST_NVIDIA_BASE64_NOT_LOGGED
        session = _FakeSession([_FakeResponse(status_code=401)])
        provider = self._provider(session)
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception) as ctx:
                provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertNotIn("base64", str(ctx.exception))
        self.assertNotIn("data:image", str(ctx.exception))

    def test_nvidia_secret_not_logged(self):
        # TEST_NVIDIA_SECRET_NOT_LOGGED
        session = _FakeSession([_FakeResponse(status_code=422)])
        provider = self._provider(session, NVIDIA_API_KEY="totally-secret-key")
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread):
            with self.assertRaises(Exception) as ctx:
                provider.recognize_page(_fake_image(), context={"page_id": 0})
        self.assertNotIn("totally-secret-key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
