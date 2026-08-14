import _test_bootstrap  # noqa: F401
import json
import unittest

from provider_transport import (
    CircuitBreakerPolicy,
    ProviderCircuitBreaker,
    ProviderTransportError,
)
from translator_nvidia import (
    NvidiaCredential,
    NvidiaCredentialPool,
    TranslationResult,
    TranslatorNvidiaBatch,
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def circuit_policy():
    return CircuitBreakerPolicy(
        failure_threshold=3,
        recovery_timeout_seconds=10,
        half_open_max_calls=1,
        success_threshold=1,
        counted_failure_types=(
            "provider_connect_timeout",
            "provider_read_timeout",
            "provider_total_deadline_exceeded",
            "provider_connection_error",
            "provider_server_error",
        ),
        ignored_failure_types=(
            "provider_rate_limited",
            "provider_client_error",
            "provider_response_invalid",
            "provider_schema_invalid",
        ),
    )


class FakeCompletion:
    def __init__(self, content, *, finish_reason="stop"):
        message = type("Message", (), {"content": content})()
        self.choices = [
            type("Choice", (), {
                "message": message,
                "finish_reason": finish_reason,
            })()
        ]
        self.usage = type("Usage", (), {"completion_tokens": 7})()


class FakeClient:
    def __init__(self, responses, calls):
        self.responses = list(responses)
        self.calls = calls
        self.chat = type("Chat", (), {})()
        self.chat.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):
            return FakeCompletion(result[0], finish_reason=result[1])
        return FakeCompletion(result)


class Context:
    def prompt_fragment(self):
        return "TERMINOLOGY: Guildmaster => mestre da guilda"

    def signature(self):
        return "context-v1"


