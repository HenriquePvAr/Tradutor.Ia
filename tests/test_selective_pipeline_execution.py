"""Physical proof that the output plan drives real selective execution.

Reuses the hermetic local-folder harness (real benchmark_pipeline, fake OCR/translator) and
runs it under different YOMU_EFFECTIVE_OPS to prove the CLEAN/TYPESET decoupling:

  clean_only      -> clean produced, NO translated, NO PDF, 0 provider calls
  translate_clean -> translation runs, clean produced, NO translated, NO PDF

The automatic + professional regressions live in test_local_benchmark_pipeline_e2e.py and
remain green (they carry no YOMU_EFFECTIVE_OPS, so they take the identical legacy path).
"""
import test_support._test_bootstrap as _test_bootstrap  # noqa: F401

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling harness import

import yomu.pipeline.benchmark_pipeline as benchmark_pipeline
import yomu.runtime.artifact_contract as ac
from test_local_benchmark_pipeline_e2e import (
    LocalBenchmarkPipelineE2ETests,
    _OfflineTranslator,
)
import yomu.download.local.local_folder_input as local_folder_input


class SelectivePipelineExecutionTests(LocalBenchmarkPipelineE2ETests):
    def _run(self, translator, *, effective_ops, translation_enabled, output_format):
        reference = local_folder_input.local_source_reference(
            self.snapshot.analysis.source_fingerprint,
        )
        env = {
            "TRANSLATION_ENABLED": "true" if translation_enabled else "false",
            "YOMU_EFFECTIVE_OPS": effective_ops,
        }
        with self._pipeline_mocks(translator, extra_env=env):
            args = self._args(reference)
            args.output_format = output_format
            return benchmark_pipeline.run_benchmark(args)

    # --- CLEAN ONLY: no translation, no typeset, no PDF, no provider -----------------------
    def test_clean_only_produces_clean_no_translated_no_provider(self):
        translator = _OfflineTranslator()
        report = self._run(translator, effective_ops="clean",
                           translation_enabled=False, output_format="png")
        # A successful terminal state (finished, or review_required = success + QC suggested).
        self.assertIn(report["status"], {"finished", "review_required"})
        # 0 provider calls, 0 YK (translation disabled).
        self.assertEqual(translator.calls, [])
        # Clean artifact exists; translated artifact does NOT (no phantom).
        clean = self.output / "images" / "clean" / "001.png"
        translated = self.output / "images" / "translated" / "001.png"
        self.assertTrue(clean.is_file())
        self.assertFalse(translated.exists())
        # No translated PDF.
        self.assertFalse(Path(report["pdf_path"]).is_file())
        # PNG deliverable of the clean pages -> caller sees success.
        self.assertTrue(report.get("png_path"))
        # Manifest: CLEAN requested+produced, TRANSLATED/PDF not in the requested set.
        manifest = json.loads((self.output / "artifact_manifest.json").read_text(encoding="utf-8"))
        self.assertIn(ac.ArtifactType.CLEAN_IMAGE, manifest["requested_artifacts"])
        self.assertNotIn(ac.ArtifactType.TRANSLATED_IMAGE, manifest["requested_artifacts"])
        self.assertNotIn(ac.ArtifactType.FINAL_PDF, manifest["requested_artifacts"])
        self.assertEqual(manifest["effective_operations"], ["clean"])

    # --- TRANSLATE + CLEAN: translation runs, clean produced, no typeset/PDF ---------------
    def test_translate_clean_translates_and_cleans_without_typeset(self):
        translator = _OfflineTranslator()
        report = self._run(translator, effective_ops="clean,translate",
                           translation_enabled=True, output_format="png")
        self.assertIn(report["status"], {"finished", "review_required"})
        # Provider called exactly once (the translation really happened).
        self.assertEqual(translator.calls, [(["GET UP!"], True)])
        # Clean produced, translated NOT produced.
        clean = self.output / "images" / "clean" / "001.png"
        translated = self.output / "images" / "translated" / "001.png"
        self.assertTrue(clean.is_file())
        self.assertFalse(translated.exists())
        self.assertFalse(Path(report["pdf_path"]).is_file())
        # The clean page must not contain the drawn translation: it equals the cleaned buffer,
        # i.e. different from a typeset page. We assert the translation was persisted in progress
        # data (translation happened) but no glyphs were burned (page == clean).
        page = (self.output / "pages" / "page_001.png").read_bytes()
        # Physical proof that no typeset happened: the rendered page is byte-identical to the
        # cleaned buffer, i.e. zero translated glyphs were burned in.
        self.assertEqual(page, clean.read_bytes())
        progress = json.loads((self.output / "progress.json").read_text(encoding="utf-8"))
        item = progress["pages"][0]["debug_data"]["items"][0]
        self.assertEqual(item["translation"], "LEVANTA!")  # translation data exists/persisted

    # --- TRANSLATE ONLY: translation persisted, no clean/typeset/image/PDF -----------------
    def test_translate_only_persists_translation_without_any_visual_artifact(self):
        translator = _OfflineTranslator()
        report = self._run(translator, effective_ops="translate",
                           translation_enabled=True, output_format="translation")
        self.assertIn(report["status"], {"finished", "review_required"})
        # Provider ran once (translation really happened).
        self.assertEqual(translator.calls, [(["GET UP!"], True)])
        # The deliverable: a translation manifest with the persisted target.
        tm_path = self.output / "translation" / "translation.json"
        self.assertTrue(tm_path.is_file())
        tm = json.loads(tm_path.read_text(encoding="utf-8"))
        self.assertEqual(tm["kind"], "translation_only")
        targets = [r["target"] for p in tm["pages"] for r in p["regions"]]
        self.assertIn("LEVANTA!", targets)
        self.assertEqual(report["translation_manifest_path"], str(tm_path))
        # NO visual artifacts of any kind.
        self.assertFalse((self.output / "images" / "clean").exists())
        self.assertFalse((self.output / "images" / "translated").exists())
        self.assertFalse((self.output / "psd").exists())
        # No rendered page image was written (the pages/ dir may be pre-created during setup,
        # but it must hold zero image files -- nothing was rendered).
        pages_dir = self.output / "pages"
        if pages_dir.exists():
            self.assertEqual(list(pages_dir.glob("*.png")), [])
        self.assertFalse(report.get("pdf_path"))
        self.assertFalse(report.get("png_path"))
        # Manifest: requests translation_manifest only; no visual artifact requested/produced.
        manifest = json.loads((self.output / "artifact_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["requested_artifacts"], ["translation_manifest"])
        self.assertEqual(manifest["produced_artifacts"], ["translation_manifest"])
        self.assertEqual(manifest["effective_operations"], ["translate"])
        self.assertNotIn(ac.ArtifactType.TRANSLATED_IMAGE, manifest["requested_artifacts"])
        self.assertNotIn(ac.ArtifactType.CLEAN_IMAGE, manifest["requested_artifacts"])

    # --- CLEAN ONLY regression vs translate+clean: clean is produced once ------------------
    def test_clean_is_produced_from_cleaned_buffer_in_both_modes(self):
        for ops, tx in (("clean", False), ("clean,translate", True)):
            with self.subTest(ops=ops):
                self.tearDown(); self.setUp()
                report = self._run(_OfflineTranslator(), effective_ops=ops,
                                   translation_enabled=tx, output_format="png")
                manifest = json.loads((self.output / "artifact_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(report["artifact_export"]["clean_produced"], [1])
                self.assertFalse(report["artifact_export"]["translated_requested"])
                self.assertEqual(manifest["provenance"]["1"]["clean_from"], "cleaned_buffer")

