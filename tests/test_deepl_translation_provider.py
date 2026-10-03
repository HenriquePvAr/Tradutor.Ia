"""TDD #27: DeepL as a first-class translation provider.

Every DeepL call in this module goes through an injected transport, so the suite
never touches the network and never reads a real credential.  The contracts
under test are the ones a mapping or fallback bug would silently break:

* provider-native ``text[]`` batching, never one request per balloon;
* strict positional association, with a count mismatch failing the batch closed;
* a failed batch producing empty candidates -- never the source text, never
  another provider's output;
* a normalized error taxonomy instead of one undifferentiated "translation failed";
* byte-budgeted chunking against DeepL's 128 KiB request maximum;
* the requested provider surviving every command rebuild down to the argv.
"""

import _test_bootstrap  # noqa: F401

import json
import unittest
from pathlib import Path
from unittest import mock

import config
import translator_deepl
import ui_helpers
from translator_deepl import (
    AUTH_ERROR,
    DeepLProviderError,
    DeepLTranslator,
    INVALID_RESPONSE,
    PROVIDER_MAX_REQUEST_BYTES,
    PROVIDER_OVERLOAD,
    QUOTA_EXCEEDED,
    RATE_LIMITED,
    RESPONSE_COUNT_MISMATCH,
    TRANSPORT_ERROR,
    plan_chunks,
)

ROOT = Path(__file__).resolve().parent
TEST_KEY = "test-key-not-a-real-credential"


class RecordingTransport:
    """Captures every request and replays a scripted response per call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({
            "url": url,
            "headers": dict(headers or {}),
            "body": json.loads(body.decode("utf-8")) if body else None,
            "body_bytes": len(body or b""),
        })
        if not self.responses:
            raise AssertionError("unexpected extra provider request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def count(self):
        return len(self.calls)


def ok(*texts, model_type_used="quality_optimized", billed=None):
    rows = []
    for index, text in enumerate(texts):
        row = {"text": text, "detected_source_language": "EN",
               "model_type_used": model_type_used}
        if billed is not None:
            row["billed_characters"] = billed[index]
        rows.append(row)
    return 200, json.dumps({"translations": rows})


def translator(transport, **kwargs):
    kwargs.setdefault("api_key", TEST_KEY)
    kwargs.setdefault("base_url", "https://api-free.deepl.com")
    return DeepLTranslator(transport=transport, **kwargs)


class BasicRequestContractTests(unittest.TestCase):
    """RED #38: the request body must reproduce the TDD #26 benchmark semantics."""

    def test_two_sources_become_one_native_array_request(self):
        transport = RecordingTransport(ok("T1", "T2"))
        out = translator(transport).translate_many(["S1", "S2"])
        self.assertEqual(out, ["T1", "T2"])
        self.assertEqual(transport.count, 1)
        body = transport.calls[0]["body"]
        self.assertEqual(body["text"], ["S1", "S2"])
        self.assertEqual(body["source_lang"], "EN")
        self.assertEqual(body["target_lang"], "PT-BR")
        self.assertEqual(body["model_type"], "quality_optimized")

    def test_request_targets_the_configured_free_endpoint(self):
        transport = RecordingTransport(ok("T1"))
        translator(transport).translate_many(["S1"])
        self.assertEqual(
            transport.calls[0]["url"], "https://api-free.deepl.com/v2/translate")

    def test_no_translation_shaping_options_are_sent(self):
        # Parity gate: the successful benchmark sent none of these, so neither
        # does production.  Adding one silently would invalidate the comparison.
        transport = RecordingTransport(ok("T1"))
        translator(transport).translate_many(["S1"])
        body = transport.calls[0]["body"]
        for invented in ("context", "glossary_id", "formality", "style",
                         "tag_handling", "temperature", "instructions"):
            self.assertNotIn(invented, body, invented)

    def test_authorization_header_uses_the_deepl_scheme(self):
        transport = RecordingTransport(ok("T1"))
        translator(transport).translate_many(["S1"])
        self.assertEqual(
            transport.calls[0]["headers"]["Authorization"],
            f"DeepL-Auth-Key {TEST_KEY}")

    def test_a_non_https_base_url_is_refused(self):
        with self.assertRaises(ValueError):
            DeepLTranslator(api_key=TEST_KEY, base_url="http://api-free.deepl.com")

    def test_blank_sources_never_reach_the_provider(self):
        transport = RecordingTransport(ok("T1"))
        out = translator(transport).translate_many(["", "S1", "   "])
        self.assertEqual(out, ["", "T1", ""])
        self.assertEqual(transport.calls[0]["body"]["text"], ["S1"])


