import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from local_images_source import LocalImagesError, LocalImagesSelection


def _image(path: Path, fmt: str = "PNG") -> None:
    image = Image.new("RGB", (32, 48), (10, 20, 30))
    image.save(path, format=fmt)


class LocalImagesSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("10.png", "2.png", "1.jpg", "3.jpeg"):
            _image(self.root / name, "JPEG" if name.endswith((".jpg", ".jpeg")) else "PNG")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_initial_batch_uses_natural_sort(self) -> None:
        selection = LocalImagesSelection()
        selection.add([self.root / "10.png", self.root / "2.png", self.root / "1.jpg"], initial=True)
        self.assertEqual([page.display_name for page in selection.pages], ["1.jpg", "2.png", "10.png"])

    def test_add_more_appends_without_resorting(self) -> None:
        selection = LocalImagesSelection()
        selection.add([self.root / "10.png", self.root / "2.png"], initial=True)
        selection.move(1, 0)
        selection.add([self.root / "1.jpg", self.root / "3.jpeg"])
        self.assertEqual([page.display_name for page in selection.pages], ["10.png", "2.png", "1.jpg", "3.jpeg"])

    def test_empty_selection_preserves_explicit_submit_order(self) -> None:
        selection = LocalImagesSelection()
        selection.add([self.root / "3.jpeg", self.root / "1.jpg", self.root / "10.png"], initial=False)
        self.assertEqual(
            [page.display_name for page in selection.pages],
            ["3.jpeg", "1.jpg", "10.png"],
        )

    def test_reorder_remove_clear_and_staging_preserve_order(self) -> None:
        selection = LocalImagesSelection()
        selection.add([self.root / "1.jpg", self.root / "2.png", self.root / "3.jpeg"], initial=True)
        selection.move(2, 0)
        selection.remove(1)
        original = (self.root / "3.jpeg").read_bytes()
        with tempfile.TemporaryDirectory() as staging_root:
            staged = selection.snapshot(staging_root, job_id="job-1")
            self.assertEqual([path.name for path in sorted(staged.iterdir())], ["0001.jpeg", "0002.png"])
            self.assertEqual((staged / "0001.jpeg").read_bytes(), original)
        self.assertEqual((self.root / "3.jpeg").read_bytes(), original)
        selection.clear()
        self.assertEqual(selection.pages, [])

    def test_duplicate_identity_is_ignored_but_same_basename_elsewhere_is_kept(self) -> None:
        other = self.root / "other"
        other.mkdir()
        _image(other / "1.jpg", "JPEG")
        selection = LocalImagesSelection()
        selection.add([self.root / "1.jpg", self.root / "1.jpg"], initial=True)
        selection.add([other / "1.jpg"])
        self.assertEqual(len(selection.pages), 2)

    def test_invalid_input_is_explicit(self) -> None:
        bad = self.root / "bad.gif"
        bad.write_bytes(b"bad")
        with self.assertRaises(LocalImagesError):
            LocalImagesSelection().add([bad], initial=True)


if __name__ == "__main__":
    unittest.main()
