import json
import re
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config
from pipeline_cache import atomic_write_json, load_json, stable_hash


PROMPT_VERSION = "nvidia-manga-v3-json-qa"
TRANSLATION_CACHE_SCHEMA_VERSION = 2
SYSTEM_PROMPT_TEMPLATE = """Traduzir textos de baloes de manga/manhwa de {source_language} para {target_language}.
Manter IDs iguais.
Nao juntar baloes.
Nao explicar.
Nao usar markdown.
Usar linguagem natural no idioma de destino.
Preservar emocao, gritos, pausas e tom dramatico.
Usar frases curtas para caber nos baloes.
Retornar somente JSON."""

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
    ):
        self.api_key = api_key if api_key is not None else config.NVIDIA_API_KEY
        self.base_url = base_url or config.NVIDIA_BASE_URL
        self.model = model or config.NVIDIA_TRANSLATION_MODEL
        self.batch_size = int(batch_size or config.NVIDIA_TRANSLATION_BATCH_SIZE or 20)
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
        self.session_context_prompt = ""
        self.session_context_signature = ""
        self.detected_names_prompt = ""
        self.detected_names_signature = ""
        self._thread_local = threading.local()
        self._request_times = deque()
        self._rate_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.stats = {
            "input_texts": 0,
            "cache_hits": 0,
            "api_texts": 0,
            "api_requests": 0,
            "failed_batches": 0,
            "parallel_requested": self.parallel,
            "parallel_used": False,
            "workers": self.workers,
            "api_seconds": 0.0,
            "context_enabled": False,
            "invalid_json_retries": 0,
            "invalid_json_failures": 0,
            "detected_name_count": 0,
        }

    @property
    def is_configured(self):
        return bool(self.api_key and self.api_key != "sua_chave_aqui")

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

    def translate_strict(
        self,
        text,
        previous_translation="",
        validation_reason="",
        force=False,
        allow_proper_names=True,
    ):
        if not str(text).strip():
            return text
        if not self.is_configured:
            return text

        self._increment_stat("api_texts", 1)
        started = time.perf_counter()
        try:
            payload = {
                "BALAO_1": str(text),
                "traducao_rejeitada": str(previous_translation or ""),
                "motivo_rejeicao": str(validation_reason or ""),
            }
            parsed = self._request_json_with_retry(
                [
                    {
                        "role": "system",
                        "content": (
                            self._system_prompt()
                            + "\nRevisao estrita: traduza TODO texto de "
                            f"{self.source_language} para {self._target_language_name()}. "
                            + (
                                "Nao deixe palavras/frases no idioma de origem, "
                                "exceto nomes proprios. "
                                if allow_proper_names
                                else (
                                    "NENHUMA palavra pode permanecer em "
                                    f"{self.source_language}: nao ha nomes proprios "
                                    "neste texto, entao traduza cada palavra. "
                                    "Preserve a pontuacao e qualquer censura (***). "
                                )
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
            )
            return parsed.get("BALAO_1", text) or text
        finally:
            self._increment_stat("api_seconds", time.perf_counter() - started)

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
            return translations

        misses = []
        for original_idx, original_text in indexed_texts:
            cached = None if force else self._load_translation_cache(original_text)
            if cached is None:
                misses.append((original_idx, original_text))
                continue
            translations[original_idx] = cached
            self._increment_stat("cache_hits")

        batches = list(self._chunks(misses, self.batch_size))
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
                if translated and str(translated).strip():
                    translations[original_idx] = str(translated).strip()
                    if succeeded:
                        self._save_translation_cache(original_text, translations[original_idx])

        return translations

    def _translate_indexed_batch(self, batch):
        batch_texts = [text for _, text in batch]
        self._increment_stat("api_texts", len(batch_texts))
        started = time.perf_counter()
        try:
            translated = self._translate_batch(batch_texts)
            return translated, True
        except Exception as exc:
            self._increment_stat("failed_batches")
            print(f"Erro na NVIDIA API. Mantendo textos originais do lote: {exc}")
            return batch_texts, False
        finally:
            self._increment_stat("api_seconds", time.perf_counter() - started)

    def _get_client(self):
        client = getattr(self._thread_local, "client", None)
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=120.0,
            )
            self._thread_local.client = client
        return client

    def _translate_batch(self, texts):
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

    def _request_json_with_retry(self, messages, expected_ids, attempts=3):
        """Retry model-format failures separately from HTTP/transport retries."""

        expected_ids = [str(item) for item in expected_ids]
        base_messages = [dict(message) for message in messages]
        request_messages = list(base_messages)
        last_error = None
        for attempt in range(1, max(1, int(attempts)) + 1):
            response_text = self._request_with_retry(request_messages)
            try:
                parsed = self._parse_json_response(response_text)
                missing = [text_id for text_id in expected_ids if not parsed.get(text_id)]
                if missing:
                    raise ValueError(
                        "Resposta JSON sem IDs obrigatorios: " + ", ".join(missing)
                    )
                return parsed
            except (TypeError, ValueError) as exc:
                last_error = exc
                if attempt >= attempts:
                    self._increment_stat("invalid_json_failures")
                    raise
                self._increment_stat("invalid_json_retries")
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

    def _request_with_retry(self, messages):
        retry_statuses = {429, 500, 502, 503, 504}
        last_error = None

        for attempt in range(1, 5):
            try:
                self._wait_for_rate_limit()
                client = self._get_client()
                self._increment_stat("api_requests")
                completion = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=4096,
                )
                return completion.choices[0].message.content or ""
            except Exception as exc:
                last_error = exc
                status = self._status_code_from_exception(exc)
                error_name = type(exc).__name__.lower()
                transient_transport_error = (
                    "timeout" in error_name or "connection" in error_name
                )

                if (
                    status not in retry_statuses
                    and not transient_transport_error
                ) or attempt == 4:
                    raise

                time.sleep(min(2**attempt, 30))

        raise last_error

    def _wait_for_rate_limit(self):
        if self.max_requests_per_minute <= 0:
            return

        while True:
            with self._rate_lock:
                now = time.monotonic()
                while self._request_times and now - self._request_times[0] >= 60:
                    self._request_times.popleft()

                if len(self._request_times) < self.max_requests_per_minute:
                    self._request_times.append(now)
                    return

                sleep_for = 60 - (now - self._request_times[0]) + 0.1
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _translation_cache_key(self, text):
        return stable_hash(
            {
                "cache_schema_version": TRANSLATION_CACHE_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
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
            or payload.get("prompt_version") != PROMPT_VERSION
            or payload.get("model") != self.model
            or payload.get("source_language") != self.source_language
            or payload.get("target_language") != self.target_language
            or payload.get("normalized_source")
            != self._normalized_cache_source(text)
        ):
            return None
        translation = payload.get("translation")
        return str(translation).strip() if translation else None

    def _save_translation_cache(self, text, translation):
        if not self.enable_cache:
            return
        key = self._translation_cache_key(text)
        atomic_write_json(
            self._translation_cache_path(text),
            {
                "key": key,
                "cache_schema_version": TRANSLATION_CACHE_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "model": self.model,
                "source_language": self.source_language,
                "target_language": self.target_language,
                "normalized_source": self._normalized_cache_source(text),
                "text": str(text),
                "translation": str(translation),
            },
        )

    def _target_language_name(self):
        if self.target_language.casefold() in {"pt-br", "pt_br", "português", "portugues"}:
            return "portugues do Brasil"
        return self.target_language

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
                return TranslatorNvidiaBatch._list_to_mapping(parsed["translations"])
            return {str(key): str(value) for key, value in parsed.items()}

        if isinstance(parsed, list):
            return TranslatorNvidiaBatch._list_to_mapping(parsed)

        raise ValueError("Resposta JSON da NVIDIA em formato inesperado.")

    @staticmethod
    def _remove_markdown_fence(text):
        cleaned = (text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            cleaned = fenced.group(1).strip()
        return cleaned

    @staticmethod
    def _loads_json(text):
        candidates = [text]

        obj_start, obj_end = text.find("{"), text.rfind("}")
        if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
            candidates.append(text[obj_start : obj_end + 1])

        arr_start, arr_end = text.find("["), text.rfind("]")
        if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
            candidates.append(text[arr_start : arr_end + 1])

        for candidate in candidates:
            try:
                return json.loads(candidate)
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
                mapping[str(text_id)] = str(translation)

        return mapping

    @staticmethod
    def _chunks(items, size):
        size = max(1, int(size or 20))
        for start in range(0, len(items), size):
            yield items[start : start + size]

    @staticmethod
    def _postprocess_translation(original_text, translated):
        return translated
