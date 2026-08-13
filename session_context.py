import re
import threading
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ocr_balloon import (
    COMMON_ENGLISH_WORDS,
    PROPER_NAME_ONLY_REASON,
    SFX_WORDS,
    _token_is_source_vocabulary,
    detect_proper_name_spans,
)
from ocr_engine import COMMON_ENGLISH_WORDS as OCR_ENGLISH_WORDS
from output_manifest import sanitize_source_url
from pipeline_cache import atomic_write_json, load_json, stable_hash


CONTEXT_VERSION = "chapter-session-v2"

# --- persistent chapter terminology ledger ----------------------------------
# The rolling dialogue window below is deliberately short: an unbounded chapter
# transcript in every prompt is not context, it is cost. But a *terminology
# decision* is not dialogue - it is a fact about this chapter that has to hold
# from the first region to the last. Keeping both in the same list meant a
# binding chosen early was evicted by unrelated chatter long before the term
# came back, so the same source word left the chapter under two different target
# forms. The ledger is the separate, compact memory: source -> target, one entry
# per unique term, never a transcript.
KIND_PROPER_NAME = "proper_name"
KIND_TERMINOLOGY = "terminology"
LEDGER_MAX_TERMS = 120
LEDGER_MAX_OBSERVATIONS = 8
LEDGER_MAX_TOKENS_PER_OBSERVATION = 16
LEDGER_MIN_TERM_LENGTH = 4
TERMINOLOGY_CONFLICT_REASON = "terminology_conflict"
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


def _fold(text):
    """Accent-free upper-case form, so a target word matches across diacritics."""
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(text or "").upper())
        if not unicodedata.combining(char)
    )


def _word_surfaces(text):
    """Word tokens of a text in reading order, target-language letters included."""
    return re.findall(r"[^\W\d_]+", str(text or ""), flags=re.UNICODE)


def _candidate_term_keys(text):
    """Source tokens worth tracking as chapter terminology.

    Ordinary source vocabulary and sfx are excluded, using the same lexical
    authority that already decides whether a token may be preserved as a name.
    Freezing an everyday word to one target form is not terminology, it is a
    translation bug waiting to be enforced 90 regions later: an offline replay of
    a real chapter bound ordinary verbs and interjections until this gate was
    tightened to it.
    """
    keys = []
    for token in _tokens(text):
        key = _normalized_token(token)
        if (
            len(key) < LEDGER_MIN_TERM_LENGTH
            or key in SFX_WORDS
            or _is_known_english_word(key)
            or _token_is_source_vocabulary(key)
            or key in keys
        ):
            continue
        keys.append(key)
    return keys