class PositionalMappingTests(unittest.TestCase):
    """RED #39/#40/#41: order, count and empties."""

    def test_order_is_positional_not_alphabetical(self):
        transport = RecordingTransport(ok("TA", "TB", "TC"))
        out = translator(transport).translate_many(["A", "B", "C"])
        self.assertEqual(out, ["TA", "TB", "TC"])

    def test_short_response_fails_the_batch_closed(self):
        transport = RecordingTransport((200, json.dumps(
            {"translations": [{"text": "TA"}, {"text": "TB"}]})))
        client = translator(transport)
        out = client.translate_many(["A", "B", "C"])
        # No shifting, no source fallback, no partial trust.
        self.assertEqual(out, ["", "", ""])
        self.assertEqual(client.stats["last_transport_reason"], RESPONSE_COUNT_MISMATCH)
        self.assertEqual(client.stats["failed_batches"], 1)

    def test_long_response_fails_the_batch_closed(self):
        transport = RecordingTransport(ok("TA", "TB", "TC", "TD"))
        client = translator(transport)
        self.assertEqual(client.translate_many(["A", "B", "C"]), ["", "", ""])
        self.assertEqual(client.stats["last_transport_reason"], RESPONSE_COUNT_MISMATCH)

    def test_malformed_translation_entry_fails_closed(self):
        transport = RecordingTransport(
            (200, json.dumps({"translations": ["TA", "TB"]})))
        client = translator(transport)
        self.assertEqual(client.translate_many(["A", "B"]), ["", ""])
        self.assertEqual(client.stats["last_transport_reason"], INVALID_RESPONSE)

    def test_missing_translations_key_fails_closed(self):
        transport = RecordingTransport((200, json.dumps({"message": "hi"})))
        client = translator(transport)
        self.assertEqual(client.translate_many(["A"]), [""])
        self.assertEqual(client.stats["last_transport_reason"], INVALID_RESPONSE)

    def test_unparseable_body_fails_closed(self):
        transport = RecordingTransport((200, "<html>gateway</html>"))
        client = translator(transport)
        self.assertEqual(client.translate_many(["A"]), [""])
        self.assertEqual(client.stats["last_transport_reason"], INVALID_RESPONSE)

    def test_empty_candidate_stays_empty_and_never_becomes_the_source(self):
        transport = RecordingTransport(ok("", "TB"))
        out = translator(transport).translate_many(["A", "B"])
        self.assertEqual(out, ["", "TB"])

    def test_a_failed_batch_never_returns_source_text(self):
        transport = RecordingTransport((503, "overloaded"))
        out = translator(transport).translate_many(["SOURCE ONE", "SOURCE TWO"])
        self.assertEqual(out, ["", ""])
        self.assertNotIn("SOURCE ONE", out)


