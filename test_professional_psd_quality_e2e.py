"""Quality (direct, no Review) PSD now uses the professional layered exporter, ON and OFF.

Runs the REAL offline local pipeline (benchmark_pipeline.run_benchmark) — same harness as
the PDF e2e — and validates the produced PSD's layer contract for each typesetting mode.
No provider/OCR/YK is added by the PSD step (the offline translator is called exactly once,
identically to the PDF path).
"""
from unittest import mock

from psd_tools import PSDImage

import benchmark_pipeline
import config
import local_folder_input
from test_local_benchmark_pipeline_e2e import (
    LocalBenchmarkPipelineE2ETests, _OfflineTranslator, _NoopResourceMonitor)


class QualityDirectProfessionalPsdTests(LocalBenchmarkPipelineE2ETests):
    def _run_psd(self, typesetting_mode):
        translator = _OfflineTranslator()
        reference = local_folder_input.local_source_reference(
            self.snapshot.analysis.source_fingerprint)
        args = self._args(reference)
        args.output_format = "psd"
        args.typesetting_mode = typesetting_mode
        with (
            mock.patch.object(local_folder_input, "LOCAL_SNAPSHOT_ROOT", self.snapshots),
            mock.patch.object(local_folder_input, "REPO_ROOT", self.root),
            mock.patch.object(benchmark_pipeline, "get_translator", return_value=(translator, "eng")),
            mock.patch.object(benchmark_pipeline, "detect_ocr_jobs", side_effect=self._fake_detect_ocr_jobs),
            mock.patch.object(benchmark_pipeline, "analyze_image_array", side_effect=self._fake_analyse),
            mock.patch.object(benchmark_pipeline, "apply_speech_container_reocr", side_effect=self._same_lines),
            mock.patch.object(benchmark_pipeline, "apply_selective_ocr_fallbacks", side_effect=self._same_lines),
            mock.patch.object(benchmark_pipeline, "_grouping_fallback_reason", return_value=""),
            mock.patch.object(benchmark_pipeline, "detect_gpu_basic", return_value={"synthetic": True}),
            mock.patch.object(benchmark_pipeline, "_git_metadata", return_value={"commit_hash": "test", "branch": "test"}),
            mock.patch.object(benchmark_pipeline, "ResourceMonitor", _NoopResourceMonitor),
            mock.patch.multiple(
                config, OCR_ENGINE="synthetic-ocr", OCR_FALLBACK_ENGINE="", OCR_HYBRID_FALLBACK=False,
                SKIP_NO_TEXT_IMAGES=False, ENABLE_DOWNLOAD_CACHE=False, ENABLE_OCR_CACHE=False,
                ENABLE_IMAGE_PROCESS_CACHE=False, RESOURCE_MONITORING=False, CLASSIFICATION_PROFILING=False,
                POST_RENDER_OCR_VALIDATION=False, VISUAL_DIFF_VALIDATION=False, TRANSLATION_VALIDATION=True,
                TRANSLATION_RETRY_ON_MIXED_LANGUAGE=True, TRANSLATE_SFX=False,
            ),
            mock.patch.dict("os.environ", {"TRANSLATION_ENABLED": "true", "YOMU_ENV": "test", "YOMU_TEST_BACKEND": "mock"}, clear=False),
        ):
            report = benchmark_pipeline.run_benchmark(args)
        return report, translator

    def _psd_layers(self, report):
        from pathlib import Path
        psd_dir = Path(report["psd_path"])
        psd_file = sorted(psd_dir.glob("*.psd"))[0]
        return [layer.name for layer in PSDImage.open(str(psd_file))], psd_dir

    def test_quality_direct_professional_psd_on(self):
        report, translator = self._run_psd("on")
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["output_format"], "psd")
        names, _ = self._psd_layers(report)
        # ON: Original + Cleaned + at least one Text/Region + Translated Preview.
        self.assertEqual(names[0], "Original")
        self.assertEqual(names[1], "Cleaned")
        self.assertIn("Translated Preview", names)
        self.assertTrue(any(n.startswith("Text/Region") for n in names))
        # PSD export added no provider calls (one translation, same as the PDF path).
        self.assertEqual(translator.calls, [(["GET UP!"], True)])

    def test_quality_direct_professional_psd_off(self):
        report, translator = self._run_psd("off")
        self.assertEqual(report["status"], "finished")
        names, psd_dir = self._psd_layers(report)
        self.assertEqual(names, ["Original", "Cleaned"])
        self.assertFalse(any(n.startswith("Text/Region") for n in names))
        import json
        from pathlib import Path
        manifest = json.loads((psd_dir / "psd_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["typesetting_mode"], "off")
        self.assertTrue(manifest["pages"][0]["regions"])  # region metadata preserved
        self.assertEqual(translator.calls, [(["GET UP!"], True)])