class SessionContextStore:
    def __init__(self, path, chapter_url):
        self.path = Path(path).resolve()
        # Context is an output-side convenience artifact, not a credential store. The
        # downloader retains the raw URL in memory; this persisted record never needs it.
        self.chapter_url = sanitize_source_url(str(chapter_url))
        self.data = load_json(self.path, default={})
        # Chapter-local authority: a store reopened for a different chapter starts
        # empty rather than inheriting another chapter's names, terms and bindings.
        # There is no global translation memory here, by design.
        if str(self.data.get("chapter_url") or "") != self.chapter_url:
            self.data = {}
        self._lock = threading.Lock()

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
            # Carried forward untouched: a terminology decision outlives the
            # dialogue window and every re-preparation of the chapter.
            "term_bindings": dict(compatible_data.get("term_bindings") or {}),
            "ledger_stats": dict(compatible_data.get("ledger_stats") or {}),
        }
        self.save()
        return self.data

    # --- terminology ledger -------------------------------------------------
    def _bindings(self):
        bindings = self.data.get("term_bindings")
        if not isinstance(bindings, dict):
            bindings = {}
            self.data["term_bindings"] = bindings
        return bindings

    def _stats(self):
        stats = self.data.get("ledger_stats")
        if not isinstance(stats, dict):
            stats = {}
            self.data["ledger_stats"] = stats
        return stats

    def _bump(self, name, value=1):
        stats = self._stats()
        stats[name] = int(stats.get(name) or 0) + int(value)

    def _entry(self, key, source, kind, provenance):
        bindings = self._bindings()
        entry = bindings.get(key)
        if entry is None:
            if len(bindings) >= LEDGER_MAX_TERMS:
                self._drop_weakest_binding()
            if len(bindings) >= LEDGER_MAX_TERMS:
                return None
            entry = {
                "source": source,
                "target": "",
                "kind": kind,
                "provenance": "",
                "observations": [],
                "conflicts": 0,
                "first_seen": _utc_now(),
            }
            bindings[key] = entry
        # A proper name is the stronger claim: once a span is known to be a name
        # it stays one, so the kind is only ever upgraded.
        if kind == KIND_PROPER_NAME:
            entry["kind"] = KIND_PROPER_NAME
        entry.setdefault("provenance", "")
        if not entry["provenance"]:
            entry["provenance"] = provenance
        return entry

    def _drop_weakest_binding(self):
        """Make room by discarding an unestablished entry, never a decision."""
        bindings = self._bindings()
        weakest = [key for key, entry in bindings.items() if not entry.get("target")]
        if weakest:
            bindings.pop(sorted(weakest)[0], None)

    def _establish(self, entry, target, provenance):
        current = str(entry.get("target") or "")
        if current:
            if _fold(current) != _fold(target):
                # Deterministic and fail-closed: the binding already trusted by
                # the chapter stays authoritative, and the disagreement is
                # counted rather than silently applied.
                entry["conflicts"] = int(entry.get("conflicts") or 0) + 1
                self._bump("binding_conflicts")
            return
        entry["target"] = target
        entry["provenance"] = provenance
        entry["observations"] = []

    def _observe_terminology(self, entry, target_surfaces):
        """Accumulate evidence until exactly one target word explains the term.

        One observation is only enough when the region carries a single target
        word, which leaves no ambiguity about what the term became. Anything
        longer needs a second region: the words common to both are what the term
        actually maps to, and the rest is sentence.
        """
        surfaces = list(dict.fromkeys(target_surfaces))[:LEDGER_MAX_TOKENS_PER_OBSERVATION]
        if not surfaces:
            return
        if len(surfaces) == 1:
            # Unambiguous evidence, in both directions: it either establishes the
            # binding or contradicts the one already established.
            self._establish(entry, surfaces[0], "single_target_word_region")
            return
        if entry.get("target"):
            return
        observations = list(entry.get("observations") or [])
        observations.append(surfaces)
        entry["observations"] = observations[-LEDGER_MAX_OBSERVATIONS:]
        if len(entry["observations"]) < 2:
            return
        common = set(_fold(word) for word in entry["observations"][0])
        for observation in entry["observations"][1:]:
            common &= {_fold(word) for word in observation}
        if len(common) != 1:
            return
        folded = next(iter(common))
        for word in surfaces:
            if _fold(word) == folded:
                self._establish(entry, word, "recurring_region_alignment")
                return

    def _learn_from_group(self, group):
        source = str(getattr(group, "text", "") or "").strip()
        target = str(getattr(group, "translation", "") or "").strip()
        if not source or not target:
            return
        if not bool(getattr(group, "translation_valid", True)):
            return

        known = list(getattr(group, "detected_proper_names", []) or [])
        names = list(known)
        names.extend(detect_proper_name_spans(source, known_names=known))
        if str(getattr(group, "translation_final_reason", "") or "") == PROPER_NAME_ONLY_REASON:
            # The model was told the region held no names and every word had to be
            # translated, and it handed the text straight back. That is the model
            # reporting there is nothing to translate, which is what a name is.
            names.append(source)

        target_surfaces = _word_surfaces(target)
        folded_targets = {_fold(word): word for word in target_surfaces}
        name_keys = set()
        for name in names:
            key = _normalized_token(name)
            if len(key) < 2:
                continue
            name_keys.add(key)
            surface = folded_targets.get(_fold(name))
            if not surface:
                continue
            entry = self._entry(key, str(name).strip(), KIND_PROPER_NAME, "name_preserved_in_translation")
            if entry is not None:
                self._establish(entry, surface, entry.get("provenance") or "name_preserved_in_translation")
                self._merge_proper_name(str(name).strip())

        for key in _candidate_term_keys(source):
            if key in name_keys:
                continue
            # A "target" identical to the source term is a preservation question,
            # not a terminology decision; the proper-name path above owns it.
            candidates = [word for word in target_surfaces if _fold(word) != _fold(key)]
            entry = self._entry(key, key, KIND_TERMINOLOGY, "first_validated_translation")
            if entry is not None:
                self._observe_terminology(entry, candidates)

    def _merge_proper_name(self, name_text):
        """Add a newly proven name without disturbing one already recorded."""
        names = list(self.data.get("proper_names") or [])
        key = _normalized_token(name_text)
        if any(_normalized_token(item.get("text")) == key for item in names if isinstance(item, dict)):
            return
        names.append({"text": name_text, "mentions": 1, "preserve": True})
        self.data["proper_names"] = names[:80]

    def term_bindings(self):
        """Established source -> target decisions, as an ordered mapping."""
        return {
            key: dict(entry)
            for key, entry in sorted(self._bindings().items())
            if isinstance(entry, dict) and entry.get("target")
        }

    def binding_for(self, source_term):
        entry = self._bindings().get(_normalized_token(source_term))
        return str((entry or {}).get("target") or "")

    def drift_reason(self, source_text, candidate):
        """Why a candidate contradicts a decision this chapter already took.

        Only established bindings whose source term is actually present in this
        region are checked, and only the absence of the agreed target is
        reported. Anything else is a semantic question this does not answer.

        ponytail: the target is matched accent-folded but not inflected, so a
        legitimately conjugated form of the agreed word reads as a conflict. On a
        real 95-region chapter that cost one extra call; add stemming only if
        that ratio ever stops being noise.
        """
        candidate = str(candidate or "").strip()
        if not candidate:
            return ""
        present = set(_candidate_term_keys(source_text))
        present.update(
            _normalized_token(word) for word in _word_surfaces(source_text)
        )
        candidate_folded = {_fold(word) for word in _word_surfaces(candidate)}
        for key, entry in sorted(self._bindings().items()):
            target = str(entry.get("target") or "")
            if not target or key not in present:
                continue
            if _fold(target) in candidate_folded:
                self._bump("bindings_reused")
                continue
            return f"{TERMINOLOGY_CONFLICT_REASON}:{entry.get('source') or key}"
        return ""

    def record_translations(self, groups):
        with self._lock:
            previous = {
                str(item.get("source") or "").strip(): dict(item)
                for item in self.data.get("translations_used", [])
                if isinstance(item, dict) and str(item.get("source") or "").strip()
            }
            for group in groups:
                source = str(getattr(group, "text", "") or "").strip()
                translation = str(getattr(group, "translation", "") or "").strip()
                self._learn_from_group(group)
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
        # The ledger is emitted in full and independently of the dialogue window
        # above: it is what must not expire, and it is bounded by the number of
        # distinct terms in the chapter, never by how much was said.
        bindings = self.term_bindings()
        if bindings:
            proper = [
                f"{entry['source']} => {entry['target']}"
                for entry in bindings.values()
                if entry.get("kind") == KIND_PROPER_NAME
            ]
            glossary = [
                f"{entry['source']} => {entry['target']}"
                for entry in bindings.values()
                if entry.get("kind") != KIND_PROPER_NAME
            ]
            if proper:
                sections.append(
                    "Nomes proprios ja fixados neste capitulo (use exatamente esta "
                    "forma):\n" + "\n".join(proper)
                )
            if glossary:
                sections.append(
                    "Terminologia ja fixada neste capitulo (obrigatoria, mesmo que a "
                    "decisao tenha sido tomada muitas falas atras):\n" + "\n".join(glossary)
                )
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
                "bindings": self.term_bindings(),
            }
        )

    def summary(self):
        bindings = self.term_bindings()
        stats = self._stats()
        return {
            "path": str(self.path),
            "proper_names": len(self.data.get("proper_names", [])),
            "possible_characters": len(self.data.get("possible_characters", [])),
            "recurring_terms": len(self.data.get("recurring_terms", [])),
            "translations_used": len(self.data.get("translations_used", [])),
            "ledger_terms_count": len(bindings),
            "ledger_proper_names_count": sum(
                1 for entry in bindings.values() if entry.get("kind") == KIND_PROPER_NAME
            ),
            "binding_conflicts": int(stats.get("binding_conflicts") or 0),
            "bindings_reused": int(stats.get("bindings_reused") or 0),
        }

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.data)