class ErrorTaxonomyTests(unittest.TestCase):
    """RED #42/#43/#44/#45: distinct, sanitized failure reasons."""

    def _reason(self, response):
        client = translator(RecordingTransport(response))
        client.translate_many(["A"])
        return client.stats["last_transport_reason"]

    def test_quota_exhaustion_is_its_own_reason(self):
        self.assertEqual(self._reason((456, "Quota exceeded")), QUOTA_EXCEEDED)

    def test_rate_limit_is_its_own_reason(self):
        self.assertEqual(self._reason((429, "Too many requests")), RATE_LIMITED)

    def test_auth_failure_is_its_own_reason(self):
        self.assertEqual(self._reason((403, "Authorization failure")), AUTH_ERROR)
        self.assertEqual(self._reason((401, "Unauthorized")), AUTH_ERROR)

    def test_server_errors_are_provider_overload(self):
        self.assertEqual(self._reason((503, "Service unavailable")), PROVIDER_OVERLOAD)
        self.assertEqual(self._reason((500, "boom")), PROVIDER_OVERLOAD)

    def test_transport_exception_is_normalized_not_raised(self):
        client = translator(RecordingTransport(OSError("connection reset")))
        self.assertEqual(client.translate_many(["A"]), [""])
        self.assertEqual(client.stats["last_transport_reason"], TRANSPORT_ERROR)

    def test_quota_failure_never_retries_and_never_changes_endpoint(self):
        transport = RecordingTransport((456, "Quota exceeded"))
        translator(transport).translate_many(["A"])
        self.assertEqual(transport.count, 1)
        self.assertIn("api-free.deepl.com", transport.calls[0]["url"])

    def test_rate_limit_is_bounded_to_a_single_attempt(self):
        transport = RecordingTransport((429, "slow down"))
        translator(transport).translate_many(["A"])
        self.assertEqual(transport.count, 1)

    def test_the_quality_retry_entrypoint_is_bounded_to_one_extra_call(self):
        # #84F5: the absence of this attribute used to keep DeepL to one pass
        # per region, which meant a semantically rejected candidate had no
        # second attempt and the region kept its English source on the page.
        # Both callers (ocr_balloon, provider_execution) allow exactly one
        # extra call, so exposing it adds a recovery path, never a loop.
        transport = RecordingTransport()
        client = translator(transport)
        self.assertTrue(hasattr(client, "translate_strict"))
        client.translate_strict("A", previous_translation="B",
                                validation_reason="fidelity_uncertain")
        self.assertEqual(transport.count, 1)

    def test_failure_diagnostics_are_bounded_and_sanitized(self):
        client = translator(RecordingTransport((400, "x" * 5000)))
        client.translate_many(["A"])
        failure = client.stats["provider_failures"][0]
        self.assertLessEqual(len(failure["detail"]), 200)
        self.assertEqual(failure["status_code"], 400)

    def test_an_echoed_authorization_header_is_redacted(self):
        client = translator(
            RecordingTransport((400, f"bad header Authorization: DeepL-Auth-Key {TEST_KEY}")))
        client.translate_many(["A"])
        blob = json.dumps(client.stats["provider_failures"])
        self.assertNotIn(TEST_KEY, blob)

    def test_an_unconfigured_translator_fails_closed_without_calling_out(self):
        transport = RecordingTransport()
        client = translator(transport, api_key="")
        self.assertEqual(client.translate_many(["A"]), [""])
        self.assertEqual(transport.count, 0)
        self.assertEqual(client.stats["last_transport_reason"], "deepl_not_configured")


