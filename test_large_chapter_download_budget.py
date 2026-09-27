"""Hermetic regression for the beta-17 large-chapter download stall.

Physical bug (build 0.9.1-beta.17, job cf1bfa871f15, 205-page Comix chapter):
a legitimate chapter stopped at 195/205 with ``LimitExceeded: incomplete_download:
max_files``.  Root cause: inline-canvas pages (bytes already in memory) were charged
against the shared per-chapter file budget TWICE -- once by the parallel network
prefetch that fetched their URL and discarded the bytes, and once by the canvas
``reserve_local_content`` in the save loop.  With ``DOWNLOAD_WORKERS=2`` the prefetch
consumed 205 slots and the save loop consumed 195 more, hitting the 400-file ceiling
at page 196.

These tests prove: (1) a 205-page canvas chapter now downloads in full and charges
exactly one file slot per page; (2) inline-canvas pages are never network-fetched;
(3) the runaway file ceiling still blocks a chapter above the hard bound.

No real network: transports use a fake session that fails if touched.
"""
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

import down
from download_transport import DownloadLimits, LimitExceeded, RequestsTransport, _BudgetTracker


def _png(seed: int, w: int = 800, h: int = 1200) -> bytes:
    # Distinct bytes per page: identical bytes would be dropped as duplicate content.
    color = (seed % 256, (seed * 7) % 256, (seed * 13) % 256)
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class _ExplodingSession:
    """Any HTTP use is a defect here: inline-canvas pages must never be fetched."""

    trust_env = False
    max_redirects = 3

    def get(self, *args, **kwargs):  # noqa: D401 - test double
        raise AssertionError("network fetch attempted for an inline-canvas page")

    def close(self):
        pass


class _FakeAdapter:
    name = "fake"

    def validate_url(self, url):  # pragma: no cover - never reached for canvas pages
        return None

    def validate_navigation_url(self, url):  # pragma: no cover
        return None

    def validate_path(self, url):  # pragma: no cover
        return None


def _canvas_candidates(count: int):
    return [
        {
            "url": f"https://reader.example/{i:03}.png",
            "canvas_data": _png(i),
            "isChapterCandidate": True,
            "width": 800,
            "height": 1200,
            "order": i,
            "logical_page_index": i,
            "candidate_id": f"c{i}",
        }
        for i in range(1, count + 1)
    ]


def _download(count: int, limits: DownloadLimits, tmp: str):
    budget = _BudgetTracker(limits)
    transport = RequestsTransport(
        _FakeAdapter(), limits=limits, budget=budget, session=_ExplodingSession())
    target = Path(tmp) / "input"
    target.mkdir(parents=True, exist_ok=True)
    report = {
        "downloaded": [],
        "ignored": [],
        "timings": {"download_seconds": 0.0, "validation_seconds": 0.0, "image_save_seconds": 0.0},
        "expected_chapter_candidate_ids": [f"c{i}" for i in range(1, count + 1)],
    }
    saved = down._download_candidates(
        None,
        _canvas_candidates(count),
        None,
        3,
        None,
        tmp,
        report,
        referer="https://reader.example/",
        target_folder=str(target),
        transports=[transport],
        parallel_diagnostics={},
    )
    return saved, budget, report, target


class LargeChapterDownloadBudgetTests(unittest.TestCase):
    def test_205_page_canvas_chapter_downloads_in_full(self):
        with TemporaryDirectory() as tmp:
            saved, budget, report, target = _download(205, DownloadLimits(), tmp)
        self.assertEqual(len(saved), 205)
        self.assertEqual(len(report["downloaded"]), 205)
        # Exactly one file slot per saved page -- not ~2x from the prefetch+reserve
        # double charge that produced the physical 195/205 stall.
        self.assertEqual(budget.files, 205)

    def test_250_page_canvas_chapter_downloads_in_full(self):
        with TemporaryDirectory() as tmp:
            saved, budget, _report, _target = _download(250, DownloadLimits(), tmp)
        self.assertEqual(len(saved), 250)
        self.assertEqual(budget.files, 250)

    def test_inline_canvas_pages_are_never_network_fetched(self):
        # The exploding session raises on any .get(); success proves the prefetch
        # skips inline-canvas candidates entirely (the fix).  Pre-fix this raised.
        with TemporaryDirectory() as tmp:
            saved, _budget, _report, _target = _download(12, DownloadLimits(), tmp)
        self.assertEqual(len(saved), 12)

    def test_file_budget_hard_ceiling_still_blocks_runaway(self):
        # A chapter above the hard file ceiling must fail closed, not download forever.
        limits = DownloadLimits(max_files=400)
        with TemporaryDirectory() as tmp:
            with self.assertRaises(LimitExceeded) as ctx:
                _download(401, limits, tmp)
            self.assertEqual(ctx.exception.detail, "max_files")
            written = list((Path(tmp) / "input").glob("*.png"))
        # Exactly the ceiling is written before the terminal stop (one slot per page).
        self.assertEqual(len(written), 400)

    def test_has_inline_canvas_predicate(self):
        self.assertTrue(down._has_inline_canvas({"canvas_data": b"abc"}))
        self.assertFalse(down._has_inline_canvas({"canvas_data": b""}))
        self.assertFalse(down._has_inline_canvas({"url": "https://x/y.png"}))


if __name__ == "__main__":
    unittest.main()