class RivaProviderTests(unittest.TestCase):
    def _translator(self, responses):
        calls = []
        clock = Clock()
        translator = TranslatorNvidiaBatch(
            api_key="secret-one",
            translation_provider="riva",
            model="nvidia/riva-translate-4b-instruct-v2",
            enable_cache=False,
            parallel=False,
            clock=clock,
            sleeper=clock.sleep,
            circuit_breaker=ProviderCircuitBreaker(circuit_policy(), clock=clock),
        )
        client = FakeClient(responses, calls)
        translator._get_client = lambda *, remaining_total_seconds=None: client
        return translator, calls

    def test_riva_request_uses_official_model_language_pair_and_compact_prompt(self):
        translator, calls = self._translator(
            ['{"BALAO_1":"Olá!","BALAO_2":"Tchau!"}']
        )
        translator.set_session_context(Context())
        translator.set_detected_names(["Cho-I"])

        translated = translator.translate_many(["HELLO!", "BYE!"], force=True)

        self.assertEqual([str(item) for item in translated], ["Olá!", "Tchau!"])
        self.assertEqual(calls[0]["model"], "nvidia/riva-translate-4b-instruct-v2")
        self.assertEqual(calls[0]["messages"][0], {"role": "system", "content": "en-pt-BR"})
        user_prompt = calls[0]["messages"][1]["content"]
        self.assertIn('"BALAO_1":"HELLO!"', user_prompt)
        self.assertIn('"BALAO_2":"BYE!"', user_prompt)
        self.assertIn("Guildmaster => mestre da guilda", user_prompt)
        self.assertIn("Cho-I", user_prompt)
        self.assertIn("Do not add explanations", user_prompt)
        self.assertNotIn("chain-of-thought", user_prompt.casefold())
        self.assertEqual(translator.stats["provider_name"], "riva")
        self.assertEqual(translator.stats["credential_pool_size"], 1)
        self.assertLess(calls[0]["max_tokens"], 4096)
        self.assertLessEqual(calls[0]["max_tokens"], 512)

    def test_riva_translation_result_does_not_fabricate_naturalization_signal(self):
        translator, _calls = self._translator(['{"BALAO_1":"Eu assumo daqui."}'])

        result = translator.translate_many(["I will handle it from here."], force=True)[0]

        self.assertIsInstance(result, TranslationResult)
        self.assertEqual(result.quality_evidence["translation_provider"], "riva")
        self.assertEqual(
            result.quality_evidence["naturalization_eligibility_source"],
            "riva_translation_only",
        )
        self.assertNotIn("ptbr_naturalization_needed", result.quality_evidence)

    def test_riva_strict_retry_uses_single_source_payload_without_rejected_metadata(self):
        translator, calls = self._translator(['{"BALAO_1":"Obrigado!"}'])

        result = translator.translate_strict(
            "THANK YOU!",
            previous_translation="THANK YOU!",
            validation_reason="candidate_equals_source",
        )

        self.assertEqual(result, "Obrigado!")
        prompt = calls[0]["messages"][1]["content"]
        self.assertIn('"BALAO_1":"THANK YOU!"', prompt)
        self.assertNotIn("traducao_rejeitada", prompt)
        self.assertNotIn("motivo_rejeicao", prompt)
        self.assertNotIn("restricao", prompt)
        self.assertGreaterEqual(calls[0]["max_tokens"], 96)

    def test_riva_logical_call_telemetry_distinguishes_initial_and_quality_retry(self):
        translator, _calls = self._translator([
            '{"BALAO_1":"Olá!"}',
            '{"BALAO_1":"Obrigado!"}',
        ])

        translator.translate_many(["HELLO!"], force=True)
        translator.translate_strict(
            "THANK YOU!",
            previous_translation="THANK YOU!",
            validation_reason="candidate_equals_source",
            retry_origin="quality_retry",
        )

        telemetry = translator.stats["provider_request_telemetry"]
        self.assertEqual(
            [entry["logical_call_origin"] for entry in telemetry],
            ["initial_batch", "quality_retry"],
        )
        self.assertEqual(
            translator.stats["logical_calls_by_origin"],
            {"initial_batch": 1, "quality_retry": 1},
        )
        self.assertEqual(
            sum(translator.stats["logical_calls_by_origin"].values()),
            translator.stats["logical_batches"],
        )
        self.assertEqual(
            translator.stats["provider_attempts_by_kind"]["quality_retry"], 1
        )

    def test_riva_missing_output_preserves_partial_success_and_fails_closed_item(self):
        translator, calls = self._translator([
            '{"BALAO_1":"Olá"}',
            ProviderTransportError("provider_unavailable"),
        ])

        translated = translator.translate_many(["HELLO", "BYE"], force=True)

        self.assertEqual([str(item) for item in translated], ["Olá", "BYE"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(translator.stats["format_failure_missing_id"], 1)
        self.assertEqual(translator.stats["selective_recovery_requests"], 1)
        self.assertEqual(translator.stats["riva_corrective_retry_failed"], 1)
        self.assertEqual(translator.stats["translation_results_associated"], 2)

    def test_riva_extra_output_fails_closed(self):
        translator, _calls = self._translator(
            ['{"BALAO_1":"Olá","BALAO_2":"extra"}']
        )

        translated = translator.translate_many(["HELLO"], force=True)

        self.assertEqual(translated, ["HELLO"])
        self.assertEqual(translator.stats["failed_batches"], 1)
        self.assertEqual(translator.stats["translation_results_associated"], 0)

    def test_riva_mapping_returns_requested_order_not_provider_order(self):
        translator, _calls = self._translator(
            ['{"BALAO_2":"Segundo","BALAO_1":"Primeiro"}']
        )

        translated = translator.translate_many(["FIRST", "SECOND"], force=True)

        self.assertEqual([str(item) for item in translated], ["Primeiro", "Segundo"])

    def test_riva_tiny_request_uses_bounded_output_budget_not_4096(self):
        translator, calls = self._translator(['{"BALAO_1":"Oi"}'])

        translated = translator.translate_many(["HI"], force=True)

        self.assertEqual([str(item) for item in translated], ["Oi"])
        self.assertGreaterEqual(calls[0]["max_tokens"], 1)
        self.assertLess(calls[0]["max_tokens"], 4096)
        self.assertLessEqual(calls[0]["max_tokens"], 512)

    def test_riva_eight_short_items_get_response_budget_for_json_structure(self):
        translator, calls = self._translator([
            "{"
            + ",".join(f'"BALAO_{index}":"T{index}"' for index in range(1, 9))
            + "}"
        ])

        translated = translator.translate_many([f"GO {index}" for index in range(1, 9)], force=True)

        self.assertEqual([str(item) for item in translated], [f"T{index}" for index in range(1, 9)])
        self.assertGreaterEqual(calls[0]["max_tokens"], 8 + 8 * 24)
        self.assertLess(calls[0]["max_tokens"], 4096)

    def test_riva_markdown_fence_json_recovers_locally_without_provider_retry(self):
        translator, calls = self._translator([
            '```json\n{"BALAO_1":"Olá"}\n```',
        ])

        translated = translator.translate_many(["HELLO"], force=True)

        self.assertEqual([str(item) for item in translated], ["Olá"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(translator.stats["format_recoverable_wrapper"], 1)
        self.assertEqual(translator.stats["format_retry_requests"], 0)

    def test_riva_wrong_schema_gets_one_bounded_format_retry_then_fails_closed(self):
        translator, calls = self._translator([
            '["Olá"]',
            '{"unexpected":"Olá"}',
            '{"BALAO_1":"Nao deve ser chamado"}',
        ])

        translated = translator.translate_many(["HELLO"], force=True)

        self.assertEqual(translated, ["HELLO"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(translator.stats["format_failure_wrong_schema"], 1)
        self.assertEqual(translator.stats["format_failure_extra_id"], 1)
        self.assertEqual(translator.stats["format_retry_requests"], 1)
        self.assertEqual(translator.stats["format_retry_failure"], 1)

    def test_riva_partial_success_recovers_only_missing_item_not_whole_batch(self):
        initial = {
            f"BALAO_{index}": f"T{index}"
            for index in range(1, 8)
        }
        translator, calls = self._translator([
            json.dumps(initial, ensure_ascii=False, separators=(",", ":")),
            '{"BALAO_8":"T8"}',
        ])

        translated = translator.translate_many([f"SOURCE {index}" for index in range(1, 9)], force=True)

        self.assertEqual([str(item) for item in translated], [f"T{index}" for index in range(1, 9)])
        self.assertEqual(len(calls), 2)
        self.assertIn('"BALAO_8":"SOURCE 8"', calls[1]["messages"][1]["content"])
        self.assertNotIn('"BALAO_1":"SOURCE 1"', calls[1]["messages"][1]["content"])
        self.assertEqual(translator.stats["format_failure_missing_id"], 1)
        self.assertEqual(translator.stats["selective_recovery_requests"], 1)
        self.assertEqual(translator.stats["provider_http_attempts"], 2)
        self.assertEqual(
            translator.stats["provider_attempts_by_kind"],
            {"initial": 1, "selective_recovery": 1},
        )

    def test_riva_transport_and_json_retries_share_logical_budget(self):
        translator, calls = self._translator(["not-json", "still-not-json", "bad"])
        translator.transport_retry_limit = 4
        translator.json_retry_limit = 3
        translator.riva_logical_request_attempt_limit = 3

        translated = translator.translate_many(["HELLO"], force=True)

        self.assertEqual(translated, ["HELLO"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(translator.stats["failed_batches"], 1)
        self.assertEqual(translator.stats["invalid_json_failures"], 1)

    def test_riva_transport_timeouts_do_not_exceed_logical_budget(self):
        translator, calls = self._translator([
            TimeoutError("read timeout"),
            TimeoutError("read timeout"),
            TimeoutError("read timeout"),
            '{"BALAO_1":"Nunca"}',
        ])
        translator.transport_retry_limit = 4
        translator.json_retry_limit = 3
        translator.riva_logical_request_attempt_limit = 3

        translated = translator.translate_many(["HELLO"], force=True)

        self.assertEqual(translated, ["HELLO"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(translator.stats["provider_timeout_count"], 3)

    def test_riva_finish_reason_length_gets_one_bounded_format_retry(self):
        translator, calls = self._translator([
            ('{"BALAO_1":"Truncado', "length"),
            '{"BALAO_1":"Recuperado"}',
        ])

        translated = translator.translate_many(["HELLO"], force=True)

        self.assertEqual([str(item) for item in translated], ["Recuperado"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(translator.stats["finish_reason_length"], 1)
        self.assertEqual(translator.stats["format_retry_requests"], 1)
        self.assertEqual(
            translator.stats["provider_request_telemetry"][0]["finish_reason"],
            "length",
        )

    def test_riva_finish_reason_length_second_failure_fails_closed(self):
        translator, calls = self._translator([
            ('{"BALAO_1":"Truncado', "length"),
            ('{"BALAO_1":"Ainda truncado', "length"),
            '{"BALAO_1":"Nao deve ser chamado"}',
        ])

        translated = translator.translate_many(["HELLO"], force=True)

        self.assertEqual(translated, ["HELLO"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(translator.stats["failed_batches"], 1)
        self.assertEqual(translator.stats["finish_reason_length"], 2)
        self.assertEqual(translator.stats["format_retry_failure"], 1)

    def test_riva_twenty_items_are_split_by_riva_batch_limit(self):
        responses = []
        for count in (8, 8, 4):
            responses.append(
                "{"
                + ",".join(
                    f'"BALAO_{index}":"T{index}"'
                    for index in range(1, count + 1)
                )
                + "}"
            )
        translator, calls = self._translator(responses)

        translated = translator.translate_many(
            [f"SOURCE {index}" for index in range(20)],
            force=True,
        )

        self.assertEqual(len(translated), 20)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call["messages"][1]["content"].count("BALAO_") for call in calls],
            [8, 8, 4],
        )
        self.assertTrue(all(call["max_tokens"] <= 512 for call in calls))

    def test_riva_source_equal_english_subset_gets_one_corrective_request(self):
        translator, calls = self._translator([
            (
                '{"BALAO_1":"Tudo bem.","BALAO_2":"TH-THANK YOU!",'
                '"BALAO_3":"Vamos.","BALAO_4":"Certo.","BALAO_5":"Sim.",'
                '"BALAO_6":"Nao.","BALAO_7":"Agora.","BALAO_8":"Depois."}'
            ),
            '{"BALAO_2":"O-OBRIGADO!"}',
        ])

        translated = translator.translate_many(
            [
                "ALL RIGHT.",
                "TH-THANK YOU!",
                "LET'S GO.",
                "OKAY.",
                "YES.",
                "NO.",
                "NOW.",
                "LATER.",
            ],
            force=True,
        )

        self.assertEqual(str(translated[1]), "O-OBRIGADO!")
        self.assertTrue(translated[1].quality_evidence["riva_corrective_retry"])
        self.assertEqual(len(calls), 2)
        self.assertIn('"BALAO_2":"TH-THANK YOU!"', calls[1]["messages"][1]["content"])
        self.assertNotIn('"BALAO_1":"ALL RIGHT."', calls[1]["messages"][1]["content"])
        self.assertEqual(calls[1]["messages"][1]["content"].count("\nJSON:\n"), 1)
        self.assertNotIn("Rejected source-equal output", calls[1]["messages"][1]["content"])
        self.assertEqual(translator.stats["riva_source_equal_detected"], 1)
        self.assertEqual(translator.stats["riva_corrective_retry_requested"], 1)
        self.assertEqual(translator.stats["riva_corrective_retry_succeeded"], 1)

    def test_riva_normal_sentence_echo_gets_corrective_request(self):
        translator, calls = self._translator([
            "{\"BALAO_1\":\"I'M GOING HOME.\"}",
            '{"BALAO_1":"VOU PARA CASA."}',
        ])

        translated = translator.translate_many(["I'M GOING HOME."], force=True)

        self.assertEqual([str(item) for item in translated], ["VOU PARA CASA."])
        self.assertEqual(len(calls), 2)
        self.assertEqual(translator.stats["riva_corrective_retry_requested"], 1)

    def test_riva_second_source_equal_failure_stops_without_third_call(self):
        translator, calls = self._translator([
            '{"BALAO_1":"TH-THANK YOU!"}',
            '{"BALAO_1":"TH-THANK YOU!"}',
            '{"BALAO_1":"Nao deve ser chamado"}',
        ])

        translated = translator.translate_many(["TH-THANK YOU!"], force=True)

        self.assertEqual([str(item) for item in translated], ["TH-THANK YOU!"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(translator.stats["riva_corrective_retry_failed"], 1)
        self.assertEqual(translator.stats["riva_untranslated_blocked"], 1)

    def test_riva_partial_english_residual_gets_one_corrective_request(self):
        translator, calls = self._translator([
            '{"BALAO_1":"TOOK YOU LONGER THAN I EXPECTED.","BALAO_2":"Tudo certo."}',
            '{"BALAO_1":"Você demorou mais do que eu esperava."}',
        ])

        translated = translator.translate_many(
            ["IT TOOK YOU LONGER THAN I EXPECTED.", "ALL RIGHT."],
            force=True,
        )

        self.assertEqual(str(translated[0]), "Você demorou mais do que eu esperava.")
        self.assertEqual(str(translated[1]), "Tudo certo.")
        self.assertEqual(len(calls), 2)
        self.assertIn('"BALAO_1":"IT TOOK YOU LONGER THAN I EXPECTED."', calls[1]["messages"][1]["content"])
        self.assertNotIn('"BALAO_2":"ALL RIGHT."', calls[1]["messages"][1]["content"])
        self.assertIn("partially untranslated", calls[1]["messages"][1]["content"])
        self.assertEqual(translator.stats["riva_residual_english_detected"], 1)
        self.assertEqual(translator.stats["riva_corrective_retry_requested"], 1)

    def test_riva_multiple_residuals_are_corrected_individually_not_as_full_batch(self):
        translator, calls = self._translator([
            (
                '{"BALAO_1":"TOOK YOU LONGER THAN I EXPECTED.",'
                '"BALAO_2":"Tudo certo.",'
                '"BALAO_3":"YOU SHOULD RUN."}'
            ),
            '{"BALAO_1":"Você demorou mais do que eu esperava."}',
            '{"BALAO_3":"Se você está atrasado, deveria correr."}',
        ])

        translated = translator.translate_many(
            [
                "IT TOOK YOU LONGER THAN I EXPECTED.",
                "ALL RIGHT.",
                "IF YOU ARE LATE, YOU SHOULD RUN.",
            ],
            force=True,
        )

        self.assertEqual(str(translated[0]), "Você demorou mais do que eu esperava.")
        self.assertEqual(str(translated[1]), "Tudo certo.")
        self.assertEqual(str(translated[2]), "Se você está atrasado, deveria correr.")
        self.assertEqual(len(calls), 3)
        self.assertIn('"BALAO_1":"IT TOOK YOU LONGER THAN I EXPECTED."', calls[1]["messages"][1]["content"])
        self.assertNotIn('"BALAO_2":"ALL RIGHT."', calls[1]["messages"][1]["content"])
        self.assertNotIn('"BALAO_3":"IF YOU ARE LATE, YOU SHOULD RUN."', calls[1]["messages"][1]["content"])
        self.assertIn('"BALAO_3":"IF YOU ARE LATE, YOU SHOULD RUN."', calls[2]["messages"][1]["content"])
        self.assertNotIn('"BALAO_1":"IT TOOK YOU LONGER THAN I EXPECTED."', calls[2]["messages"][1]["content"])
        self.assertEqual(translator.stats["riva_residual_english_detected"], 2)
        self.assertEqual(translator.stats["riva_corrective_retry_requested"], 2)
        self.assertEqual(translator.stats["riva_corrective_retry_succeeded"], 2)

    def test_riva_name_code_and_sfx_source_equal_are_not_corrected(self):
        translator, calls = self._translator([
            '{"BALAO_1":"PAEHYEOK","BALAO_2":"S-RANK","BALAO_3":"BANG"}',
        ])

        translated = translator.translate_many(["PAEHYEOK", "S-RANK", "BANG"], force=True)

        self.assertEqual([str(item) for item in translated], ["PAEHYEOK", "S-RANK", "BANG"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(translator.stats["riva_source_equal_detected"], 3)
        self.assertEqual(translator.stats["riva_source_equal_legitimate"], 3)
        self.assertEqual(translator.stats["riva_corrective_retry_requested"], 0)

    def test_riva_all_valid_batch_has_no_extra_request(self):
        translator, calls = self._translator([
            '{"BALAO_1":"Vou para casa.","BALAO_2":"Obrigado."}',
        ])

        translated = translator.translate_many(
            ["I'M GOING HOME.", "THANK YOU."],
            force=True,
        )

        self.assertEqual([str(item) for item in translated], ["Vou para casa.", "Obrigado."])
        self.assertEqual(len(calls), 1)
        self.assertEqual(translator.stats["riva_source_equal_detected"], 0)


class CredentialPoolTests(unittest.TestCase):
    def test_single_legacy_key_behaves_like_one_slot(self):
        pool = NvidiaCredentialPool([NvidiaCredential("slot_1", "secret")], clock=Clock())

        credential = pool.acquire()
        pool.release_success(credential)

        self.assertEqual(credential.credential_id, "slot_1")
        self.assertEqual(pool.snapshot()["pool_size"], 1)
        self.assertEqual(pool.snapshot()["credential_switches"], 0)

    def test_two_credentials_choose_least_in_flight(self):
        pool = NvidiaCredentialPool(
            [
                NvidiaCredential("slot_1", "secret-a"),
                NvidiaCredential("slot_2", "secret-b"),
            ],
            clock=Clock(),
        )

        first = pool.acquire()
        second = pool.acquire()

        self.assertEqual(first.credential_id, "slot_1")
        self.assertEqual(second.credential_id, "slot_2")
        self.assertEqual(pool.snapshot()["credential_switches"], 1)

    def test_429_cools_only_one_credential_and_selects_other(self):
        clock = Clock()
        pool = NvidiaCredentialPool(
            [
                NvidiaCredential("slot_1", "secret-a"),
                NvidiaCredential("slot_2", "secret-b"),
            ],
            clock=clock,
            cooldown_seconds=30,
        )

        first = pool.acquire()
        pool.release_failure(first, "provider_rate_limited")
        second = pool.acquire()

        self.assertEqual(second.credential_id, "slot_2")
        snapshot = pool.snapshot()
        self.assertEqual(snapshot["eligible_credentials"], 1)
        self.assertEqual(snapshot["credential_429_cooldowns"], 1)

    def test_three_credentials_all_unavailable_fail_closed(self):
        clock = Clock()
        pool = NvidiaCredentialPool(
            [
                NvidiaCredential("slot_1", "secret-a"),
                NvidiaCredential("slot_2", "secret-b"),
                NvidiaCredential("slot_3", "secret-c"),
            ],
            clock=clock,
            cooldown_seconds=30,
        )
        for _ in range(3):
            credential = pool.acquire()
            pool.release_failure(credential, "provider_rate_limited")

        with self.assertRaisesRegex(
            ProviderTransportError, "provider_credentials_unavailable"
        ):
            pool.acquire()


if __name__ == "__main__":
    unittest.main()
