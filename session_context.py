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
TERM_AUTHORITY_EXPLICIT_GLOSSARY = "explicit_glossary"
TERM_AUTHORITY_PROPER_NAME = "proper_name"
TERM_AUTHORITY_ENTITY_TERM = "entity_term"
TERM_AUTHORITY_DOMAIN_TERM = "established_domain_term"
TERM_AUTHORITY_LEARNED_TERM = "learned_term"
TERM_AUTHORITY_LEXICAL_HINT = "learned_lexical_hint"
TERM_AUTHORITY_NON_AUTHORITATIVE = "non_authoritative"
LEDGER_MAX_TERMS = 120
LEDGER_MAX_OBSERVATIONS = 8
LEDGER_MAX_TOKENS_PER_OBSERVATION = 16
LEDGER_MIN_TERM_LENGTH = 4
TERMINOLOGY_CONFLICT_REASON = "terminology_conflict"
_AUTHORITATIVE_TERM_AUTHORITIES = {
    TERM_AUTHORITY_EXPLICIT_GLOSSARY,
    TERM_AUTHORITY_PROPER_NAME,
    TERM_AUTHORITY_ENTITY_TERM,
    TERM_AUTHORITY_DOMAIN_TERM,
    TERM_AUTHORITY_LEARNED_TERM,
}
_AUTHORITATIVE_TERM_PROVENANCES = {
    "explicit_glossary",
    "domain_term",
    "established_domain_term",
    "name_preserved_in_translation",
}
_DOMAIN_TERM_KEYS = {
    "DUNGEON", "DUNGEONS", "GUILD", "GUILDS", "GUILDMASTER", "GUILDMASTERS",
    "HUNTER", "HUNTERS", "RANK",
}
_LEXICAL_HINT_KEYS = {
    "ABOUT", "CRAP", "HELP", "HOLY", "LOOKS", "LUCKY", "MONSTER", "MONSTERS",
    "QUIT", "REASON", "SERIOUSLY", "SOMEONE", "STOP", "THAT", "THING",
    "THINGS", "UNFORTUNATE",
}
_CONTRACTION_RE = re.compile(r"[A-Z]+(?:'[A-Z]+)+$")

# --- chapter character registry ---------------------------------------------
# The ledger above preserves *text*: which target form a source term was bound
# to. It says nothing about the entity behind the name, so a character
# established early as feminine could be addressed later with a masculine form
# of address and nothing in the pipeline noticed. The registry is the separate,
# entity-level memory: who exists in this chapter and which linguistic
# properties we have actual evidence for. Authority is split on purpose -
# term_bindings owns source -> target spelling, the registry owns attributes.
CHARACTER_MAX_RECORDS = 60
CHARACTER_MAX_EVIDENCE = 8
CHARACTER_MAX_IN_PROMPT = 12
CHARACTER_MIN_NAME_LENGTH = 3
CHARACTER_GENDER_CONFLICT_REASON = "character_gender_conflict"
CHARACTER_PRONOUN_CONFLICT_REASON = "character_pronoun_conflict"
GENDER_FEATURE = "gender"
PRONOUN_FEATURE = "pronouns"
GENDER_UNKNOWN = "unknown"
HONORIFIC_CONFIDENCE = 0.9
HONORIFIC_PROVENANCE = "explicit_honorific"

# Honorific -> grammatical gender, as data keyed by source language. No business
# logic below names a specific honorific, and an entry mapping to "" addresses a
# person without asserting any gender at all.
SOURCE_HONORIFICS = {
    "ingles": {
        "MISS": "feminine",
        "MRS": "feminine",
        "MS": "feminine",
        "MADAM": "feminine",
        "MADAME": "feminine",
        "LADY": "feminine",
        "MR": "masculine",
        "SIR": "masculine",
        "LORD": "masculine",
        "DR": "",
        "DOCTOR": "",
        "PROF": "",
        "PROFESSOR": "",
        "CAPTAIN": "",
    },
}

