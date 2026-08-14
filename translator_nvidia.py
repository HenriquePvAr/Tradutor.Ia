import json
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config
import semantic_fidelity
from pipeline_cache import atomic_write_json, load_json, stable_hash
from provider_transport import (
    CircuitBreakerPolicy,
    ProviderTimeoutPolicy,
    ProviderTransportError,
    circuit_scope_key,
    shared_circuit_breaker,
)


PROMPT_VERSION = "nvidia-manga-v4-json-qa-naturalization-signal"
RIVA_PROMPT_VERSION = "nvidia-riva-translate-v3-json-framing-ptbr-anti-echo"
TRANSLATION_CACHE_SCHEMA_VERSION = 3
_RIVA_ENGLISH_SIGNAL_TOKENS = {
    "A", "AN", "AND", "ARE", "BE", "BEING", "BUT", "CAN", "DID", "DIDN", "DO",
    "DON", "EVERY", "FOR", "GET", "GOING", "HAD", "HAS", "HAVE", "HERE", "HOME",
    "I", "IF", "IN", "INTO", "IS", "IT", "JUST", "KNOW", "LATE", "LIKE",
    "LONGER", "ME", "NEXT", "NOT", "OF", "REMEMBER", "RUN", "SHOULD", "STILL",
    "TAKEN", "THANK", "THAN", "THAT", "THE", "THERE", "THIS", "TIME", "TO",
    "TOOK", "TWO", "US", "WAS", "WAY", "WERE", "WHAT", "WHEN", "WHY", "WITH",
    "YOU", "YOUR",
}
_RIVA_PRESERVED_SFX_TOKENS = {
    "AH", "BAM", "BANG", "BOOM", "CRACK", "GASP", "GRR", "HA", "HAHA",
    "HUH", "MM", "MMM", "OOF", "OW", "SFX", "SLAM", "SNAP", "SWISH",
    "THUD", "UGH", "WHAM", "WHOOSH", "ZAP",
}
SYSTEM_PROMPT_TEMPLATE = """Traduzir textos de baloes de manga/manhwa de {source_language} para {target_language}.
Manter IDs iguais.
Nao juntar baloes.
Nao explicar.
Nao usar markdown.
Usar linguagem natural no idioma de destino.
Preservar emocao, gritos, pausas e tom dramatico.
Usar frases curtas para caber nos baloes.
Retornar somente JSON.
Para cada ID, pode retornar string simples ou objeto com:
translation, naturalization_needed, naturalization_reason.
naturalization_needed e apenas sinal de estilo, nunca autoridade semantica.
Use naturalization_needed=true somente quando a traducao estiver fiel mas soar
literal, travada ou pouco natural em portugues brasileiro.
Quando true, use naturalization_reason como um destes codigos compactos:
literal_translation, awkward_ptbr, style_refinement_needed."""


class TranslationResult(str):
    def __new__(cls, text, *, quality_evidence=None):
        obj = str.__new__(cls, str(text or ""))
        obj.quality_evidence = dict(quality_evidence or {})
        return obj


@dataclass
class NvidiaCredential:
    credential_id: str
    api_key: str
    enabled: bool = True
    in_flight: int = 0
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    rate_limited: bool = False


class NvidiaCredentialPool:
    """Small deterministic pool for authorized NVIDIA credentials.

    It exists to make future authorized multi-key benchmarking observable, not to
    manufacture capacity. With one key it behaves like the legacy transport.
    """

    def __init__(self, credentials, *, clock=None, cooldown_seconds=60.0):
        self._clock = clock or time.monotonic
        self.cooldown_seconds = max(0.0, float(cooldown_seconds or 0.0))
        self.credentials = [
            credential
            for credential in (credentials or [])
            if credential.enabled and str(credential.api_key or "").strip()
        ]
        self._lock = threading.Lock()
        self._last_credential_id = ""
        self.credential_switches = 0
        self.cooldown_events = 0

    @classmethod
    def from_config(cls, *, api_key=None, clock=None, cooldown_seconds=60.0):
        configured = _configured_nvidia_credentials(api_key)
        return cls(configured, clock=clock, cooldown_seconds=cooldown_seconds)

    @property
    def size(self):
        return len(self.credentials)

    def snapshot(self):
        with self._lock:
            now = self._clock()
            eligible = [
                item for item in self.credentials
                if item.enabled and item.cooldown_until <= now
            ]
            return {
                "pool_size": len(self.credentials),
                "eligible_credentials": len(eligible),
                "credential_switches": self.credential_switches,
                "credential_429_cooldowns": self.cooldown_events,
                "credentials": [
                    {
                        "credential_id": item.credential_id,
                        "enabled": item.enabled,
                        "in_flight": item.in_flight,
                        "cooldown_active": item.cooldown_until > now,
                        "consecutive_failures": item.consecutive_failures,
                        "rate_limited": item.rate_limited,
                    }
                    for item in self.credentials
                ],
            }

    def acquire(self):
        with self._lock:
            now = self._clock()
            eligible = [
                item for item in self.credentials
                if item.enabled and item.cooldown_until <= now
            ]
            if not eligible:
                raise ProviderTransportError("provider_credentials_unavailable")
            selected = min(eligible, key=lambda item: (item.in_flight, item.credential_id))
            if self._last_credential_id and selected.credential_id != self._last_credential_id:
                self.credential_switches += 1
            self._last_credential_id = selected.credential_id
            selected.in_flight += 1
            return selected

    def release_success(self, credential):
        with self._lock:
            credential.in_flight = max(0, credential.in_flight - 1)
            credential.consecutive_failures = 0
            credential.rate_limited = False

    def release_failure(self, credential, reason):
        with self._lock:
            credential.in_flight = max(0, credential.in_flight - 1)
            credential.consecutive_failures += 1
            if reason == "provider_rate_limited" and len(self.credentials) > 1:
                credential.rate_limited = True
                credential.cooldown_until = self._clock() + self.cooldown_seconds
                self.cooldown_events += 1


def _configured_nvidia_credentials(api_key=None):
    raw_registry = str(getattr(config, "NVIDIA_API_KEYS_JSON", "") or "").strip()
    credentials = []
    if raw_registry:
        try:
            parsed = json.loads(raw_registry)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            for index, item in enumerate(parsed, start=1):
                if isinstance(item, str):
                    key = item
                    enabled = True
                elif isinstance(item, dict):
                    key = item.get("api_key") or item.get("key") or ""
                    enabled = bool(item.get("enabled", True))
                else:
                    continue
                key = str(key or "").strip()
                if key and key != "sua_chave_aqui":
                    credentials.append(
                        NvidiaCredential(
                            credential_id=f"slot_{index}",
                            api_key=key,
                            enabled=enabled,
                        )
                    )
    legacy = api_key if api_key is not None else config.NVIDIA_API_KEY
    legacy = str(legacy or "").strip()
    if not credentials and legacy and legacy != "sua_chave_aqui":
        credentials.append(NvidiaCredential("slot_1", legacy, enabled=True))
    return credentials


