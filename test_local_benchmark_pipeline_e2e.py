"""Hermetic local-folder hand-off through the real benchmark pipeline.

This is deliberately narrower than a chapter smoke: it creates a validated local snapshot
and invokes :func:`benchmark_pipeline.run_benchmark` itself.  OCR and translation are
deterministic fakes, while snapshot materialisation, translation validation, rendering,
quality reporting, and PDF generation remain the production code paths.  No provider,
browser, downloader, cache, or real OCR engine is available to this test.
"""

import _test_bootstrap  # noqa: F401

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

import benchmark_pipeline
import config
import local_folder_input
from local_folder_source import LocalFolderChapterAdapter, LocalFolderPolicy
from ocr_balloon import TextCandidate, TextGroup
from ocr_engine import OCRLine


class _OfflineTranslator:
    """A small deterministic translator; it raises if validation asks for a retry."""

    model = "offline-test-translator"

    def __init__(self):
        self.calls = []
        self.force_cache = False
        self.stats = {
            "api_texts": 0,
            "api_requests": 0,
            "cache_hits": 0,
            "failed_batches": 0,
        }

    def translate_many(self, texts, force=False):
        self.calls.append((list(texts), bool(force)))
        # This is intentionally a valid Portuguese result, so the production validator
        # accepts it and no retry/provider-shaped path can execute.
        return ["LEVANTA!" for _ in texts]

    def translate_strict(self, *_args, **_kwargs):
        raise AssertionError("a valid synthetic translation must not request a retry")

    def set_detected_names(self, _names):
        return None


class _NoopResourceMonitor:
    """Keep the orchestration test single-process and artifact-local."""

    def __init__(self, *_args, **_kwargs):
        self.stages = []

    def start(self):
        return None

    def set_stage(self, stage):
        self.stages.append(stage)

    def set_progress(self, **_kwargs):
        return None

    def register_worker_roles(self, *_args, **_kwargs):
        return None

    def stop(self):
        return {"enabled": False, "synthetic": True}


class RunManifestSourceProvenanceTests(unittest.TestCase):
    def test_remote_download_report_becomes_scalar_only_manifest_evidence(self):
        evidence = benchmark_pipeline._source_manifest_provenance({
            "source_type": "url",
            "adapter_name": "universal",
            "adapter_version": "1",
            "transport_name": "requests",
            "source_analysis": {
                "outcome": "review_required_medium_confidence",
                "confidence": 0.73,
                "candidate_count": 9,
                "accepted_count": 6,
                "discarded_count": 3,
            },
            "source_selection": {
                "candidate_ids": ["opaque-a", "opaque-b", "opaque-c"],
                "automatic": False,
                "observed_candidate_count": 6,
                "confirmed_candidate_count": 3,
                "manual_subset": True,
                "manual_reordered": True,
                "reason_code": "review_required_medium_confidence",
                "url": "https://example.invalid/chapter?token=secret",
            },
        })

        self.assertEqual(evidence, {
            "source_type": "url",
            "adapter_name": "universal",
            "adapter_version": "1",
            "transport_name": "requests",
            "outcome": "review_required_medium_confidence",
            "score": 0.73,
            "candidate_count": 9,
            "accepted_page_count": 6,
            "rejected_page_count": 3,
            "selection": {
                "mode": "manual",
                "selected_page_count": 3,
                "accepted_candidate_count": 6,
                "manual_subset": True,
                "manual_reordered": True,
                "reason_code": "review_required_medium_confidence",
            },
        })
        serialised = json.dumps(evidence)
        self.assertNotIn("opaque-a", serialised)
        self.assertNotIn("example.invalid", serialised)