# Third-person personal pronouns of the source language. Their only job is to
# *suppress* a finding: when the source itself spells out a pronoun, whatever
# pronoun the target uses is a rendering of that word, not drift about our
# character. An offline replay of a real chapter flagged exactly this - a region
# whose source said "he" about somebody else, in a sentence that merely
# contained a known character's family name.
SOURCE_PERSONAL_PRONOUNS = {
    "ingles": {
        "HE", "HIM", "HIS", "SHE", "HER", "HERS", "THEY", "THEM", "THEIR",
        "THEIRS", "HIMSELF", "HERSELF", "THEMSELVES",
    },
}

# Per target language: how a gender is stated in the prompt, which pronouns it
# implies, and the few closed-class words needed to keep the agreement check
# from firing on an unrelated person.
TARGET_LANGUAGE_FEATURES = {
    "pt": {
        "labels": {"feminine": "feminino", "masculine": "masculino"},
        "pronouns": {
            "feminine": ["ela", "dela"],
            "masculine": ["ele", "dele"],
        },
        "pronoun_gender": {
            "ELA": "feminine",
            "DELA": "feminine",
            "ELE": "masculine",
            "DELE": "masculine",
        },
        # A pronoun governed by a preposition is usually an oblique reference to
        # somebody else in the panel, so it is never treated as a contradiction.
        "prepositions": {
            "PARA", "PRA", "COM", "POR", "SEM", "SOBRE", "ENTRE", "CONTRA",
            "ATE", "APOS", "PERANTE", "DE", "A", "EM", "NELE", "NELA",
        },
        "feminine_endings": ("A", "AS"),
        "masculine_endings": ("O", "OS", "OR", "ORES"),
    },
}
DEFAULT_TARGET_LANGUAGE = "pt"
DEFAULT_SOURCE_LANGUAGE = "ingles"

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


def _source_term_authority(key, *, kind=KIND_TERMINOLOGY, provenance=""):
    key = _normalized_token(key)
    provenance = str(provenance or "")
    if kind == KIND_PROPER_NAME:
        return TERM_AUTHORITY_PROPER_NAME
    if provenance in _AUTHORITATIVE_TERM_PROVENANCES:
        if provenance == "name_preserved_in_translation":
            return TERM_AUTHORITY_PROPER_NAME
        return TERM_AUTHORITY_DOMAIN_TERM
    if key in _DOMAIN_TERM_KEYS:
        return TERM_AUTHORITY_DOMAIN_TERM
    if key in _LEXICAL_HINT_KEYS:
        return TERM_AUTHORITY_LEXICAL_HINT
    if _CONTRACTION_RE.fullmatch(key):
        return TERM_AUTHORITY_LEXICAL_HINT
    if _is_known_english_word(key) or _token_is_source_vocabulary(key):
        return TERM_AUTHORITY_LEXICAL_HINT
    # Glued OCR terms and invented proper nouns are useful context, but the
    # ledger should not hard-fail them unless another authority upgrades them.
    if len(key) > 14:
        return TERM_AUTHORITY_LEXICAL_HINT
    return TERM_AUTHORITY_LEARNED_TERM


def _binding_authority(entry):
    if not isinstance(entry, dict):
        return TERM_AUTHORITY_NON_AUTHORITATIVE
    explicit = str(entry.get("authority") or "").strip()
    if explicit:
        return explicit
    return _source_term_authority(
        entry.get("source") or "",
        kind=entry.get("kind") or KIND_TERMINOLOGY,
        provenance=entry.get("provenance") or "",
    )


def _binding_is_authoritative(entry):
    return _binding_authority(entry) in _AUTHORITATIVE_TERM_AUTHORITIES