class ChunkingTests(unittest.TestCase):
    """RED #46/#47: byte-budgeted chunking, not a text-count heuristic."""

    def test_small_input_is_exactly_one_chunk(self):
        self.assertEqual(plan_chunks(["a", "b", "c"]), [[0, 1, 2]])

    def test_ninety_six_episode_sized_sources_stay_one_request(self):
        # Episode-51 parity: 96 groups, ~3.4k source characters.
        sources = [f"GROUP {i} SOME ENGLISH DIALOGUE HERE." for i in range(96)]
        transport = RecordingTransport(ok(*[f"T{i}" for i in range(96)]))
        client = translator(transport)
        out = client.translate_many(sources)
        self.assertEqual(transport.count, 1, "96 sources must not fan out into 96 requests")
        self.assertEqual(len(out), 96)
        self.assertEqual(out[0], "T0")
        self.assertEqual(out[95], "T95")
        self.assertEqual(client.stats["provider_request_count"], 1)

    def test_chunks_never_exceed_the_budget_and_lose_nothing(self):
        budget = 8 * 1024
        sources = [f"{i:04d}" + "A" * 900 for i in range(40)]
        chunks = plan_chunks(sources, max_bytes=budget)
        self.assertGreater(len(chunks), 1, "input must actually straddle the budget")
        flattened = [index for chunk in chunks for index in chunk]
        self.assertEqual(flattened, list(range(40)), "no loss, duplication or reordering")
        for chunk in chunks:
            body = translator_deepl._body_bytes(
                [sources[i] for i in chunk], "EN", "quality_optimized")
            self.assertLessEqual(len(body), budget)

    def test_multibyte_and_escaped_text_is_measured_in_serialized_bytes(self):
        # A count-based limit would call these identical; they are not.
        budget = 2048
        wide = ["ç\"\\\n" * 120 for _ in range(20)]
        chunks = plan_chunks(wide, max_bytes=budget)
        for chunk in chunks:
            body = translator_deepl._body_bytes(
                [wide[i] for i in chunk], "EN", "quality_optimized")
            self.assertLessEqual(len(body), budget)

    def test_the_budget_can_never_exceed_the_provider_maximum(self):
        chunks = plan_chunks(["a", "b"], max_bytes=10 * 1024 * 1024)
        self.assertEqual(chunks, [[0, 1]])
        self.assertLessEqual(config.DEEPL_MAX_REQUEST_BYTES, PROVIDER_MAX_REQUEST_BYTES)

    def test_multiple_chunks_keep_strict_cross_chunk_association(self):
        sources = [f"{i:03d}" + "B" * 700 for i in range(6)]
        chunks = plan_chunks(sources, max_bytes=2200)
        self.assertGreater(len(chunks), 1)
        transport = RecordingTransport(
            *[ok(*[f"T{i}" for i in chunk]) for chunk in chunks])
        client = translator(transport, max_request_bytes=2200)
        self.assertEqual(client.translate_many(sources), [f"T{i}" for i in range(6)])

    def test_one_failed_chunk_never_contaminates_a_healthy_one(self):
        sources = [f"{i:03d}" + "C" * 700 for i in range(6)]
        chunks = plan_chunks(sources, max_bytes=2200)
        responses = [ok(*[f"T{i}" for i in chunks[0]])] + [(503, "down")] * (len(chunks) - 1)
        client = translator(RecordingTransport(*responses), max_request_bytes=2200)
        out = client.translate_many(sources)
        for index in chunks[0]:
            self.assertEqual(out[index], f"T{index}")
        for chunk in chunks[1:]:
            for index in chunk:
                self.assertEqual(out[index], "")


class TelemetryTests(unittest.TestCase):
    """§29/§31: truthful provenance and billed-character accounting."""

    def test_stats_report_provider_identity_and_model_types(self):
        client = translator(RecordingTransport(ok("T1", model_type_used="quality_optimized")))
        client.translate_many(["S1"])
        self.assertEqual(client.stats["provider_name"], "deepl")
        self.assertEqual(client.stats["provider_family"], "deepl")
        self.assertEqual(client.stats["model_type_requested"], "quality_optimized")
        self.assertEqual(client.stats["model_type_used"], "quality_optimized")

    def test_model_type_used_is_the_returned_value_not_the_request(self):
        client = translator(RecordingTransport(ok("T1", model_type_used="latency_optimized")))
        client.translate_many(["S1"])
        self.assertEqual(client.stats["model_type_requested"], "quality_optimized")
        self.assertEqual(client.stats["model_type_used"], "latency_optimized")

    def test_billed_characters_aggregate_across_chunks(self):
        sources = [f"{i:03d}" + "D" * 700 for i in range(4)]
        chunks = plan_chunks(sources, max_bytes=2200)
        responses = [
            ok(*[f"T{i}" for i in chunk], billed=[10] * len(chunk)) for chunk in chunks
        ]
        client = translator(RecordingTransport(*responses), max_request_bytes=2200)
        client.translate_many(sources)
        self.assertEqual(client.stats["provider_billed_characters"], 40)
        self.assertEqual(client.stats["provider_request_count"], len(chunks))

    def test_stats_never_contain_the_credential(self):
        client = translator(RecordingTransport(ok("T1")), api_key=TEST_KEY)
        client.translate_many(["S1"])
        blob = json.dumps(client.stats, default=str)
        self.assertNotIn(TEST_KEY, blob)
        self.assertNotIn("Authorization", blob)
        self.assertNotIn("DeepL-Auth-Key", blob)

    def test_usage_probe_reports_quota_without_exposing_the_key(self):
        transport = RecordingTransport(
            (200, json.dumps({"character_count": 1000, "character_limit": 500000})))
        report = translator_deepl.usage(
            api_key=TEST_KEY, base_url="https://api-free.deepl.com", transport=transport)
        self.assertEqual(report["character_remaining"], 499000)
        self.assertNotIn(TEST_KEY, json.dumps(report))


