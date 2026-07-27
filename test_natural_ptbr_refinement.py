import _test_bootstrap  # noqa: F401
import json
import tempfile
import unittest

import natural_ptbr_refinement as npr


def request():
    return npr.build_request(
        owner="owner", job_id="job", run_id="run", revision_id="rev",
        page_id="page", region_id="region", source_text="I am ready!",
        current_translation="Eu estou pronto!", context_before="Olá.",
        context_after="Vamos.", region_type="speech", emotion="confident",
        visual_character_limit=30, glossary={}, provider="nvidia",
        model="configured")


def valid():
    return {
        "natural_ptbr": "Estou pronto!", "compact_ptbr": "Pronto!",
        "neutral_ptbr": "Eu estou pronto!", "literalness_detected": True,
        "meaning_preserved": True, "emotion_preserved": True,
        "information_added": False, "information_removed": False,
        "glossary_respected": True, "fits_visual_limit": True,
        "confidence": 0.9, "warnings": [], "brief_reason": "Mais natural."
    }


class NaturalRefinementTests(unittest.TestCase):
    def test_request_and_prompt_are_deterministic_and_contextual(self):
        self.assertEqual(request()["request_hash"], request()["request_hash"])
        prompt = npr.build_prompt(request())
        self.assertIn("português brasileiro", prompt)
        self.assertIn("context_before", prompt)
        for key in npr.RESULT_KEYS:
            self.assertIn(f'"{key}"', prompt)

    def test_valid_and_invalid_json(self):
        self.assertEqual(npr.validate_result(
            json.dumps(valid()), request=request())["status"],
            "valid_suggestion")
        self.assertEqual(npr.validate_result(
            "{", request=request())["status"], "provider_response_invalid")

    def test_json_scalar_types_are_strict_and_never_coerced(self):
        value = valid()
        value.update(
            confidence="0.95", meaning_preserved="True",
            information_added="False", warnings="[]")
        result = npr.validate_result(value, request=request())
        self.assertEqual(result["status"], "provider_response_invalid")
        self.assertIn(
            "provider_response_schema_invalid", result["reason_codes"])
        self.assertNotIn("information_added", result["reason_codes"])

    def test_semantic_and_information_failures_block(self):
        value = valid()
        value.update(meaning_preserved=False, information_added=True)
        result = npr.validate_result(value, request=request())
        self.assertIn("semantic_drift_detected", result["reason_codes"])
        self.assertIn("information_added", result["reason_codes"])

    def test_visual_limit_and_glossary_block(self):
        value = valid()
        value["compact_ptbr"] = "x" * 40
        value["glossary_respected"] = False
        result = npr.validate_result(value, request=request())
        self.assertIn("visual_limit_exceeded", result["reason_codes"])
        self.assertIn("glossary_violation", result["reason_codes"])

    def test_provider_is_opt_in_and_cached(self):
        calls = []
        service = npr.RefinementService(
            lambda prompt: calls.append(prompt) or valid())
        with self.assertRaises(ValueError):
            service.refine(request(), authorized=False)
        first = service.refine(request(), authorized=True)
        second = service.refine(request(), authorized=True)
        self.assertFalse(first["applied_automatically"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(calls), 1)

    def test_selection_requires_human_authorization(self):
        result = npr.validate_result(valid(), request=request())
        with self.assertRaises(ValueError):
            npr.select_option(result, owner="owner", option="natural",
                              reviewer="human", authorization="implicit")
        selected = npr.select_option(
            result, owner="owner", option="natural", reviewer="human",
            authorization="delegated_by_user")
        self.assertEqual(selected["status"], "confirmed_human_selection")

    def test_provider_failure_is_never_approval(self):
        service = npr.RefinementService(
            lambda _prompt: (_ for _ in ()).throw(TimeoutError("offline")))
        result = service.refine(request(), authorized=True)
        self.assertEqual(result["status"], "provider_unavailable")
        self.assertFalse(result["applied_automatically"])

    def test_structured_human_review_result_is_idempotent(self):
        value = valid()
        value["information_removed"] = True
        calls = []
        service = npr.RefinementService(
            lambda prompt: calls.append(prompt) or value)
        first = service.refine(request(), authorized=True)
        second = service.refine(request(), authorized=True)
        self.assertEqual(first["status"], "needs_human_review")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(calls), 1)

    def test_store_restores_result_without_provider_call_and_is_owner_scoped(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            store = npr.RefinementStore(folder)
            first = npr.RefinementService(
                lambda prompt: calls.append(prompt) or valid(), store=store)
            first.refine(request(), authorized=True)
            restored = npr.RefinementService(
                lambda prompt: calls.append(prompt) or valid(), store=store
            ).refine(request(), authorized=True)
            self.assertTrue(restored["cache_hit"])
            self.assertEqual(len(calls), 1)
            self.assertIsNone(
                store.get_result(request()["request_hash"], owner="different-owner"))

    def test_decisions_are_append_only_reversible_and_manual_requires_text(self):
        result = npr.validate_result(valid(), request=request())
        with tempfile.TemporaryDirectory() as folder:
            store = npr.RefinementStore(folder)
            with self.assertRaises(ValueError):
                npr.select_option(
                    result, owner="owner", option="manual", reviewer="human",
                    authorization="delegated_by_user")
            first = npr.select_option(
                result, owner="owner", option="keep_current", reviewer="human",
                authorization="delegated_by_user")
            second = npr.select_option(
                result, owner="owner", option="manual", reviewer="human",
                authorization="delegated_by_user",
                previous_decision_id=first["selection_decision_id"],
                manual_text="Uma opção humana.")
            store.append_decision(first)
            store.append_decision(second)
            self.assertNotEqual(
                first["selection_decision_id"], second["selection_decision_id"])
            self.assertEqual(
                second["previous_decision_id"], first["selection_decision_id"])

    def test_nvidia_adapter_reuses_configured_translator_path(self):
        class FakeTranslator:
            is_configured = True
            def __init__(self):
                self.messages = None
            def _request_json_with_retry(self, messages, expected_ids):
                self.messages = messages
                self.expected_ids = expected_ids
                return valid()
        translator = FakeTranslator()
        raw = npr.NvidiaRefinementProvider(translator)("prompt")
        self.assertEqual(raw["natural_ptbr"], "Estou pronto!")
        self.assertEqual(translator.messages[-1]["content"], "prompt")
        self.assertEqual(set(translator.expected_ids), npr.RESULT_KEYS)


if __name__ == "__main__":
    unittest.main()