def _target_family_forms(target):
    target = str(target or "").strip()
    if not target:
        return set()
    forms = {_fold(target)}
    folded = _fold(target)
    suffixes = {
        "O": ("A", "OS", "AS"),
        "A": ("O", "AS", "OS"),
        "OR": ("ORA", "ORES", "ORAS"),
        "AO": ("A", "OES", "AS"),
    }
    for suffix, variants in suffixes.items():
        if folded.endswith(suffix) and len(folded) > len(suffix) + 2:
            stem = folded[: -len(suffix)]
            forms.update(stem + variant for variant in variants)
    if folded.endswith("S") and len(folded) > 4:
        forms.add(folded[:-1])
    else:
        forms.add(folded + "S")
    return forms


def _target_binding_present(target, candidate_folded_words):
    return bool(_target_family_forms(target) & set(candidate_folded_words))


def _target_word_gender(language, word):
    """Grammatical gender a target word carries by its ending, or "".

    Deliberately morphological and deliberately tiny: it is only ever asked
    about the single word the source itself marked as a form of address, never
    about a sentence. Anything wider would be a grammar engine.
    """
    features = TARGET_LANGUAGE_FEATURES.get(str(language or ""))
    if not features:
        return ""
    folded = _fold(word)
    if folded.endswith(features["feminine_endings"]):
        return "feminine"
    if folded.endswith(features["masculine_endings"]):
        return "masculine"
    return ""


def _derive_feature(evidence, feature):
    """Value of a feature implied by the evidence, plus whether it conflicts.

    Order-independent by construction: the answer is a function of the evidence
    *set*, never of the order observations arrived in, so two workers observing
    the same chapter concurrently converge on the same record. The strongest
    evidence wins; a tie between different values is a conflict and yields no
    authoritative value at all, because unknown is better than wrong.
    """
    strongest = {}
    for item in evidence or []:
        if not isinstance(item, dict) or str(item.get("feature") or "") != feature:
            continue
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        confidence = float(item.get("confidence") or 0.0)
        strongest[value] = max(strongest.get(value, 0.0), confidence)
    if not strongest:
        return "", 0.0, False
    top = max(strongest.values())
    winners = sorted(
        value for value, confidence in strongest.items() if confidence >= top - 1e-9
    )
    if len(winners) > 1:
        return "", top, True
    return winners[0], top, False