class LocalBenchmarkPipelineE2ETests(unittest.TestCase):
    """One logical local page through local input -> OCR -> validation -> render -> PDF."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_root = self.root / "allowed_input"
        self.chapter = self.source_root / "chapter"
        self.chapter.mkdir(parents=True)
        self.snapshots = self.root / "runtime" / "local_sources"
        self.snapshots.mkdir(parents=True)
        self.output = self.root / "output" / "synthetic_chapter"
        self.original = self.chapter / "private-source-page.png"
        self._write_source_page(self.original)
        self.original_digest = hashlib.sha256(self.original.read_bytes()).hexdigest()

        adapter = LocalFolderChapterAdapter(
            LocalFolderPolicy(allowed_roots=(self.source_root,)),
        )
        self.snapshot = adapter.snapshot(
            self.chapter,
            self.snapshots,
            snapshot_id="synthetic_local_run",
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _write_source_page(path):
        image = np.full((480, 640, 3), 255, dtype=np.uint8)
        # The production renderer uses this same conservative white-background shape.
        cv2.rectangle(image, (55, 150), (585, 330), (245, 245, 245), -1)
        cv2.putText(
            image,
            "GET UP!",
            (150, 250),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.55,
            (0, 0, 0),
            4,
            cv2.LINE_AA,
        )
        if not cv2.imwrite(str(path), image):
            raise RuntimeError("could_not_create_synthetic_page")

    @staticmethod
    def _fake_detect_ocr_jobs(jobs, _language, **_kwargs):
        results = {}
        for job in jobs:
            polygon = np.array(
                [[135, 190], [480, 190], [480, 270], [135, 270]],
                dtype=np.int32,
            )
            line = OCRLine(
                text="GET UP!",
                raw_text="GET UP!",
                confidence=0.99,
                polygon=polygon,
                box=(135, 190, 345, 80),
                engine="synthetic-ocr",
                page=int(job["index"]),
                metadata={"engine": "synthetic-ocr"},
            )
            results[int(job["index"])] = {
                "lines": [line],
                "ocr_metadata": {"final_engine": "synthetic-ocr"},
                "elapsed_seconds": 0.0,
            }
        return results, {
            "parallel": False,
            "worker_pids": [],
            "adaptive_decisions": [],
        }

    @staticmethod
    def _fake_analyse(_original, raw_lines, page_index=None):
        if len(raw_lines) != 1:
            raise AssertionError("synthetic OCR result was not handed to grouping")
        line = raw_lines[0]
        group = TextGroup(
            group_id=f"SPEECH_{int(page_index or 1):03d}",
            lines=[line],
            text=line.text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="synthetic-ocr",
            quality_score=1.0,
        )
        return [TextCandidate(line=line)], [group]

    @staticmethod
    def _same_lines(_original, raw_lines, *_args, **_kwargs):
        return raw_lines, []

    def _args(self, reference):
        return SimpleNamespace(
            url=reference,
            max_images=1,
            full=False,
            debug_folder=str(self.output / "debug"),
            keep_debug=False,
            fast=True,
            benchmark=True,
            force=True,
            force_download=True,
            page_indices="",
            output_folder=str(self.output),
            ocr_engine="synthetic-ocr",
            use_context=False,
            session_context_path=str(self.output / "session_context.json"),
            source_candidate_ids=[],
            local_manifest_path=str(self.snapshot.manifest_path),
        )

    def test_local_snapshot_runs_through_validation_render_and_pdf_offline(self):
        """The real pipeline consumes only the owned manifest and returns a verified PDF."""

        translator = _OfflineTranslator()
        reference = local_folder_input.local_source_reference(
            self.snapshot.analysis.source_fingerprint,
        )
        with (
            mock.patch.object(local_folder_input, "LOCAL_SNAPSHOT_ROOT", self.snapshots),
            mock.patch.object(local_folder_input, "REPO_ROOT", self.root),
            mock.patch.object(benchmark_pipeline, "get_translator", return_value=(translator, "eng")),
            mock.patch.object(benchmark_pipeline, "detect_ocr_jobs", side_effect=self._fake_detect_ocr_jobs),
            mock.patch.object(benchmark_pipeline, "analyze_image_array", side_effect=self._fake_analyse),
            mock.patch.object(benchmark_pipeline, "apply_speech_container_reocr", side_effect=self._same_lines),
            mock.patch.object(benchmark_pipeline, "apply_selective_ocr_fallbacks", side_effect=self._same_lines),
            mock.patch.object(benchmark_pipeline, "_grouping_fallback_reason", return_value=""),
            mock.patch.object(benchmark_pipeline, "ResourceMonitor", _NoopResourceMonitor),
            mock.patch.object(benchmark_pipeline, "detect_gpu_basic", return_value={"synthetic": True}),
            mock.patch.object(benchmark_pipeline, "_git_metadata", return_value={"commit_hash": "test", "branch": "test"}),
            mock.patch.multiple(
                config,
                OCR_ENGINE="synthetic-ocr",
                OCR_FALLBACK_ENGINE="",
                OCR_HYBRID_FALLBACK=False,
                SKIP_NO_TEXT_IMAGES=False,
                ENABLE_DOWNLOAD_CACHE=False,
                ENABLE_OCR_CACHE=False,
                ENABLE_IMAGE_PROCESS_CACHE=False,
                RESOURCE_MONITORING=False,
                CLASSIFICATION_PROFILING=False,
                POST_RENDER_OCR_VALIDATION=False,
                VISUAL_DIFF_VALIDATION=False,
                TRANSLATION_VALIDATION=True,
                TRANSLATION_RETRY_ON_MIXED_LANGUAGE=True,
                TRANSLATE_SFX=False,
            ),
        ):
            report = benchmark_pipeline.run_benchmark(self._args(reference))

        # Local intake and logical-page handling are production code, not a fake downloader.
        self.assertEqual(report["source_type"], "local_folder")
        self.assertEqual(report["transport_name"], "local_snapshot")
        self.assertEqual(report["source_provenance"], {
            "source_type": "local_folder",
            "adapter_name": "local_folder",
            "adapter_version": "1",
            "transport_name": "local_snapshot",
            "score": 1.0,
            "candidate_count": 1,
            "accepted_page_count": 1,
            "rejected_page_count": 0,
            "outcome": "supported_specific_adapter",
            "selection": {
                "mode": "automatic",
                "selected_page_count": 1,
                "accepted_candidate_count": 1,
                "manual_subset": False,
                "reason_code": "supported_specific_adapter",
            },
        })
        self.assertFalse(report["smart_pdf_split"]["enabled"])
        self.assertEqual(report["ocr_runs"], 1)
        self.assertEqual(translator.calls, [(["GET UP!"], True)])
        self.assertEqual(report["translation_retries"], 0)
        self.assertEqual(report["translation_rejections"], 0)
        self.assertEqual(report["groups_translated"], 1)
        self.assertTrue(report["quality_validation"]["passed"])
        self.assertEqual(report["status"], "finished")

        # The actual renderer emits a real page, and the ordinary PIL PDF writer receives it.
        page = self.output / "pages" / "page_001.png"
        pdf = Path(report["pdf_path"])
        self.assertTrue(page.is_file())
        self.assertTrue(pdf.is_file())
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
        self.assertTrue((self.output / "timing_report.json").is_file())

        run_manifest = json.loads((self.output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(run_manifest["source_provenance"], report["source_provenance"])

        progress = json.loads((self.output / "progress.json").read_text(encoding="utf-8"))
        item = progress["pages"][0]["debug_data"]["items"][0]
        self.assertEqual(item["clean_text"], "GET UP!")
        self.assertEqual(item["translation"], "LEVANTA!")
        self.assertTrue(item["translation_valid"])
        self.assertEqual(item["translation_final_state"], "translated")
        self.assertTrue(item["redrawn"])

        # No source path/name crosses the snapshot boundary into run-facing artifacts.
        persisted = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                self.output / "downloaded_images.json",
                self.output / "progress.json",
                self.output / "quality_report.json",
                self.output / "run_manifest.json",
            )
        )
        self.assertNotIn(str(self.chapter), persisted)
        self.assertNotIn(self.original.name, persisted)
        self.assertIn(reference, persisted)
        self.assertEqual(
            hashlib.sha256(self.original.read_bytes()).hexdigest(),
            self.original_digest,
        )


if __name__ == "__main__":
    unittest.main()
