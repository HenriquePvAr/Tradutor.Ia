"""Hermetic fixtures for the controlled generic chapter-reader fallback."""

import _test_bootstrap  # noqa: F401

import base64
import io
import json
import unittest
from unittest import mock

from PIL import Image

import chapter_source
from chapter_source import (
    AUTHENTICATION_REQUIRED,
    CHALLENGE_REQUIRED,
    INCOMPLETE_DOWNLOAD,
    NO_CHAPTER_IMAGES,
    REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
    SUPPORTED_GENERIC_HIGH_CONFIDENCE,
    SUPPORTED_SPECIFIC_ADAPTER,
    UNSUPPORTED_CANVAS_READER,
    UNSUPPORTED_CROSS_ORIGIN_READER,
    UNSUPPORTED_LOW_CONFIDENCE,
    UniversalChapterAdapter,
    WEBTOONS,
)
from universal_chapter_adapter import (
    HIGH_CONFIDENCE,
    MAX_AUTOMATIC_PAGES,
    MAX_CANDIDATES,
    MAX_NETWORK_CANDIDATES,
    MEDIUM_CONFIDENCE,
    _COLLECTION_SCRIPT,
    analyse_candidates,
    analyse_driver,
    attach_review_thumbnails,
    collect_from_driver,
    extract_manifest_urls_from_text,
)

PAGE = "https://reader.example.test/chapter/1"


