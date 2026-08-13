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

# --- structured findings ----------------------------------------------------
QUANTITY_CHANGED = "quantity_changed"
ENTITY_CHANGED = "entity_changed"
NEGATION_CHANGED = "negation_changed"
STATE_ACTION_CHANGED = "state_action_changed"
ACTOR_RELATION_CHANGED = "actor_relation_changed"
MEANING_MISMATCH = "meaning_mismatch"
FIDELITY_UNCERTAIN = "fidelity_uncertain"

FIDELITY_REASON_CODES = frozenset({
    QUANTITY_CHANGED, ENTITY_CHANGED, NEGATION_CHANGED, STATE_ACTION_CHANGED,
    ACTOR_RELATION_CHANGED, MEANING_MISMATCH, FIDELITY_UNCERTAIN,
})

# The constraint the retry must satisfy, one per finding. This is what the model
# is told; it is never asked for, and never stores, any reasoning.
FIDELITY_RETRY_CONSTRAINTS = {
    QUANTITY_CHANGED: "preserve_quantity",
    ENTITY_CHANGED: "preserve_protected_entity",
    NEGATION_CHANGED: "preserve_negation",
    STATE_ACTION_CHANGED: "preserve_intent",
    ACTOR_RELATION_CHANGED: "preserve_actor_relationship",
    MEANING_MISMATCH: "preserve_meaning",
    FIDELITY_UNCERTAIN: "preserve_meaning",
}

TRANSLATABLE_CLASSIFICATIONS = frozenset({"speech", "thought", "narration", "unknown"})


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


def _state_replaced_action(source, candidate):
    folded_candidate = _fold(candidate)
    if not _SOURCE_PROGRESSIVE.search(_fold(source)):
        return False
    if _TARGET_GERUND.search(folded_candidate):
        return False
    return any(
        _ADJECTIVAL.search(match.group(1))
        for match in _TARGET_COPULA.finditer(folded_candidate)
    )


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

    missing_numbers = _quantity_mismatches(source, target)
    if missing_numbers:
        return FidelityFinding(BLOCKED, (QUANTITY_CHANGED,), tuple(missing_numbers))

    if _negation_asymmetry(source, target):
        return FidelityFinding(VERIFY, (NEGATION_CHANGED,))
    if _state_replaced_action(source, target):
        return FidelityFinding(VERIFY, (STATE_ACTION_CHANGED,))
    known = protected + tuple(
        entity for entity in _entities_present(source, proper_names)
        if entity not in protected
    )
    if _entities_reordered(source, target, known):
        return FidelityFinding(VERIFY, (ACTOR_RELATION_CHANGED,), tuple(known[:4]))
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
