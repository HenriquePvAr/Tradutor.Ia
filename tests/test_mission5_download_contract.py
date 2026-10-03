"""Mission 5 bounded-download safety regressions (local fixtures only)."""
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import down


class RequestsTransport:
    name = "requests"


def _png(seed):
    image = Image.new("RGB", (480, 640), (seed + 1, 20, 40))
    out = io.BytesIO(); image.save(out, "PNG"); return out.getvalue()


def _candidates(count=4):
    return [{"candidate_id": f"p{i}", "url": f"http://fixture/page{i}.png",
             "source": "fixture", "order": i, "isChapterCandidate": True}
            for i in range(1, count + 1)]


def _report(count=4):
    return {"viewer_image_count": count, "ignored": [], "downloaded": [],
            "timings": {"download_seconds": 0, "validation_seconds": 0,
                        "image_save_seconds": 0}}


class Mission5DownloadContractTests(unittest.TestCase):
    def _run(self, workers=2, fetch=None, cancel_event=None, existing=False):
        payloads = {i: _png(i) for i in range(1, 5)}
        if fetch is None:
            def fetch(url, referer, max_retries, transports):
                fetch.last_transport_name = "requests"
                return payloads[int(url.split("page")[1].split(".")[0])]
        fetch.last_transport_name = "requests"
        diag = {}
        with tempfile.TemporaryDirectory(prefix="m5-test-") as folder:
            if existing:
                Path(folder, "001.png").write_bytes(payloads[1])
            with mock.patch.object(down, "DOWNLOAD_WORKERS", workers), \
                 mock.patch.object(down, "_download_url", side_effect=fetch):
                paths = down._download_candidates(
                    None, _candidates(), None, 2, 4, None, _report(), "http://fixture",
                    folder, [RequestsTransport()], cancel_event=cancel_event,
                    parallel_diagnostics=diag)
            blobs = [Path(path).read_bytes() for path in paths]
            return blobs, diag

    def test_serial_baseline(self):
        blobs, diag = self._run(workers=1)
        self.assertEqual(len(blobs), 4); self.assertEqual(diag["workers_effective"], 1)

    def test_bounded_workers_and_inflight(self):
        _, diag = self._run(workers=2)
        self.assertEqual(diag["workers_effective"], 2)
        self.assertLessEqual(diag["max_in_flight"], 2)

    def test_out_of_order_completion_preserves_page_order(self):
        def fetch(url, referer, max_retries, transports):
            index = int(url.split("page")[1].split(".")[0]); fetch.last_transport_name = "requests"
            time.sleep((5 - index) * .01); return _png(index)
        blobs, _ = self._run(fetch=fetch)
        self.assertEqual([blob == _png(i) for i, blob in enumerate(blobs, 1)], [True] * 4)

    def test_content_hash_integrity(self):
        blobs, _ = self._run()
        self.assertEqual(blobs, [_png(i) for i in range(1, 5)])

    def test_worker_exception_is_item_scoped(self):
        def fetch(url, referer, max_retries, transports):
            index = int(url.split("page")[1].split(".")[0]); fetch.last_transport_name = "requests"
            if index == 2: raise RuntimeError("fixture failure")
            return _png(index)
        blobs, diag = self._run(fetch=fetch)
        self.assertEqual(len(blobs), 3); self.assertGreaterEqual(diag["failure_count"], 1)

    def test_cancellation_does_not_submit_unbounded_work(self):
        cancel = threading.Event(); started = []
        def fetch(url, referer, max_retries, transports):
            index = int(url.split("page")[1].split(".")[0]); fetch.last_transport_name = "requests"
            started.append(index); cancel.set(); time.sleep(.02); return _png(index)
        self._run(fetch=fetch, cancel_event=cancel)
        self.assertLessEqual(len(started), 2)

    def test_cache_hit_is_reported(self):
        _, diag = self._run(workers=1, existing=True)
        self.assertEqual(diag["cache_hits"], 1); self.assertEqual(diag["cache_misses"], 3)

    def test_diagnostics_are_sanitized_scalars(self):
        _, diag = self._run()
        self.assertIsInstance(diag["cumulative_download_ms"], float)
        self.assertNotIn("url", diag)

    def test_retry_contract_existing_transport(self):
        calls = {"n": 0}
        def fetch(url, referer, max_retries, transports):
            calls["n"] += 1; fetch.last_transport_name = "requests"
            if calls["n"] == 1: raise RuntimeError("temporary")
            return _png(1)
        # The downloader delegates retry policy to _download_url; this test confirms a
        # worker failure is not retried by the pool itself (no duplicate submission).
        self._run(workers=1, fetch=fetch)
        self.assertEqual(calls["n"], 4)

    def test_browser_transport_stays_serial(self):
        class BrowserSessionTransport: name = "browser"
        active = maximum = 0; lock = threading.Lock()
        def fetch(url, referer, max_retries, transports):
            nonlocal active, maximum
            with lock: active += 1; maximum = max(maximum, active)
            time.sleep(.005)
            with lock: active -= 1
            fetch.last_transport_name = "browser"
            return _png(int(url.split("page")[1].split(".")[0]))
        fetch.last_transport_name = "browser"
        with tempfile.TemporaryDirectory(prefix="m5-browser-") as folder:
            with mock.patch.object(down, "DOWNLOAD_WORKERS", 4), mock.patch.object(down, "_download_url", side_effect=fetch):
                down._download_candidates(None, _candidates(), None, 1, 4, None, _report(),
                                          "http://fixture", folder, [BrowserSessionTransport()])
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
