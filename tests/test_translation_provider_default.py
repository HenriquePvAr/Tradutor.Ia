"""TDD #43: DeepL is the beta translation default, and generic labels are provider-neutral.

Full E2E #11 cleared DeepL/quality_optimized as the beta quality baseline, so the
canonical default moves from Nemotron to DeepL.  Two invariants matter more than the
move itself:

* an explicit provider selection is still authoritative, and a default never rewrites
  a job that already recorded one;
* the *generic* pipeline stage and timing labels stop naming NVIDIA, while historical
  artifacts that do name it keep parsing.

Everything here is offline: no provider is called, no job is created.
"""

import _test_bootstrap  # noqa: F401

import os
import unittest
from pathlib import Path
from unittest import mock

import config
import ui_helpers

ROOT = Path(__file__).resolve().parent
LOOKISM_URL = (
    "https://m.webtoons.com/en/drama/lookism/ep-50/viewer"
    "?title_no=1049&episode_no=50"
)


def _command(*, mode, translation_provider):
    from ui_helpers import build_run_command

    return build_run_command(
        url=LOOKISM_URL, mode=mode, output="lookism ep 50", full=True,
        max_images=None, use_cache=False, force=False, use_context=True,
        translation_provider=translation_provider,
    )


def _no_provider_env():
    env = dict(os.environ)
    env.pop("NVIDIA_TRANSLATION_PROVIDER", None)
    return mock.patch.dict(os.environ, env, clear=True)


class CanonicalDefaultTests(unittest.TestCase):
    """§6/§38: one resolver owns the default; nothing copies it independently."""

    def test_the_canonical_default_is_deepl(self):
        self.assertEqual(ui_helpers.DEFAULT_TRANSLATION_PROVIDER, "deepl")

    def test_the_selectable_provider_set_is_unchanged(self):
        self.assertEqual(ui_helpers.TRANSLATION_PROVIDERS,
                         frozenset({"nemotron", "riva", "deepl"}))

    def test_the_canonical_beta_model_is_quality_optimized(self):
        import translator_deepl

        self.assertEqual(config.DEEPL_MODEL_TYPE, "quality_optimized")
        self.assertEqual(translator_deepl.DEFAULT_MODEL_TYPE, "quality_optimized")

    def test_the_default_endpoint_is_still_the_free_host(self):
        # §11: promotion must not start billing.  Host only -- no key is read here.
        self.assertEqual(config.DEEPL_API_BASE_URL, "https://api-free.deepl.com")

    def test_the_nvidia_family_selector_is_not_the_global_default(self):
        # NVIDIA_TRANSLATION_PROVIDER still picks nemotron vs riva *within* the
        # NVIDIA family; it is no longer the application-wide default.
        self.assertEqual(config.NVIDIA_TRANSLATION_PROVIDER, "nemotron")


class BackendDefaultResolutionTests(unittest.TestCase):
    """§14/§23-§27: the omitted-provider path resolves in the backend, not the UI."""

    def _normalize(self, payload):
        import ui_bridge

        return ui_bridge.UiBridge._normalize_translation_provider(payload)

    def test_case_a_an_omitted_provider_resolves_to_deepl(self):
        with _no_provider_env():
            self.assertEqual(self._normalize({}), "deepl")

    def test_case_b_explicit_deepl_stays_deepl(self):
        with _no_provider_env():
            self.assertEqual(self._normalize({"translation_provider": "deepl"}), "deepl")

    def test_case_c_explicit_nemotron_is_not_overridden(self):
        with _no_provider_env():
            self.assertEqual(
                self._normalize({"translation_provider": "nemotron"}), "nemotron")

    def test_case_d_explicit_riva_is_not_overridden(self):
        with _no_provider_env():
            self.assertEqual(self._normalize({"translation_provider": "riva"}), "riva")

    def test_case_e_an_unknown_provider_fails_closed(self):
        with _no_provider_env(), self.assertRaises(ValueError):
            self._normalize({"translation_provider": "gemini"})

    def test_an_explicit_environment_selection_still_wins_over_the_default(self):
        with mock.patch.dict(os.environ, {"NVIDIA_TRANSLATION_PROVIDER": "riva"}):
            self.assertEqual(self._normalize({}), "riva")