class TranslatorNvidiaBatch:
    def __init__(
        self,
        api_key=None,
        base_url=None,
        model=None,
        batch_size=None,
        max_requests_per_minute=None,
        source_language="ingles",
        target_language="pt-BR",
        enable_cache=None,
        parallel=None,
        workers=None,
        force_cache=False,
        operation="translation",
        clock=None,
        sleeper=None,
        circuit_breaker=None,
        translation_provider=None,
        credential_pool=None,
    ):
        self.translation_provider = str(
            translation_provider or config.NVIDIA_TRANSLATION_PROVIDER or "nemotron"
        ).strip().lower()
        if self.translation_provider in {"", "nvidia"}:
            self.translation_provider = "nemotron"
        if self.translation_provider not in {"nemotron", "riva"}:
            raise ValueError("nvidia_translation_provider_invalid")
        self.credential_pool = credential_pool or NvidiaCredentialPool.from_config(
            api_key=api_key,
            clock=clock,
            cooldown_seconds=float(config.NVIDIA_RETRY_BACKOFF_SECONDS or 1.0) * 30,
        )
        first_credential = (
            self.credential_pool.credentials[0]
            if self.credential_pool.credentials else None
        )
        self.api_key = (
            first_credential.api_key
            if first_credential is not None
            else (api_key if api_key is not None else config.NVIDIA_API_KEY)
        )
        self.base_url = base_url or config.NVIDIA_BASE_URL
        default_model = (
            config.NVIDIA_RIVA_TRANSLATION_MODEL
            if self.translation_provider == "riva"
            else config.NVIDIA_TRANSLATION_MODEL
        )
        self.model = model or default_model
        self.batch_size = int(batch_size or config.NVIDIA_TRANSLATION_BATCH_SIZE or 20)
        if self.translation_provider == "riva":
            self.batch_size = max(
                1,
                min(
                    self.batch_size,
                    int(getattr(config, "NVIDIA_RIVA_MAX_BATCH_ITEMS", 8) or 8),
                ),
            )
        self.max_requests_per_minute = int(
            max_requests_per_minute or config.NVIDIA_MAX_REQUESTS_PER_MINUTE or 20
        )
        self.source_language = source_language
        self.target_language = str(target_language or "pt-BR")
        self.enable_cache = (
            config.ENABLE_TRANSLATION_CACHE if enable_cache is None else bool(enable_cache)
        )
        self.parallel = config.TRANSLATION_PARALLEL if parallel is None else bool(parallel)
        self.workers = max(
            1,
            int(config.TRANSLATION_WORKERS if workers is None else workers),
        )
        self.force_cache = bool(force_cache)
        self.operation = str(operation or "translation")
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self.timeout_policy = ProviderTimeoutPolicy(
            connect_timeout_seconds=float(config.NVIDIA_CONNECT_TIMEOUT_SECONDS),
            read_timeout_seconds=float(config.NVIDIA_READ_TIMEOUT_SECONDS),
            write_timeout_seconds=float(config.NVIDIA_WRITE_TIMEOUT_SECONDS),
            pool_timeout_seconds=float(config.NVIDIA_POOL_TIMEOUT_SECONDS),
            total_timeout_seconds=float(config.NVIDIA_TOTAL_TIMEOUT_SECONDS),
            source="config",
            provider="nvidia",
            operation=self.operation,
        )
        counted = (
            "provider_connect_timeout", "provider_read_timeout",
            "provider_total_deadline_exceeded", "provider_connection_error",
            "provider_server_error", "provider_unavailable",
        )
        ignored = (
            "provider_rate_limited", "provider_client_error",
            "provider_response_invalid", "provider_schema_invalid",
        )
        self.circuit_policy = CircuitBreakerPolicy(
            failure_threshold=int(config.NVIDIA_CIRCUIT_FAILURE_THRESHOLD),
            recovery_timeout_seconds=float(config.NVIDIA_CIRCUIT_RECOVERY_SECONDS),
            half_open_max_calls=int(config.NVIDIA_CIRCUIT_HALF_OPEN_MAX_CALLS),
            success_threshold=int(config.NVIDIA_CIRCUIT_SUCCESS_THRESHOLD),
            counted_failure_types=counted,
            ignored_failure_types=ignored,
        )
        scope = circuit_scope_key("nvidia", self.base_url, self.operation)
        self.circuit_breaker = circuit_breaker or shared_circuit_breaker(
            scope, self.circuit_policy)
        self.transport_retry_limit = int(config.NVIDIA_TRANSPORT_RETRY_LIMIT)
        self.json_retry_limit = int(config.NVIDIA_JSON_RETRY_LIMIT)
        self.retry_backoff_seconds = float(config.NVIDIA_RETRY_BACKOFF_SECONDS)
        self.riva_logical_request_attempt_limit = int(
            getattr(config, "NVIDIA_RIVA_LOGICAL_REQUEST_ATTEMPT_LIMIT", 3) or 3
        )
        self.riva_logical_batch_timeout_seconds = float(
            getattr(config, "NVIDIA_RIVA_LOGICAL_BATCH_TIMEOUT_SECONDS", 60.0) or 60.0
        )
        self.riva_format_retry_limit = max(
            0, int(getattr(config, "NVIDIA_RIVA_FORMAT_RETRY_LIMIT", 1) or 0)
        )
        if (
            self.transport_retry_limit <= 0
            or self.json_retry_limit <= 0
            or self.retry_backoff_seconds < 0
        ):
            raise ValueError("provider_retry_policy_invalid")
        self.session_context_prompt = ""
        self.session_context_signature = ""
        self.detected_names_prompt = ""
        self.detected_names_signature = ""
        self._thread_local = threading.local()
        self._request_times = deque()
        self._rate_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._telemetry_lock = threading.Lock()
        self._request_sequence = 0
        self._logical_call_metadata = {}
        self.stats = {
            "input_texts": 0,
            "provider_name": self.translation_provider,
            "model": self.model,
            "language_pair": self._riva_language_pair(),
            "credential_pool_size": self.credential_pool.size,
            "eligible_credentials": self.credential_pool.snapshot()["eligible_credentials"],
            "credential_switches": 0,
            "credential_429_cooldowns": 0,
            "active_real_credentials": self.credential_pool.size,
            "cache_hits": 0,
            "api_texts": 0,
            "api_requests": 0,
            "failed_batches": 0,
            "successful_batches": 0,
            "translation_configuration_missing": 0,
            "translation_batches": 0,
            "translation_responses_received": 0,
            "translation_responses_nonempty": 0,
            "translation_results_parsed": 0,
            "translation_results_associated": 0,
            "parallel_requested": self.parallel,
            "parallel_used": False,
            "workers": self.workers,
            "api_seconds": 0.0,
            "context_enabled": False,
            "invalid_json_retries": 0,
            "invalid_json_failures": 0,
            "detected_name_count": 0,
            "transport_attempts": 0,
            "provider_rate_limited_count": 0,
            "provider_timeout_count": 0,
            "provider_error_count": 0,
            "last_credential_id": "",
            "json_repair_attempts": 0,
            "circuit_rejections": 0,
            "last_transport_reason": "",
            "riva_max_batch_items": int(getattr(config, "NVIDIA_RIVA_MAX_BATCH_ITEMS", 8) or 8),
            "riva_max_batch_source_tokens": int(getattr(config, "NVIDIA_RIVA_MAX_BATCH_SOURCE_TOKENS", 600) or 600),
            "riva_output_token_floor": int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_FLOOR", 96) or 96),
            "riva_output_token_ceiling": int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_CEILING", 2048) or 2048),
            "riva_logical_request_attempt_limit": self.riva_logical_request_attempt_limit,
            "riva_logical_batch_timeout_seconds": self.riva_logical_batch_timeout_seconds,
            "riva_source_equal_detected": 0,
            "riva_source_equal_legitimate": 0,
            "riva_residual_english_detected": 0,
            "riva_corrective_retry_requested": 0,
            "riva_corrective_retry_succeeded": 0,
            "riva_corrective_retry_failed": 0,
            "riva_untranslated_blocked": 0,
            # Attempt conservation: logical_batches + every named corrective call
            # must reconcile with provider_http_attempts.
            "logical_batches": 0,
            "logical_calls_by_origin": {},
            "provider_http_attempts": 0,
            "provider_attempts_by_kind": {},
            "format_failures_total": 0,
            "format_failure_truncated": 0,
            "format_failure_malformed": 0,
            "format_failure_wrong_schema": 0,
            "format_failure_missing_id": 0,
            "format_failure_extra_id": 0,
            "format_failure_duplicate_id": 0,
            "format_failure_empty_translation": 0,
            "format_recoverable_wrapper": 0,
            "format_retry_requests": 0,
            "format_retry_success": 0,
            "format_retry_failure": 0,
            "selective_recovery_requests": 0,
            "source_equal_recovery_requests": 0,
            "partial_residual_recovery_requests": 0,
            "finish_reason_length": 0,
            "retry_budget_exhausted": 0,
            "provider_request_telemetry": [],
        }

    @property
    def is_configured(self):
        return self.credential_pool.size > 0

    def translate(self, text):
        return self.translate_many([text])[0]

    def set_session_context(self, context_store):
        if context_store is None:
            self.session_context_prompt = ""
            self.session_context_signature = ""
        else:
            self.session_context_prompt = str(context_store.prompt_fragment() or "").strip()
            self.session_context_signature = str(context_store.signature() or "")
        self.stats["context_enabled"] = bool(self.session_context_prompt)

    def set_detected_names(self, names):
        normalized = sorted(
            {
                str(name or "").strip()
                for name in (names or [])
                if str(name or "").strip()
            },
            key=str.casefold,
        )
        self.detected_names_prompt = (
            "Nomes proprios detectados dinamicamente neste capitulo; "
            "preserve exatamente a grafia: " + ", ".join(normalized) + "."
            if normalized
            else ""
        )
        self.detected_names_signature = stable_hash(normalized) if normalized else ""
        self.stats["detected_name_count"] = len(normalized)

    def _proper_name_instruction(self, proper_names, allow_proper_names):
        """Name the spans the model may keep, never merely that names may exist.

        A vague 'keep proper names' lets the model elect its own candidate and then
        adapt it into the target language, which is how a source auxiliary becomes an
        invented character name. Only detected spans may survive, and an empty list
        means every single word must be translated.
        """
        spans = [str(span).strip() for span in (proper_names or []) if str(span).strip()]
        if not allow_proper_names or not spans:
            return (
                "NENHUMA palavra pode permanecer em "
                f"{self.source_language}: nao ha nomes proprios "
                "neste texto, entao traduza cada palavra. "
                "Preserve a pontuacao e qualquer censura (***). "
            )
        return (
            "Os unicos nomes proprios deste texto sao: "
            + ", ".join(spans)
            + ". Copie esses nomes exatamente como estao, sem traduzir, adaptar "
            "nem substituir por equivalentes. Nenhum outro token pode ser tratado "
            "como nome proprio: traduza todas as demais palavras. "
            "Preserve a pontuacao e qualquer censura (***). "
        )

    def _quality_retry_instruction(self, validation_reason):
        """One targeted sentence per rejection reason, never a generic re-ask.

        The reason code is our own closed vocabulary (produced by
        linguistic_triage.py), never raw OCR/user text, so building this
        sentence with an f-string carries no injection risk; the untrusted
        text (the balloon itself) still only ever reaches the model through
        the JSON-encoded payload below.
        """
        target = self._target_language_name()
        table = {
            "empty_translation":
                "A tentativa anterior veio vazia; produza uma traducao completa.",
            "source_language_residual":
                f"A tentativa anterior manteve o texto em {self.source_language} "
                f"quase sem traduzir; traduza tudo para {target}.",
            "candidate_equals_source":
                f"A tentativa anterior repetiu o texto original sem traduzir; "
                f"traduza de fato para {target}.",
            "no_target_language_orthography":
                f"A tentativa anterior nao soou como {target} natural; escreva "
                f"como um falante nativo de {target} escreveria, com gramatica correta.",
            "mixed_language_candidate":
                f"A tentativa anterior misturou {self.source_language} com {target}; "
                f"a saida deve ficar inteiramente em {target}, sem palavras residuais.",
            "suspicious_truncation":
                "A tentativa anterior ficou incompleta ou truncada; traduza novamente "
                "todo o conteudo da regiao, sem omitir nenhuma informacao, ideia, "
                "negacao, relacao, nome ou intencao. Use uma formulacao natural e "
                "economica apenas na escolha das palavras, nunca reduzindo ou "
                "resumindo o conteudo.",
            "possible_semantic_inversion":
                "A tentativa anterior pode ter invertido o sentido; preserve sujeito, "
                "objeto, negacao, tempo verbal e intencao do original.",
            "terminology_conflict":
                "A tentativa anterior usou termo inconsistente com o glossario/contexto "
                "ja estabelecidos neste capitulo; use a terminologia ja adotada.",
            "character_gender_conflict":
                "A tentativa anterior tratou um personagem com genero gramatical "
                "diferente do ja estabelecido neste capitulo; use exatamente o genero "
                "e as formas de tratamento listados no contexto de personagens.",
            "character_pronoun_conflict":
                "A tentativa anterior usou um pronome incompativel com o personagem ja "
                "identificado neste capitulo; use os pronomes listados no contexto de "
                "personagens.",
            # Semantic fidelity: the constraint that failed, never why we think so.
            semantic_fidelity.NEGATION_CHANGED:
                "A tentativa anterior alterou a negacao do original; preserve exatamente "
                "o que e negado e o que e afirmado.",
            semantic_fidelity.QUANTITY_CHANGED:
                "A tentativa anterior alterou um numero ou quantidade do original; "
                "preserve todos os valores numericos exatamente como no original.",
            semantic_fidelity.ENTITY_CHANGED:
                "A tentativa anterior removeu ou substituiu um nome proprio ja "
                "estabelecido neste capitulo; mantenha o nome exatamente como esta.",
            semantic_fidelity.ACTOR_RELATION_CHANGED:
                "A tentativa anterior pode ter trocado quem faz o que a quem; preserve "
                "exatamente o sujeito, o objeto e a direcao da acao.",
            semantic_fidelity.STATE_ACTION_CHANGED:
                "A tentativa anterior transformou uma acao ou decisao do original em um "
                "estado do personagem; preserve a intencao e a acao do original.",
            semantic_fidelity.MEANING_MISMATCH:
                "A tentativa anterior mudou o sentido do original; traduza preservando "
                "negacao, quantidades, nomes, relacoes e intencao.",
            semantic_fidelity.FIDELITY_UNCERTAIN:
                "Nao foi possivel confirmar que a tentativa anterior preserva o sentido "
                "do original; traduza de forma direta, preservando negacao, quantidades, "
                "nomes, relacoes e intencao.",
        }
        # A fidelity reason carries the offending facts after a colon; the
        # instruction is chosen by the code alone.
        code = str(validation_reason or "").split(":", 1)[0].strip()
        return table.get(code,
                          "Refaca a traducao evitando o mesmo problema da tentativa anterior.")

    def translate_strict(
        self,
        text,
        previous_translation="",
        validation_reason="",
        force=False,
        allow_proper_names=True,
        proper_names=None,
        retry_origin="quality_retry",
        retry_attempt=1,
    ):
        if not str(text).strip():
            return text
        if not self.is_configured:
            return text

        self._increment_stat("api_texts", 1)
        started = time.perf_counter()
        try:
            if self.translation_provider == "riva":
                return self._translate_strict_riva(
                    text,
                    validation_reason,
                    retry_origin=retry_origin,
                    retry_attempt=retry_attempt,
                )
            payload = {
                "BALAO_1": str(text),
                "traducao_rejeitada": str(previous_translation or ""),
                "motivo_rejeicao": str(validation_reason or ""),
            }
            constraint = semantic_fidelity.retry_constraint(validation_reason)
            if constraint:
                # The model is told which constraint failed, never any reasoning.
                payload["restricao"] = constraint
            parsed = self._request_json_with_retry(
                [
                    {
                        "role": "system",
                        "content": (
                            self._system_prompt()
                            + "\nRevisao estrita: traduza TODO texto de "
                            f"{self.source_language} para {self._target_language_name()}. "
                            + self._quality_retry_instruction(validation_reason) + " "
                            + self._proper_name_instruction(
                                proper_names,
                                allow_proper_names,
                            )
                            + "Se o texto for uma onomatopeia/SFX, "
                            "preserve. Retorne somente JSON."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "A traducao anterior foi rejeitada por controle de qualidade. "
                            "Refaca somente BALAO_1 em portugues natural e curto, sem "
                            "misturar ingles e portugues:\n"
                            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
                        ),
                    },
                ],
                expected_ids=["BALAO_1"],
                max_tokens=(
                    self._riva_max_tokens_for_texts([text])
                    if self.translation_provider == "riva" else None
                ),
                logical_batch_id=self._next_logical_batch_id(
                    "strict",
                    origin=retry_origin,
                    item_count=1,
                    reason=validation_reason,
                    attempt=retry_attempt,
                ),
                item_count=1,
                source_chars=len(str(text or "")),
                attempt_kind=retry_origin,
            )
            return parsed.get("BALAO_1", text) or text
        finally:
            self._increment_stat("api_seconds", time.perf_counter() - started)

    # Riva is a translation model, not an instruction-following one: it
    # translates every JSON string value it is given. The Nemotron strict
    # payload also carried traducao_rejeitada/motivo_rejeicao/restricao, so the
    # reply came back with those keys translated too, the expected-id check
    # rejected it as "IDs inesperados" on 100% of calls, and every quality retry
    # burned the whole logical budget for nothing. Only the source goes in the
    # payload here; the corrective guidance stays prose outside the JSON.
    _RIVA_STRICT_HINTS = {
        "empty_translation": "The previous attempt was empty; produce a complete translation.",
        "candidate_equals_source": "The previous attempt repeated the English source; do not repeat it.",
        "source_language_residual": "The previous attempt left English behind; translate everything.",
        "mixed_language_candidate": "The previous attempt mixed English and Portuguese; output only Portuguese.",
        "suspicious_truncation": "The previous attempt was incomplete; translate the whole text without omitting anything.",
        "no_target_language_orthography": "The previous attempt did not read as natural Brazilian Portuguese.",
    }

    def _riva_strict_hint(self, validation_reason):
        code = str(validation_reason or "").split(":", 1)[0].strip()
        return self._RIVA_STRICT_HINTS.get(code, "")

    def _translate_strict_riva(
        self,
        text,
        validation_reason,
        *,
        retry_origin="quality_retry",
        retry_attempt=1,
    ):
        text_id = "BALAO_1"
        hint = self._riva_strict_hint(validation_reason)
        parsed = self._request_json_with_retry(
            [
                {"role": "system", "content": self._riva_system_prompt()},
                {
                    "role": "user",
                    "content": (
                        "Translate every JSON string value from English to natural "
                        "Brazilian Portuguese. The previous translation was rejected by "
                        "quality control; translate the complete English meaning again. "
                        + (hint + " " if hint else "")
                        + "Preserve only proper names, codes, and ranks that appear "
                        "inside the sentence. Preserve all JSON keys exactly. Return "
                        "only a valid JSON object containing exactly this key: "
                        f"{text_id}. Do not add explanations, markdown, or extra keys.\n"
                        + self._riva_context_prompt()
                        + "\nJSON:\n"
                        + json.dumps(
                            {text_id: str(text)},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                },
            ],
            expected_ids=[text_id],
            max_tokens=self._riva_max_tokens_for_texts([text]),
            logical_batch_id=self._next_logical_batch_id(
                "strict",
                origin=retry_origin,
                item_count=1,
                reason=validation_reason,
                attempt=retry_attempt,
            ),
            item_count=1,
            source_chars=len(str(text or "")),
            attempt_kind=retry_origin,
        )
        return parsed.get(text_id, text) or text

    def translate_many(self, texts, force=None):
        if not texts:
            return []

        force = self.force_cache if force is None else bool(force)
        translations = list(texts)
        indexed_texts = [(idx, text) for idx, text in enumerate(texts) if str(text).strip()]
        self._increment_stat("input_texts", len(indexed_texts))

        if not indexed_texts:
            return translations

        if not self.is_configured:
            print("NVIDIA_API_KEY nao configurada. Mantendo textos originais.")
            self._increment_stat("failed_batches")
            self._increment_stat("translation_configuration_missing")
            self.stats["last_transport_reason"] = "provider_not_configured"
            return translations

        misses = []
        for original_idx, original_text in indexed_texts:
            cached = None if force else self._load_translation_cache(original_text)
            if cached is None:
                misses.append((original_idx, original_text))
                continue
            translations[original_idx] = cached
            self._increment_stat("cache_hits")

        batches = list(self._translation_batches(misses))
        self._increment_stat("translation_batches", len(batches))
        if not batches:
            return translations

        parallel_used = self.parallel and self.workers > 1 and len(batches) > 1
        if parallel_used:
            self.stats["parallel_used"] = True
            with ThreadPoolExecutor(max_workers=min(self.workers, len(batches))) as executor:
                batch_results = list(executor.map(self._translate_indexed_batch, batches))
        else:
            batch_results = [self._translate_indexed_batch(batch) for batch in batches]

        for batch, (batch_translations, succeeded) in zip(batches, batch_results):
            batch_indexes = [idx for idx, _ in batch]
            batch_texts = [text for _, text in batch]

            for original_idx, original_text, translated in zip(
                batch_indexes, batch_texts, batch_translations
            ):
                translated = self._postprocess_translation(original_text, translated)
                if succeeded and translated and str(translated).strip():
                    translations[original_idx] = translated
                    self._increment_stat("translation_results_associated")
                    self._save_translation_cache(original_text, translated)

        return translations

    def _translate_indexed_batch(self, batch):
        batch_texts = [text for _, text in batch]
        self._increment_stat("api_texts", len(batch_texts))
        started = time.perf_counter()
        try:
            translated = self._translate_batch(batch_texts)
            response_count = len(translated)
            nonempty_count = sum(1 for item in translated if str(item or "").strip())
            self._increment_stat("translation_responses_received", response_count)
            self._increment_stat("translation_responses_nonempty", nonempty_count)
            if response_count < len(batch_texts) or nonempty_count == 0:
                self._increment_stat("failed_batches")
                self.stats["last_transport_reason"] = "empty_translation_response"
                return batch_texts, False
            self._increment_stat("successful_batches")
            return translated, True
        except Exception as exc:
            self._increment_stat("failed_batches")
            print(f"Erro na NVIDIA API. Mantendo textos originais do lote: {exc}")
            return batch_texts, False
        finally:
            self._increment_stat("api_seconds", time.perf_counter() - started)

    def _get_client(self, *, remaining_total_seconds=None):
        from openai import OpenAI

        return OpenAI(
            api_key=getattr(self._thread_local, "api_key", self.api_key),
            base_url=self.base_url,
            timeout=self.timeout_policy.httpx_timeout(remaining_total_seconds),
        )

    def _translate_batch(self, texts):
        if self.translation_provider == "riva":
            return self._translate_batch_riva(texts)
        return self._translate_batch_nemotron(texts)

    def _translate_batch_nemotron(self, texts):
        ids = [f"BALAO_{idx}" for idx in range(1, len(texts) + 1)]
        payload = dict(zip(ids, texts))
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        parsed = self._request_json_with_retry(
            [
                {
                    "role": "system",
                    "content": self._system_prompt(),
                },
                {
                    "role": "user",
                    "content": (
                        f"Traduza estes textos para {self._target_language_name()} e retorne "
                        f"somente JSON com os mesmos IDs:\n{content}"
                    ),
                },
            ],
            expected_ids=ids,
        )
        return [parsed.get(text_id, original) or original for text_id, original in zip(ids, texts)]

    def _translate_batch_riva(self, texts):
        ids = [f"BALAO_{idx}" for idx in range(1, len(texts) + 1)]
        payload = dict(zip(ids, texts))
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        logical_batch_id = self._next_logical_batch_id(
            "riva",
            origin="initial_batch",
            item_count=len(texts),
            reason="initial_translation",
            attempt=1,
        )
        provider_budget = {
            "remaining": self._logical_provider_call_limit(),
            "limit": self._logical_provider_call_limit(),
        }
        parsed = self._request_json_with_retry(
            [
                {"role": "system", "content": self._riva_system_prompt()},
                {
                    "role": "user",
                    "content": (
                        "Translate every JSON string value from English to Brazilian "
                        "Portuguese. Preserve all JSON keys exactly. Return only a "
                        "valid JSON object with the same keys and translated string "
                        "values. Do not add explanations, markdown, or extra keys.\n"
                        + self._riva_context_prompt()
                        + "\nJSON:\n"
                        + content
                    ),
                },
            ],
            expected_ids=ids,
            max_tokens=self._riva_max_tokens_for_texts(texts),
            logical_batch_id=logical_batch_id,
            item_count=len(texts),
            source_chars=sum(len(str(text or "")) for text in texts),
            provider_budget=provider_budget,
        )
        parsed = self._recover_riva_missing_or_empty_subset(
            texts,
            ids,
            parsed,
            logical_batch_id=logical_batch_id,
            provider_budget=provider_budget,
        )
        parsed = self._recover_riva_source_equal_subset(
            texts,
            ids,
            parsed,
            logical_batch_id=logical_batch_id,
            provider_budget=provider_budget,
        )
        return [
            self._riva_translation_result(parsed.get(text_id, original), original)
            for text_id, original in zip(ids, texts)
        ]

    def _riva_translation_result(self, value, original):
        evidence = dict(getattr(value, "quality_evidence", {}) or {})
        evidence.update(
            {
                "translation_provider": "riva",
                "translation_model": self.model,
                "naturalization_eligibility_source": "riva_translation_only",
            }
        )
        return TranslationResult(str(value or original), quality_evidence=evidence)

    def _recover_riva_missing_or_empty_subset(
        self,
        texts,
        ids,
        parsed,
        *,
        logical_batch_id,
        provider_budget,
    ):
        if self.translation_provider != "riva":
            return parsed
        updated = dict(parsed)
        for text_id, original in zip(ids, texts):
            if text_id in updated and str(updated.get(text_id) or "").strip():
                continue
            if not provider_budget or provider_budget.get("remaining", 0) <= 0:
                self._increment_stat("riva_corrective_retry_failed")
                self._increment_stat("riva_untranslated_blocked")
                continue
            self._increment_stat("riva_corrective_retry_requested")
            self._increment_stat("selective_recovery_requests")
            corrective_payload = {text_id: str(original)}
            try:
                recovered = self._request_json_with_retry(
                    [
                        {"role": "system", "content": self._riva_system_prompt()},
                        {
                            "role": "user",
                            "content": (
                                "Translate this single JSON string value from English "
                                "to natural Brazilian Portuguese. The previous batch "
                                "reply omitted this item or returned it empty. Preserve "
                                "the JSON key exactly. Return only a valid JSON object "
                                f"containing exactly this key: {text_id}.\n"
                                + self._riva_context_prompt()
                                + "\nJSON:\n"
                                + json.dumps(
                                    corrective_payload,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            ),
                        },
                    ],
                    expected_ids=[text_id],
                    max_tokens=self._riva_max_tokens_for_texts([original]),
                    logical_batch_id=logical_batch_id,
                    item_count=1,
                    source_chars=len(str(original or "")),
                    provider_budget=provider_budget,
                    attempt_kind="selective_recovery",
                )
            except Exception:
                self._increment_stat("riva_corrective_retry_failed")
                self._increment_stat("riva_untranslated_blocked")
                continue
            new_value = str(recovered.get(text_id, "") or "")
            if new_value.strip():
                updated[text_id] = TranslationResult(
                    new_value,
                    quality_evidence={
                        "riva_corrective_retry": True,
                        "riva_corrective_retry_reason": "missing_or_empty_json_item",
                    },
                )
                self._increment_stat("riva_corrective_retry_succeeded")
            else:
                self._increment_stat("riva_corrective_retry_failed")
                self._increment_stat("riva_untranslated_blocked")
        return updated

    def _recover_riva_source_equal_subset(
        self,
        texts,
        ids,
        parsed,
        *,
        logical_batch_id,
        provider_budget,
    ):
        if self.translation_provider != "riva":
            return parsed
        corrective = []
        for text_id, original in zip(ids, texts):
            candidate = str(parsed.get(text_id, "") or "")
            source_equal = self._riva_is_source_equal(original, candidate)
            residual_english = (
                not source_equal
                and self._riva_untranslated_output_recovery_eligible(original, candidate)
            )
            if not (source_equal or residual_english):
                continue
            if source_equal:
                self._increment_stat("riva_source_equal_detected")
                if not self._riva_source_equal_recovery_eligible(original):
                    self._increment_stat("riva_source_equal_legitimate")
                    continue
            else:
                self._increment_stat("riva_residual_english_detected")
            corrective.append((text_id, original, candidate))

        if not corrective:
            return parsed
        if not provider_budget or provider_budget.get("remaining", 0) <= 0:
            self._increment_stat("riva_untranslated_blocked", len(corrective))
            return parsed

        updated = dict(parsed)
        for text_id, original, candidate in corrective:
            if not provider_budget or provider_budget.get("remaining", 0) <= 0:
                self._increment_stat("riva_corrective_retry_failed")
                self._increment_stat("riva_untranslated_blocked")
                continue
            self._increment_stat("riva_corrective_retry_requested")
            self._increment_stat("selective_recovery_requests")
            if self._riva_is_source_equal(original, candidate):
                self._increment_stat("source_equal_recovery_requests")
                attempt_kind = "source_equal_recovery"
            else:
                self._increment_stat("partial_residual_recovery_requests")
                attempt_kind = "partial_residual_recovery"
            corrective_payload = {text_id: str(original)}
            try:
                recovered = self._request_json_with_retry(
                [
                    {
                        "role": "system",
                        "content": self._riva_system_prompt(),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Translate every JSON string value from English to "
                            "natural Brazilian Portuguese. These inputs are ordinary "
                            "translatable dialogue or narration that previously kept "
                            "English unchanged or partially untranslated. Translate the "
                            "complete English meaning, including stuttered or hyphenated "
                            "words. Do not repeat the English source. "
                            "Preserve only proper names, codes, and ranks that appear "
                            "inside the sentence; do not treat ordinary interjections or "
                            "stuttered words as names or SFX. "
                            "Preserve all JSON keys exactly. Return only a valid JSON "
                            "object with the same keys and translated string values. "
                            f"The output object must contain exactly this key: {text_id}. "
                            "Do not add explanations, markdown, or extra keys.\n"
                            + self._riva_context_prompt()
                            + "\nJSON:\n"
                            + json.dumps(corrective_payload, ensure_ascii=False, separators=(",", ":"))
                        ),
                    },
                ],
                expected_ids=[text_id],
                max_tokens=self._riva_max_tokens_for_texts([original]),
                logical_batch_id=logical_batch_id,
                item_count=1,
                source_chars=len(str(original or "")),
                provider_budget=provider_budget,
                attempt_kind=attempt_kind,
            )
            except Exception:
                self._increment_stat("riva_corrective_retry_failed")
                self._increment_stat("riva_untranslated_blocked")
                continue
            new_value = str(recovered.get(text_id, "") or "")
            if (
                new_value.strip()
                and not self._riva_is_source_equal(original, new_value)
                and not self._riva_untranslated_output_recovery_eligible(original, new_value)
            ):
                updated[text_id] = TranslationResult(
                    new_value,
                    quality_evidence={
                        "riva_corrective_retry": True,
                        "riva_corrective_retry_reason": (
                            "source_equal_english"
                            if self._riva_is_source_equal(original, candidate)
                            else "residual_english"
                        ),
                    },
                )
                self._increment_stat("riva_corrective_retry_succeeded")
            else:
                self._increment_stat("riva_corrective_retry_failed")
                self._increment_stat("riva_untranslated_blocked")
        return updated

    def _request_json_with_retry(
        self,
        messages,
        expected_ids,
        attempts=None,
        *,
        max_tokens=None,
        logical_batch_id="",
        item_count=0,
        source_chars=0,
        provider_budget=None,
        attempt_kind="initial",
    ):
        """Retry model-format failures separately from HTTP/transport retries."""

        expected_ids = [str(item) for item in expected_ids]
        base_messages = [dict(message) for message in messages]
        request_messages = list(base_messages)
        last_error = None
        if attempts is None:
            attempts = (
                1 + self.riva_format_retry_limit
                if self.translation_provider == "riva"
                else self.json_retry_limit
            )
        else:
            attempts = int(attempts)
        effective_max_tokens = max_tokens
        deadline_seconds = self.timeout_policy.total_timeout_seconds
        if self.translation_provider == "riva":
            deadline_seconds = min(
                deadline_seconds,
                max(1.0, self.riva_logical_batch_timeout_seconds),
            )
        deadline = self._clock() + deadline_seconds
        if provider_budget is None:
            provider_budget = {
                "remaining": self._logical_provider_call_limit(),
                "limit": self._logical_provider_call_limit(),
            }
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response_text = self._call_request_with_retry(
                    request_messages,
                    deadline=deadline,
                    max_tokens=effective_max_tokens,
                    provider_budget=provider_budget,
                    logical_batch_id=logical_batch_id,
                    logical_attempt=attempt,
                    item_count=item_count,
                    source_chars=source_chars,
                    attempt_kind=attempt_kind,
                )
            except ProviderTransportError as exc:
                # A reply cut off by the output budget is a format failure, not a
                # transport failure: repeating it with the same budget can only
                # truncate again, so the one allowed retry raises the budget.
                if exc.reason_code != "provider_response_truncated":
                    raise
                self._record_format_failure("truncated")
                self._increment_stat("finish_reason_length")
                if (
                    attempt >= attempts
                    or (provider_budget is not None and provider_budget["remaining"] <= 0)
                ):
                    self._increment_stat("format_retry_failure")
                    raise
                effective_max_tokens = self._escalated_max_tokens(
                    effective_max_tokens, request_messages)
                self._increment_stat("format_retry_requests")
                continue
            try:
                parsed = self._parse_json_response(response_text)
                duplicates = self._duplicate_ids(response_text)
                if duplicates:
                    raise ValueError(
                        "duplicate_id: Resposta JSON com IDs repetidos: "
                        + ", ".join(duplicates)
                    )
                extra = sorted(set(parsed) - set(expected_ids))
                if extra:
                    raise ValueError(
                        "extra_id: Resposta JSON contem IDs inesperados: "
                        + ", ".join(extra)
                    )
                absent = [text_id for text_id in expected_ids if text_id not in parsed]
                if absent:
                    if self.translation_provider == "riva":
                        self._record_format_failure("missing_id")
                        self._mark_latest_request_parse(True, False)
                        return parsed
                    raise ValueError(
                        "missing_id: Resposta JSON sem IDs obrigatorios: "
                        + ", ".join(absent)
                    )
                empty = [
                    text_id for text_id in expected_ids
                    if not str(parsed.get(text_id) or "").strip()
                ]
                if empty:
                    if self.translation_provider == "riva":
                        self._record_format_failure("empty_translation")
                        self._mark_latest_request_parse(True, False)
                        return parsed
                    raise ValueError(
                        "empty_translation: Resposta JSON sem traducao para: "
                        + ", ".join(empty)
                    )
                self._increment_stat("translation_results_parsed", len(parsed))
                self._mark_latest_request_parse(True, True)
                if not str(response_text or "").strip().startswith(("{", "[")):
                    self._increment_stat("format_recoverable_wrapper")
                if attempt > 1:
                    self._increment_stat("format_retry_success")
                return parsed
            except (TypeError, ValueError) as exc:
                last_error = exc
                self._mark_latest_request_parse(False, False)
                self._record_format_failure(self._format_failure_class(exc))
                if attempt >= attempts:
                    self._increment_stat("invalid_json_failures")
                    if attempt > 1:
                        self._increment_stat("format_retry_failure")
                    raise
                if provider_budget["remaining"] <= 0:
                    self._increment_stat("invalid_json_failures")
                    self._increment_stat("retry_budget_exhausted")
                    raise ProviderTransportError("provider_logical_attempt_budget_exhausted") from exc
                self._increment_stat("invalid_json_retries")
                self._increment_stat("json_repair_attempts")
                self._increment_stat("format_retry_requests")
                request_messages = base_messages + [
                    {
                        "role": "assistant",
                        "content": str(response_text or "")[:2000],
                    },
                    {
                        "role": "user",
                        "content": (
                            "A resposta anterior nao era um objeto JSON valido com "
                            "todos os IDs solicitados. Responda novamente somente com "
                            "JSON, sem markdown ou explicacoes. IDs obrigatorios: "
                            + ", ".join(expected_ids)
                        ),
                    },
                ]
        raise last_error or ValueError("Resposta JSON invalida.")

    def _call_request_with_retry(self, messages, **kwargs):
        """Call the transport layer while keeping older hermetic test doubles valid."""
        try:
            return self._request_with_retry(messages, **kwargs)
        except TypeError as exc:
            text = str(exc)
            extended = (
                "max_tokens",
                "provider_budget",
                "logical_batch_id",
                "logical_attempt",
                "item_count",
                "source_chars",
                "attempt_kind",
            )
            if "unexpected keyword argument" not in text or not any(
                f"'{name}'" in text or f'"{name}"' in text for name in extended
            ):
                raise
            legacy_kwargs = {
                "deadline": kwargs.get("deadline"),
                "response_format": kwargs.get("response_format"),
            }
            return self._request_with_retry(messages, **legacy_kwargs)

    def _request_with_retry(
        self,
        messages,
        *,
        deadline=None,
        response_format=None,
        max_tokens=None,
        provider_budget=None,
        logical_batch_id="",
        logical_attempt=1,
        item_count=0,
        source_chars=0,
        attempt_kind="initial",
    ):
        retry_statuses = {429, 500, 502, 503, 504}
        logical_kind = attempt_kind if logical_attempt == 1 else "format_retry"
        last_error = None
        deadline = (
            self._clock() + self.timeout_policy.total_timeout_seconds
            if deadline is None else float(deadline)
        )
        for attempt in range(1, max(1, self.transport_retry_limit) + 1):
            credential = None
            request_id = ""
            request_started = self._clock()
            effective_max_tokens = (
                int(max_tokens)
                if max_tokens is not None
                else self._default_max_tokens_for_messages(messages)
            )
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ProviderTransportError("provider_total_deadline_exceeded")
            if provider_budget is not None and provider_budget["remaining"] <= 0:
                raise ProviderTransportError("provider_logical_attempt_budget_exhausted")
            try:
                self.circuit_breaker.before_call()
            except ProviderTransportError:
                self._increment_stat("circuit_rejections")
                raise
            try:
                self._wait_for_rate_limit(deadline=deadline)
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise ProviderTransportError(
                        "provider_total_deadline_exceeded")
                credential = self.credential_pool.acquire()
                self._thread_local.api_key = credential.api_key
                self._thread_local.credential_id = credential.credential_id
                self.stats["last_credential_id"] = credential.credential_id
                if provider_budget is not None:
                    provider_budget["remaining"] -= 1
                self._increment_stat("api_requests")
                self._increment_stat("transport_attempts")
                request_id = self._next_request_id()
                request_started = self._clock()
                request_kwargs = dict(
                    model=self.model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=effective_max_tokens,
                )
                if response_format is not None:
                    request_kwargs["response_format"] = response_format
                client = self._get_client(remaining_total_seconds=remaining)
                completion = client.chat.completions.create(**request_kwargs)
                choice = completion.choices[0]
                content = choice.message.content or ""
                finish_reason = str(getattr(choice, "finish_reason", "") or "")
                usage = getattr(completion, "usage", None)
                generated_tokens = getattr(usage, "completion_tokens", None)
                self._record_request_telemetry(
                    request_id=request_id,
                    logical_batch_id=logical_batch_id,
                    attempt=attempt,
                    logical_attempt=logical_attempt,
                    attempt_reason=(
                        logical_kind if attempt == 1 else "transport_retry"),
                    item_count=item_count,
                    source_chars=source_chars,
                    messages=messages,
                    max_tokens=effective_max_tokens,
                    credential_id=credential.credential_id,
                    started_at=request_started,
                    finished_at=self._clock(),
                    http_status=200,
                    finish_reason=finish_reason,
                    response_chars=len(content),
                    generated_tokens=generated_tokens,
                    parse_success=None,
                    mapping_success=None,
                    retry_scheduled=False,
                    retry_reason="",
                )
                if finish_reason == "length":
                    raise ProviderTransportError("provider_response_truncated")
                if self._clock() > deadline:
                    raise ProviderTransportError(
                        "provider_total_deadline_exceeded")
                self.circuit_breaker.record_success()
                self.credential_pool.release_success(credential)
                self._refresh_pool_stats()
                return content
            except Exception as exc:
                last_error = exc
                status = self._status_code_from_exception(exc)
                reason = self._transport_reason(exc, status)
                self.stats["last_transport_reason"] = reason
                if credential is not None:
                    self.credential_pool.release_failure(credential, reason)
                    self._refresh_pool_stats()
                    self._record_request_telemetry(
                        request_id=locals().get("request_id", self._next_request_id()),
                        logical_batch_id=logical_batch_id,
                        attempt=attempt,
                        logical_attempt=logical_attempt,
                        attempt_reason=(
                            logical_kind if attempt == 1 else "transport_retry"),
                        item_count=item_count,
                        source_chars=source_chars,
                        messages=messages,
                        max_tokens=locals().get(
                            "effective_max_tokens",
                            self._default_max_tokens_for_messages(messages),
                        ),
                        credential_id=credential.credential_id,
                        started_at=locals().get("request_started", self._clock()),
                        finished_at=self._clock(),
                        http_status=status,
                        finish_reason="",
                        response_chars=0,
                        generated_tokens=None,
                        parse_success=False,
                        mapping_success=False,
                        retry_scheduled=False,
                        retry_reason=reason,
                    )
                if reason == "provider_rate_limited":
                    self._increment_stat("provider_rate_limited_count")
                elif reason in {"provider_connect_timeout", "provider_read_timeout", "provider_total_deadline_exceeded"}:
                    self._increment_stat("provider_timeout_count")
                else:
                    self._increment_stat("provider_error_count")
                self.circuit_breaker.record_failure(reason)
                retryable = (
                    status in retry_statuses
                    or reason in {
                        "provider_connect_timeout", "provider_read_timeout",
                        "provider_connection_error", "provider_server_error",
                        "provider_rate_limited",
                    }
                )
                if not retryable or attempt >= self.transport_retry_limit:
                    if isinstance(exc, ProviderTransportError):
                        raise
                    raise ProviderTransportError(
                        "provider_retry_exhausted" if retryable else reason,
                        status_code=status) from exc
                retry_after = self._retry_after_seconds(exc) if status == 429 else None
                backoff = (
                    retry_after if retry_after is not None
                    else min(self.retry_backoff_seconds * (2 ** (attempt - 1)), 30.0)
                )
                if self._clock() + backoff >= deadline:
                    raise ProviderTransportError(
                        "provider_total_deadline_exceeded") from exc
                if provider_budget is not None and provider_budget["remaining"] <= 0:
                    raise ProviderTransportError(
                        "provider_logical_attempt_budget_exhausted") from exc
                if self.stats["provider_request_telemetry"]:
                    self.stats["provider_request_telemetry"][-1]["retry_scheduled"] = True
                    self.stats["provider_request_telemetry"][-1]["retry_reason"] = reason
                self._sleep(backoff)

        raise last_error

    def _wait_for_rate_limit(self, *, deadline=None):
        if self.max_requests_per_minute <= 0:
            return

        while True:
            with self._rate_lock:
                now = self._clock()
                while self._request_times and now - self._request_times[0] >= 60:
                    self._request_times.popleft()

                if len(self._request_times) < self.max_requests_per_minute:
                    self._request_times.append(now)
                    return

                sleep_for = 60 - (now - self._request_times[0]) + 0.1
            if sleep_for > 0:
                if deadline is not None and self._clock() + sleep_for >= deadline:
                    raise ProviderTransportError(
                        "provider_total_deadline_exceeded")
                self._sleep(sleep_for)

    @staticmethod
    def _transport_reason(exc, status):
        if isinstance(exc, ProviderTransportError):
            return exc.reason_code
        name = type(exc).__name__.casefold()
        text = str(exc).casefold()
        if "connecttimeout" in name or ("connect" in text and "timeout" in text):
            return "provider_connect_timeout"
        if "readtimeout" in name or ("read" in text and "timeout" in text):
            return "provider_read_timeout"
        if "timeout" in name:
            return "provider_read_timeout"
        if "connection" in name or "connecterror" in name:
            return "provider_connection_error"
        if status == 429:
            return "provider_rate_limited"
        if status in {500, 502, 503, 504}:
            return "provider_server_error"
        if status and 400 <= status < 500:
            return "provider_client_error"
        return "provider_unavailable"

    @staticmethod
    def _retry_after_seconds(exc):
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        raw = headers.get("retry-after") or headers.get("Retry-After")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if 0 <= value <= 300 else None

    def _translation_cache_key(self, text):
        return stable_hash(
            {
                "cache_schema_version": TRANSLATION_CACHE_SCHEMA_VERSION,
                "prompt_version": self._prompt_version(),
                "translation_provider": self.translation_provider,
                "text": self._normalized_cache_source(text),
                "model": self.model,
                "source_language": self.source_language,
                "target_language": self.target_language,
                "session_context": self.session_context_signature,
                "detected_names": self.detected_names_signature,
            }
        )

    def _system_prompt(self):
        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            source_language=self.source_language,
            target_language=self._target_language_name(),
        )
        if self.session_context_prompt:
            prompt += "\n\n" + self.session_context_prompt
        if self.detected_names_prompt:
            prompt += "\n\n" + self.detected_names_prompt
        return prompt

    def _translation_cache_path(self, text):
        folder = Path(config.CACHE_ROOT).resolve() / "translations"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self._translation_cache_key(text)}.json"

    def _load_translation_cache(self, text):
        if not self.enable_cache:
            return None
        key = self._translation_cache_key(text)
        payload = load_json(self._translation_cache_path(text))
        if (
            payload.get("key") != key
            or payload.get("cache_schema_version")
            != TRANSLATION_CACHE_SCHEMA_VERSION
            or payload.get("prompt_version") != self._prompt_version()
            or payload.get("translation_provider", "nemotron") != self.translation_provider
            or payload.get("model") != self.model
            or payload.get("source_language") != self.source_language
            or payload.get("target_language") != self.target_language
            or payload.get("normalized_source")
            != self._normalized_cache_source(text)
        ):
            return None
        translation = payload.get("translation")
        if not translation:
            return None
        return TranslationResult(
            str(translation).strip(),
            quality_evidence=payload.get("quality_evidence") or {},
        )

    def _save_translation_cache(self, text, translation):
        if not self.enable_cache:
            return
        key = self._translation_cache_key(text)
        atomic_write_json(
            self._translation_cache_path(text),
            {
                "key": key,
                "cache_schema_version": TRANSLATION_CACHE_SCHEMA_VERSION,
                "prompt_version": self._prompt_version(),
                "translation_provider": self.translation_provider,
                "model": self.model,
                "source_language": self.source_language,
                "target_language": self.target_language,
                "normalized_source": self._normalized_cache_source(text),
                "text": str(text),
                "translation": str(translation),
                "quality_evidence": dict(getattr(translation, "quality_evidence", {}) or {}),
            },
        )

    def _target_language_name(self):
        if self.target_language.casefold() in {"pt-br", "pt_br", "português", "portugues"}:
            return "portugues do Brasil"
        return self.target_language

    def _prompt_version(self):
        return RIVA_PROMPT_VERSION if self.translation_provider == "riva" else PROMPT_VERSION

    def _riva_language_pair(self):
        source = "en" if str(self.source_language).casefold() in {"ingles", "english", "en"} else str(self.source_language)
        target = "pt-BR" if self.target_language.casefold() in {"pt-br", "pt_br", "portugues", "português"} else self.target_language
        return f"{source}-{target}"

    def _riva_system_prompt(self):
        pair = self._riva_language_pair()
        return (
            f"{pair}\n"
            "You are a professional comic/manhwa dialogue translator. Translate "
            "English source text into natural Brazilian Portuguese. Never echo "
            "ordinary English dialogue or narration unchanged. Preserve only "
            "proper names, ranks/codes, and true SFX/onomatopoeia when they are "
            "not meant to be translated. Return translations only through the "
            "requested JSON contract."
        )

    def _riva_context_prompt(self):
        fragments = []
        if self.session_context_prompt:
            fragments.append(
                "Use this bounded chapter context for terminology and character consistency only; "
                "do not translate or explain the context itself:\n" + self.session_context_prompt
            )
        if self.detected_names_prompt:
            fragments.append(self.detected_names_prompt)
        if not fragments:
            return ""
        return "\n".join(fragments)

    def _refresh_pool_stats(self):
        snapshot = self.credential_pool.snapshot()
        self.stats["credential_pool_size"] = snapshot["pool_size"]
        self.stats["eligible_credentials"] = snapshot["eligible_credentials"]
        self.stats["credential_switches"] = snapshot["credential_switches"]
        self.stats["credential_429_cooldowns"] = snapshot["credential_429_cooldowns"]

    @staticmethod
    def _normalized_cache_source(text):
        normalized = unicodedata.normalize("NFC", str(text or ""))
        return re.sub(r"\s+", " ", normalized).strip()

    def _increment_stat(self, name, value=1):
        with self._stats_lock:
            self.stats[name] = self.stats.get(name, 0) + value

    @staticmethod
    def _status_code_from_exception(exc):
        status = getattr(exc, "status_code", None)
        if status is not None:
            return status
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None)

    @staticmethod
    def _parse_json_response(text):
        cleaned = TranslatorNvidiaBatch._remove_markdown_fence(text)
        parsed = TranslatorNvidiaBatch._loads_json(cleaned)

        if isinstance(parsed, dict):
            if isinstance(parsed.get("translations"), list):
                mapped = TranslatorNvidiaBatch._list_to_mapping(parsed["translations"])
                if parsed["translations"] and not mapped:
                    raise ValueError("wrong_schema: Resposta JSON em schema invalido.")
                return mapped
            return {
                str(key): TranslatorNvidiaBatch._translation_result_from_value(value)
                for key, value in parsed.items()
            }

        if isinstance(parsed, list):
            mapped = TranslatorNvidiaBatch._list_to_mapping(parsed)
            if parsed and not mapped:
                raise ValueError("wrong_schema: Resposta JSON em schema invalido.")
            return mapped

        raise ValueError("Resposta JSON da NVIDIA em formato inesperado.")

    @staticmethod
    def _remove_markdown_fence(text):
        cleaned = (text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            cleaned = fenced.group(1).strip()
        return cleaned

    @staticmethod
    def _loads_json(text, *, object_pairs_hook=None):
        candidates = [text]

        obj_start, obj_end = text.find("{"), text.rfind("}")
        if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
            candidates.append(text[obj_start : obj_end + 1])

        arr_start, arr_end = text.find("["), text.rfind("]")
        if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
            candidates.append(text[arr_start : arr_end + 1])

        for candidate in candidates:
            try:
                return json.loads(candidate, object_pairs_hook=object_pairs_hook)
            except json.JSONDecodeError:
                continue

        raise ValueError("Resposta da NVIDIA nao contem JSON valido.")

    @staticmethod
    def _list_to_mapping(items):
        mapping = {}
        for item in items:
            if not isinstance(item, dict):
                continue

            text_id = item.get("id") or item.get("ID") or item.get("Id")
            translation = (
                item.get("translation")
                or item.get("traducao")
                or item.get("translated_text")
                or item.get("text")
            )

            if text_id and translation:
                mapping[str(text_id)] = TranslatorNvidiaBatch._translation_result_from_value(item)

        return mapping

    @staticmethod
    def _translation_result_from_value(value):
        if not isinstance(value, dict):
            return TranslationResult(str(value or ""))
        translation = (
            value.get("translation")
            or value.get("traducao")
            or value.get("translated_text")
            or value.get("text")
            or ""
        )
        evidence = {}
        raw_needed = value.get("naturalization_needed")
        if isinstance(raw_needed, str):
            needed = raw_needed.strip().lower() in {"1", "true", "yes", "sim"}
        else:
            needed = bool(raw_needed)
        reason = str(value.get("naturalization_reason") or "").strip()
        if needed:
            evidence["naturalization_needed"] = True
            evidence["ptbr_naturalization_needed"] = True
            evidence["naturalization_eligibility_source"] = "provider_structured_metadata"
            evidence["naturalization_reason"] = reason or "style_refinement_needed"
            if reason in {
                "naturalization_needed",
                "style_refinement_needed",
                "literal_translation",
                "literalness",
                "awkward_ptbr",
            }:
                evidence[
                    "literal_translation" if reason == "literalness" else reason
                ] = True
            elif reason == "register":
                evidence["style_refinement_needed"] = True
        else:
            evidence["naturalization_needed"] = False
            evidence["ptbr_naturalization_needed"] = False
            evidence["naturalization_eligibility_source"] = "provider_structured_metadata"
            if reason:
                evidence["naturalization_reason"] = reason
        nested = value.get("quality_evidence")
        if isinstance(nested, dict):
            evidence.update(nested)
        return TranslationResult(str(translation or ""), quality_evidence=evidence)

    @staticmethod
    def _chunks(items, size):
        size = max(1, int(size or 20))
        for start in range(0, len(items), size):
            yield items[start : start + size]

    def _translation_batches(self, items):
        if self.translation_provider != "riva":
            yield from self._chunks(items, self.batch_size)
            return
        max_items = max(1, int(getattr(config, "NVIDIA_RIVA_MAX_BATCH_ITEMS", 8) or 8))
        max_source_tokens = max(
            1,
            int(getattr(config, "NVIDIA_RIVA_MAX_BATCH_SOURCE_TOKENS", 600) or 600),
        )
        batch = []
        token_total = 0
        for item in items:
            item_tokens = self._approx_tokens(str(item[1] if isinstance(item, tuple) else item))
            if batch and (
                len(batch) >= min(self.batch_size, max_items)
                or token_total + item_tokens > max_source_tokens
            ):
                yield batch
                batch = []
                token_total = 0
            batch.append(item)
            token_total += item_tokens
        if batch:
            yield batch

    def _riva_output_token_ceiling(self):
        floor = max(1, int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_FLOOR", 96) or 96))
        return max(floor, int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_CEILING", 2048) or 2048))

    def _riva_max_tokens_for_texts(self, texts):
        """Budget the RESPONSE, not the source.

        Every item costs its key, quotes, escaping and a comma whatever the
        source length, so a batch of eight two-word balloons still needs far
        more than the old source-driven floor. Each item gets its own floor and
        the envelope gets a fixed allowance on top.
        """
        texts = list(texts or [])
        floor = max(1, int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_FLOOR", 96) or 96))
        ceiling = self._riva_output_token_ceiling()
        ratio = max(1.0, float(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_RATIO", 2.4) or 2.4))
        overhead = max(0, int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_ITEM_OVERHEAD", 12) or 12))
        item_floor = max(
            1, int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_PER_ITEM_FLOOR", 24) or 24))
        envelope = 8
        estimate = envelope + sum(
            overhead + max(item_floor, math.ceil(self._approx_tokens(text) * ratio))
            for text in texts
        )
        return min(ceiling, max(floor, estimate))

    def _escalated_max_tokens(self, current, messages):
        base = int(current or self._default_max_tokens_for_messages(messages))
        if self.translation_provider != "riva":
            return base
        return min(self._riva_output_token_ceiling(), max(base + 1, base * 2))

    def _record_format_failure(self, kind):
        self._increment_stat("format_failures_total")
        self._increment_stat(f"format_failure_{kind}")

    @staticmethod
    def _format_failure_class(exc):
        text = str(exc)
        folded = text.casefold()
        for prefix in (
            "duplicate_id", "missing_id", "empty_translation", "extra_id",
            "wrong_schema",
        ):
            if text.startswith(prefix + ":"):
                return prefix
        if "formato inesperado" in folded:
            return "wrong_schema"
        return "malformed"

    @classmethod
    def _duplicate_ids(cls, text):
        """Detect repeated keys json.loads would silently collapse."""
        seen = []

        def hook(pairs):
            seen.extend(key for key, _ in pairs)
            return dict(pairs)

        try:
            cls._loads_json(cls._remove_markdown_fence(text), object_pairs_hook=hook)
        except (TypeError, ValueError):
            return []
        counts = {}
        for key in seen:
            counts[key] = counts.get(key, 0) + 1
        return sorted(key for key, count in counts.items() if count > 1)

    def _default_max_tokens_for_messages(self, messages):
        if self.translation_provider == "riva":
            # Fallback for call sites that do not have source texts separated.
            user_chars = sum(
                len(str(message.get("content") or ""))
                for message in (messages or [])
                if message.get("role") == "user"
            )
            return min(
                int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_CEILING", 512) or 512),
                max(
                    int(getattr(config, "NVIDIA_RIVA_OUTPUT_TOKEN_FLOOR", 96) or 96),
                    math.ceil(self._approx_tokens(user_chars) * 1.2),
                ),
            )
        return 4096

    def _logical_provider_call_limit(self):
        if self.translation_provider == "riva":
            return max(1, int(self.riva_logical_request_attempt_limit or 1))
        return max(1, int(self.transport_retry_limit) * int(self.json_retry_limit))

    @classmethod
    def _riva_is_source_equal(cls, source, candidate):
        return (
            cls._riva_normalized_source_equal_text(source)
            == cls._riva_normalized_source_equal_text(candidate)
        )

    @staticmethod
    def _riva_normalized_source_equal_text(text):
        normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
        normalized = re.sub(r"[\u2018\u2019]", "'", normalized)
        normalized = re.sub(r"[\u201c\u201d]", '"', normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip()

    @staticmethod
    def _riva_ascii_tokens(text):
        return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", str(text or ""))

    @classmethod
    def _riva_source_equal_recovery_eligible(cls, source):
        text = str(source or "").strip()
        if not text:
            return False
        tokens = cls._riva_ascii_tokens(text)
        if not tokens:
            return False
        compact = re.sub(r"[^A-Za-z0-9]+", "", text).upper()
        token_upper = [re.sub(r"[^A-Za-z]+", "", token).upper() for token in tokens]
        token_upper = [token for token in token_upper if token]
        if not token_upper:
            return False
        if cls._riva_looks_like_code_or_rank(text, compact, token_upper):
            return False
        if cls._riva_looks_like_preserved_sfx(compact, token_upper):
            return False
        if cls._riva_looks_like_name_or_entity_only(text, token_upper):
            return False
        return cls._riva_likely_ordinary_english(token_upper)

    @classmethod
    def _riva_untranslated_output_recovery_eligible(cls, source, candidate):
        if not cls._riva_source_equal_recovery_eligible(source):
            return False
        candidate_tokens = [
            re.sub(r"[^A-Za-z]+", "", token).upper()
            for token in cls._riva_ascii_tokens(candidate)
        ]
        candidate_tokens = [token for token in candidate_tokens if token]
        if not candidate_tokens:
            return False
        english_hits = [
            token for token in candidate_tokens
            if token in _RIVA_ENGLISH_SIGNAL_TOKENS
        ]
        if len(english_hits) >= 3:
            return True
        source_token_set = {
            re.sub(r"[^A-Za-z]+", "", token).upper()
            for token in cls._riva_ascii_tokens(source)
        }
        source_token_set.discard("")
        overlap = [token for token in english_hits if token in source_token_set]
        return len(overlap) >= 2 and len(candidate_tokens) >= 4

    @staticmethod
    def _riva_looks_like_code_or_rank(text, compact, token_upper):
        if re.fullmatch(r"[A-Z]-RANK", str(text or "").strip().upper()):
            return True
        if re.fullmatch(r"[A-Z]{1,4}[-_]?[0-9]+", compact):
            return True
        if re.fullmatch(r"[A-Z0-9]{1,4}", compact) and any(ch.isdigit() for ch in compact):
            return True
        return False

    @staticmethod
    def _riva_looks_like_preserved_sfx(compact, token_upper):
        if bool(getattr(config, "TRANSLATE_SFX", False)):
            return False
        if compact in _RIVA_PRESERVED_SFX_TOKENS:
            return True
        return (
            len(token_upper) <= 2
            and all(token in _RIVA_PRESERVED_SFX_TOKENS for token in token_upper)
        )

    @staticmethod
    def _riva_looks_like_name_or_entity_only(text, token_upper):
        stripped = re.sub(r"^[.\s]+|[.\s!?,;:]+$", "", str(text or "")).strip()
        if not stripped:
            return False
        if len(token_upper) == 1:
            token = token_upper[0]
            original_letters = "".join(ch for ch in stripped if ch.isalpha())
            if token not in _RIVA_ENGLISH_SIGNAL_TOKENS:
                return True
            if (
                original_letters
                and not original_letters.isupper()
                and not original_letters.islower()
                and token not in {"I"}
            ):
                return True
        if len(token_upper) <= 3:
            entity_titles = {"MR", "MRS", "MS", "MISS", "SIR", "LADY", "LORD"}
            if token_upper[0] in entity_titles:
                return True
            if all(token not in _RIVA_ENGLISH_SIGNAL_TOKENS for token in token_upper):
                return True
        return False

    @staticmethod
    def _riva_likely_ordinary_english(token_upper):
        hits = sum(1 for token in token_upper if token in _RIVA_ENGLISH_SIGNAL_TOKENS)
        if hits >= 2:
            return True
        if hits == 1 and len(token_upper) <= 3:
            return True
        contraction_markers = {"DIDN", "DON", "I", "I'LL", "I'M", "YOU'RE"}
        return any(token in contraction_markers for token in token_upper)

    def _next_request_id(self):
        with self._telemetry_lock:
            self._request_sequence += 1
            return f"nvidia_req_{self._request_sequence:06d}"

    def _next_logical_batch_id(
        self,
        prefix,
        *,
        origin="initial_batch",
        parent_id="",
        item_count=0,
        reason="",
        attempt=1,
    ):
        self._increment_stat("logical_batches")
        with self._telemetry_lock:
            self._request_sequence += 1
            logical_call_id = f"{prefix}_batch_{self._request_sequence:06d}"
            normalized_origin = str(origin or "initial_batch")
            self._logical_call_metadata[logical_call_id] = {
                "logical_call_id": logical_call_id,
                "logical_call_origin": normalized_origin,
                "logical_call_parent_id": str(parent_id or ""),
                "logical_call_item_count": int(item_count or 0),
                "logical_call_reason": str(reason or ""),
                "logical_call_attempt": int(attempt or 1),
            }
        with self._stats_lock:
            origins = self.stats.setdefault("logical_calls_by_origin", {})
            origins[normalized_origin] = int(origins.get(normalized_origin) or 0) + 1
        return logical_call_id

    def _count_attempt_kind(self, kind):
        with self._stats_lock:
            kinds = self.stats.setdefault("provider_attempts_by_kind", {})
            kinds[kind] = kinds.get(kind, 0) + 1

    def _record_request_telemetry(
        self,
        *,
        request_id,
        logical_batch_id,
        attempt,
        logical_attempt,
        attempt_reason,
        item_count,
        source_chars,
        messages,
        max_tokens,
        credential_id,
        started_at,
        finished_at,
        http_status,
        finish_reason,
        response_chars,
        generated_tokens,
        parse_success,
        mapping_success,
        retry_scheduled,
        retry_reason,
    ):
        input_chars = sum(len(str(message.get("content") or "")) for message in (messages or []))
        logical_meta = dict(self._logical_call_metadata.get(logical_batch_id) or {})
        logical_origin = logical_meta.get("logical_call_origin") or {
            "initial": "initial_batch",
            "format_retry": "format_recovery",
            "source_equal_recovery": "source_equal_recovery",
            "partial_residual_recovery": "partial_residual_recovery",
            "selective_recovery": "selective_recovery",
            "transport_retry": "transport_retry",
        }.get(str(attempt_reason or ""), str(attempt_reason or "initial_batch"))
        entry = {
            "request_id": request_id,
            "logical_batch_id": logical_batch_id,
            "logical_call_id": logical_meta.get("logical_call_id", logical_batch_id),
            "logical_call_origin": logical_origin,
            "logical_call_parent_id": logical_meta.get("logical_call_parent_id", ""),
            "logical_call_item_count": int(
                logical_meta.get("logical_call_item_count", item_count) or 0
            ),
            "logical_call_reason": logical_meta.get("logical_call_reason", ""),
            "logical_call_attempt": int(
                logical_meta.get("logical_call_attempt", logical_attempt) or 1
            ),
            "provider": self.translation_provider,
            "model": self.model,
            "attempt": attempt,
            "logical_attempt": logical_attempt,
            "attempt_reason": attempt_reason,
            "item_count": int(item_count or 0),
            "source_chars": int(source_chars or 0),
            "prompt_chars": input_chars,
            "approx_input_tokens": self._approx_tokens(input_chars),
            "max_tokens": int(max_tokens or 0),
            "credential_slot": str(credential_id or ""),
            "started_at": float(started_at),
            "finished_at": float(finished_at),
            "latency_ms": int(max(0.0, float(finished_at) - float(started_at)) * 1000),
            "http_status": http_status,
            "finish_reason": str(finish_reason or ""),
            "response_chars": int(response_chars or 0),
            "generated_tokens": generated_tokens,
            "parse_success": parse_success,
            "mapping_success": mapping_success,
            "retry_scheduled": bool(retry_scheduled),
            "retry_reason": str(retry_reason or ""),
        }
        with self._stats_lock:
            self.stats["provider_http_attempts"] = (
                int(self.stats.get("provider_http_attempts") or 0) + 1
            )
            kinds = self.stats.setdefault("provider_attempts_by_kind", {})
            kinds[attempt_reason] = int(kinds.get(attempt_reason) or 0) + 1
            telemetry = self.stats.setdefault("provider_request_telemetry", [])
            telemetry.append(entry)
            if len(telemetry) > 500:
                del telemetry[:-500]

    def _mark_latest_request_parse(self, parse_success, mapping_success):
        with self._stats_lock:
            telemetry = self.stats.get("provider_request_telemetry") or []
            if telemetry:
                telemetry[-1]["parse_success"] = bool(parse_success)
                telemetry[-1]["mapping_success"] = bool(mapping_success)

    @staticmethod
    def _approx_tokens(value):
        if isinstance(value, (int, float)):
            chars = int(value)
        else:
            chars = len(str(value or ""))
        return max(1, math.ceil(chars / 4))

    @staticmethod
    def _postprocess_translation(original_text, translated):
        evidence = dict(getattr(translated, "quality_evidence", {}) or {})
        return TranslationResult(str(translated or ""), quality_evidence=evidence)