class FactoryTests(unittest.TestCase):
    """§20: canonical provider resolution."""

    def test_deepl_resolves_the_deepl_adapter(self):
        from translator_nllb import get_translator

        with mock.patch.object(config, "DEEPL_API_KEY", TEST_KEY):
            client, ocr_code = get_translator("3", translation_provider="deepl")
        self.assertIsInstance(client, DeepLTranslator)
        self.assertEqual(ocr_code, "eng")
        self.assertEqual(client.stats["provider_name"], "deepl")

    def test_deepl_gets_no_automatic_naturalization_pass(self):
        from translator_nllb import get_translator

        with mock.patch.object(config, "DEEPL_API_KEY", TEST_KEY), \
                mock.patch.object(config, "PTBR_NATURALIZATION_MODE", "selective"):
            client, _ = get_translator("3", translation_provider="deepl")
        self.assertIsNone(client.ptbr_naturalizer)
        self.assertFalse(client.stats["naturalization_enabled"])

    def test_an_unknown_provider_is_still_invalid(self):
        from translator_nllb import get_translator

        with self.assertRaises(ValueError):
            get_translator("3", translation_provider="not-a-provider")

    def test_the_canonical_provider_set_is_exactly_these_three(self):
        self.assertEqual(ui_helpers.TRANSLATION_PROVIDERS,
                         frozenset({"nemotron", "riva", "deepl"}))

    def test_the_runtime_default_is_deepl_and_the_nvidia_selector_is_scoped(self):
        # TDD #43: DeepL is the beta default; NVIDIA_TRANSLATION_PROVIDER now only
        # picks nemotron vs riva *inside* the NVIDIA family.
        self.assertEqual(ui_helpers.DEFAULT_TRANSLATION_PROVIDER, "deepl")
        self.assertEqual(config.NVIDIA_TRANSLATION_PROVIDER, "nemotron")

    def test_normalize_rejects_unknown_and_passes_known(self):
        self.assertEqual(ui_helpers.normalize_translation_provider(" DeepL "), "deepl")
        self.assertEqual(ui_helpers.normalize_translation_provider(None), "")
        with self.assertRaises(ValueError):
            ui_helpers.normalize_translation_provider("gemini")