def public_dns(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def page(index, **overrides):
    candidate = {
        "url": f"https://cdn.example.test/chapter/page-{index:03}.webp",
        "source": "currentSrc",
        "order": index,
        "y": index * 1400,
        "width": 900,
        "height": 1400,
        "naturalWidth": 900,
        "naturalHeight": 1400,
        "container": "chapter-reader pages",
        "context": "reader chapter pages",
    }
    candidate.update(overrides)
    return candidate


def canvas_page(index):
    buffer = io.BytesIO()
    Image.new("RGB", (900, 1400), (index * 30, 40, 90)).save(buffer, "PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return {
        "url": data_uri, "source": "canvas_capture", "order": index, "y": index * 1400,
        "width": 900, "height": 1400, "naturalWidth": 900, "naturalHeight": 1400,
        "container": "chapter-reader pages", "context": "reader chapter pages",
    }


class UniversalAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.dns = mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns)
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def analyse(self, candidates, **kwargs):
        return analyse_candidates(PAGE, candidates, adapter=UniversalChapterAdapter(PAGE), **kwargs)

    def test_vertical_reader_is_high_confidence_and_authorizes_only_selected_cdn(self):
        result = self.analyse([page(1), page(2), page(3), page(4), page(
            99, url="https://ads.example.test/banner.png", className="advert banner",
            width=728, height=90, naturalWidth=728, naturalHeight=90)])
        self.assertEqual(result.outcome, SUPPORTED_GENERIC_HIGH_CONFIDENCE)
        self.assertGreaterEqual(result.confidence, HIGH_CONFIDENCE)
        self.assertEqual([candidate.order for candidate in result.accepted], [1, 2, 3, 4])
        self.assertTrue(any(item["reason"] in {"advert", "ads", "banner"}
                            for item in result.discarded))
        public = result.public()
        self.assertNotIn("?", str(public))
        self.assertNotIn("page-001.webp", str(public))

    def test_two_ambiguous_pages_need_confirmation(self):
        result = self.analyse([
            page(1, container="", context="", width=800, height=1200),
            page(2, container="", context="", width=800, height=1200),
        ])
        self.assertEqual(result.outcome, REVIEW_REQUIRED_MEDIUM_CONFIDENCE)
        self.assertGreaterEqual(result.confidence, MEDIUM_CONFIDENCE)
        self.assertLess(result.confidence, HIGH_CONFIDENCE)
        self.assertTrue(result.requires_review)
        self.assertFalse(result.can_download)

    def test_review_thumbnails_are_bounded_data_uris_and_never_source_urls(self):
        result = self.analyse([
            page(1, container="", context="", width=800, height=1200),
            page(2, container="", context="", width=800, height=1200),
        ])
        payload = "data:image/jpeg;base64," + ("QUJD" * 12)

        class Driver:
            def execute_script(self, _script, requested):
                self.requested = requested
                return [
                    {"id": requested[0]["id"], "thumbnail": payload},
                    {"id": requested[1]["id"], "thumbnail": "https://unsafe.example.test/thumb"},
                ]

        driver = Driver()
        attached = attach_review_thumbnails(driver, result)
        public = attached.public()
        self.assertEqual(len(driver.requested), 2)
        self.assertEqual(public["accepted"][0]["thumbnail"], payload)
        self.assertNotIn("thumbnail", public["accepted"][1])
        self.assertNotIn("page-001.webp", str(public))

    def test_scattered_small_images_are_low_confidence(self):
        result = self.analyse([
            page(1, url="https://reader.example.test/a.png", width=200, height=260,
                 naturalWidth=200, naturalHeight=260, container="", context=""),
            page(2, url="https://another.example.test/b.png", width=200, height=260,
                 naturalWidth=200, naturalHeight=260, container="", context=""),
        ])
        self.assertEqual(result.outcome, UNSUPPORTED_LOW_CONFIDENCE)
        self.assertFalse(result.accepted)

    def test_advertisements_thumbnails_and_duplicates_do_not_enter_selection(self):
        result = self.analyse([
            page(1), page(1),
            page(2, url="https://cdn.example.test/thumb-2.webp", className="thumbnail"),
            page(3, url="https://cdn.example.test/page-003.webp"),
        ])
        urls = [candidate.url for candidate in result.accepted]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertFalse(any("thumb" in url for url in urls))
        self.assertTrue(any(item["reason"] in {"duplicate_resource", "thumbnail"}
                            for item in result.discarded))

    def test_rotating_signed_variants_are_one_selectable_resource_per_analysis(self):
        result = self.analyse([
            page(1, url="https://cdn.example.test/chapter/page-001.webp?sig=old"),
            page(1, url="https://cdn.example.test/chapter/page-001.webp?sig=new"),
            page(2), page(3),
        ])
        self.assertEqual(len(result.accepted), 3)
        self.assertEqual(len({candidate.id for candidate in result.accepted}), 3)
        self.assertTrue(any(item["reason"] == "duplicate_resource" for item in result.discarded))

    def test_query_addressed_pages_are_not_collapsed_as_rotating_signed_variants(self):
        result = self.analyse([
            page(index, url=f"https://cdn.example.test/render?page={index}&token=private")
            for index in (1, 2, 3)
        ])
        self.assertEqual(result.outcome, SUPPORTED_GENERIC_HIGH_CONFIDENCE)
        self.assertEqual([candidate.order for candidate in result.accepted], [1, 2, 3])
        self.assertNotIn("token=private", str(result.public()))

    def test_advertisements_in_the_same_container_reduce_that_cluster_score(self):
        result = self.analyse([
            page(1), page(2), page(3),
            *[
                page(20 + index, url=f"https://cdn.example.test/ad-{index}.webp",
                     className="advert banner", container="chapter-reader pages")
                for index in range(4)
            ],
        ])
        self.assertEqual(result.outcome, REVIEW_REQUIRED_MEDIUM_CONFIDENCE)
        self.assertTrue(result.requires_review)
        self.assertIn("interface_penalty", result.clusters[0].signals)

    def test_truncated_collection_is_blocked_even_when_visible_pages_score_high(self):
        result = self.analyse(
            [page(1), page(2), page(3)], warnings=["candidate_limit"])
        self.assertEqual(result.outcome, INCOMPLETE_DOWNLOAD)
        self.assertFalse(result.can_download)
        self.assertIn("candidate_limit", result.warnings)

    def test_incomplete_scroll_and_page_limit_never_offer_partial_selection(self):
        scroll = self.analyse([page(1), page(2), page(3)], warnings=["scroll_incomplete"])
        pages = self.analyse([page(index) for index in range(1, MAX_AUTOMATIC_PAGES + 2)])
        self.assertEqual(scroll.outcome, INCOMPLETE_DOWNLOAD)
        self.assertFalse(scroll.can_download)
        self.assertEqual(pages.outcome, INCOMPLETE_DOWNLOAD)
        self.assertIn("page_limit_exceeded", pages.warnings)

    def test_malformed_candidate_is_discarded_without_crashing_analysis(self):
        result = self.analyse([page(1), object(), page(2), page(3)])
        self.assertEqual(result.outcome, SUPPORTED_GENERIC_HIGH_CONFIDENCE)
        self.assertTrue(any(item["reason"] == "malformed_candidate" for item in result.discarded))

    def test_page_controlled_container_text_is_opaque_in_public_diagnostics(self):
        result = self.analyse([
            page(index, container="chapter-reader token=secret-value")
            for index in (1, 2, 3)
        ])
        public = result.public()
        self.assertNotIn("secret-value", str(public))
        self.assertTrue(public["clusters"][0]["key"].startswith("cluster:"))

    def test_signed_url_rotation_keeps_the_opaque_selection_id_stable(self):
        first = self.analyse([page(1, url="https://cdn.example.test/chapter/page-001.webp?sig=old"),
                              page(2), page(3)])
        second = self.analyse([page(1, url="https://cdn.example.test/chapter/page-001.webp?sig=new"),
                               page(2), page(3)])
        self.assertEqual(first.accepted[0].id, second.accepted[0].id)

    def test_challenge_auth_zero_images_and_canvas_have_coded_results(self):
        self.assertEqual(self.analyse([], page_text="Checking your browser").outcome,
                         CHALLENGE_REQUIRED)
        self.assertEqual(self.analyse([], page_text="Please sign in to continue").outcome,
                         AUTHENTICATION_REQUIRED)
        self.assertEqual(self.analyse([]).outcome, NO_CHAPTER_IMAGES)
        self.assertEqual(self.analyse([], canvas_detected=1).outcome,
                         UNSUPPORTED_CANVAS_READER)

    def test_visible_canvas_capture_is_an_in_memory_page_candidate(self):
        result = self.analyse(
            [canvas_page(1), canvas_page(2), canvas_page(3)],
            canvas_detected=3, canvas_captured=3,
        )
        self.assertEqual(result.outcome, SUPPORTED_GENERIC_HIGH_CONFIDENCE)
        self.assertEqual([candidate.origin for candidate in result.accepted], [
            "canvas_capture", "canvas_capture", "canvas_capture",
        ])
        self.assertTrue(all(candidate.canvas_data for candidate in result.accepted))
        public = result.public()
        self.assertNotIn("data:image", str(public))
        self.assertNotIn("base64", str(public))

    def test_json_manifest_is_collected_without_executing_script_text(self):
        manifest = '{"pages":[{"image":"/p/001.webp"},{"image":"/p/002.webp"}]}'
        self.assertEqual(extract_manifest_urls_from_text(manifest, page_url=PAGE), [
            "https://reader.example.test/p/001.webp",
            "https://reader.example.test/p/002.webp",
        ])
        self.assertEqual(extract_manifest_urls_from_text("window.pages = []", page_url=PAGE), [])

    def test_specific_adapter_wins_and_reports_specific_outcome(self):
        candidates = [
            page(1, url="https://webtoons.com/p/1.webp"),
            page(2, url="https://webtoons.com/p/2.webp"),
            page(3, url="https://webtoons.com/p/3.webp"),
        ]
        result = analyse_candidates(
            "https://webtoons.com/en/x/viewer", candidates, adapter=WEBTOONS,
        )
        self.assertEqual(result.outcome, SUPPORTED_SPECIFIC_ADAPTER)

    def test_specific_adapter_needs_reader_evidence_not_just_one_large_image(self):
        result = analyse_candidates(
            "https://webtoons.com/en/x/viewer",
            [page(1, url="https://webtoons.com/p/1.webp", container="", context="")],
            adapter=WEBTOONS,
        )
        self.assertEqual(result.outcome, REVIEW_REQUIRED_MEDIUM_CONFIDENCE)
        self.assertFalse(result.can_download)

    def test_hidden_candidates_are_excluded_from_the_reader_manifest(self):
        result = self.analyse([page(1), page(2), page(3, visible=False), page(4)])
        self.assertFalse(any(candidate.order == 3 for candidate in result.accepted))
        self.assertTrue(any(item["reason"] == "hidden_candidate" for item in result.discarded))

    def test_vertical_order_signal_requires_dom_order_to_match_positions(self):
        result = self.analyse([page(1, y=3000), page(2, y=1000), page(3, y=2000)])
        self.assertNotIn("vertical_dom_order", result.clusters[0].signals)

    def test_unobserved_public_resource_is_not_authorized(self):
        adapter = UniversalChapterAdapter(PAGE)
        adapter.validate_url(PAGE)
        with self.assertRaises(chapter_source.SourceError) as ctx:
            adapter.validate_url("https://unobserved.example.test/page.png")
        self.assertEqual(ctx.exception.detail, "unrelated_resource_host")

    def test_unsafe_observed_resource_is_discarded_without_granting_authority(self):
        result = self.analyse([
            page(1), page(2), page(3),
            page(4, url="http://127.0.0.1:8080/private.png"),
        ])
        self.assertEqual(result.outcome, SUPPORTED_GENERIC_HIGH_CONFIDENCE)
        self.assertTrue(any(item["reason"] == "private_host" for item in result.discarded))
        self.assertFalse(any("127.0.0.1" in candidate.url for candidate in result.accepted))


