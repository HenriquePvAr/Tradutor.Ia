import _test_bootstrap  # noqa: F401
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

import natural_ptbr_refinement as refinement
from provider_transport import (
    CircuitBreakerPolicy,
    ProviderCircuitBreaker,
    ProviderTimeoutPolicy,
    ProviderTransportError,
    circuit_scope_key,
)
from translator_nvidia import TranslatorNvidiaBatch


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def circuit_policy(threshold=2, recovery=10, probes=1, successes=1):
    return CircuitBreakerPolicy(
        failure_threshold=threshold,
        recovery_timeout_seconds=recovery,
        half_open_max_calls=probes,
        success_threshold=successes,
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


class TimeoutPolicyTests(unittest.TestCase):
    def test_configured_components_reach_httpx(self):
        policy = ProviderTimeoutPolicy(
            2, 3, 4, 5, 6, "test", "provider", "operation")
        timeout = policy.httpx_timeout()
        self.assertEqual(
            (timeout.connect, timeout.read, timeout.write, timeout.pool),
            (2, 3, 4, 5))

    def test_remaining_deadline_caps_each_transport_timeout(self):
        policy = ProviderTimeoutPolicy(
            20, 20, 20, 20, 30, "test", "provider", "operation")
        timeout = policy.httpx_timeout(1.5)
        self.assertEqual(
            (timeout.connect, timeout.read, timeout.write, timeout.pool),
            (1.5, 1.5, 1.5, 1.5))

    def test_policy_is_content_addressed(self):
        first = ProviderTimeoutPolicy(
            1, 2, 3, 4, 5, "test", "p", "op")
        second = ProviderTimeoutPolicy(
            1, 2, 3, 4, 5, "test", "p", "op")
        self.assertEqual(first.policy_hash, second.policy_hash)

    def test_zero_negative_and_missing_values_fail_closed(self):
        for value in (0, -1, None):
            with self.subTest(value=value), self.assertRaises(
                (ValueError, TypeError)
            ):
                ProviderTimeoutPolicy(
                    value, 2, 3, 4, 5, "test", "p", "op")


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.breaker = ProviderCircuitBreaker(
            circuit_policy(), clock=self.clock)

    def test_initial_state_and_success_are_closed(self):
        self.assertEqual(self.breaker.before_call(), "closed")
        self.breaker.record_success()
        self.assertEqual(self.breaker.snapshot()["state"], "closed")

    def test_threshold_opens_and_open_rejects(self):
        self.breaker.record_failure("provider_server_error")
        self.assertEqual(self.breaker.snapshot()["state"], "closed")
        self.breaker.record_failure("provider_server_error")
        self.assertEqual(self.breaker.snapshot()["state"], "open")
        with self.assertRaisesRegex(
            ProviderTransportError, "provider_circuit_open"
        ):
            self.breaker.before_call()

    def test_recovery_enters_half_open_and_success_closes(self):
        self.breaker.record_failure("provider_server_error")
        self.breaker.record_failure("provider_server_error")
        self.clock.now += 10
        self.assertEqual(self.breaker.before_call(), "half_open")
        self.breaker.record_success()
        self.assertEqual(self.breaker.snapshot()["state"], "closed")

    def test_half_open_failure_reopens(self):
        self.breaker.record_failure("provider_server_error")
        self.breaker.record_failure("provider_server_error")
        self.clock.now += 10
        self.breaker.before_call()
        self.breaker.record_failure("provider_connect_timeout")
        self.assertEqual(self.breaker.snapshot()["state"], "open")

    def test_half_open_probe_limit_is_concurrency_safe(self):
        breaker = ProviderCircuitBreaker(
            circuit_policy(threshold=1, probes=1), clock=self.clock)
        breaker.record_failure("provider_server_error")
        self.clock.now += 10
        self.assertEqual(breaker.before_call(), "half_open")
        with self.assertRaisesRegex(
            ProviderTransportError, "provider_half_open_probe_rejected"
        ):
            breaker.before_call()

    def test_ignored_failures_do_not_count(self):
        for reason in (
            "provider_rate_limited",
            "provider_response_invalid",
            "provider_schema_invalid",
        ):
            self.breaker.record_failure(reason)
        self.assertEqual(self.breaker.snapshot()["consecutive_failures"], 0)

    def test_scope_excludes_content_identity(self):
        one = circuit_scope_key("p", "https://host.example/v1", "refinement")
        two = circuit_scope_key("p", "https://host.example/other", "refinement")
        other = circuit_scope_key("p", "https://other.example/v1", "refinement")
        self.assertEqual(one, two)
        self.assertNotEqual(one, other)


class FakeCompletion:
    def __init__(self, content):
        message = type("Message", (), {"content": content})()
        self.choices = [type("Choice", (), {"message": message})()]


class FakeClient:
    def __init__(self, effects):
        self.effects = effects
        self.chat = type("Chat", (), {})()
        self.chat.completions = self

    def create(self, **_kwargs):
        effect = self.effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return FakeCompletion(effect)


class TransportIntegrationTests(unittest.TestCase):
    def translator(self, effects, *, clock=None):
        clock = clock or Clock()
        translator = TranslatorNvidiaBatch(
            api_key="fake", enable_cache=False, clock=clock,
            sleeper=clock.sleep, operation="test")
        translator.transport_retry_limit = 3
        translator.retry_backoff_seconds = 1
        translator.timeout_policy = ProviderTimeoutPolicy(
            2, 3, 4, 5, 20, "test", "nvidia", "test")
        translator.circuit_breaker = ProviderCircuitBreaker(
            circuit_policy(threshold=3), clock=clock)
        client = FakeClient(list(effects))
        seen_remaining = []
        translator._get_client = lambda *, remaining_total_seconds=None: (
            seen_remaining.append(remaining_total_seconds) or client)
        return translator, clock, seen_remaining

    def test_transient_error_retries_then_succeeds(self):
        translator, _clock, _seen = self.translator([
            ConnectionError("offline"), '{"ok":true}'])
        self.assertEqual(
            translator._request_with_retry([]), '{"ok":true}')
        self.assertEqual(translator.stats["transport_attempts"], 2)

    def test_permanent_client_error_does_not_retry(self):
        error = RuntimeError("bad request")
        error.status_code = 400
        translator, _clock, _seen = self.translator([error])
        with self.assertRaisesRegex(
            ProviderTransportError, "provider_client_error"
        ):
            translator._request_with_retry([])
        self.assertEqual(translator.stats["transport_attempts"], 1)

    def test_retry_exhaustion_has_specific_reason(self):
        translator, _clock, _seen = self.translator([
            ConnectionError("x"), ConnectionError("x"), ConnectionError("x")])
        with self.assertRaisesRegex(
            ProviderTransportError, "provider_retry_exhausted"
        ):
            translator._request_with_retry([])

    def test_backoff_cannot_cross_total_deadline(self):
        translator, clock, _seen = self.translator([ConnectionError("x")])
        translator.retry_backoff_seconds = 30
        with self.assertRaisesRegex(
            ProviderTransportError, "provider_total_deadline_exceeded"
        ):
            translator._request_with_retry([], deadline=clock() + 1)
        self.assertEqual(clock.sleeps, [])

    def test_no_attempt_starts_after_deadline(self):
        translator, clock, _seen = self.translator([])
        with self.assertRaisesRegex(
            ProviderTransportError, "provider_total_deadline_exceeded"
        ):
            translator._request_with_retry([], deadline=clock())
        self.assertEqual(translator.stats["transport_attempts"], 0)

    def test_open_circuit_precedes_rate_limit_and_transport(self):
        translator, _clock, _seen = self.translator([])
        translator.circuit_breaker.record_failure("provider_server_error")
        translator.circuit_breaker.record_failure("provider_server_error")
        translator.circuit_breaker.record_failure("provider_server_error")
        tokens = []
        translator._wait_for_rate_limit = lambda **_kwargs: tokens.append(1)
        with self.assertRaisesRegex(
            ProviderTransportError, "provider_circuit_open"
        ):
            translator._request_with_retry([])
        self.assertEqual(tokens, [])
        self.assertEqual(translator.stats["transport_attempts"], 0)

    def test_rate_limit_wait_cannot_cross_deadline(self):
        translator, clock, _seen = self.translator([])
        translator.max_requests_per_minute = 1
        translator._request_times.append(clock())
        with self.assertRaisesRegex(
            ProviderTransportError, "provider_total_deadline_exceeded"
        ):
            translator._wait_for_rate_limit(deadline=clock() + 1)
        self.assertEqual(clock.sleeps, [])

    def test_remaining_total_is_forwarded_to_client(self):
        translator, _clock, seen = self.translator(['{}'])
        translator._request_with_retry([])
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0)
        self.assertLessEqual(seen[0], 20)

    def test_json_retry_is_separate_and_does_not_open_breaker(self):
        translator, _clock, _seen = self.translator([
            "not-json", '{"BALAO_1":"ok"}'])
        result = translator._request_json_with_retry(
            [], ["BALAO_1"], attempts=2)
        self.assertEqual(result["BALAO_1"], "ok")
        self.assertEqual(translator.stats["json_repair_attempts"], 1)
        self.assertEqual(translator.circuit_breaker.snapshot()["state"], "closed")

    def test_rate_limit_honors_valid_retry_after_without_opening_breaker(self):
        error = RuntimeError("rate limited")
        error.status_code = 429
        error.response = type(
            "Response", (), {"headers": {"Retry-After": "2.5"}})()
        translator, clock, _seen = self.translator([error, "{}"])
        translator._request_with_retry([])
        self.assertEqual(clock.sleeps, [2.5])
        self.assertEqual(translator.circuit_breaker.snapshot()["state"], "closed")

    def test_transport_reason_taxonomy(self):
        self.assertEqual(
            TranslatorNvidiaBatch._transport_reason(
                TimeoutError("read timeout"), None),
            "provider_read_timeout")
        self.assertEqual(
            TranslatorNvidiaBatch._transport_reason(RuntimeError(), 429),
            "provider_rate_limited")
        self.assertEqual(
            TranslatorNvidiaBatch._transport_reason(RuntimeError(), 503),
            "provider_server_error")


