import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ocr_balloon import COMMON_ENGLISH_WORDS, SFX_WORDS
from ocr_engine import COMMON_ENGLISH_WORDS as OCR_ENGLISH_WORDS
from pipeline_cache import atomic_write_json, load_json, stable_hash


CONTEXT_VERSION = "chapter-session-v2"
TRANSLATION_STYLE = "portugues brasileiro natural para webtoon/manhwa"
PRESERVATION_RULES = [
    "Preservar nomes proprios e a grafia escolhida durante o capitulo.",
    "Nao traduzir SFX ou texto decorativo marcado para preservacao.",
    "Reutilizar traducoes anteriores quando o sentido e o contexto forem equivalentes.",
    "Manter falas naturais, curtas e adequadas ao espaco do balao.",
]


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tokens(text):
    return re.findall(r"[A-Za-z][A-Za-z'-]{1,}", str(text or ""))


def _normalized_token(token):
    return re.sub(r"[^A-Z']", "", str(token or "").upper())


def _entry_map(entries, key="text"):
    result = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get(key) or "").strip()
        if value:
            result[value.upper()] = dict(entry)
    return result


def _is_known_english_word(token):
    normalized = _normalized_token(token)
    return normalized in COMMON_ENGLISH_WORDS or normalized.lower() in OCR_ENGLISH_WORDS


class SessionContextStore:
    def __init__(self, path, chapter_url):
        self.path = Path(path).resolve()
        self.chapter_url = str(chapter_url)
        self.data = load_json(self.path, default={})

    def prepare(self, groups):
        compatible_data = (
            self.data if self.data.get("version") == CONTEXT_VERSION else {}
        )
        token_counts = Counter()
        speech_counts = Counter()
        explicit_name_counts = Counter()
        explicit_name_text = {}
        for group in groups:
            classification = str(getattr(group, "classification", "unknown"))
            text = str(getattr(group, "text", "") or "")
            text_tokens = _tokens(text)
            for token in text_tokens:
                normalized = _normalized_token(token)
                if len(normalized) < 2:
                    continue
                token_counts[normalized] += 1
                if classification in {"speech", "thought", "narration"}:
                    speech_counts[normalized] += 1
            for name in getattr(group, "detected_proper_names", []) or []:
                name_text = str(name or "").strip()
                normalized_name = _normalized_token(name_text)
                if len(normalized_name) < 2:
                    continue
                explicit_name_counts[normalized_name] += 1
                explicit_name_text.setdefault(normalized_name, name_text)

        existing_names = _entry_map(compatible_data.get("proper_names"))
        name_candidates = {}
        for token, count in explicit_name_counts.items():
            previous = existing_names.get(token, {})
            name_candidates[token] = {
                "text": previous.get("text") or explicit_name_text[token].title(),
                "mentions": max(int(previous.get("mentions") or 0), int(count)),
                "preserve": True,
            }

        for token, previous in existing_names.items():
            if not _is_known_english_word(token):
                name_candidates.setdefault(token, previous)

        names = sorted(
            name_candidates.values(),
            key=lambda item: (-int(item.get("mentions") or 0), str(item.get("text") or "")),
        )[:80]
        name_keys = {_normalized_token(item.get("text")) for item in names}

        existing_terms = {
            token: entry
            for token, entry in _entry_map(
                compatible_data.get("recurring_terms")
            ).items()
            if not _is_known_english_word(token)
        }
        term_candidates = dict(existing_terms)
        for token, count in token_counts.most_common():
            if (
                count < 2
                or len(token) < 4
                or token in name_keys
                or _is_known_english_word(token)
                or token in SFX_WORDS
            ):
                continue
            previous = existing_terms.get(token, {})
            term_candidates[token] = {
                "text": previous.get("text") or token.lower(),
                "mentions": max(int(previous.get("mentions") or 0), int(count)),
            }
        recurring_terms = sorted(
            term_candidates.values(),
            key=lambda item: (-int(item.get("mentions") or 0), str(item.get("text") or "")),
        )[:80]
        possible_characters = [
            {
                "name": item["text"],
                "mentions": int(item.get("mentions") or 0),
            }
            for item in names
            if (
                speech_counts.get(_normalized_token(item.get("text")), 0) >= 1
                or not token_counts
            )
        ][:50]

        now = _utc_now()
        self.data = {
            "version": CONTEXT_VERSION,
            "chapter_url": self.chapter_url,
            "created_at": compatible_data.get("created_at") or now,
            "updated_at": now,
            "translation_style": TRANSLATION_STYLE,
            "preservation_rules": list(PRESERVATION_RULES),
            "proper_names": names,
            "possible_characters": possible_characters,
            "recurring_terms": recurring_terms,
            "translations_used": list(
                compatible_data.get("translations_used") or []
            )[-250:],
        }
        self.save()
        return self.data

    def record_translations(self, groups):
        previous = {
            str(item.get("source") or "").strip(): dict(item)
            for item in self.data.get("translations_used", [])
            if isinstance(item, dict) and str(item.get("source") or "").strip()
        }
        for group in groups:
            source = str(getattr(group, "text", "") or "").strip()
            translation = str(getattr(group, "translation", "") or "").strip()
            if not source or not translation or source.casefold() == translation.casefold():
                continue
            previous[source] = {
                "source": source,
                "translation": translation,
                "region_type": str(getattr(group, "classification", "unknown")),
            }
        self.data["translations_used"] = list(previous.values())[-250:]
        self.data["updated_at"] = _utc_now()
        self.save()
        return self.data

    def prompt_fragment(self):
        names = [
            str(item.get("text") or "")
            for item in self.data.get("proper_names", [])[:40]
            if str(item.get("text") or "").strip()
        ]
        terms = [
            str(item.get("text") or "")
            for item in self.data.get("recurring_terms", [])[:30]
            if str(item.get("text") or "").strip()
        ]
        translations = [
            f"{item.get('source')} => {item.get('translation')}"
            for item in self.data.get("translations_used", [])[-60:]
            if item.get("source") and item.get("translation")
        ]
        sections = [
            "Contexto temporario deste capitulo:",
            f"Estilo: {self.data.get('translation_style') or TRANSLATION_STYLE}.",
            "Regras: " + " ".join(self.data.get("preservation_rules") or PRESERVATION_RULES),
        ]
        if names:
            sections.append("Nomes a preservar: " + ", ".join(names) + ".")
        if terms:
            sections.append("Termos recorrentes: " + ", ".join(terms) + ".")
        if translations:
            sections.append("Traducoes ja usadas:\n" + "\n".join(translations))
        return "\n".join(sections)

    def signature(self):
        return stable_hash(
            {
                "version": self.data.get("version"),
                "style": self.data.get("translation_style"),
                "rules": self.data.get("preservation_rules"),
                "names": self.data.get("proper_names"),
                "terms": self.data.get("recurring_terms"),
                "translations": self.data.get("translations_used"),
            }
        )

    def summary(self):
        return {
            "path": str(self.path),
            "proper_names": len(self.data.get("proper_names", [])),
            "possible_characters": len(self.data.get("possible_characters", [])),
            "recurring_terms": len(self.data.get("recurring_terms", [])),
            "translations_used": len(self.data.get("translations_used", [])),
        }

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.data)
