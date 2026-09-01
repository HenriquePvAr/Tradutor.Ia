"""Did the target preserve the meaning of the source?

Every validator this pipeline had until now answered a different question: is the
candidate in the target language, does it echo the source, does it keep the
terminology the chapter already agreed on. A candidate can pass all of them, read
as perfect Portuguese, and still say something the source never said - a decision
rendered as a state, a negation dropped, three enemies turned into thirty.

Two layers, in this order, because the second one costs a provider call:

* **Local invariants** - facts that are true or false without judgement.  A number
  that vanished, a protected name that became an ordinary word.  These are
  *proven*, so they block.
* **Selective adjudication** - constructions where the local evidence says
  "something may have moved" but cannot say what.  Negation asymmetry, an ongoing
  action rendered as a static attribute, two known characters swapping places.
  These are *routed*, never rejected on the regex alone.

Everything else - the overwhelming majority - is faithful and never leaves this
module.  Lexical overlap is deliberately not a signal: "Leave it to me." ->
"Deixa comigo." shares no word and is a perfect translation, while "I won't go."
-> "Eu vou." shares one and inverts the sentence.

Nothing here calls out, mutates or persists anything, and no chapter, page, job
or sentence is special-cased.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

FIDELITY_VERSION = "1"

# --- outcomes ---------------------------------------------------------------
FAITHFUL = "faithful"
BLOCKED = "blocked"
VERIFY = "verify"
# Neither proven wrong nor trustworthy. The candidate still renders - holding it
# would put the English source back on the page, which is a worse defect than a
# doubtful translation - but it is never counted as clean, and it carries the
# exact reason into the semantic review ledger.
REVIEW = "review"

# --- structured findings ----------------------------------------------------
QUANTITY_CHANGED = "quantity_changed"
ENTITY_CHANGED = "entity_changed"
NEGATION_CHANGED = "negation_changed"
STATE_ACTION_CHANGED = "state_action_changed"
ACTOR_RELATION_CHANGED = "actor_relation_changed"
TEMPORAL_RELATION_CHANGED = "temporal_relation_changed"
MEANING_MISMATCH = "meaning_mismatch"
FIDELITY_UNCERTAIN = "fidelity_uncertain"
# Review-only: the defect is upstream of the provider, or in the surface form.
SOURCE_OCR_SUSPICIOUS = "source_ocr_suspicious"
GRAMMAR_MALFORMED = "ptbr_grammar_malformed"
# The OCR lost the word boundaries and the repair pass could not put them back.
SOURCE_SEGMENTATION_INCOMPLETE = "source_segmentation_incomplete"
# Same collapsed source, opposite evidence: the candidate is built on none of the
# unreadable part.  Still uncertain, still reviewed, but readable - see
# ``unresolved_run_debris``.
SOURCE_SEGMENTATION_RECOVERED = "source_segmentation_recovered"
# The source word has more than one sense and the candidate picked the one the
# surrounding context does not support.
WORD_SENSE_CONTEXT_MISMATCH = "word_sense_context_mismatch"

REVIEW_ONLY_FIDELITY_REASON_CODES = frozenset({
    SOURCE_OCR_SUSPICIOUS, GRAMMAR_MALFORMED, SOURCE_SEGMENTATION_INCOMPLETE,
    SOURCE_SEGMENTATION_RECOVERED, WORD_SENSE_CONTEXT_MISMATCH,
})

# --- how usable is a ``review`` verdict? ------------------------------------
# ``REVIEW`` renders, because holding a region puts the English source back on
# the page.  That is the right trade only while the Portuguese is something a
# reader can actually use.  These three reasons say the opposite by
# construction:
#
# * ``source_ocr_suspicious`` fires *only* when the corrupt source token is
#   still present in the candidate - the garbage is literally on the shipped
#   page ("UM RATO DO SLLM", "COLLD E WIELD HERDARAM").
# * ``ptbr_grammar_malformed`` is malformed Portuguese, also on the page.
# * ``source_segmentation_incomplete`` means nobody knows where the source words
#   were, so no candidate built from it can be verified against anything.
# * ``word_sense_context_mismatch`` is fluent Portuguese that states a different
#   fact than the source ("PRECINCT 7" -> "7 DISTRITO ELEITORAL").  A reader
#   cannot recover the intended sense from it, and cannot tell it is wrong.
#
# Such a region still renders and still routes to review, but it is a Setup
# blocker: "flagged for review" is not a licence to ship nonsense as final
# story output.
REVIEW_RENDERABLE = "review_renderable"
REVIEW_UNUSABLE = "review_unusable"
UNUSABLE_REVIEW_REASON_CODES = frozenset({
    SOURCE_OCR_SUSPICIOUS, GRAMMAR_MALFORMED, SOURCE_SEGMENTATION_INCOMPLETE,
    WORD_SENSE_CONTEXT_MISMATCH,
})


def review_usability(reason):
    """``REVIEW_UNUSABLE`` when the flagged output is unfit to read, else renderable."""
    code = str(reason or "").split(":", 1)[0]
    return REVIEW_UNUSABLE if code in UNUSABLE_REVIEW_REASON_CODES else REVIEW_RENDERABLE


def is_review_unusable(reason):
    """True for a review verdict that may never be shipped as final story output."""
    return review_usability(reason) == REVIEW_UNUSABLE
BLOCKING_FIDELITY_REASON_CODES = frozenset({
    QUANTITY_CHANGED, ENTITY_CHANGED, NEGATION_CHANGED, STATE_ACTION_CHANGED,
    ACTOR_RELATION_CHANGED, TEMPORAL_RELATION_CHANGED, MEANING_MISMATCH,
    FIDELITY_UNCERTAIN,
})
FIDELITY_REASON_CODES = BLOCKING_FIDELITY_REASON_CODES | REVIEW_ONLY_FIDELITY_REASON_CODES

# The constraint the retry must satisfy, one per finding. This is what the model
# is told; it is never asked for, and never stores, any reasoning.
FIDELITY_RETRY_CONSTRAINTS = {
    QUANTITY_CHANGED: "preserve_quantity",
    ENTITY_CHANGED: "preserve_protected_entity",
    NEGATION_CHANGED: "preserve_negation",
    STATE_ACTION_CHANGED: "preserve_intent",
    ACTOR_RELATION_CHANGED: "preserve_actor_relationship",
    TEMPORAL_RELATION_CHANGED: "preserve_temporal_relation",
    MEANING_MISMATCH: "preserve_meaning",
    FIDELITY_UNCERTAIN: "preserve_meaning",
    SOURCE_OCR_SUSPICIOUS: "preserve_meaning",
    GRAMMAR_MALFORMED: "preserve_natural_grammar",
    SOURCE_SEGMENTATION_INCOMPLETE: "preserve_meaning",
    SOURCE_SEGMENTATION_RECOVERED: "preserve_meaning",
    WORD_SENSE_CONTEXT_MISMATCH: "preserve_word_sense",
}

TRANSLATABLE_CLASSIFICATIONS = frozenset({"speech", "thought", "narration", "unknown"})

# What a retry may show the provider *about the scene*, as opposed to about the
# rejection. Descriptive source evidence only - the neighbouring lines, verbatim
# - which is what disambiguates a word whose senses the region alone cannot
# settle. Bounded so no chapter is ever assembled and no prompt grows without
# limit, and never phrased as an instruction: the provider is given something to
# read, not an answer to repeat.
SCENE_CONTEXT_MAX_LINES = 6
SCENE_CONTEXT_MAX_CHARS = 600


def scene_context(context_texts):
    """The bounded neighbouring source lines a retry may see, as one string."""
    lines, total = [], 0
    for text in context_texts or ():
        line = " ".join(str(text or "").split())
        if not line or line in lines:
            continue
        if total + len(line) > SCENE_CONTEXT_MAX_CHARS:
            break
        lines.append(line)
        total += len(line)
        if len(lines) >= SCENE_CONTEXT_MAX_LINES:
            break
    return " ".join(lines)


@dataclass(frozen=True)
class FidelityFinding:
    """What the local layer concluded, and why. Compact by construction."""

    status: str = FAITHFUL
    reason_codes: tuple = ()
    critical_fact_mismatches: tuple = field(default=())

    @property
    def faithful(self):
        return self.status == FAITHFUL

    @property
    def primary_reason(self):
        return self.reason_codes[0] if self.reason_codes else ""

    def reason(self):
        """``code:fact`` - the same shape the terminology/character checks use."""
        if not self.reason_codes:
            return ""
        code = self.reason_codes[0]
        facts = ",".join(self.critical_fact_mismatches[:4])
        return f"{code}:{facts}" if facts else code


def retry_constraint(reason):
    """The corrective constraint for a reason code, with or without its facts."""
    return FIDELITY_RETRY_CONSTRAINTS.get(str(reason or "").split(":", 1)[0], "")


def is_fidelity_reason(reason):
    return str(reason or "").split(":", 1)[0] in FIDELITY_REASON_CODES


# --- text helpers -----------------------------------------------------------
def _fold(text):
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _words(text):
    # Apostrophes are joined, not split: "don't" is one negation marker, and
    # splitting it produces two tokens that mean nothing.
    return re.findall(r"[^\W\d_]+", _fold(text).replace("'", "").replace("’", ""),
                      flags=re.UNICODE)


# --- quantities -------------------------------------------------------------
# A number is content, not formatting. Locale rewrites it ("1,000" -> "1.000",
# "3.5" -> "3,5") without changing it, so both sides are reduced to the value
# before they are compared. A separator followed by exactly three digits groups
# thousands; anything else is a decimal mark.
_NUMBER = re.compile(r"\d[\d.,]*\d|\d")
_GROUPING = re.compile(r"(?<=\d)[.,](?=\d{3}(?:\D|$))")


def _numeric_value(token):
    unified = _GROUPING.sub("", token.strip(".,")).replace(",", ".")
    try:
        return float(unified)
    except ValueError:
        return unified


def numeric_values(text):
    return [_numeric_value(match.group(0)) for match in _NUMBER.finditer(str(text or ""))]


def _quantity_mismatches(source, candidate):
    """Source numbers that no longer exist in the candidate, as written."""
    remaining = list(numeric_values(candidate))
    missing = []
    for value in numeric_values(source):
        if value in remaining:
            remaining.remove(value)
        else:
            missing.append(value)
    return [f"{value:g}" if isinstance(value, float) else str(value) for value in missing]


# A count spelled as a word is still a count. Only a *conflict* is evidence:
# Portuguese routinely carries the number in the noun or drops an English "one"
# that was never a quantity ("give me one moment" -> "me da um segundo"), so a
# quantity word that simply has no counterpart proves nothing. Two quantity
# words that disagree do.
_SOURCE_QUANTITY_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "hundred": 100, "thousand": 1000,
}
_TARGET_QUANTITY_WORDS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11,
    "doze": 12, "cem": 100, "cento": 100, "mil": 1000,
}


def _quantity_word_conflict(source, candidate):
    """The counts both sides spell out, when they cannot both be true."""
    spelled = {_SOURCE_QUANTITY_WORDS[word] for word in _words(source)
               if word in _SOURCE_QUANTITY_WORDS}
    rendered = {_TARGET_QUANTITY_WORDS[word] for word in _words(candidate)
                if word in _TARGET_QUANTITY_WORDS}
    if not spelled or not rendered or spelled & rendered:
        return ()
    return tuple(str(value) for value in sorted(spelled))


# --- protected entities -----------------------------------------------------
ENTITY_STEM_LENGTH = 5


def _survives(part, candidate_words):
    """The name is still there, in whatever form the target language needs.

    An exact-string rule is unsafe in Portuguese: a form of address the chapter
    filed under its source spelling comes back inflected, and rejecting that
    would be rejecting the correct translation. A shared stem is enough evidence
    that the identity survived; it is nowhere near enough to turn a name into an
    unrelated common noun.
    """
    if part in candidate_words:
        return True
    if len(part) < ENTITY_STEM_LENGTH:
        return False
    stem = part[:ENTITY_STEM_LENGTH]
    return any(word.startswith(stem) for word in candidate_words)


def _entity_mismatches(source, candidate, protected_entities):
    """Protected names the source spells and the candidate silently dropped."""
    source_words = set(_words(source))
    candidate_words = set(_words(candidate))
    missing = []
    for entity in protected_entities:
        parts = _words(entity)
        if not parts or str(entity) in missing:
            continue
        if not all(part in source_words for part in parts):
            continue
        if all(_survives(part, candidate_words) for part in parts):
            continue
        missing.append(str(entity))
    return missing


# --- negation ---------------------------------------------------------------
# Presence, never count: Portuguese negative concordance legitimately spells two
# markers ("nao vi ninguem") where English spells one, so only the disappearance
# of negation altogether is evidence - and even that is routed for adjudication
# rather than rejected, because lexical negation ("impossible") and idiom carry
# no marker at all.
#
# Only one direction is checked. A candidate that negates where the source shows
# no marker is overwhelmingly a source we failed to read (OCR glues "IDIDN'TKNOW"
# into one token) or English negating lexically ("hardly", "fail to", "unless"),
# and on 1608 persisted real regions that direction alone produced 159 findings,
# every sampled one of them a correct translation. Losing a negation the source
# spelled out is the defect that matters, and it is the one kept.
_SOURCE_NEGATIONS = frozenset({
    "not", "no", "never", "none", "nobody", "nothing", "neither", "nor",
    "cannot", "cant", "dont", "doesnt", "didnt", "wont", "wouldnt", "shouldnt",
    "couldnt", "isnt", "arent", "wasnt", "werent", "havent", "hasnt", "hadnt",
    "aint", "without",
})
_TARGET_NEGATIONS = frozenset({
    "nao", "nunca", "jamais", "nem", "ninguem", "nada", "nenhum", "nenhuma",
    "nenhuns", "nenhumas", "sem", "tampouco",
})


def _negation_asymmetry(source, candidate):
    source_words = _words(source)
    source_negated = any(word in _SOURCE_NEGATIONS for word in source_words) or bool(
        re.search(r"\bn['’]t\b", _fold(source))
    )
    if not source_negated:
        return False
    return not any(word in _TARGET_NEGATIONS for word in _words(candidate))


# --- ongoing action rendered as a static attribute --------------------------
# The severe class: the source says the speaker is doing/deciding something, the
# candidate says the speaker *is* something. Both are fluent, only one is true.
# Narrow on purpose - it needs a progressive source *and* a copular candidate
# with no gerund of its own, so "I'm going home" -> "Estou indo para casa" (a
# gerund) and "I'm tired" -> "Estou cansado" (no progressive source) are silent.
_SOURCE_PROGRESSIVE = re.compile(r"\b(?:am|is|are|'m|m|re|s)\s+\w+ing\b")
_TARGET_COPULA = re.compile(
    r"\b(?:sou|es|e|somos|sao|serei|sera|seremos|serao|seria|"
    r"estou|esta|estamos|estao|estarei|estara|estaremos|estarao|estaria|"
    r"fico|fica|ficarei|ficara|ficamos)\s+(\w+)\b"
)
_TARGET_GERUND = re.compile(r"\w{3,}ndo\b")
_ADJECTIVAL = re.compile(r"(?:ado|ada|ados|adas|ido|ida|idos|idas|vel|veis)$")
_SOURCE_PASSIVE_PROGRESSIVE = re.compile(
    r"\bbeing\s+\w+(?:ed|en|osen|own|ung|ought|aught)\b"
)
_TARGET_PASSIVE_INFINITIVE = re.compile(
    r"\bser\s+\w+(?:ado|ada|ados|adas|ido|ida|idos|idas|to|ta|tos|tas)\b"
)


def _state_replaced_action(source, candidate):
    folded_source = _fold(source)
    folded_candidate = _fold(candidate)
    if not _SOURCE_PROGRESSIVE.search(folded_source):
        return False
    if _TARGET_GERUND.search(folded_candidate):
        return False
    if _SOURCE_PASSIVE_PROGRESSIVE.search(folded_source) and _TARGET_PASSIVE_INFINITIVE.search(
        folded_candidate
    ):
        return False
    return any(
        _ADJECTIVAL.search(match.group(1))
        for match in _TARGET_COPULA.finditer(folded_candidate)
    )


# --- ordering in time -------------------------------------------------------
# Only the two relations a reader cannot recover from context: what happens
# first. "when", "while" and "as" are deliberately absent - Portuguese inserts
# "quando"/"enquanto" freely without changing anything, and rejecting that would
# reject correct translations. On 542 persisted real regions these two rules
# fired exactly once, on the one region whose meaning really had moved.
#
# Only the *subordinating* target form counts. A bare "depois" is the ordinary
# adverb "later" ("I will explain later." -> "Eu explicarei depois.") and states
# no order at all; "depois que" does.
_SOURCE_ORDER_BEFORE = frozenset({"before", "until", "till"})
_SOURCE_ORDER_AFTER = frozenset({"after", "once"})
_TARGET_ORDER_BEFORE = re.compile(r"\bantes\s+(?:que|de|d[oa]s?)\b|\bate\s+que\b")
_TARGET_ORDER_AFTER = re.compile(r"\bdepois\s+(?:que|de|d[oa]s?)\b|\bapos\s+que\b")


def _temporal_relation_change(source, candidate):
    """'' when the ordering survived, otherwise which way it moved.

    ``inverted`` is proven: both sides name an order and they disagree.
    ``introduced`` is not proof but is not nothing either - the candidate states
    an order the source never stated, which is how a class of people ("the
    nearest Awakened") became an event ("after [someone] wakes up").
    """
    source_words = set(_words(source))
    folded_candidate = _fold(candidate)
    source_before = bool(source_words & _SOURCE_ORDER_BEFORE)
    source_after = bool(source_words & _SOURCE_ORDER_AFTER)
    target_before = bool(_TARGET_ORDER_BEFORE.search(folded_candidate))
    target_after = bool(_TARGET_ORDER_AFTER.search(folded_candidate))
    if (source_before and target_after and not target_before) or (
        source_after and target_before and not target_after
    ):
        return "inverted"
    if not (source_before or source_after) and (target_before or target_after):
        return "introduced"
    return ""


# --- can the source be read at all ------------------------------------------
# Before anything is blamed on the provider. The complement, not a duplicate, of
# the ``ocr_source_suspicious`` evidence the OCR router already files: that one
# says the *read* was doubtful but worth translating; this one says the doubtful
# part came out the other end untranslated.
#
# The shape is the one the OCR quality score already uses: a token the source
# language does not spell - no vowel, or three consonants in a row - that the
# candidate then carried over verbatim ("SLLM RAT" -> "RATO DO SLLM"). A letter
# repeated three times is a stylised interjection ("HMMM"), not damage.
#
# Run-together tokens ("TAKEAFEWHOURS") are deliberately *not* a signal here.
# They are endemic in this OCR corpus - 126 of 542 persisted real regions carry
# one - and the provider recovers almost all of them correctly, so flagging them
# would bury the real defects under a review queue four times their size. What
# survives verbatim into the Portuguese is the part the provider could not
# recover, and that is what is reported.
_IMPROBABLE_TOKEN = re.compile(
    r"^(?![a-z]*(.)\1\1)(?:[^aeiouy]{4,}$|[a-z]*[bcdfghjklmnpqrstvwxz]{3,}[a-z]*$)"
)
SUSPICIOUS_TOKEN_LENGTH = 4


def improbable_source_token(token):
    """True when the source language does not spell a token this shape."""
    return bool(_IMPROBABLE_TOKEN.match(_fold(token)))


def _known_word(token, is_source_word):
    if is_source_word is None:
        return False
    try:
        return bool(is_source_word(token.upper()))
    except Exception:  # noqa: BLE001 - a lexicon outage must never sink a region.
        return True


# A run this long with no space inside it is not a word, it is several words the
# OCR glued together. Kept above the longest ordinary English word a balloon
# realistically carries so that legitimate long vocabulary never trips it.
# ponytail: one length threshold, no lexicon. The repo's English reference list
# is too sparse to segment against (it has MONSTER but not GATE or BECOME), so a
# dictionary-based split would be less reliable than this, not more. Revisit if
# a full lexicon lands.
UNSEGMENTED_RUN_LENGTH = 13
_ALPHA_RUN = re.compile(r"[^\W\d_]{%d,}" % UNSEGMENTED_RUN_LENGTH, re.UNICODE)


def unsegmented_source_runs(source):
    """Glued word runs the OCR produced and the repair pass never split."""
    return tuple(match.group(0).upper() for match in _ALPHA_RUN.finditer(str(source or "")))


# --- collapsed source runs, proved without provenance ------------------------
# Provenance is useful when it reaches this gate, but the residual text can carry
# its own evidence: a long uppercase run whose edge tiles into multiple closed-
# class English words has lost boundaries even when the line-level repair reason
# was not copied onto the grouped region. Length alone is deliberately rejected;
# the rule needs corroborating function-word structure at an edge and never
# rewrites the source.
COLLAPSED_RUN_MIN_TILED_WORDS = 2
_TILING_WORDS = frozenset({
    "ABOUT", "AFTER", "AGAIN", "ALREADY", "ALSO", "ALWAYS", "ANOTHER",
    "ANYTHING", "BECAUSE", "BEEN", "BEFORE", "BEING", "COULD", "EACH", "EVEN",
    "EVER", "EVERY", "EVERYTHING", "FROM", "HAVE", "HAVING", "HERE", "INTO",
    "JUST", "MANY", "MINE", "MORE", "MOST", "MUCH", "MUST", "NEVER", "NOTHING",
    "ONLY", "ONTO", "OTHER", "OURS", "OVER", "SHALL", "SHOULD", "SOME",
    "SOMETHING", "STILL", "SUCH", "THAN", "THAT", "THEIR", "THEM", "THEN",
    "THERE", "THESE", "THEY", "THIS", "THOSE", "THOUGH", "THROUGH", "UNDER",
    "UPON", "VERY", "WERE", "WHAT", "WHEN", "WHERE", "WHICH", "WHILE", "WHOM",
    "WHOSE", "WILL", "WITH", "WOULD", "YOUR", "YOURS",
})
_TILING_BY_LENGTH = tuple(sorted(_TILING_WORDS, key=len, reverse=True))


def _tile_from_end(run, reverse):
    """The closed-class words tiling ``run`` inward from one end, plus what is left."""
    text, words = run[::-1] if reverse else run, []
    while True:
        word = next(
            (
                candidate
                for candidate in _TILING_BY_LENGTH
                if text.startswith(candidate[::-1] if reverse else candidate)
            ),
            None,
        )
        if word is None:
            return words, (text[::-1] if reverse else text)
        text = text[len(word):]
        words.append(word)


def _best_tiling(run):
    """``(words, residual, reverse)`` for whichever end tiles further."""
    forward, reverse = _tile_from_end(run, False), _tile_from_end(run, True)
    if len(reverse[0]) > len(forward[0]):
        return reverse[0], reverse[1], True
    return forward[0], forward[1], False


def collapsed_source_runs(source):
    """Runs whose word boundaries are provably gone, in order."""
    return tuple(
        run
        for run in unsegmented_source_runs(source)
        if len(_best_tiling(run)[0]) >= COLLAPSED_RUN_MIN_TILED_WORDS
    )


# Deliberately no repair here, unlike the single-edit rule above. The tiling
# proves that some boundaries collapsed, never the unique full sentence.


# --- did the collapse actually reach the candidate? --------------------------
# A collapsed run says the *source* cannot be verified. It does not, on its own,
# say the translation is unusable, and treating the two as the same thing is what
# held back correct Portuguese: "BUT YOU WILL ALSO" came back as ordinary
# Portuguese that shares nothing with the glued "BUTYOU", while "A GATE THROUGH
# WHICH" came back as "A GATES", carrying the unsegmented "AGATE" straight onto
# the page as a word that is neither English nor Portuguese.
#
# The discriminator is that debris, and it is measured against the *residual* of
# the tiling - the stretch no closed-class word could account for, which is
# precisely the part nobody could read.  A candidate word that *contains* four or
# more of those letters in sequence was built out of the unreadable fragment,
# whatever ending the provider then put on it ("AGATE" -> "GÁTES", "GÁTETA").
# Four is the floor the module already uses for a token worth suspecting; below
# it a shared run is a coincidence between two languages that share an alphabet.
#
# ponytail: substring match, no Portuguese lexicon. It over-matches Latin
# cognates - "ORIENTADA" shares six letters with "ORIENTEDASPECT" and is a
# correct translation - so a region can stay held on a shared root. That is the
# safe direction: the rule only ever chooses between "keep the old veto" and
# "release", so it can never block something the previous code allowed.
# Separating a cognate from carried-over debris needs a real PT-BR lexicon the
# repo does not have. Revisit if one lands.
UNRESOLVED_RUN_DEBRIS_MIN_LENGTH = 4


def unresolved_run_debris(runs, candidate):
    """Candidate words carried over from the part of a collapsed run nobody read."""
    words = dict.fromkeys(word.upper() for word in _words(candidate))
    debris = []
    for run in runs or ():
        residual = _best_tiling(str(run or "").upper())[1]
        fragments = {
            residual[start:start + UNRESOLVED_RUN_DEBRIS_MIN_LENGTH]
            for start in range(len(residual) - UNRESOLVED_RUN_DEBRIS_MIN_LENGTH + 1)
        }
        for word in words:
            if len(word) < UNRESOLVED_RUN_DEBRIS_MIN_LENGTH or word in debris:
                continue
            if any(fragment in word for fragment in fragments):
                debris.append(word)
    return tuple(debris)


def suspicious_source_tokens(source, candidate, *, known_entities=(), is_source_word=None):
    """Source tokens that make the source itself untrustworthy, in order."""
    entities = {part for entity in known_entities for part in _words(entity)}
    candidate_words = set(_words(candidate))
    suspicious = []
    for token in _words(source):
        if (
            len(token) < SUSPICIOUS_TOKEN_LENGTH
            or token in entities
            or token in suspicious
            or _known_word(token, is_source_word)
        ):
            continue
        if token in candidate_words and _IMPROBABLE_TOKEN.match(token):
            suspicious.append(token)
    return tuple(token.upper() for token in suspicious)


# --- repairing a source the detector just called untrustworthy ----------------
# Detection was where #84F14 stopped: a corrupt token that survived into the
# Portuguese was filed unusable and shipped.  The provider is then being asked to
# translate a word that does not exist, so before the one retry it gets, the
# source is given its best shot at being a word again.
#
# This is deliberately *not* a spellchecker over OCR text.  It runs only on
# tokens ``suspicious_source_tokens`` already proved untrustworthy - improbably
# spelled, unknown to the lexicon, and carried untranslated into the candidate -
# and it corrects one only when the vocabulary the caller vouches for holds
# exactly one word a single edit away.  Two candidates is a guess, and a guess
# that rewrites a name, a fantasy term or an SFX is a worse defect than the one
# it is trying to fix, so ambiguity means no repair and the region stays
# ``REVIEW_UNUSABLE``.
#
# The raw OCR is never overwritten: the caller keeps the original text and the
# canonical form travels separately, with its evidence.
SOURCE_REPAIR_MIN_LENGTH = 4
SOURCE_REPAIR_REASON = "unique_single_edit_source_vocabulary"


def _within_one_edit(left, right):
    """True when one substitution, insertion or deletion turns one into the other."""
    if left == right:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(right) - len(left) > 1:
        return False
    index = 0
    while index < len(left) and left[index] == right[index]:
        index += 1
    if len(left) == len(right):
        return left[index + 1:] == right[index + 1:]
    return left[index:] == right[index + 1:]


def unique_source_repair(token, vocabulary):
    """The one vocabulary word a single edit from ``token``, or ``""``.

    ``""`` for no candidate *and* for more than one: both mean the evidence does
    not name a single answer, and only a single answer may rewrite a source.
    """
    token = str(token or "").upper()
    if len(token) < SOURCE_REPAIR_MIN_LENGTH or token in vocabulary:
        return ""
    matches = {word for word in vocabulary if _within_one_edit(token, word)}
    return matches.pop() if len(matches) == 1 else ""


def repair_suspicious_source(source, suspicious_tokens, vocabulary, *, protected=()):
    """``(canonical_source, repairs)`` - unique high-confidence repairs only.

    ``repairs`` carries the provenance the caller must persist: what the OCR
    actually read, what it was corrected to, and on what evidence.
    """
    text = str(source or "")
    protected_tokens = {part.upper() for entry in protected for part in _words(entry)}
    lexicon = {str(word).upper() for word in (vocabulary or ())}
    repairs = []
    for raw in dict.fromkeys(str(token or "").upper() for token in suspicious_tokens):
        if not raw or raw in protected_tokens:
            continue
        canonical = unique_source_repair(raw, lexicon)
        if not canonical:
            continue
        repaired = re.sub(
            rf"\b{re.escape(raw)}\b", canonical, text, flags=re.IGNORECASE
        )
        if repaired == text:
            continue
        text = repaired
        repairs.append({
            "raw_source": raw,
            "canonical_source": canonical,
            "repair_reason": SOURCE_REPAIR_REASON,
            # Uniqueness is the gate, so every accepted repair is equally
            # evidenced; the number exists so the provenance record has the
            # same shape as every other confidence the pipeline persists.
            "repair_confidence": 1.0,
            "repair_evidence": f"unique_single_edit:{raw}>{canonical}",
        })
    return text, tuple(repairs)


# --- Portuguese a reader would not accept ------------------------------------
# Two shapes only, both unambiguous. Anything subtler is a judgement call and
# belongs to the adjudicator, not to a regex.
_BARE_INFINITIVE = re.compile(
    r"\b(?:voce|voces|eu|ele|ela|eles|elas|nos)\s+"
    r"(?:fazer|ser|ter|ir|estar|dizer|ver|saber|poder|querer|dar|vir|ficar|falar)\b"
)
_DUPLICATED_FUNCTION_WORD = re.compile(
    r"\b(de|do|da|dos|das|o|a|os|as|em|no|na|que|para|com|um|uma)\s+\1\b"
)
_THIRD_PERSON_NOUN_WITH_FIRST_PERSON_STEP = re.compile(
    r"\b(?:esse|essa|este|esta|aquele|aquela|o|a)\s+"
    r"[a-z]{4,}\s+passo\s+a\s+ser\b"
)


def _malformed_portuguese(candidate):
    folded = _fold(candidate)
    match = (
        _BARE_INFINITIVE.search(folded)
        or _DUPLICATED_FUNCTION_WORD.search(folded)
        or _THIRD_PERSON_NOUN_WITH_FIRST_PERSON_STEP.search(folded)
    )
    return match.group(0) if match else ""


# --- which sense of an ambiguous word -----------------------------------------
# A different defect from every rule above: the source is clean, the Portuguese
# is well formed, every invariant holds - and the noun means something else.
# "PRECINCT 7" came back "7 DISTRITO ELEITORAL" on a page that also says
# "EMERGENCY CONTAINMENT VAULT".  Nothing local can catch that, because nothing
# local is wrong.
#
# The evidence is three-part and all three parts are required, which is what
# keeps this from becoming the short-balloon detector again:
#
#   1. the source spells a word this table knows to be ambiguous;
#   2. the candidate *commits* to one of its senses, by carrying a target word
#      that only that sense would produce ("eleitoral", "celula");
#   3. the bounded context - this region plus its neighbours - does not support
#      that sense.  Either it supports a different one (a proven mismatch) or it
#      supports none, in which case nobody knows and a confident guess is not a
#      clean translation.
#
# A candidate that stays neutral ("DISTRITO 7") commits to no sense and is never
# flagged.  The table is small and meant to stay small: an entry earns its place
# by being a word whose senses a reader cannot recover from the wrong choice.
AMBIGUOUS_WORD_SENSES = {
    "precinct": {
        "police": {
            "source": frozenset({
                "police", "officer", "officers", "cop", "cops", "sheriff",
                "detective", "arrest", "arrested", "patrol", "security",
                "guard", "guards", "detention", "containment", "custody",
                "checkpoint", "station", "crime", "suspect", "prisoner",
            }),
            "target": frozenset({"policial", "policiais", "policia", "delegacia"}),
        },
        "electoral": {
            "source": frozenset({
                "vote", "votes", "voter", "voters", "ballot", "ballots",
                "election", "elections", "electoral", "poll", "polls",
                "polling", "campaign", "candidate",
            }),
            "target": frozenset({
                "eleitoral", "eleitorais", "eleicao", "eleicoes", "votacao",
            }),
        },
    },
    "cell": {
        "prison": {
            "source": frozenset({
                "prison", "jail", "cage", "guard", "guards", "inmate",
                "prisoner", "prisoners", "warden", "bars", "locked", "chains",
                "slave", "slaves", "captive",
            }),
            "target": frozenset({"cela", "celas"}),
        },
        "biological": {
            "source": frozenset({
                "blood", "tissue", "dna", "organism", "body", "bone",
                "membrane", "microscope", "flesh", "muscle", "nerve",
            }),
            "target": frozenset({"celula", "celulas"}),
        },
        "device": {
            "source": frozenset({
                "phone", "battery", "signal", "charger", "call", "screen",
            }),
            "target": frozenset({"celular", "celulares"}),
        },
    },
}


def word_sense_conflicts(source, candidate, context_texts=()):
    """``term>sense`` for every sense the candidate asserts and nothing supports."""
    source_words = set(_words(source))
    if not source_words & set(AMBIGUOUS_WORD_SENSES):
        return ()
    candidate_words = set(_words(candidate))
    evidence = set(source_words)
    for text in context_texts or ():
        evidence |= set(_words(text))
    conflicts = []
    for term, senses in AMBIGUOUS_WORD_SENSES.items():
        if term not in source_words:
            continue
        asserted = [
            name for name, sense in senses.items()
            if candidate_words & sense["target"]
        ]
        # Exactly one, or the candidate is saying something this table cannot
        # read and guessing would be worse than staying quiet.
        if len(asserted) != 1:
            continue
        sense = asserted[0]
        if evidence & senses[sense]["source"]:
            continue
        conflicts.append(f"{term}>{sense}")
    return tuple(conflicts)


# --- who did what to whom ---------------------------------------------------
def _entities_reordered(source, candidate, protected_entities):
    """The same two known identities, in the other order.

    No regex settles a semantic role, so this only reports that the evidence for
    one moved: the sequence is a question for the adjudicator, never a verdict.
    """
    keys = {_fold(entity) for entity in protected_entities if _fold(entity)}
    if len(keys) < 2:
        return False

    def sequence(text):
        seen = []
        for word in _words(text):
            if word in keys and word not in seen:
                seen.append(word)
        return seen

    source_order = sequence(source)
    candidate_order = sequence(candidate)
    if len(source_order) < 2 or set(source_order) != set(candidate_order):
        return False
    return source_order != candidate_order


# --- layer A ----------------------------------------------------------------
def ledger_entities_for(source_text, ledger):
    """Identities the *chapter* established, that this region actually mentions.

    Only these may block. A name merely detected inside this region is already
    enforced verbatim by the translation validator; re-deciding it here would be
    a second proper-name authority, and the region-level detector is noisy enough
    ("Station", "One", "Here") that giving it blocking power would reject correct
    translations.
    """
    bindings = {}
    if ledger is not None and hasattr(ledger, "term_bindings"):
        try:
            bindings = ledger.term_bindings() or {}
        except Exception:  # noqa: BLE001 - a missing ledger must never sink a region.
            bindings = {}
    entities = [
        str(entry.get("source") or key)
        for key, entry in bindings.items()
        if isinstance(entry, dict) and entry.get("kind") == "proper_name"
    ]
    return _entities_present(source_text, entities)


def _entities_present(source_text, entities):
    source_words = set(_words(source_text))
    unique = []
    for entity in entities:
        entity = str(entity or "").strip()
        parts = _words(entity)
        if parts and all(part in source_words for part in parts) and entity not in unique:
            unique.append(entity)
    return tuple(unique)


def evaluate_local_fidelity(
    source_text,
    candidate,
    *,
    classification="speech",
    protected_entities=(),
    proper_names=(),
    is_source_word=None,
    source_repair_reason="",
    context_texts=(),
):
    """Layer A. Deterministic, free, and honest about what it cannot decide.

    ``protected_entities`` are identities the *chapter* established and the
    caller already vouched for - only those may block. ``proper_names`` (the
    region's own detected spans, enforced verbatim upstream already) never
    blocks here; it only adds evidence for routing.
    """
    source = str(source_text or "").strip()
    target = str(candidate or "").strip()
    if not source or not target:
        return FidelityFinding()
    if classification not in TRANSLATABLE_CLASSIFICATIONS:
        return FidelityFinding()

    protected = _entities_present(source, protected_entities)
    missing_entities = _entity_mismatches(source, target, protected)
    if missing_entities:
        return FidelityFinding(BLOCKED, (ENTITY_CHANGED,), tuple(missing_entities))

    missing_numbers = _quantity_mismatches(source, target) or _quantity_word_conflict(
        source, target
    )
    if missing_numbers:
        return FidelityFinding(BLOCKED, (QUANTITY_CHANGED,), tuple(missing_numbers))

    if _negation_asymmetry(source, target):
        return FidelityFinding(VERIFY, (NEGATION_CHANGED,))
    temporal = _temporal_relation_change(source, target)
    if temporal == "inverted":
        return FidelityFinding(BLOCKED, (TEMPORAL_RELATION_CHANGED,), (temporal,))
    if temporal:
        return FidelityFinding(VERIFY, (TEMPORAL_RELATION_CHANGED,), (temporal,))
    if _state_replaced_action(source, target):
        return FidelityFinding(VERIFY, (STATE_ACTION_CHANGED,))
    known = protected + tuple(
        entity for entity in _entities_present(source, proper_names)
        if entity not in protected
    )
    if _entities_reordered(source, target, known):
        return FidelityFinding(VERIFY, (ACTOR_RELATION_CHANGED,), tuple(known[:4]))

    # Nothing about the *meaning* is provably wrong. What is left is doubt about
    # the inputs and the surface form: reported, never blocked, because holding
    # the region here would put untranslated English back on the page.
    suspicious = suspicious_source_tokens(
        source, target, known_entities=known, is_source_word=is_source_word
    )
    if suspicious:
        return FidelityFinding(REVIEW, (SOURCE_OCR_SUSPICIOUS,), suspicious)
    # The word-segmentation repair ran and still left glued runs behind. The
    # pipeline is saying, in its own provenance, that it does not know where the
    # source words are. Nothing built on a source with unresolved collapsed
    # boundaries can be verified, so it renders under review and never clean.
    runs = (
        unsegmented_source_runs(source)
        if "segment" in str(source_repair_reason or "")
        # No provenance reached this gate, which on the real corpus means the
        # repair recorded itself somewhere else, not that it never ran. The
        # residual proves the collapse on its own.
        else collapsed_source_runs(source)
    )
    if runs:
        # The collapse is real either way. What decides usability is whether it
        # reached the candidate: unreadable debris on the page, malformed
        # Portuguese, or a sense nothing supports all mean nobody can check the
        # result against anything. None of those, and every rule above already
        # passed, is the strongest evidence this pipeline has that the provider
        # recovered the sentence - so the region renders, under review, with the
        # segmentation uncertainty recorded in its own reason code.
        unresolved = (
            unresolved_run_debris(runs, target)
            or _malformed_portuguese(target)
            or word_sense_conflicts(source, target, context_texts)
        )
        code = SOURCE_SEGMENTATION_INCOMPLETE if unresolved else SOURCE_SEGMENTATION_RECOVERED
        return FidelityFinding(REVIEW, (code,), runs[:4])
    malformed = _malformed_portuguese(target)
    if malformed:
        return FidelityFinding(REVIEW, (GRAMMAR_MALFORMED,), (malformed,))
    # Last, because it is the only rule that reads anything outside this region:
    # everything provable about the region alone has already had its say.
    senses = word_sense_conflicts(source, target, context_texts)
    if senses:
        return FidelityFinding(REVIEW, (WORD_SENSE_CONTEXT_MISMATCH,), senses[:4])
    return FidelityFinding()


# --- layer B ----------------------------------------------------------------
def adjudicate(
    verifier,
    *,
    source_text,
    candidate,
    finding,
    target_language="pt",
    protected_entities=(),
    terminology=None,
    context=(),
):
    """Ask the semantic verifier once, and believe only a clear yes.

    ``uncertain`` is not a pass: an answer nobody is sure of leaves the region
    exactly as unproven as no answer at all, and a wrong meaning that renders is
    worse than a region held for review.
    """
    if verifier is None or not hasattr(verifier, "verify_fidelity"):
        return FidelityFinding(VERIFY, (FIDELITY_UNCERTAIN,) + tuple(finding.reason_codes))
    try:
        answer = verifier.verify_fidelity(
            source_text=str(source_text or ""),
            candidate=str(candidate or ""),
            target_language=str(target_language or ""),
            protected_entities=tuple(protected_entities or ()),
            terminology=dict(terminology or {}),
            context=tuple(context or ()),
            reason_codes=tuple(finding.reason_codes),
        )
    except Exception:  # noqa: BLE001 - a verifier outage is uncertainty, not a pass.
        return FidelityFinding(VERIFY, (FIDELITY_UNCERTAIN,) + tuple(finding.reason_codes))

    answer = answer if isinstance(answer, dict) else {}
    faithful = answer.get("faithful")
    codes = tuple(
        str(code) for code in (answer.get("reason_codes") or ()) if str(code).strip()
    )
    facts = tuple(
        str(fact) for fact in (answer.get("critical_fact_mismatches") or ()) if str(fact).strip()
    )
    if faithful is True:
        return FidelityFinding()
    if faithful is False:
        return FidelityFinding(BLOCKED, codes or finding.reason_codes or (MEANING_MISMATCH,), facts)
    return FidelityFinding(
        VERIFY, (FIDELITY_UNCERTAIN,) + (codes or finding.reason_codes), facts
    )
