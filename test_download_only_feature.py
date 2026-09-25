import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import run_webtoon
import job_runner


class DownloadOnlyFeatureTests(unittest.TestCase):
    def args(self, output, *, force=False):
        return SimpleNamespace(
            local_manifest_path="", url="https://example.test/chapter/1", max_images=None,
            force=force, source_candidate_id=[], open_output=False,
        ), Path(output)

    def test_remote_download_only_promotes_atomic_directory_and_preserves_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            args, output = self.args(Path(tmp) / "chapter")

            def fake_download(_url, **kwargs):
                staging = Path(kwargs["debug_folder"])
                input_dir = Path(kwargs["target_folder"])
                input_dir.mkdir(parents=True)
                (input_dir / "001.webp").write_bytes(b"RIFFwebp")
                report = {
                    "download_gate": {"passed": True, "reasons": []},
                    "downloaded": [{"path": str(input_dir / "001.webp"), "order": 1}],
                }
                (staging / "downloaded_images.json").write_text(json.dumps(report), encoding="utf-8")
                (staging / "download_report.json").write_text(json.dumps(report), encoding="utf-8")
                return [str(input_dir / "001.webp")]

            with mock.patch("down.download_images", side_effect=fake_download) as downloader:
                result = run_webtoon._run_download_only(args, output)
            self.assertTrue(output.is_dir())
            self.assertTrue((output / "input" / "001.webp").is_file())
            self.assertEqual(result["result_type"], "directory")
            self.assertEqual(result["result_path"], str(output))
            self.assertTrue(downloader.call_args.kwargs["preserve_original"])
            self.assertFalse(list(Path(tmp).glob(".*yomu-download-tmp-*")))

    def test_existing_output_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            args, output = self.args(Path(tmp) / "chapter")
            output.mkdir()
            (output / "old.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "download_output_collision"):
                run_webtoon._run_download_only(args, output)
            self.assertEqual((output / "old.txt").read_text(encoding="utf-8"), "keep")

    def test_runner_finalizes_valid_download_directory_as_finished(self):
        class Store:
            def __init__(self):
                self.fields = {}

            def transition(self, _job_id, status, **fields):
                self.fields = {"status": status, **fields}
                return {"id": _job_id, "run_id": "run", "status": status, **fields}

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "input").mkdir()
            (output / "input" / "001.webp").write_bytes(b"bytes")
            (output / "downloaded_images.json").write_text(json.dumps({
                "download_gate": {"passed": True, "reasons": []},
                "downloaded": [{"path": str(output / "input" / "001.webp")}],
            }), encoding="utf-8")
            job = {"id": "job", "run_id": "run", "status": "running",
                   "configuration": {"download_only": True}}
            store = Store()
            code = job_runner._finalize(
                store, "job", job, output, 0, False, str(output / "log.txt"))
            self.assertEqual(code, 0)
            self.assertEqual(store.fields["status"], "finished")
            manifest = json.loads((output / "job_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["result_type"], "directory")
            self.assertEqual(manifest["result_path"], str(output))


if __name__ == "__main__":
    unittest.main()