def valid_result():
    return {
        "natural_ptbr": "Natural", "compact_ptbr": "Curta",
        "neutral_ptbr": "Neutra", "literalness_detected": False,
        "meaning_preserved": True, "emotion_preserved": True,
        "information_added": False, "information_removed": False,
        "glossary_respected": True, "fits_visual_limit": True,
        "confidence": 0.9, "warnings": [], "brief_reason": "ok",
    }


class RefinementIdempotencyTests(unittest.TestCase):
    def test_simultaneous_request_hash_calls_provider_once(self):
        request = refinement.build_request(
            owner="owner", source_text="source", current_translation="current")
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def provider(_prompt):
            calls.append(1)
            entered.set()
            release.wait(2)
            return json.dumps(valid_result())

        with tempfile.TemporaryDirectory() as folder:
            store = refinement.RefinementStore(folder)
            services = [
                refinement.RefinementService(provider, store=store),
                refinement.RefinementService(provider, store=store),
            ]
            results = []
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        service.refine(request, authorized=True)))
                for service in services
            ]
            for thread in threads:
                thread.start()
            entered.wait(1)
            release.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(bool(row["cache_hit"]) for row in results), 1)

    def test_specific_transport_reason_reaches_ui_contract(self):
        request = refinement.build_request(
            owner="owner", source_text="source", current_translation="current")

        def unavailable(_prompt):
            raise ProviderTransportError("provider_circuit_open")

        result = refinement.RefinementService(unavailable).refine(
            request, authorized=True)
        self.assertEqual(result["status"], "provider_circuit_open")
        self.assertEqual(result["reason_codes"], ["provider_circuit_open"])
        self.assertFalse(result["applied_automatically"])


if __name__ == "__main__":
    unittest.main()
