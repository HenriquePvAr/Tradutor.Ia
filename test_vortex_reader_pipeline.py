"""Offline contract tests for the current Vortex reader shape.

The fixture models the public reader's visible article section, page figures and lazy slots;
it never opens a browser or performs a request.
"""

import _test_bootstrap  # noqa: F401

import unittest
from unittest import mock

import chapter_source
import down
from chapter_source import select_adapter
from lazy_slot_resolver import ResolverLimits


PAGE = "https://vortexscans.org/series/demo-series/chapter-42"
RESOURCE_HOST = "storage.vortexscans.org"


def public_dns(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


class VortexReaderDriver:
    def __init__(self, total=3, resolved=1):
        self.total = total
        self.resolved = set(range(resolved))
        self.selectors = []
        self.scrolls = []

    def execute_script(self, script, *args):
        text = str(script)
        if "reader_bottom" in text:
            return {
                "found": True,
                "reader_top": 100,
                "reader_bottom": 100 + self.total * 1000,
                "viewport_height": 1000,
            }
        if "window.scrollTo" in text:
            self.scrolls.append(int(args[1]))
            self.resolved.update(range(self.total))
            return True
        if "slots: images.map" in text:
            return {"found": True, "slots": self._slots()}
        if "const els = [...root.querySelectorAll(IMAGE)]" in text:
            self.selectors.append((str(args[0]), str(args[1])))
            return {"found": True, "candidates": self._slots()}
        raise AssertionError("unexpected browser script")

    def _slots(self):
        result = []
        for index in range(self.total):
            done = index in self.resolved
            url = f"https://{RESOURCE_HOST}/pages/{index + 1:03}.webp"
            result.append({
                "tag": "img",
                "url": url,
                "currentSrc": url,
                "src": url,
                "data_url": "",
                "data_src": "",
                "source": "currentSrc",
                "order": index,
                "width": 800 if done else 1,
                "height": 1000 if done else 1,
                "naturalWidth": 800 if done else 1,
                "naturalHeight": 1000 if done else 1,
                "complete": done,
                "y": 100 + index * 1000,
                "inContainer": True,
                "isChapterCandidate": True,
                "container": "article.immersive-reader section",
                "className": "h-auto w-full object-contain",
                "id": "",
                "alt": f"Demo Chapter Page {index + 1}",
            })
        return result


class VortexReaderPipelineTests(unittest.TestCase):
    def test_selectors_match_current_public_reader_contract(self):
        adapter = select_adapter(PAGE)
        selectors = adapter.reader_selectors()
        self.assertIn("article.immersive-reader", selectors["container"])
        self.assertIn("articleBody", selectors["container"])
        self.assertIn("figure.image-container", selectors["image"])
        self.assertIn("data-reader-page-image", selectors["image"])

    def test_dom_observation_grants_dynamic_resource_host_before_authorization(self):
        adapter = select_adapter(PAGE)
        driver = VortexReaderDriver(total=3, resolved=3)
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            payload = adapter.collect_reader_payload(driver, PAGE)
        self.assertEqual(payload["slot_counts"], {
            "total": 3, "resolved": 3, "pending": 0, "rejected": 0,
        })
        self.assertEqual(len(payload["dom_candidates"]), 3)
        self.assertEqual(len(driver.selectors), 1)
        self.assertIn("article.immersive-reader", driver.selectors[0][0])
        self.assertIn("figure.image-container", driver.selectors[0][1])

    def test_vortex_lazy_slots_resolve_in_dom_order_without_network(self):
        adapter = select_adapter(PAGE)
        driver = VortexReaderDriver(total=3, resolved=1)
        resolver = down._webtoons_lazy_resolver(
            limits=ResolverLimits(max_rounds=5, stable_rounds=1, settle_seconds=0),
            sleep=lambda _seconds: None,
        )
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            analysis = adapter.analyze(
                {"driver": driver, "page_url": PAGE, "lazy_slot_resolver": resolver},
            )
        self.assertEqual(analysis.outcome, chapter_source.SUPPORTED_SPECIFIC_ADAPTER)
        self.assertEqual(len(analysis.accepted), 3)
        self.assertEqual([item.order for item in analysis.accepted], [0, 1, 2])
        self.assertEqual(analysis.reader_diagnostics["slots_total"], 3)
        self.assertEqual(analysis.reader_diagnostics["slots_pending"], 0)
        self.assertTrue(driver.scrolls)


if __name__ == "__main__":
    unittest.main()
