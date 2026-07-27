import _test_bootstrap  # noqa: F401
import json
import unittest

import natural_ptbr_refinement as npr
from provider_transport import ProviderTransportError


def request(**extra):
    return npr.build_request(
        owner="owner", source_text="Generic source.",
        current_translation="Tradução atual.", provider="nvidia",
        model="configured", **extra)


def valid():
    return {
        "natural_ptbr": "Opção natural.",
        "compact_ptbr": "Opção curta.",
        "neutral_ptbr": "Opção neutra.",
        "literalness_detected": False,
        "meaning_preserved": True,
        "emotion_preserved": True,
        "information_added": False,
        "information_removed": False,
        "glossary_respected": True,
        "fits_visual_limit": True,
        "confidence": 0.91,
        "warnings": [],
        "brief_reason": "Ajuste natural.",
    }


def stringly():
    return {
        key: (
            str(value) if isinstance(value, (bool, float, list)) else value
        )
        for key, value in valid().items()
    }


class FakeTranslator:
    is_configured = True

    def __init__(self, responses, *, native_error=0):
        self.responses = list(responses)
        self.native_error = int(native_error)
        self._clock = lambda: 100.0
        self.timeout_policy = type(
            "Policy", (), {"total_timeout_seconds": 30.0})()
        self.json_retry_limit = 3
        self.calls = []
        self.stats = {"json_repair_attempts": 0}

    def _request_with_retry(
        self, messages, deadline=None, response_format=None
    ):
        self.calls.append({
            "messages": messages, "deadline": deadline,
            "response_format": response_format,
        })
        if self.native_error and len(self.calls) <= self.native_error:
            raise ProviderTransportError(
                "provider_client_error", status_code=400)
        return json.dumps(self.responses.pop(0))

    def _increment_stat(self, key, value=1):
        self.stats[key] = self.stats.get(key, 0) + value


class SchemaContractTests(unittest.TestCase):
    def test_schema_and_prompt_versions_change_request_identity(self):
        built = request()
        self.assertEqual(built["schema_version"], "2")
        self.assertEqual(
            built["prompt_contract_version"], npr.PROMPT_CONTRACT_VERSION)
        self.assertEqual(
            built["response_schema_version"], npr.RESPONSE_SCHEMA_VERSION)
        self.assertEqual(len(npr.SCHEMA_HASH), 64)

    def test_prompt_forbids_stringly_types_and_markdown(self):
        prompt = npr.build_prompt(request())
        for text in (
            "booleanos JSON reais", "confidence como número",
            "warnings como array", "Não coloque esses tipos entre aspas",
            "SCHEMA FORMAL",
        ):
            self.assertIn(text, prompt)

    def test_strict_schema_matrix(self):
        mutations = [
            ("confidence_string", {"confidence": "0.91"}),
            ("boolean_string", {"meaning_preserved": "true"}),
            ("warnings_string", {"warnings": "[]"}),
            ("missing_key", {"brief_reason": None}),
            ("null", {"natural_ptbr": None}),
            ("out_of_range", {"confidence": 1.1}),
            ("extra_key", {"extra": True}),
        ]
        for name, mutation in mutations:
            with self.subTest(name=name):
                value = valid()
                if name == "missing_key":
                    value.pop("brief_reason")
                else:
                    value.update(mutation)
                result = npr.validate_result(value, request=request())
                self.assertEqual(result["status"], "provider_response_invalid")

    def test_markdown_and_prefix_are_invalid(self):
        encoded = json.dumps(valid())
        for raw in (f"```json\n{encoded}\n```", f"resultado:\n{encoded}"):
            with self.subTest(raw=raw[:8]):
                self.assertEqual(
                    npr.validate_result(raw, request=request())["status"],
                    "provider_response_invalid")