class RuntimeResolutionTests(unittest.TestCase):
    """§8/§12: the runner itself -- not only the payload -- defaults to DeepL."""

    def test_an_omitted_provider_builds_the_deepl_translator(self):
        from translator_deepl import DeepLTranslator
        from translator_nllb import get_translator

        translator, _ = get_translator("3")
        self.assertIsInstance(translator, DeepLTranslator)
        self.assertEqual(translator.translation_provider, "deepl")
        self.assertEqual(translator.model, "quality_optimized")

    def test_explicit_nemotron_still_builds_the_nvidia_translator(self):
        from translator_nvidia import TranslatorNvidiaBatch
        from translator_nllb import get_translator

        translator, _ = get_translator("3", translation_provider="nemotron")
        self.assertIsInstance(translator, TranslatorNvidiaBatch)
        self.assertEqual(translator.translation_provider, "nemotron")

    def test_explicit_riva_still_builds_the_nvidia_translator(self):
        from translator_nllb import get_translator

        translator, _ = get_translator("3", translation_provider="riva")
        self.assertEqual(translator.translation_provider, "riva")

    def test_a_non_nvidia_translation_mode_is_not_hijacked_by_the_default(self):
        # TRANSLATION_MODE is a separate, older axis; promoting the provider
        # default must not seize an explicitly configured google/hf install.
        from translator_nllb import get_translator

        with mock.patch.object(config, "TRANSLATION_MODE", "google"):
            translator, _ = get_translator("3")
        self.assertNotEqual(getattr(translator, "translation_provider", ""), "deepl")

    def test_case_f_an_unconfigured_deepl_default_fails_closed(self):
        from translator_deepl import NOT_CONFIGURED
        from translator_nllb import get_translator

        with mock.patch.object(config, "DEEPL_API_KEY", ""):
            translator, _ = get_translator("3")
            self.assertFalse(translator.is_configured)
            result = translator.translate_many(["hello"])

        # Fail closed: no translation, an explicit not-configured reason, and the
        # job stays a DeepL job -- it never becomes a Nemotron/Riva one.
        self.assertEqual(result, [""])
        self.assertEqual(translator.stats["last_transport_reason"], NOT_CONFIGURED)
        self.assertEqual(translator.stats["provider_name"], "deepl")
        self.assertEqual(translator.stats["failed_batches"], 1)
        self.assertEqual(translator.stats["api_requests"], 0)


class ProvenanceTests(unittest.TestCase):
    """§15/§33/§39/§40: provenance follows the job, never the global default."""

    def _provenance(self, provider):
        from benchmark_pipeline import report_provider_provenance

        return report_provider_provenance({
            "provider_name": provider,
            "provider_requested": provider,
            "model": "quality_optimized" if provider == "deepl" else "nvidia/x",
        })

    def test_a_default_job_records_deepl_on_both_sides(self):
        provenance = self._provenance("deepl")
        self.assertEqual(provenance["provider_requested"], "deepl")
        self.assertEqual(provenance["provider_effective"], "deepl")

    def test_an_explicit_nemotron_job_never_reports_deepl(self):
        provenance = self._provenance("nemotron")
        self.assertEqual(provenance["provider_effective"], "nemotron")
        self.assertNotIn("deepl", str(provenance).lower())

    def test_a_persisted_job_keeps_the_provider_it_recorded(self):
        from ui_helpers import requested_translation_provider

        historical = {"translation_provider": "nemotron",
                      "provider_provenance": {"provider_requested": "nemotron"}}
        self.assertEqual(requested_translation_provider(historical), "nemotron")

    def test_a_rebuilt_command_carries_the_original_provider(self):
        from ui_helpers import assert_command_provider

        historical = {"translation_provider": "riva",
                      "provider_provenance": {"provider_requested": "riva"}}
        command = _command(mode="quality", translation_provider="riva")
        assert_command_provider(command, historical)
        self.assertIn("riva", command)
        self.assertNotIn("deepl", command)


class ModeIndependenceTests(unittest.TestCase):
    """§21: processing mode and translation provider are orthogonal controls."""

    def test_every_mode_and_provider_pair_is_representable(self):
        from ui_helpers import command_translation_provider

        for mode in ("fast", "quality"):
            for provider in ("deepl", "nemotron", "riva"):
                command = _command(mode=mode, translation_provider=provider)
                self.assertEqual(command_translation_provider(command), provider)
                self.assertIn(mode, command)

    def test_the_default_provider_does_not_pin_a_processing_mode(self):
        command = _command(mode="quality", translation_provider="deepl")
        self.assertIn("quality", command)
        self.assertNotIn("fast", command)


