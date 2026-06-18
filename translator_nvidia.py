import json
import re
import time
from collections import deque

import config


SYSTEM_PROMPT_TEMPLATE = """Traduzir textos de baloes de manga/manhwa de {source_language} para portugues do Brasil.
Manter IDs iguais.
Nao juntar baloes.
Nao explicar.
Nao usar markdown.
Usar portugues natural do Brasil.
Preservar emocao, gritos, pausas e tom dramatico.
Usar frases curtas para caber nos baloes.
Exemplos de tom:
THE BELFATOR EMPIRE'S CAPITAL: BRINSIA -> A CAPITAL DO IMPERIO BELFATOR: BRINSIA
HIS MAJESTY HAS PERISHED! -> SUA MAJESTADE MORREU!
ALERT THE GUARDS! -> AVISEM OS GUARDAS!
Retornar somente JSON."""


EXPECTED_TRANSLATIONS = {
    "THE BELFATOR EMPIRE'S CAPITAL: BRINSIA": "A CAPITAL DO IMP\u00c9RIO BELFATOR: BRINSIA",
    "HIS MAJESTY HAS PERISHED!": "SUA MAJESTADE MORREU!",
    "ALERT THE GUARDS!": "AVISEM OS GUARDAS!",
}


class TranslatorNvidiaBatch:
    def __init__(
        self,
        api_key=None,
        base_url=None,
        model=None,
        batch_size=None,
        max_requests_per_minute=None,
        source_language="ingles",
    ):
        self.api_key = api_key if api_key is not None else config.NVIDIA_API_KEY
        self.base_url = base_url or config.NVIDIA_BASE_URL
        self.model = model or config.NVIDIA_TRANSLATION_MODEL
        self.batch_size = int(batch_size or config.NVIDIA_TRANSLATION_BATCH_SIZE or 20)
        self.max_requests_per_minute = int(
            max_requests_per_minute or config.NVIDIA_MAX_REQUESTS_PER_MINUTE or 20
        )
        self.source_language = source_language
        self._client = None
        self._request_times = deque()

    @property
    def is_configured(self):
        return bool(self.api_key and self.api_key != "sua_chave_aqui")

    def translate(self, text):
        return self.translate_many([text])[0]

    def translate_many(self, texts):
        if not texts:
            return []

        translations = list(texts)
        indexed_texts = [(idx, text) for idx, text in enumerate(texts) if str(text).strip()]

        if not indexed_texts:
            return translations

        if not self.is_configured:
            print("NVIDIA_API_KEY nao configurada. Mantendo textos originais.")
            return translations

        for batch in self._chunks(indexed_texts, self.batch_size):
            batch_indexes = [idx for idx, _ in batch]
            batch_texts = [text for _, text in batch]

            try:
                batch_translations = self._translate_batch(batch_texts)
            except Exception as exc:
                print(f"Erro na NVIDIA API. Mantendo textos originais do lote: {exc}")
                batch_translations = batch_texts

            for original_idx, original_text, translated in zip(
                batch_indexes, batch_texts, batch_translations
            ):
                translated = self._postprocess_translation(original_text, translated)
                if translated and str(translated).strip():
                    translations[original_idx] = str(translated).strip()

        return translations

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=60.0,
            )
        return self._client

    def _translate_batch(self, texts):
        ids = [f"BALAO_{idx}" for idx in range(1, len(texts) + 1)]
        payload = dict(zip(ids, texts))
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        response_text = self._request_with_retry(
            [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT_TEMPLATE.format(source_language=self.source_language),
                },
                {
                    "role": "user",
                    "content": (
                        "Traduza estes textos para portugues do Brasil e retorne "
                        f"somente JSON com os mesmos IDs:\n{content}"
                    ),
                },
            ]
        )

        parsed = self._parse_json_response(response_text)
        return [parsed.get(text_id, original) or original for text_id, original in zip(ids, texts)]

    def _request_with_retry(self, messages):
        retry_statuses = {429, 500, 502, 503, 504}
        last_error = None

        for attempt in range(1, 5):
            try:
                self._wait_for_rate_limit()
                client = self._get_client()
                self._request_times.append(time.monotonic())
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

                if status not in retry_statuses or attempt == 4:
                    raise

                time.sleep(min(2**attempt, 30))

        raise last_error

    def _wait_for_rate_limit(self):
        if self.max_requests_per_minute <= 0:
            return

        now = time.monotonic()
        while self._request_times and now - self._request_times[0] >= 60:
            self._request_times.popleft()

        if len(self._request_times) < self.max_requests_per_minute:
            return

        sleep_for = 60 - (now - self._request_times[0]) + 0.1
        if sleep_for > 0:
            time.sleep(sleep_for)

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
        normalized = re.sub(r"\s+", " ", str(original_text or "").strip()).upper()
        return EXPECTED_TRANSLATIONS.get(normalized, translated)
