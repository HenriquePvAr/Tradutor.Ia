import _test_bootstrap  # noqa: F401
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
    def __init__(self, content):
        message = type("Message", (), {"content": content})()
        self.choices = [type("Choice", (), {"message": message})()]


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
        translator._get_client = lambda *, remaining_total_seconds=None: FakeClient(
            responses, calls
        )
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

    def test_riva_missing_output_fails_closed(self):
        translator, _calls = self._translator(['{"BALAO_1":"Olá"}'])

        translated = translator.translate_many(["HELLO", "BYE"], force=True)

        self.assertEqual(translated, ["HELLO", "BYE"])
        self.assertEqual(translator.stats["failed_batches"], 1)
        self.assertEqual(translator.stats["translation_results_associated"], 0)

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