class RepairTests(unittest.TestCase):
    def provider(self, responses, **kwargs):
        translator = FakeTranslator(responses, **kwargs)
        return npr.NvidiaRefinementProvider(translator), translator

    def test_type_error_triggers_repair_and_becomes_valid(self):
        provider, translator = self.provider([stringly(), valid()])
        outcome = provider.refine("prompt", request=request())
        self.assertEqual(outcome.trace["status"], "valid_after_repair")
        self.assertEqual(len(outcome.trace["repair_attempts"]), 1)
        self.assertEqual(translator.stats["json_repair_attempts"], 1)
        self.assertEqual(outcome.payload["confidence"], 0.91)

    def test_missing_key_and_markdown_trigger_repair(self):
        incomplete = valid()
        incomplete.pop("brief_reason")
        for first in (incomplete, "markdown"):
            with self.subTest(first=type(first).__name__):
                response = (
                    f"```json\n{json.dumps(valid())}\n```"
                    if first == "markdown" else first)
                translator = FakeTranslator.__new__(FakeTranslator)
                FakeTranslator.__init__(translator, [valid()])
                calls = [response, json.dumps(valid())]
                translator._request_with_retry = lambda *args, **kwargs: calls.pop(0)
                provider = npr.NvidiaRefinementProvider(translator)
                outcome = provider.refine("prompt", request=request())
                self.assertEqual(outcome.trace["status"], "valid_after_repair")

    def test_semantic_failure_does_not_trigger_format_repair(self):
        value = valid()
        value["information_added"] = True
        provider, translator = self.provider([value])
        outcome = provider.refine("prompt", request=request())
        validated = npr.validate_result(outcome.payload, request=request())
        self.assertEqual(outcome.trace["status"], "valid_without_repair")
        self.assertIn("information_added", validated["reason_codes"])
        self.assertEqual(len(translator.calls), 1)

    def test_repair_still_invalid_exhausts_limit(self):
        provider, translator = self.provider(
            [stringly(), stringly(), stringly()])
        outcome = provider.refine("prompt", request=request())
        self.assertEqual(
            outcome.trace["status"], "repair_schema_still_invalid")
        self.assertEqual(len(outcome.trace["repair_attempts"]), 2)
        self.assertEqual(len(translator.calls), 3)

    def test_repair_content_change_is_rejected(self):
        changed = valid()
        changed["natural_ptbr"] = "Conteúdo diferente."
        provider, _translator = self.provider([stringly(), changed])
        outcome = provider.refine("prompt", request=request())
        self.assertEqual(
            outcome.trace["status"], "repair_semantic_content_changed")
        result = npr.RefinementService(provider).refine(
            request(), authorized=True)
        self.assertIn(
            result["status"],
            {"repair_semantic_content_changed", "provider_unavailable"})

    def test_repair_identity_is_deterministic(self):
        first = npr.NvidiaRefinementProvider._repair_hash(
            "parent", 1, stringly(), ["schema"])
        second = npr.NvidiaRefinementProvider._repair_hash(
            "parent", 1, stringly(), ["schema"])
        self.assertEqual(first, second)
        for key in (
            "parent_request_hash", "repair_attempt_index",
            "repair_request_hash", "original_response_hash",
            "validation_error_hash", "schema_hash",
        ):
            self.assertIn(key, first)

    def test_native_json_mode_and_safe_client_error_fallback(self):
        provider, translator = self.provider([valid()])
        provider.refine("prompt", request=request())
        self.assertEqual(
            translator.calls[0]["response_format"]["type"], "json_schema")
        provider, translator = self.provider([valid()], native_error=1)
        outcome = provider.refine("prompt", request=request())
        self.assertEqual(outcome.trace["native_mode"], "json_object")
        self.assertEqual(
            translator.calls[1]["response_format"], {"type": "json_object"})
        provider, translator = self.provider([valid()], native_error=2)
        outcome = provider.refine("prompt", request=request())
        self.assertEqual(
            outcome.trace["native_mode"], "prompt_only_structured_output")
        self.assertIsNone(translator.calls[2]["response_format"])

    def test_service_persists_raw_and_repair_trace_without_applying(self):
        provider, _translator = self.provider([stringly(), valid()])
        result = npr.RefinementService(provider).refine(
            request(), authorized=True)
        self.assertEqual(
            result["provider_trace"]["status"], "valid_after_repair")
        self.assertTrue(result["provider_trace"]["raw_response"])
        self.assertFalse(result["applied_automatically"])

    def test_terminal_invalid_result_is_cached_without_second_provider_call(self):
        provider, translator = self.provider(
            [stringly(), stringly(), stringly()])
        service = npr.RefinementService(provider)
        first = service.refine(request(), authorized=True)
        second = service.refine(request(), authorized=True)
        self.assertEqual(first["status"], "provider_response_invalid")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(translator.calls), 3)


if __name__ == "__main__":
    unittest.main()