class StageLabelTests(unittest.TestCase):
    """§16/§30/§32: the generic stage label is neutral; legacy lines still parse."""

    def _stage(self, line):
        from ui_helpers import ProgressSnapshot, parse_progress_line

        return parse_progress_line(line, ProgressSnapshot()).stage

    def test_the_canonical_stage_label_is_provider_neutral(self):
        self.assertEqual(self._stage("Tradução: iniciando"), "Tradução")

    def test_a_historical_nvidia_progress_line_still_parses(self):
        self.assertEqual(self._stage("Tradução NVIDIA: iniciando"), "Tradução")

    def test_a_counter_under_the_translation_stage_still_advances(self):
        from ui_helpers import ProgressSnapshot, parse_progress_line

        snapshot = parse_progress_line("Tradução: 5/40", ProgressSnapshot())
        self.assertEqual(snapshot.counter_stage, "Tradução")
        self.assertGreater(snapshot.percent, 0.25)

    def test_the_runner_no_longer_emits_a_provider_specific_stage(self):
        source = (ROOT / "benchmark_pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("Tradução NVIDIA", source)

    def test_the_frontend_maps_both_the_new_and_the_legacy_label(self):
        script = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("'tradução': 'translate'", script)
        self.assertIn("'tradução nvidia': 'translate'", script)


class ReportLabelTests(unittest.TestCase):
    """§17/§31/§46: timing report text names the provider only in provenance."""

    def _text(self):
        from benchmark_pipeline import _timing_report_text

        report = {
            "mode": "rapido", "force": False, "total_images": 1, "processed_images": 1,
            "images_skipped_by_cache": 0, "images_skipped_by_no_text_precheck": 0,
            "ocr_runs": 1, "ocr_cache_hits": 0, "ocr_page_fallbacks": 0,
            "ocr_text_repairs": 0, "translation_api_texts": 97,
            "translation_cache_hits": 0, "total_seconds": 489.73,
            "average_seconds_per_image": 1.0,
            "average_seconds_per_image_without_cache": 1.0,
            "average_seconds_per_image_with_cache": 1.0,
            "reduction_from_baseline_percent": 0.0,
            "slowest_stage": "translation", "pdf_path": "out.pdf",
            "preview_contact_sheet": "contact.png",
            "preview_compare_sheet": "compare.png",
            "stage_seconds": {
                "download_collection": 1.0, "image_validation": 1.0,
                "no_text_precheck": 1.0, "ocr": 1.0, "ocr_cpu": 1.0,
                "classification_grouping": 1.0, "translation": 2.0,
                "inpainting": 1.0, "redraw": 1.0, "image_save": 1.0, "pdf": 1.0,
            },
        }
        return _timing_report_text(report)

    def test_the_report_never_claims_texts_were_sent_to_nvidia(self):
        self.assertNotIn("NVIDIA", self._text())

    def test_the_neutral_labels_are_present(self):
        text = self._text()
        self.assertIn("Textos enviados ao provedor: 97", text)
        self.assertIn("Traducao: 2.00s", text)


class UiDefaultTests(unittest.TestCase):
    """§13/§20/§34: the fresh form agrees with the backend default."""

    def setUp(self):
        self.shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        self.script = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")

    def test_deepl_is_the_preselected_option(self):
        self.assertIn('<option value="deepl" selected>', self.shell)

    def test_no_other_provider_is_preselected(self):
        self.assertNotIn('<option value="nemotron" selected', self.shell)
        self.assertNotIn('<option value="riva" selected', self.shell)

    def test_the_other_providers_remain_selectable_exactly_once(self):
        for provider in ("riva", "nemotron", "deepl"):
            self.assertEqual(self.shell.count(f'<option value="{provider}"'), 1, provider)

    def test_the_frontend_fallback_matches_the_backend_default(self):
        self.assertIn("|| 'deepl'", self.script)
        self.assertNotIn("|| 'nemotron'", self.script)

    def test_the_shell_does_not_advertise_nvidia_as_the_translation_engine(self):
        row = self.shell[self.shell.index('<span class="sc-role">Tradução</span>'):][:200]
        self.assertNotIn("NVIDIA", row)


class Tdd42PreflightUntouchedTests(unittest.TestCase):
    """§22: the OCR engine preflight and its zero-denominator gate are unchanged."""

    def test_the_ocr_engine_preflight_still_exists(self):
        import ocr_engine

        self.assertTrue(hasattr(ocr_engine, "require_available_engine"))
        self.assertTrue(hasattr(ocr_engine, "engine_availability"))

    def test_the_ocr_mode_is_not_coupled_to_the_provider(self):
        import fast_ocr_policy

        source = Path(fast_ocr_policy.__file__).read_text(encoding="utf-8")
        for provider in ("deepl", "nemotron", "riva"):
            self.assertNotIn(provider, source.lower())


if __name__ == "__main__":
    unittest.main()
