"""Offline five-page post-translation replay through the production pipeline.

All inputs, OCR, and translation are deterministic local fixtures.  This is a
diagnostic replay for timing cleanup/render/PDF stages; it must never require
NVIDIA, DeepL, Supabase, or network access.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import os
import shutil
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


class _ReplayTranslator:
    model = "offline-replay-translator"

    def __init__(self):
        self.calls = []
        self.force_cache = False
        self.stats = {"api_texts": 0, "api_requests": 0, "cache_hits": 0, "failed_batches": 0,
                      "provider_name": "yomu_backend"}

    def translate_many(self, texts, force=False):
        self.calls.append((list(texts), bool(force)))
        return [f"LEVANTA {index + 1}!" for index, _ in enumerate(texts)]

    def translate_strict(self, *_args, **_kwargs):
        raise AssertionError("offline replay must not retry a valid result")

    def set_detected_names(self, _names):
        return None


class _NoopMonitor:
    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        pass

    def set_stage(self, *_args, **_kwargs):
        pass

    def set_progress(self, **_kwargs):
        pass

    def register_worker_roles(self, *_args, **_kwargs):
        pass

    def stop(self):
        return {"enabled": False, "synthetic": True}


class OfflineFivePageReplayTests(unittest.TestCase):
    def test_five_pages_post_translation_replay_is_offline_and_timed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "allowed_input"
            chapter = source_root / "chapter"
            chapter.mkdir(parents=True)
            for index in range(1, 6):
                image = np.full((480, 640, 3), 255, dtype=np.uint8)
                cv2.rectangle(image, (55, 150), (585, 330), (245, 245, 245), -1)
                cv2.putText(image, "GET UP!", (150, 250), cv2.FONT_HERSHEY_SIMPLEX,
                            1.55, (0, 0, 0), 4, cv2.LINE_AA)
                image[0, 0] = (index, 0, 0)
                self.assertTrue(cv2.imwrite(str(chapter / f"{index:03d}.png"), image))

            snapshots = root / "runtime" / "local_sources"
            snapshots.mkdir(parents=True)
            output = root / "output" / "offline_replay_5pages"
            snapshot = LocalFolderChapterAdapter(
                LocalFolderPolicy(allowed_roots=(source_root,)),
            ).snapshot(chapter, snapshots, snapshot_id="offline_replay_5pages")
            reference = local_folder_input.local_source_reference(snapshot.analysis.source_fingerprint)
            translator = _ReplayTranslator()

            def fake_ocr(jobs, _language, **_kwargs):
                results = {}
                for job in jobs:
                    page = int(job["index"])
                    polygon = np.array([[135, 190], [480, 190], [480, 270], [135, 270]], dtype=np.int32)
                    line = OCRLine(text="GET UP!", raw_text="GET UP!", confidence=0.99,
                                   polygon=polygon, box=(135, 190, 345, 80),
                                   engine="synthetic-ocr", page=page,
                                   metadata={"engine": "synthetic-ocr"})
                    results[page] = {"lines": [line], "ocr_metadata": {"final_engine": "synthetic-ocr"},
                                     "elapsed_seconds": 0.0}
                return results, {"parallel": False, "worker_pids": [], "adaptive_decisions": []}

            def fake_analyse(_original, raw_lines, page_index=None):
                line = raw_lines[0]
                group = TextGroup(group_id=f"SPEECH_{int(page_index or 1):03d}", lines=[line], text=line.text,
                                  classification="speech", inside_balloon_like_region=True,
                                  source_engine="synthetic-ocr", quality_score=1.0)
                return [TextCandidate(line=line)], [group]

            args = SimpleNamespace(url=reference, max_images=5, full=False,
                                   debug_folder=str(output / "debug"), keep_debug=False, fast=True,
                                   benchmark=True, force=True, force_download=True, page_indices="",
                                   output_folder=str(output), ocr_engine="synthetic-ocr", use_context=False,
                                   session_context_path=str(output / "session_context.json"), source_candidate_ids=[],
                                   local_manifest_path=str(snapshot.manifest_path))
            with (
                mock.patch.object(local_folder_input, "LOCAL_SNAPSHOT_ROOT", snapshots),
                mock.patch.object(local_folder_input, "REPO_ROOT", root),
                mock.patch.object(benchmark_pipeline, "get_translator", return_value=(translator, "eng")),
                mock.patch.object(benchmark_pipeline, "detect_ocr_jobs", side_effect=fake_ocr),
                mock.patch.object(benchmark_pipeline, "analyze_image_array", side_effect=fake_analyse),
                mock.patch.object(benchmark_pipeline, "apply_speech_container_reocr", side_effect=lambda _i, lines, *_a, **_k: (lines, [])),
                mock.patch.object(benchmark_pipeline, "apply_selective_ocr_fallbacks", side_effect=lambda _i, lines, *_a, **_k: (lines, [])),
                mock.patch.object(benchmark_pipeline, "_grouping_fallback_reason", return_value=""),
                mock.patch.object(benchmark_pipeline, "ResourceMonitor", _NoopMonitor),
                mock.patch.object(benchmark_pipeline, "detect_gpu_basic", return_value={"synthetic": True}),
                mock.patch.object(benchmark_pipeline, "_git_metadata", return_value={"commit_hash": "test", "branch": "test"}),
                mock.patch.multiple(config, OCR_ENGINE="synthetic-ocr", OCR_FALLBACK_ENGINE="",
                                    OCR_HYBRID_FALLBACK=False, SKIP_NO_TEXT_IMAGES=False,
                                    ENABLE_DOWNLOAD_CACHE=False, ENABLE_OCR_CACHE=False,
                                    ENABLE_IMAGE_PROCESS_CACHE=False, RESOURCE_MONITORING=False,
                                    CLASSIFICATION_PROFILING=False, POST_RENDER_OCR_VALIDATION=False,
                                    VISUAL_DIFF_VALIDATION=False, TRANSLATION_VALIDATION=True,
                                    TRANSLATION_RETRY_ON_MIXED_LANGUAGE=True, TRANSLATE_SFX=False),
                mock.patch.dict("os.environ", {"TRANSLATION_ENABLED": "true", "YOMU_ENV": "test", "YOMU_TEST_BACKEND": "mock", "TRADUTOR_OUTPUT_ROOT": str(root / "output")}, clear=False),
            ):
                report = benchmark_pipeline.run_benchmark(args)

            self.assertEqual(report["status"], "finished")
            self.assertEqual(report["ocr_runs"], 5)
            self.assertEqual(report["groups_translated"], 5)
            self.assertTrue(Path(report["pdf_path"]).is_file())
            timing = json.loads((output / "timing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["logical_page_count"], 5)
            self.assertEqual(timing["logical_pages"], 5)
            self.assertEqual(len(translator.calls), 1)
            self.assertEqual(len(translator.calls[0][0]), 5)
            persist_target = os.getenv("OFFLINE_REPLAY_OUTPUT")
            if persist_target:
                target = Path(persist_target)
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(output, target)


if __name__ == "__main__":
    unittest.main()