class NoCrossProviderFallbackTests(unittest.TestCase):
    """RED #50: a DeepL job that fails stays a DeepL job."""

    def test_a_deepl_failure_never_produces_another_providers_effective_name(self):
        from benchmark_pipeline import resolve_provider_provenance

        client = translator(RecordingTransport((503, "down")))
        client.translate_many(["A"])
        provenance = resolve_provider_provenance(client, "deepl")
        self.assertEqual(provenance["provider_requested"], "deepl")
        self.assertEqual(provenance["provider_effective"], "deepl")
        self.assertFalse(provenance["provider_mismatch"])
        self.assertFalse(provenance["provider_fallback_used"])

    def test_an_nvidia_translator_against_a_deepl_request_fails_closed(self):
        from benchmark_pipeline import resolve_provider_provenance

        class _Riva:
            stats = {"provider_name": "riva", "model": "riva-x"}

        with self.assertRaises(RuntimeError):
            resolve_provider_provenance(_Riva(), "deepl")

    def test_the_deepl_module_names_no_other_provider_as_a_fallback(self):
        # Executable code only: docstrings and comments legitimately name the
        # other providers to explain the boundary, but nothing that *runs* may
        # reference one, nor the paid host.
        import ast

        tree = ast.parse((ROOT / "translator_deepl.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) and ast.get_docstring(node):
                node.body = node.body[1:] or [ast.Pass()]
        code = ast.unparse(tree).lower()
        for other in ("riva", "nemotron", "gemini", "nvidia", "api.deepl.com"):
            self.assertNotIn(other, code, other)


class ProvenanceTests(unittest.TestCase):
    """RED #48: deepl at every boundary, argv included."""

    def test_the_run_command_carries_the_canonical_provider_id(self):
        command = ui_helpers.build_run_command(
            url="https://www.webtoons.com/en/x/y/z/viewer?title_no=1&episode_no=51",
            output="ep51", mode="fast", max_images=1, full=False,
            use_cache=True, force=False, use_context=True,
            translation_provider="deepl",
        )
        self.assertIn("--translation-provider", command)
        self.assertEqual(ui_helpers.command_translation_provider(command), "deepl")
        self.assertNotIn("DeepL (Qualidade)", " ".join(command))

    def test_a_rebuild_that_drops_deepl_is_refused(self):
        configuration = {"provider_provenance": {"provider_requested": "deepl"}}
        with self.assertRaises(ValueError):
            ui_helpers.assert_command_provider(["python", "run_webtoon.py", "u"], configuration)

    def test_a_rebuild_that_swaps_deepl_for_riva_is_refused(self):
        configuration = {"provider_provenance": {"provider_requested": "deepl"}}
        with self.assertRaises(ValueError):
            ui_helpers.assert_command_provider(
                ["python", "run_webtoon.py", "u", "--translation-provider", "riva"],
                configuration)

    def test_a_matching_deepl_rebuild_passes(self):
        configuration = {"provider_provenance": {"provider_requested": "deepl"}}
        command = ["python", "run_webtoon.py", "u", "--translation-provider", "deepl"]
        self.assertEqual(ui_helpers.assert_command_provider(command, configuration), command)

    def test_the_cli_parser_accepts_deepl(self):
        from run_webtoon import build_parser

        args = build_parser().parse_args(["https://x/y", "--translation-provider", "deepl"])
        self.assertEqual(args.translation_provider, "deepl")

    def test_the_manifest_records_deepl_model_types_truthfully(self):
        from output_manifest import sanitize_provider_provenance

        record = sanitize_provider_provenance({
            "provider_requested": "deepl", "provider_effective": "deepl",
            "provider_model": "quality_optimized", "provider_family": "deepl",
            "provider_model_type_requested": "quality_optimized",
            "provider_model_type_used": "quality_optimized",
            "provider_source": "run_argument",
        })
        self.assertEqual(record["provider_requested"], "deepl")
        self.assertEqual(record["provider_family"], "deepl")
        self.assertEqual(record["provider_model_type_used"], "quality_optimized")
        self.assertFalse(record["provider_mismatch"])

    def test_the_manifest_never_carries_a_credential_shaped_value(self):
        from output_manifest import sanitize_provider_provenance

        record = sanitize_provider_provenance({
            "provider_requested": "deepl", "provider_effective": "deepl",
            "provider_model_type_used": f"DeepL-Auth-Key {TEST_KEY}",
        })
        self.assertNotIn("provider_model_type_used", record)

    def test_report_provenance_omits_model_types_for_the_nvidia_family(self):
        from benchmark_pipeline import report_provider_provenance

        record = report_provider_provenance(
            {"provider_requested": "riva", "provider_effective": "riva", "model": "riva-x"})
        self.assertNotIn("provider_model_type_requested", record)
        self.assertNotIn("provider_family", record)


class UiSelectionTests(unittest.TestCase):
    """§25/§26/§28: selectable, canonical, and not the default."""

    def test_the_shell_offers_deepl_with_the_canonical_value(self):
        shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        self.assertIn('<option value="deepl" selected>DeepL (Qualidade)</option>', shell)
        self.assertIn('<option value="riva">', shell)
        self.assertIn('<option value="nemotron">', shell)

    def test_deepl_is_the_preselected_default(self):
        shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        self.assertIn('<option value="deepl" selected', shell)
        self.assertNotIn('<option value="nemotron" selected', shell)

    def test_the_frontend_normalizer_accepts_deepl_and_rejects_labels(self):
        script = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn(
            "return ['riva', 'nemotron', 'deepl'].includes(provider) ? provider : '';",
            script)

    def test_the_backend_payload_normalizer_accepts_deepl(self):
        import ui_bridge

        self.assertEqual(
            ui_bridge.UiBridge._normalize_translation_provider(
                {"translation_provider": "deepl"}),
            "deepl")

    def test_a_label_string_is_refused_as_a_provider(self):
        import ui_bridge

        with self.assertRaises(ValueError):
            ui_bridge.UiBridge._normalize_translation_provider(
                {"translation_provider": "DeepL (Qualidade)"})

    def test_an_absent_selection_defaults_to_deepl(self):
        import ui_bridge

        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("NVIDIA_TRANSLATION_PROVIDER", None)
            self.assertEqual(
                ui_bridge.UiBridge._normalize_translation_provider({}), "deepl")


class OtherProvidersUnchangedTests(unittest.TestCase):
    """§37: adding DeepL must not have touched Riva or Nemotron."""

    def test_the_nvidia_translator_still_refuses_deepl(self):
        from translator_nvidia import TranslatorNvidiaBatch

        with self.assertRaises(ValueError):
            TranslatorNvidiaBatch(translation_provider="deepl")

    def test_riva_and_nemotron_batch_configuration_is_untouched(self):
        self.assertEqual(config.NVIDIA_TRANSLATION_BATCH_SIZE, 20)
        self.assertEqual(config.NVIDIA_RIVA_MAX_BATCH_ITEMS, 8)
        self.assertEqual(config.NVIDIA_RIVA_LOGICAL_REQUEST_ATTEMPT_LIMIT, 3)
        self.assertEqual(config.NVIDIA_TRANSPORT_RETRY_LIMIT, 4)

    def test_deepl_output_cannot_enter_riva_specific_recovery(self):
        # The Riva recovery paths are methods on TranslatorNvidiaBatch; DeepL's
        # adapter exposes none of them, so nothing can route into them.
        client = translator(RecordingTransport())
        for riva_only in ("_recover_riva_source_equal_subset",
                          "_recover_riva_missing_or_empty_subset",
                          "_recover_riva_text_if_needed",
                          "_translate_batch_riva",
                          "_request_json_with_retry"):
            self.assertFalse(hasattr(client, riva_only), riva_only)


class QualityPipelineTests(unittest.TestCase):
    """§32/§34: DeepL flows through the unmodified downstream quality path."""

    def test_source_equal_output_is_not_pre_trusted_by_the_provider(self):
        # The adapter returns exactly what DeepL said, including a candidate
        # identical to the source; classification belongs to the validators.
        transport = RecordingTransport(ok("HELLO", "OLÁ"))
        out = translator(transport).translate_many(["HELLO", "HELLO THERE"])
        self.assertEqual(out, ["HELLO", "OLÁ"])

    def test_the_adapter_performs_no_post_processing_of_candidates(self):
        transport = RecordingTransport(ok("  espaçado  "))
        self.assertEqual(translator(transport).translate_many(["x"]), ["  espaçado  "])


class ProviderErrorShapeTests(unittest.TestCase):
    def test_reason_codes_are_a_closed_documented_set(self):
        self.assertIn(QUOTA_EXCEEDED, translator_deepl.REASON_CODES)
        self.assertEqual(len(translator_deepl.REASON_CODES), 9)

    def test_public_error_view_is_scalar_and_sanitized(self):
        error = DeepLProviderError(QUOTA_EXCEEDED, status_code=456, detail="Quota exceeded")
        self.assertEqual(error.public(),
                         {"reason_code": QUOTA_EXCEEDED, "status_code": 456,
                          "detail": "Quota exceeded"})

    def test_unknown_status_codes_map_to_a_generic_http_error(self):
        self.assertEqual(translator_deepl.reason_for_status(418),
                         translator_deepl.PROVIDER_HTTP_ERROR)
        self.assertEqual(translator_deepl.reason_for_status(None), TRANSPORT_ERROR)


if __name__ == "__main__":
    unittest.main()
