"""Runtime wiring and routing provenance for selective PT-BR naturalization."""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import config
import semantic_fidelity
import translator_nllb
from natural_ptbr_refinement import RuntimePtBrNaturalizer
from ocr_balloon import OCRLine, TextGroup, apply_group_translations, validate_and_retry_translations
from translator_nvidia import TranslationResult, TranslatorNvidiaBatch


def _line(text):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    return OCRLine(
        text=text,
        confidence=0.95,
        polygon=polygon,
        box=(10, 10, 200, 35),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


def _group(text, group_id="G001"):
    return TextGroup(
        group_id=group_id,
        lines=[_line(text)],
        text=text,
        classification="speech",
        inside_balloon_like_region=True,
    )


class _Naturalizer:
    def __init__(self, *responses):
        self.responses = list(responses or ["Deixa comigo daqui pra frente."])
        self.calls = []

    def naturalize_ptbr(self, request):
        self.calls.append(dict(request))
        return self.responses.pop(0) if self.responses else request["trusted_translation"]


class RuntimePtBrNaturalizationWiringTests(unittest.TestCase):
    def test_normal_nvidia_factory_attaches_selective_runtime_naturalizer(self):
        with mock.patch.object(config, "TRANSLATION_MODE", "nvidia"), \
             mock.patch.object(config, "PTBR_NATURALIZATION_MODE", "selective"), \
             mock.patch.object(config, "NVIDIA_API_KEY", "test-key"):
            translator, ocr_code = translator_nllb.get_translator("3")

        self.assertEqual(ocr_code, "eng")
        self.assertIsInstance(translator, TranslatorNvidiaBatch)
        self.assertIsInstance(translator.ptbr_naturalizer, RuntimePtBrNaturalizer)
        self.assertIs(translator.ptbr_naturalizer.service.provider.translator, translator)
        self.assertEqual(translator.stats["naturalization_mode"], "selective")
        self.assertTrue(translator.stats["naturalization_enabled"])

    def test_normal_nvidia_factory_supports_disabled_naturalizer_mode(self):
        with mock.patch.object(config, "TRANSLATION_MODE", "nvidia"), \
             mock.patch.object(config, "PTBR_NATURALIZATION_MODE", "off"), \
             mock.patch.object(config, "NVIDIA_API_KEY", "test-key"):
            translator, _ocr_code = translator_nllb.get_translator("3")

        self.assertIsNone(translator.ptbr_naturalizer)
        self.assertEqual(translator.stats["naturalization_mode"], "off")
        self.assertFalse(translator.stats["naturalization_enabled"])

    def test_provider_structured_signal_survives_parser_apply_and_router(self):
        translator = TranslatorNvidiaBatch(api_key="test-key", enable_cache=False)
        source = "I'll handle it from here."
        group = _group(source)
        naturalizer = _Naturalizer()
        with mock.patch.object(
            translator,
            "_request_with_retry",
            return_value=(
                '{"BALAO_1":{"translation":"Eu assumirei daqui.",'
                '"naturalization_needed":true,'
                '"naturalization_reason":"awkward_ptbr"}}'
            ),
        ):
            translations = translator.translate_many([source])

        apply_group_translations([group], translations)
        stats = {}
        validate_and_retry_translations(
            [group],
            translator,
            fidelity_stats=stats,
            ptbr_naturalizer=naturalizer,
        )

        self.assertEqual(group.quality_evidence["naturalization_eligibility_source"], "provider_structured_metadata")
        self.assertTrue(group.quality_evidence["ptbr_naturalization_needed"])
        self.assertTrue(group.quality_evidence["awkward_ptbr"])
        self.assertEqual(len(naturalizer.calls), 1)
        self.assertEqual(group.translation, "Deixa comigo daqui pra frente.")
        self.assertEqual(group.naturalization_selected_version, "naturalized")

    def test_already_natural_structured_signal_false_skips_naturalizer(self):
        translator = TranslatorNvidiaBatch(api_key="test-key", enable_cache=False)
        group = _group("I will handle it from here.")
        naturalizer = _Naturalizer("Deixa comigo daqui pra frente.")
        result = TranslationResult(
            "Eu cuido disso daqui para frente.",
            quality_evidence={
                "ptbr_naturalization_needed": False,
                "naturalization_eligibility_source": "provider_structured_metadata",
            },
        )

        apply_group_translations([group], [result])
        validate_and_retry_translations([group], translator, ptbr_naturalizer=naturalizer)

        self.assertEqual(naturalizer.calls, [])
        self.assertEqual(group.translation, "Eu cuido disso daqui para frente.")

    def test_semantic_failed_translation_never_reaches_naturalizer_even_if_signal_true(self):
        translator = TranslatorNvidiaBatch(api_key="test-key", enable_cache=False)
        group = _group("I won't go.")
        naturalizer = _Naturalizer("Eu vou.")
        result = TranslationResult(
            "Eu vou.",
            quality_evidence={
                "ptbr_naturalization_needed": True,
                "naturalization_eligibility_source": "provider_structured_metadata",
                "awkward_ptbr": True,
            },
        )

        apply_group_translations([group], [result])
        stats = {}
        validate_and_retry_translations(
            [group],
            translator,
            fidelity_stats=stats,
            ptbr_naturalizer=naturalizer,
        )

        self.assertEqual(naturalizer.calls, [])
        self.assertFalse(group.translation_valid)
        self.assertEqual(stats.get("naturalization_attempted", 0), 0)

    def test_disabled_mode_keeps_translation_without_naturalizer_call(self):
        translator = TranslatorNvidiaBatch(api_key="test-key", enable_cache=False)
        group = _group("I'll handle it from here.")
        result = TranslationResult(
            "Eu assumirei daqui.",
            quality_evidence={"ptbr_naturalization_needed": True},
        )

        apply_group_translations([group], [result])
        validate_and_retry_translations([group], translator, ptbr_naturalizer=None)

        self.assertEqual(group.translation, "Eu assumirei daqui.")
        self.assertNotEqual(group.naturalization_selected_version, "naturalized")

    def test_translation_cache_preserves_structured_naturalization_metadata(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(config, "CACHE_ROOT", str(Path(folder))):
            first = TranslatorNvidiaBatch(api_key="test-key", enable_cache=True)
            source = "I'll handle it from here."
            with mock.patch.object(
                first,
                "_request_with_retry",
                return_value=(
                    '{"BALAO_1":{"translation":"Eu assumirei daqui.",'
                    '"naturalization_needed":true,'
                    '"naturalization_reason":"literalness"}}'
                ),
            ):
                first_result = first.translate_many([source])[0]

            second = TranslatorNvidiaBatch(api_key="test-key", enable_cache=True)
            second_result = second.translate_many([source])[0]

        self.assertEqual(str(first_result), "Eu assumirei daqui.")
        self.assertEqual(str(second_result), "Eu assumirei daqui.")
        self.assertTrue(second_result.quality_evidence["ptbr_naturalization_needed"])
        self.assertTrue(second_result.quality_evidence["literal_translation"])


class SyntheticDistributionTests(unittest.TestCase):
    def test_only_explicitly_eligible_faithful_items_naturalize(self):
        translator = TranslatorNvidiaBatch(api_key="test-key", enable_cache=False)
        naturalizer = _Naturalizer(
            "Deixa comigo daqui pra frente.",
            "Eu explico depois.",
        )
        groups = [
            _group("I will wait.", "G1"),
            _group("I will run.", "G2"),
            _group("I won't go.", "G3"),
            _group("I'll handle it from here.", "G4"),
            _group("I saw three gates.", "G5"),
            _group("I will explain later.", "G6"),
        ]
        translations = [
            TranslationResult("Eu vou esperar.", quality_evidence={"ptbr_naturalization_needed": False}),
            TranslationResult("Eu vou correr.", quality_evidence={"ptbr_naturalization_needed": False}),
            TranslationResult("Eu vou.", quality_evidence={"ptbr_naturalization_needed": True, "awkward_ptbr": True}),
            TranslationResult("Eu assumirei daqui.", quality_evidence={"ptbr_naturalization_needed": True, "awkward_ptbr": True}),
            TranslationResult("Eu vi três portões.", quality_evidence={"ptbr_naturalization_needed": False}),
            TranslationResult("Eu explicarei depois.", quality_evidence={"ptbr_naturalization_needed": True, "literal_translation": True}),
        ]

        apply_group_translations(groups, translations)
        stats = {}
        validate_and_retry_translations(
            groups,
            translator,
            fidelity_stats=stats,
            ptbr_naturalizer=naturalizer,
        )

        self.assertEqual(len(naturalizer.calls), 2)
        self.assertEqual(stats.get("naturalization_accepted"), 2)
        self.assertEqual(stats.get("naturalization_attempted"), 2)
        self.assertEqual(stats.get("naturalization_skipped"), 3)
        self.assertFalse(groups[2].translation_valid)
        self.assertEqual(stats.get("naturalization_eligible"), 2)
        self.assertEqual(stats.get("naturalization_rejected_fidelity", 0), 0)


if __name__ == "__main__":
    unittest.main()
