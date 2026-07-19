"""Hermetic coverage for the opaque local-folder CLI hand-off."""

import _test_bootstrap  # noqa: F401

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import local_folder_input
import run_local_folder
import run_webtoon


class LocalFolderCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot_root = self.root / "local_sources"
        self.snapshot_root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_manifest(self, reference="snapshot_a", source_fingerprint="a" * 64):
        workspace = self.snapshot_root / reference
        workspace.mkdir()
        manifest = workspace / "manifest.json"
        manifest.write_text(json.dumps({
            "snapshot_id": reference,
            "source_fingerprint": source_fingerprint,
        }), encoding="utf-8")
        return manifest

    def test_url_route_remains_unchanged_and_has_no_local_manifest(self):
        parser = run_webtoon.build_parser()
        args = parser.parse_args(["https://example.test/chapter/1"])

        source_type = run_webtoon._prepare_source(args, parser)

        self.assertEqual(source_type, "url")
        self.assertEqual(args.url, "https://example.test/chapter/1")
        self.assertEqual(args.local_manifest_path, "")
        self.assertEqual(args.local_snapshot_ref, "")

    def test_local_folder_becomes_opaque_reference_before_pipeline(self):
        parser = run_webtoon.build_parser()
        args = parser.parse_args(["--local-folder", r"C:\private\chapter"])
        manifest = self.make_manifest()
        with mock.patch.object(
            run_webtoon,
            "_snapshot_local_folder",
            return_value=(manifest, "local-folder:" + "b" * 24, "snapshot_a"),
        ):
            source_type = run_webtoon._prepare_source(args, parser)

        self.assertEqual(source_type, "local_folder")
        self.assertEqual(args.url, "local-folder:" + "b" * 24)
        self.assertEqual(Path(args.local_manifest_path), manifest)
        self.assertEqual(args.local_snapshot_ref, "snapshot_a")
        self.assertNotIn(r"C:\private\chapter", args.url)

    def test_source_choice_rejects_url_and_local_folder_before_snapshot(self):
        with mock.patch.object(run_webtoon, "_snapshot_local_folder") as snapshot:
            with self.assertRaises(SystemExit) as raised:
                run_webtoon.main([
                    "https://example.test/chapter/1", "--local-folder", r"C:\private\chapter",
                ])
        self.assertEqual(raised.exception.code, 2)
        snapshot.assert_not_called()

    def test_source_candidate_selection_is_rejected_for_local_pages(self):
        with mock.patch.object(run_webtoon, "_snapshot_local_folder") as snapshot:
            with self.assertRaises(SystemExit) as raised:
                run_webtoon.main([
                    "--local-folder", r"C:\private\chapter",
                    "--source-candidate-id", "remote-only-id",
                ])
        self.assertEqual(raised.exception.code, 2)
        snapshot.assert_not_called()

    def test_owned_manifest_must_be_a_direct_snapshot_child_with_matching_identity(self):
        manifest = self.make_manifest("safe_snapshot")
        with mock.patch.object(
            local_folder_input, "snapshot_workspace_root", return_value=self.snapshot_root
        ):
            path, reference, snapshot_ref = run_webtoon._owned_local_manifest(manifest)

        self.assertEqual(path, manifest.resolve())
        self.assertEqual(reference, "local-folder:" + "a" * 24)
        self.assertEqual(snapshot_ref, "safe_snapshot")

        outside = self.root / "outside.json"
        outside.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch.object(
            local_folder_input, "snapshot_workspace_root", return_value=self.snapshot_root
        ):
            with self.assertRaises(ValueError):
                run_webtoon._owned_local_manifest(outside)

        bad = self.make_manifest("bad_identity")
        bad.write_text(json.dumps({
            "snapshot_id": "different", "source_fingerprint": "c" * 64,
        }), encoding="utf-8")
        with mock.patch.object(
            local_folder_input, "snapshot_workspace_root", return_value=self.snapshot_root
        ):
            with self.assertRaises(ValueError):
                run_webtoon._owned_local_manifest(bad)

    def test_local_output_is_constrained_to_repository_output_root(self):
        with mock.patch.object(run_webtoon, "REPO_ROOT", self.root):
            relative = run_webtoon._resolve_local_output_folder("chapter", "opaque_ref")
            prefixed = run_webtoon._resolve_local_output_folder("output/chapter2", "opaque_ref")
            default = run_webtoon._resolve_local_output_folder("", "opaque_ref")
            self.assertEqual(relative, (self.root / "output" / "chapter").resolve())
            self.assertEqual(prefixed, (self.root / "output" / "chapter2").resolve())
            self.assertIn("opaque_ref", default.name)
            with self.assertRaises(ValueError):
                run_webtoon._resolve_local_output_folder(self.root.parent / "outside", "opaque_ref")

    def test_local_main_passes_only_manifest_and_opaque_url_to_benchmark(self):
        manifest = self.make_manifest()
        output = self.root / "output" / "safe"
        captured = {}

        def fake_benchmark(args):
            captured["args"] = args
            return {"pdf_path": "", "quality_validation": {"passed": True}, "timing_report_txt": ""}

        stream = io.StringIO()
        with (
            mock.patch.object(
                run_webtoon,
                "_snapshot_local_folder",
                return_value=(manifest, "local-folder:" + "d" * 24, "snapshot_a"),
            ),
            mock.patch.object(run_webtoon, "_resolve_local_output_folder", return_value=output),
            mock.patch.object(run_webtoon, "_configure_mode", return_value="rapidocr"),
            mock.patch.object(run_webtoon, "_run_benchmark", side_effect=fake_benchmark),
            contextlib.redirect_stdout(stream),
        ):
            result = run_webtoon.main(["--local-folder", r"C:\private\chapter", "--no-context"])

        self.assertEqual(result["quality_validation"]["passed"], True)
        args = captured["args"]
        self.assertEqual(args.url, "local-folder:" + "d" * 24)
        self.assertEqual(Path(args.local_manifest_path), manifest)
        self.assertEqual(args.output_folder, str(output))
        self.assertNotIn(r"C:\private\chapter", stream.getvalue())

    def test_internal_manifest_route_uses_the_same_opaque_pipeline_contract(self):
        manifest = self.make_manifest("runner_snapshot", "f" * 64)
        output = self.root / "output" / "safe"
        captured = {}

        def fake_benchmark(args):
            captured["args"] = args
            return {"pdf_path": "", "quality_validation": {"passed": True}, "timing_report_txt": ""}

        with (
            mock.patch.object(
                local_folder_input, "snapshot_workspace_root", return_value=self.snapshot_root
            ),
            mock.patch.object(run_webtoon, "_resolve_local_output_folder", return_value=output),
            mock.patch.object(run_webtoon, "_configure_mode", return_value="rapidocr"),
            mock.patch.object(run_webtoon, "_run_benchmark", side_effect=fake_benchmark),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            run_webtoon.main(["--input-manifest", str(manifest), "--no-context"])

        self.assertEqual(captured["args"].url, "local-folder:" + "f" * 24)
        self.assertEqual(Path(captured["args"].local_manifest_path), manifest.resolve())

    def test_local_download_only_uses_local_materialisation_not_remote_downloader(self):
        args = SimpleNamespace(
            local_manifest_path="owned/manifest.json",
            force=False,
            max_images=None,
            url="local-folder:" + "e" * 24,
            open_output=False,
        )
        output = self.root / "output" / "safe"
        report = {"download_gate": {"passed": True}, "downloaded": []}
        with mock.patch.object(run_webtoon, "_run_local_download_only", return_value=report) as local:
            actual = run_webtoon._run_download_only(args, output)
        self.assertIs(actual, report)
        local.assert_called_once_with(args, output)

    def test_runner_requires_an_opaque_snapshot_ref_and_delegates_after_validation(self):
        manifest = self.make_manifest("job_snapshot")
        captured = {}

        def fake_run_webtoon(argv):
            captured["argv"] = argv
            return {"status": "finished"}

        with (
            mock.patch.object(run_local_folder, "resolve_snapshot_manifest", return_value=manifest),
            mock.patch.object(run_webtoon, "main", side_effect=fake_run_webtoon),
        ):
            code = run_local_folder.main([
                "--snapshot-ref", "job_snapshot", "--output", "safe", "--mode", "quality",
                "--force", "--no-context", "--max-images", "2", "--download-only",
                "--logical-pages",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(captured["argv"][:6], [
            "--input-manifest", str(manifest), "--output", "safe", "--mode", "quality",
        ])
        self.assertIn("--force", captured["argv"])
        self.assertIn("--no-context", captured["argv"])
        self.assertIn("--max-images", captured["argv"])
        self.assertIn("--download-only", captured["argv"])
        self.assertNotIn("job_snapshot", captured["argv"])

    def test_runner_refuses_path_like_snapshot_ref_before_delegating(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(run_webtoon, "main") as delegated,
            contextlib.redirect_stderr(stderr),
        ):
            code = run_local_folder.main([
                "--snapshot-ref", "../private", "--output", "safe",
            ])
        self.assertEqual(code, 2)
        delegated.assert_not_called()
        self.assertNotIn("private", stderr.getvalue())

    def test_runner_resolves_only_owned_direct_child_snapshot(self):
        manifest = self.make_manifest("runner_snapshot")
        with mock.patch.object(
            local_folder_input, "snapshot_workspace_root", return_value=self.snapshot_root
        ):
            self.assertEqual(
                run_local_folder.resolve_snapshot_manifest("runner_snapshot"), manifest.resolve())
            with self.assertRaises(ValueError):
                run_local_folder.resolve_snapshot_manifest("../runner_snapshot")


if __name__ == "__main__":
    unittest.main()