class SessionContextStore:
    def __init__(
        self,
        path,
        chapter_url,
        target_language=DEFAULT_TARGET_LANGUAGE,
        source_language=DEFAULT_SOURCE_LANGUAGE,
    ):
        self.target_language = str(target_language or DEFAULT_TARGET_LANGUAGE)
        self.source_language = str(source_language or DEFAULT_SOURCE_LANGUAGE)
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
            # Same reasoning as the ledger: who a character is does not expire
            # with the dialogue window, and re-preparing the chapter must not
            # forget it.
            "characters": dict(compatible_data.get("characters") or {}),
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
                "authority": _source_term_authority(
                    source, kind=kind, provenance=provenance
                ),
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
            entry["authority"] = TERM_AUTHORITY_PROPER_NAME
        entry.setdefault("provenance", "")
        if not entry["provenance"]:
            entry["provenance"] = provenance
        entry.setdefault(
            "authority",
            _source_term_authority(
                entry.get("source") or source,
                kind=entry.get("kind") or kind,
                provenance=entry.get("provenance") or provenance,
            ),
        )
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
        entry["authority"] = _source_term_authority(
            entry.get("source") or "",
            kind=entry.get("kind") or KIND_TERMINOLOGY,
            provenance=provenance,
        )
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

    # --- character registry -------------------------------------------------
    def _characters(self):
        characters = self.data.get("characters")
        if not isinstance(characters, dict):
            characters = {}
            self.data["characters"] = characters
        return characters

    def _record(self, name):
        """The chapter-scoped record for a name, created on first sighting."""
        key = _normalized_token(name)
        display = str(name or "").strip()
        if len(key) < CHARACTER_MIN_NAME_LENGTH or not display:
            return None
        characters = self._characters()
        record = characters.get(key)
        if record is None:
            if len(characters) >= CHARACTER_MAX_RECORDS:
                return None
            record = {
                "canonical_name": display,
                "aliases": [],
                "forms_of_address": [],
                "evidence": [],
                "mentions": 0,
                "first_seen": _utc_now(),
            }
            characters[key] = record
        return record

    def _observe_character(
        self,
        name,
        feature="",
        value="",
        provenance="",
        confidence=0.0,
        alias="",
        form_of_address="",
    ):
        """Record one compact, attributable observation about a character.

        Nothing here infers: a caller must say *why* it believes the fact and how
        far it trusts it. Evidence is deduplicated by (feature, value,
        provenance) and never overwritten, so the derived attributes stay a pure
        function of what was actually seen.
        """
        record = self._record(name)
        if record is None:
            return None
        record["mentions"] = int(record.get("mentions") or 0) + 1
        for field, item in (("aliases", alias), ("forms_of_address", form_of_address)):
            item = str(item or "").strip()
            values = list(record.get(field) or [])
            if item and item.upper() not in {str(entry).upper() for entry in values}:
                values.append(item)
                record[field] = sorted(values)[:8]
        feature = str(feature or "").strip()
        value = str(value or "").strip()
        if not feature or not value or value == GENDER_UNKNOWN:
            return record
        evidence = list(record.get("evidence") or [])
        signature = (feature, value, str(provenance or ""))
        for item in evidence:
            if (
                str(item.get("feature") or ""),
                str(item.get("value") or ""),
                str(item.get("provenance") or ""),
            ) == signature:
                item["confidence"] = max(
                    float(item.get("confidence") or 0.0), float(confidence or 0.0)
                )
                break
        else:
            evidence.append(
                {
                    "feature": feature,
                    "value": value,
                    "provenance": str(provenance or "unattributed"),
                    "confidence": float(confidence or 0.0),
                }
            )
        record["evidence"] = sorted(
            evidence,
            key=lambda item: (
                str(item.get("feature")),
                str(item.get("value")),
                str(item.get("provenance")),
            ),
        )[:CHARACTER_MAX_EVIDENCE]
        # A proven character name is also a name the translation must preserve.
        # The ledger stays the single authority for the *spelling*; this only
        # makes sure the name is protected at all.
        self._merge_proper_name(str(record.get("canonical_name") or name).strip())
        return record

    def observe_character(self, name, **evidence):
        with self._lock:
            record = self._observe_character(name, **evidence)
            self.save()
            return record

    def _character_facts(self, key, record):
        gender, gender_confidence, gender_conflict = _derive_feature(
            record.get("evidence"), GENDER_FEATURE
        )
        pronouns, _confidence, pronoun_conflict = _derive_feature(
            record.get("evidence"), PRONOUN_FEATURE
        )
        features = TARGET_LANGUAGE_FEATURES.get(self.target_language) or {}
        derived = list(features.get("pronouns", {}).get(gender) or [])
        return {
            "character_key": key,
            "canonical_name": str(record.get("canonical_name") or key),
            "aliases": list(record.get("aliases") or []),
            "forms_of_address": list(record.get("forms_of_address") or []),
            "gender": gender,
            "gender_confidence": gender_confidence,
            "pronouns": [item for item in pronouns.split("/") if item] or derived,
            "conflict": bool(gender_conflict or pronoun_conflict),
            "mentions": int(record.get("mentions") or 0),
            "evidence": [dict(item) for item in record.get("evidence") or []],
        }

    def characters(self):
        """Every known character of this chapter with its derived attributes."""
        return {
            key: self._character_facts(key, record)
            for key, record in sorted(self._characters().items())
            if isinstance(record, dict)
        }

    def character_facts(self, name):
        key = _normalized_token(name)
        record = self._characters().get(key)
        return self._character_facts(key, record) if isinstance(record, dict) else {}

    def _learn_characters_from_group(self, group):
        """Harvest character evidence from the source text of one region.

        The only evidence trusted here is an explicit source-language honorific
        directly addressing a name. Appearance, first names, capitalisation and
        model speculation are all deliberately absent: an honorific is something
        the author wrote, everything else would be a guess.
        """
        source = str(getattr(group, "text", "") or "")
        if not source.strip():
            return
        honorifics = SOURCE_HONORIFICS.get(self.source_language) or {}
        words = _tokens(source)
        detected = {
            _normalized_token(name)
            for name in (getattr(group, "detected_proper_names", []) or [])
        }
        for index, word in enumerate(words[:-1]):
            gender = honorifics.get(_normalized_token(word))
            if gender is None:
                continue
            following = words[index + 1]
            key = _normalized_token(following)
            if len(key) < CHARACTER_MIN_NAME_LENGTH:
                continue
            if key not in detected and _is_known_english_word(key):
                continue
            self._observe_character(
                following,
                feature=GENDER_FEATURE if gender else "",
                value=gender,
                provenance=HONORIFIC_PROVENANCE,
                confidence=HONORIFIC_CONFIDENCE if gender else 0.0,
                alias=f"{word} {following}",
                form_of_address=word,
            )

    # --- character consistency ---------------------------------------------
    def _resolved_characters(self, source_text):
        """Characters this region actually refers to, by name or alias."""
        present = {_normalized_token(word) for word in _word_surfaces(source_text)}
        return [
            (key, record, self._character_facts(key, record))
            for key, record in sorted(self._characters().items())
            if isinstance(record, dict) and key in present
        ]

    def _source_marks_address(self, source_text, key):
        honorifics = SOURCE_HONORIFICS.get(self.source_language) or {}
        words = _tokens(source_text)
        return any(
            _normalized_token(word) in honorifics
            and _normalized_token(words[index + 1]) == key
            for index, word in enumerate(words[:-1])
        )

    def character_conflict_reason(self, source_text, candidate):
        """Why a candidate contradicts a character fact this chapter established.

        Two narrow checks, both requiring exactly one resolved character with a
        high-confidence gender in the region - with two characters present no
        gendered word can be attributed to either of them, so nothing is
        reported.

        ponytail: the form-of-address check only runs where the *source* itself
        marked an address with an honorific, and the pronoun check only outside
        prepositional phrases. That trades recall for precision on purpose: a
        wrongly rejected valid translation costs a real call and real trust,
        while a missed one is the status quo. Widen it only with a real
        dependency-parsed subject, never with a bigger regex.
        """
        features = TARGET_LANGUAGE_FEATURES.get(self.target_language)
        candidate = str(candidate or "").strip()
        if not features or not candidate:
            return ""
        resolved = self._resolved_characters(source_text)
        gendered = [item for item in resolved if item[2].get("gender")]
        if len(gendered) != 1:
            return ""
        key, record, facts = gendered[0]
        gender = facts["gender"]
        name = facts["canonical_name"]
        words = _word_surfaces(candidate)
        folded_key = _fold(key)

        if self._source_marks_address(source_text, key):
            for index, word in enumerate(words):
                if index == 0 or _fold(word) != folded_key:
                    continue
                previous = words[index - 1]
                if len(previous) < 4:
                    continue
                observed = _target_word_gender(self.target_language, previous)
                if observed and observed != gender:
                    self._bump("character_fact_conflicts")
                    return f"{CHARACTER_GENDER_CONFLICT_REASON}:{name}"

        source_pronouns = SOURCE_PERSONAL_PRONOUNS.get(self.source_language) or set()
        source_spells_a_pronoun = any(
            _normalized_token(word) in source_pronouns
            for word in _word_surfaces(source_text)
        )
        if (
            len(resolved) == 1
            and not source_spells_a_pronoun
            and not self._other_names_present(words, folded_key)
        ):
            for index, word in enumerate(words):
                observed = features["pronoun_gender"].get(_fold(word))
                if not observed or observed == gender:
                    continue
                if index and _fold(words[index - 1]) in features["prepositions"]:
                    continue
                self._bump("character_fact_conflicts")
                return f"{CHARACTER_PRONOUN_CONFLICT_REASON}:{name}"
        return ""

    def note_character_retry(self):
        with self._lock:
            self._bump("character_consistency_retries")
            self.save()

    def _other_names_present(self, words, folded_key):
        """True when another known person could own a pronoun in this text."""
        folded = {_fold(word) for word in words}
        known = set(self._characters())
        known.update(
            key
            for key, entry in self._bindings().items()
            if entry.get("kind") == KIND_PROPER_NAME
        )
        return any(
            _fold(key) in folded for key in known if _fold(key) != folded_key
        )

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
            if not _binding_is_authoritative(entry):
                self._bump("term_binding_prompt_only")
                continue
            if _target_binding_present(target, candidate_folded):
                self._bump("bindings_reused")
                continue
            self._bump("terminology_hard_conflict")
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
                # Character evidence lives in the *source*, so it is harvested
                # even for a region that never produced a translation.
                self._learn_characters_from_group(group)
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
                if entry.get("kind") != KIND_PROPER_NAME and _binding_is_authoritative(entry)
            ]
            hints = [
                f"{entry['source']} => {entry['target']}"
                for entry in bindings.values()
                if entry.get("kind") != KIND_PROPER_NAME
                and not _binding_is_authoritative(entry)
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
            if hints:
                sections.append(
                    "Observacoes lexicais deste capitulo (contexto nao obrigatorio; "
                    "nao force se a gramatica ou o contexto pedirem outra forma):\n"
                    + "\n".join(hints[:40])
                )
        character_lines = self.character_prompt_lines()
        if character_lines:
            sections.append(
                "Personagens ja identificados neste capitulo (respeite genero, "
                "pronomes e formas de tratamento):\n" + "\n".join(character_lines)
            )
        if translations:
            sections.append("Traducoes ja usadas:\n" + "\n".join(translations))
        return "\n".join(sections)

    def character_prompt_lines(self):
        """The few characters worth spending prompt budget on, never all of them.

        Only established facts are emitted, and only for characters that have
        one: a character we know nothing about contributes nothing but tokens,
        and stating "gender unknown" would invite the model to invent it. The
        full registry stays internal and unbounded by this cap.
        """
        features = TARGET_LANGUAGE_FEATURES.get(self.target_language) or {}
        labels = features.get("labels", {})
        ranked = sorted(
            self.characters().values(),
            key=lambda facts: (-int(facts.get("mentions") or 0), facts["canonical_name"]),
        )
        lines = []
        for facts in ranked:
            parts = [facts["canonical_name"]]
            if facts.get("gender"):
                parts.append(f"genero: {labels.get(facts['gender'], facts['gender'])}")
            if facts.get("pronouns"):
                parts.append("pronomes: " + ", ".join(facts["pronouns"]))
            if facts.get("aliases"):
                parts.append("tambem chamado(a): " + ", ".join(facts["aliases"]))
            if len(parts) == 1:
                continue
            lines.append(" | ".join(parts))
            if len(lines) >= CHARACTER_MAX_IN_PROMPT:
                break
        return lines

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
                "characters": self.characters(),
            }
        )

    def summary(self):
        bindings = self.term_bindings()
        stats = self._stats()
        characters = self.characters()
        return {
            "character_count": len(characters),
            "characters_with_gender": sum(
                1 for facts in characters.values() if facts.get("gender")
            ),
            "characters_with_pronouns": sum(
                1 for facts in characters.values() if facts.get("pronouns")
            ),
            "character_evidence_conflicts": sum(
                1 for facts in characters.values() if facts.get("conflict")
            ),
            "character_fact_conflicts": int(stats.get("character_fact_conflicts") or 0),
            "character_consistency_retries": int(
                stats.get("character_consistency_retries") or 0
            ),
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
