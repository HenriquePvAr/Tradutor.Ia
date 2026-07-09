import io
import unittest

from PIL import Image, ImageDraw

from down import (
    _build_download_gate,
    _candidate_skip_reason,
    _validate_image_bytes,
    _viewer_image_snapshot,
)


class _FakeDriver:
    def execute_script(self, _script):
        return {
            "imageCount": 3,
            "urls": ["https://example.test/1.jpg", "https://example.test/2.jpg", "https://example.test/3.jpg"],
        }


class DownloaderRegressionTests(unittest.TestCase):
    def test_lazy_placeholder_uses_declared_chapter_dimensions(self):
        candidate = {
            "url": "https://example.test/chapter-page.jpg",
            "source": "data-url",
            "naturalWidth": 1,
            "naturalHeight": 1,
            "width": 800,
            "height": 1000,
            "declaredWidth": 800,
            "declaredHeight": 1000,
            "isChapterCandidate": True,
        }
        self.assertIsNone(_candidate_skip_reason(candidate))

    def test_non_chapter_placeholder_remains_rejected(self):
        candidate = {
            "url": "https://example.test/recommendation.jpg",
            "source": "src",
            "naturalWidth": 1,
            "naturalHeight": 1,
            "width": 1,
            "height": 1,
        }
        self.assertEqual(_candidate_skip_reason(candidate), "too_small_dom_size")

    def test_short_chapter_strip_is_validated_conservatively(self):
        image = Image.new("RGB", (800, 80), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 20, 770, 60), fill="black")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        valid, info, decoded = _validate_image_bytes(
            buffer.getvalue(), chapter_candidate=True
        )
        self.assertTrue(valid)
        self.assertEqual((info["width"], info["height"]), (800, 80))
        decoded.close()

    def test_official_flat_separator_is_preserved(self):
        image = Image.new("RGB", (800, 26), "white")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        valid, info, decoded = _validate_image_bytes(
            buffer.getvalue(), chapter_candidate=True
        )
        self.assertTrue(valid)
        self.assertEqual((info["width"], info["height"]), (800, 26))
        decoded.close()

    def test_download_gate_detects_missing_viewer_image(self):
        report = {
            "viewer_urls": ["one", "two", "three"],
            "downloaded": [
                {"url": "one", "order": 1, "is_chapter_candidate": True},
                {"url": "three", "order": 3, "is_chapter_candidate": True},
            ],
        }
        gate = _build_download_gate(report)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["missing_viewer_images"], 1)

    def test_partial_download_gate_uses_requested_limit(self):
        report = {
            "viewer_urls": ["one", "two", "three"],
            "requested_max_images": 2,
            "downloaded": [
                {"url": "one", "order": 1, "is_chapter_candidate": True},
                {"url": "two", "order": 2, "is_chapter_candidate": True},
            ],
        }
        gate = _build_download_gate(report)
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["expected_viewer_images"], 2)
        self.assertEqual(gate["total_viewer_images"], 3)

    def test_viewer_snapshot_identifies_complete_manifest(self):
        snapshot = _viewer_image_snapshot(_FakeDriver())
        self.assertEqual(snapshot["image_count"], 3)
        self.assertTrue(snapshot["complete_manifest"])


if __name__ == "__main__":
    unittest.main()