class DriverCollectionTests(unittest.TestCase):
    def setUp(self):
        self.dns = mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns)
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def test_open_shadow_same_origin_iframe_network_and_json_are_exposed_as_metadata(self):
        class Driver:
            current_url = PAGE

            def execute_script(self, _script):
                return {
                    "candidates": [page(1, source="shadow.currentSrc"), page(2, source="iframe.currentSrc")],
                    "resources": [page(3, source="network_image", origin="network", network_order=1)],
                    "json": ['{"pages":[{"image":"https://cdn.example.test/chapter/page-004.webp"}]}'],
                    "warnings": ["cross_origin_iframe"],
                    "canvasDetected": 0,
                    "canvasCaptured": 0,
                    "pageText": "",
                }

        result = analyse_driver(Driver(), PAGE, UniversalChapterAdapter(PAGE))
        self.assertEqual(result.outcome, SUPPORTED_GENERIC_HIGH_CONFIDENCE)
        self.assertIn("cross_origin_iframe", result.warnings)
        self.assertGreaterEqual(len(result.accepted), 4)

    def test_collector_and_browser_script_have_bounded_candidate_surfaces(self):
        class Driver:
            def execute_script(self, _script):
                return {
                    "candidates": [page(index) for index in range(MAX_CANDIDATES + 100)],
                    "resources": [page(index, source="network_image") for index in range(400)],
                    "json": [], "warnings": [], "canvasDetected": 0, "canvasCaptured": 0,
                }

        collected = collect_from_driver(Driver(), PAGE)
        self.assertLessEqual(len(collected["candidates"]), MAX_CANDIDATES)
        for token in ("maxDomCandidates", "maxCanvasCaptures", "maxCanvasBytes", "maxJsonChars"):
            self.assertIn(token, _COLLECTION_SCRIPT)

    def test_network_resource_cap_becomes_an_incomplete_driver_outcome(self):
        class Driver:
            current_url = PAGE

            def execute_script(self, _script):
                return {
                    "candidates": [page(1), page(2), page(3)],
                    "resources": [
                        page(index, source="network_image", origin="network", network_order=index)
                        for index in range(4, MAX_NETWORK_CANDIDATES + 5)
                    ],
                    "json": [], "warnings": [], "canvasDetected": 0, "canvasCaptured": 0,
                    "pageText": "",
                }

        result = analyse_driver(Driver(), PAGE, UniversalChapterAdapter(PAGE))
        self.assertEqual(result.outcome, INCOMPLETE_DOWNLOAD)
        self.assertIn("network_resource_limit", result.warnings)

    def test_iframe_collection_has_cycle_and_coverage_limits(self):
        class Driver:
            current_url = PAGE

            def execute_script(self, _script):
                return {
                    "candidates": [page(1), page(2), page(3)],
                    "resources": [], "json": [], "warnings": ["iframe_limit"],
                    "canvasDetected": 0, "canvasCaptured": 0, "pageText": "",
                }

        for token in ("WeakSet", "maxIframeDocuments", "maxIframeDepth", "iframe_limit", "iframe_depth_limit"):
            self.assertIn(token, _COLLECTION_SCRIPT)
        result = analyse_driver(Driver(), PAGE, UniversalChapterAdapter(PAGE))
        self.assertEqual(result.outcome, INCOMPLETE_DOWNLOAD)
        self.assertIn("iframe_limit", result.warnings)

    def test_cross_origin_reader_evidence_is_a_terminal_honest_stop(self):
        class Driver:
            current_url = PAGE

            def execute_script(self, _script):
                return {
                    "candidates": [page(1), page(2), page(3)], "resources": [], "json": [],
                    "warnings": ["cross_origin_reader"], "canvasDetected": 0,
                    "canvasCaptured": 0, "pageText": "",
                }

        result = analyse_driver(Driver(), PAGE, UniversalChapterAdapter(PAGE))
        self.assertEqual(result.outcome, UNSUPPORTED_CROSS_ORIGIN_READER)
        self.assertFalse(result.can_download)
        self.assertFalse(result.accepted)

    def test_cdp_network_json_is_parsed_without_leaking_headers_query_or_body(self):
        response_url = "https://api.example.test/reader/pages?token=secret-token"

        class Driver:
            current_url = PAGE

            def execute_script(self, _script):
                return {
                    "candidates": [page(1), page(2), page(3)], "resources": [], "json": [],
                    "warnings": [], "canvasDetected": 0, "canvasCaptured": 0, "pageText": "",
                }

            def get_log(self, name):
                assert name == "performance"
                return [{"message": json.dumps({"message": {
                    "method": "Network.responseReceived",
                    "params": {"requestId": "request-private", "type": "Fetch", "timestamp": 12.5,
                               "response": {"url": response_url, "mimeType": "application/json",
                                            "status": 200,
                                            "headers": {"Content-Length": "123", "Set-Cookie": "private"}}},
                }})}]

            def execute_cdp_cmd(self, method, payload):
                assert method == "Network.getResponseBody"
                assert payload == {"requestId": "request-private"}
                return {"body": json.dumps({"pages": [
                    {"image": "https://cdn.example.test/chapter/page-004.webp"},
                ]})}

        result = analyse_driver(Driver(), PAGE, UniversalChapterAdapter(PAGE))
        public = result.public()
        self.assertGreaterEqual(len(result.accepted), 4)
        self.assertEqual(public["network_metadata"], [{
            "host": "api.example.test", "path_fingerprint": public["network_metadata"][0]["path_fingerprint"],
            "content_type": "application/json", "content_length": 123, "status": 200,
            "order": 0, "initiator": "Fetch", "timestamp_ms": 12000,
        }])
        serialised = json.dumps(public)
        for private in ("secret-token", "request-private", "Set-Cookie", "private"):
            self.assertNotIn(private, serialised)

    def test_contract_collectors_return_separate_bounded_evidence_surfaces(self):
        class Driver:
            current_url = PAGE

            def execute_script(self, _script):
                return {
                    "candidates": [page(1)],
                    "resources": [page(2, source="network_image", origin="network")],
                    "json": [json.dumps({"pages": [{"image": "/chapter/page-003.webp"}]})],
                    "warnings": [], "canvasDetected": 0, "canvasCaptured": 0, "pageText": "",
                }

        adapter = UniversalChapterAdapter(PAGE)
        driver = Driver()
        self.assertEqual(len(adapter.collect_dom_candidates(driver, page_url=PAGE)), 1)
        self.assertEqual(len(adapter.collect_network_candidates(driver, page_url=PAGE)), 1)
        self.assertEqual(len(adapter.collect_json_candidates(driver, page_url=PAGE)), 1)
        manifest = adapter.build_page_manifest([{"id": "opaque-1"}])
        self.assertEqual(manifest["candidate_ids"], ["opaque-1"])
        self.assertEqual(manifest["adapter_version"], "1")

    def test_adapter_hooks_share_one_observation_and_drive_the_analysis(self):
        class HookAdapter(UniversalChapterAdapter):
            def __init__(self):
                super().__init__(PAGE)
                self.calls = []

            def collect_dom_candidates(self, browser, *, page_url=""):
                self.calls.append("dom")
                return [page(1), page(2), page(3)]

            def collect_network_candidates(self, browser, *, page_url=""):
                self.calls.append("network")
                return []

            def collect_json_candidates(self, browser, *, page_url=""):
                self.calls.append("json")
                return []

            def cluster_candidates(self, candidates):
                self.calls.append("cluster")
                return candidates

            def score_cluster(self, cluster):
                self.calls.append("score")
                return 0.91

        class Driver:
            current_url = PAGE

            def __init__(self):
                self.script_calls = 0

            def execute_script(self, _script):
                self.script_calls += 1
                return {"candidates": [], "resources": [], "json": [], "warnings": [],
                        "canvasDetected": 0, "canvasCaptured": 0, "pageText": ""}

        adapter = HookAdapter()
        driver = Driver()
        result = adapter.analyze({"driver": driver, "page_url": PAGE})
        self.assertEqual(driver.script_calls, 1)
        self.assertEqual(adapter.calls, ["dom", "network", "json", "cluster", "score"])
        self.assertEqual(result.outcome, SUPPORTED_GENERIC_HIGH_CONFIDENCE)
        self.assertEqual(result.public()["adapter_version"], "1")
        self.assertEqual(result.public()["page_manifest"]["candidate_ids"],
                         [candidate.id for candidate in result.accepted])


if __name__ == "__main__":
    unittest.main()
