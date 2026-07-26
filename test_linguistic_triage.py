"""Contracts for the linguistic gate, triage queue and provider set (BLOCO 5)."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import random
import string
import unittest
import uuid

import linguistic_triage as lt
import region_taxonomy as tax


def _policy(category, **kw):
    return {"normalized_classification": category,
            "translatable": tax.is_translatable(category),
            "preservable": tax.is_preservable(category),
            "provider_required": kw.get("provider", False),
            "needs_human_review": kw.get("human", False)}


def _sentence(n=5):
    stock = ["THE", "RIVER", "TURNED", "DARK", "BEFORE", "DAWN", "SHE", "LEFT",
             "WITHOUT", "LOOKING", "BACK", "AGAIN"]
    return " ".join(random.choice(stock) for _ in range(n)) + "."


def _rand_id():
    return uuid.uuid4().hex[:8].upper()


class LinguisticGateContracts(unittest.TestCase):
    def _gate(self, source, candidate, category, **kw):
        return lt.evaluate_linguistic_gate(source_text=source, current_translation=candidate,
                                           policy=_policy(category, **kw))

    def test_a_real_translation_passes(self):
        gate = self._gate(_sentence(), "Ela partiu sem olhar para trás, à noite.",
                          tax.DIALOGUE_TRANSLATE)
        self.assertEqual(gate["status"], lt.PASSED)
        self.assertEqual(gate["reason_codes"], [])

    def test_untranslated_candidate_fails(self):
        text = _sentence()
        gate = self._gate(text, text, tax.NARRATION_TRANSLATE)
        self.assertEqual(gate["status"], lt.FAILED)
        self.assertIn("candidate_equals_source", gate["reason_codes"])

    def test_empty_candidate_fails(self):
        gate = self._gate(_sentence(), "", tax.DIALOGUE_TRANSLATE)
        self.assertEqual(gate["status"], lt.FAILED)
        self.assertIn("empty_translation", gate["reason_codes"])

    def test_preserved_region_altered_fails_and_intact_passes(self):
        self.assertEqual(self._gate("BAM", "BAM", tax.SFX_PRESERVE)["status"], lt.PASSED)
        altered = self._gate("BAM", "PUM", tax.SFX_PRESERVE)
        self.assertEqual(altered["status"], lt.FAILED)
        self.assertIn("preservable_region_was_altered", altered["reason_codes"])

    def test_watermark_change_is_reported_separately(self):
        host = f"{_rand_id().lower()}.com"
        gate = self._gate(host, f"outro-{host}", tax.WATERMARK_PRESERVE)
        self.assertIn("watermark_text_changed", gate["reason_codes"])

    def test_unreadable_source_needs_review_and_is_never_passed(self):
        gate = self._gate("QXZKV", "", tax.OCR_INVALID)
        self.assertEqual(gate["status"], lt.NEEDS_REVIEW)
        self.assertIn("source_text_unreadable", gate["reason_codes"])

    def test_encoding_artefact_fails(self):
        gate = self._gate(_sentence(), "OlÃ¡ meu amigo querido", tax.DIALOGUE_TRANSLATE)
        self.assertEqual(gate["status"], lt.FAILED)
        self.assertIn("encoding_artefact_in_candidate", gate["reason_codes"])

    def test_gate_is_independent_of_the_visual_gate(self):
        # Nothing in the linguistic gate reads a visual state.
        gate = self._gate(_sentence(), _sentence(), tax.DIALOGUE_TRANSLATE)
        self.assertNotIn("visual", str(gate).lower())


class TriageQueueContracts(unittest.TestCase):
    def _record(self, category, **kw):
        return {"region_id": f"p{random.randint(1,99):03d}:{_rand_id()}",
                "page_id": f"p{random.randint(1,99):03d}",
                "classification_normalized": category,
                "translatable": tax.is_translatable(category),
                "preservable": tax.is_preservable(category),
                "provider_required": kw.get("provider", False),
                "needs_human_review": kw.get("human", False),
                "cache_status": kw.get("cache", "not_answered"),
                "linguistic_gate": {"status": kw.get("gate", lt.PASSED), "checks": {}},
                **{k: v for k, v in kw.items() if k in ("source_text", "current_translation")}}

    def test_unreadable_and_undetermined_come_first(self):
        records = [self._record(tax.DIALOGUE_TRANSLATE),
                   self._record(tax.OCR_INVALID),
                   self._record(tax.UNKNOWN_REVIEW_REQUIRED),
                   self._record(tax.SFX_PRESERVE)]
        queue = lt.build_triage_queue(records)
        top_two = {queue[0]["classification_normalized"], queue[1]["classification_normalized"]}
        self.assertEqual(top_two, {tax.OCR_INVALID, tax.UNKNOWN_REVIEW_REQUIRED})

    def test_every_item_explains_its_priority(self):
        queue = lt.build_triage_queue([self._record(tax.OCR_INVALID),
                                       self._record(tax.DIALOGUE_TRANSLATE, provider=True)])
        for item in queue:
            self.assertTrue(item["triage_reasons"], item["region_id"])
            self.assertIsInstance(item["triage_score"], int)

    def test_a_decided_item_drops_to_the_bottom(self):
        undecided = self._record(tax.OCR_INVALID)
        decided = self._record(tax.OCR_INVALID)
        queue = lt.build_triage_queue([decided, undecided],
                                      decisions={decided["region_id"]: {"decision": "ocr_invalid"}})
        self.assertEqual(queue[-1]["region_id"], decided["region_id"])

    def test_queue_is_deterministic(self):
        records = [self._record(random.choice(list(tax.ALL_CATEGORIES))) for _ in range(12)]
        first = [r["region_id"] for r in lt.build_triage_queue(records)]
        second = [r["region_id"] for r in lt.build_triage_queue(list(reversed(records)))]
        self.assertEqual(first, second)

    def test_queue_survives_a_malformed_record(self):
        queue = lt.build_triage_queue([{"region_id": "x"}, self._record(tax.OCR_INVALID)])
        self.assertEqual(len(queue), 2)


class ProviderSetContracts(unittest.TestCase):
    def _record(self, category, **kw):
        return {"region_id": f"p{random.randint(1,99):03d}:{_rand_id()}",
                "page_id": f"p{random.randint(1,99):03d}",
                "classification_normalized": category,
                "translatable": tax.is_translatable(category),
                "preservable": tax.is_preservable(category),
                "provider_required": kw.get("provider", False),
                "needs_human_review": kw.get("human", False),
                "cache_status": kw.get("cache", "not_answered"),
                "source_text": _sentence(),
                "linguistic_gate": {"status": kw.get("gate", lt.PASSED)}}

    def test_only_translatable_uncached_regions_are_included(self):
        included = self._record(tax.DIALOGUE_TRANSLATE, provider=True)
        cached = self._record(tax.NARRATION_TRANSLATE, provider=False, cache="answered")
        sfx = self._record(tax.SFX_PRESERVE)
        watermark = self._record(tax.WATERMARK_PRESERVE)
        unreadable = self._record(tax.OCR_INVALID)
        result = lt.minimal_provider_set([included, cached, sfx, watermark, unreadable])
        self.assertEqual([i["region_id"] for i in result["items"]], [included["region_id"]])
        self.assertEqual(result["estimated_requests"], 1)
        reasons = {e["region_id"]: e["excluded_reason"] for e in result["excluded"]}
        self.assertEqual(reasons[cached["region_id"]], "resolved_by_cache")
        self.assertEqual(reasons[sfx["region_id"]], "preservable_class")
        self.assertEqual(reasons[watermark["region_id"]], "preservable_class")
        self.assertEqual(reasons[unreadable["region_id"]], "ocr_invalid")

    def test_human_decisions_exclude_regions(self):
        record = self._record(tax.DIALOGUE_TRANSLATE, provider=True)
        for verdict in ("preserve", "ocr_invalid", "dismissed"):
            result = lt.minimal_provider_set([record], decisions={record["region_id"]: {"decision": verdict}})
            self.assertEqual(result["items"], [])
            self.assertEqual(result["excluded"][0]["excluded_reason"], "human_decision_excludes")

    def test_a_human_can_promote_a_preserved_region_into_the_set(self):
        record = self._record(tax.SFX_PRESERVE, provider=True)
        result = lt.minimal_provider_set([record], decisions={record["region_id"]: {"decision": "translate"}})
        self.assertEqual(len(result["items"]), 1)

    def test_empty_report_produces_an_empty_set(self):
        result = lt.minimal_provider_set([])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["estimated_requests"], 0)
        self.assertEqual(result["pages"], [])

    def test_risk_follows_the_linguistic_gate(self):
        high = self._record(tax.DIALOGUE_TRANSLATE, provider=True, gate=lt.FAILED)
        medium = self._record(tax.DIALOGUE_TRANSLATE, provider=True, gate=lt.NEEDS_REVIEW)
        low = self._record(tax.DIALOGUE_TRANSLATE, provider=True, gate=lt.PASSED)
        result = lt.minimal_provider_set([high, medium, low])
        risks = {i["region_id"]: i["risk"] for i in result["items"]}
        self.assertEqual(risks[high["region_id"]], "high")
        self.assertEqual(risks[medium["region_id"]], "medium")
        self.assertEqual(risks[low["region_id"]], "low")

    def test_no_secret_like_key_ever_reaches_the_set(self):
        result = lt.minimal_provider_set([self._record(tax.DIALOGUE_TRANSLATE, provider=True)])
        blob = str(result).lower()
        # "authorization" alone is a legitimate field name (the request's own
        # status); what must never appear is a credential.
        for forbidden in ("api_key", "authorization:", "bearer", "cookie", "senha", "token"):
            self.assertNotIn(forbidden, blob)


class EditorialQueues(unittest.TestCase):
    """The OCR queue, the editorial queue and the counters (BLOCO 6A.1)."""

    def _record(self, category, source, **kw):
        return {"region_id": f"p{random.randint(1, 99):03d}:{_rand_id()}",
                "page_id": f"p{random.randint(1, 99):03d}",
                "classification_original": "unknown",
                "classification_normalized": category,
                "source_text": source,
                "current_translation": kw.get("current_translation", ""),
                "translatable": tax.is_translatable(category),
                "preservable": tax.is_preservable(category),
                "provider_required": kw.get("provider", True),
                "needs_human_review": kw.get("human", False),
                "confidence": kw.get("confidence", 0.9),
                "cache_status": kw.get("cache", "not_answered"),
                "linguistic_gate": {"status": kw.get("gate", lt.PASSED), "checks": {}}}

    def _corpus(self):
        return [
            self._record(tax.DIALOGUE_TRANSLATE, _sentence()),          # clean
            self._record(tax.DIALOGUE_TRANSLATE, "S0METHING"),          # corrupted read
            self._record(tax.DIALOGUE_TRANSLATE, _sentence(), human=True),
            self._record(tax.SFX_PRESERVE, "BAM"),                      # preserved
            self._record(tax.OCR_INVALID, _sentence()),                 # unreadable class
        ]

    def test_the_counters_partition_the_chapter(self):
        records = self._corpus()
        counters = lt.editorial_counters(records)
        self.assertEqual(counters["total"], len(records))
        self.assertEqual(
            counters["targeted_ocr_pending"] + counters["awaiting_editorial_decision"]
            + counters["ready_for_ai_review"] + counters["settled"], len(records))

    def test_every_region_lands_in_exactly_one_state(self):
        for record in self._corpus():
            state = lt.classify_editorial_state(record)
            self.assertIn(state["state"], (lt.TARGETED_OCR_PENDING, lt.AWAITING_EDITORIAL_DECISION,
                                           lt.READY_FOR_AI_REVIEW, lt.SETTLED))
            self.assertTrue(state["reasons"], "a state must explain itself")

    def test_the_provider_set_accounts_for_every_region(self):
        records = self._corpus()
        plan = lt.minimal_provider_set(records)
        self.assertEqual(len(plan["items"]) + len(plan["awaiting_editorial"])
                         + len(plan["excluded"]), len(records))
        self.assertEqual(plan["estimated_requests"], len(plan["items"]))

    def test_a_region_awaiting_a_decision_is_never_billed(self):
        record = self._record(tax.DIALOGUE_TRANSLATE, "S0METHING")
        plan = lt.minimal_provider_set([record])
        self.assertEqual(plan["estimated_requests"], 0)
        self.assertEqual(plan["awaiting_editorial_count"], 1)

    def test_a_clean_region_is_ready_without_any_decision(self):
        record = self._record(tax.DIALOGUE_TRANSLATE, _sentence())
        self.assertEqual(lt.classify_editorial_state(record)["state"], lt.READY_FOR_AI_REVIEW)
        self.assertEqual(lt.minimal_provider_set([record])["estimated_requests"], 1)

    def test_confirming_ocr_invalid_moves_a_region_out_of_the_provider_set(self):
        record = self._record(tax.DIALOGUE_TRANSLATE, "S0METHING")
        decisions = {record["region_id"]: {"decision": "ocr_invalid"}}
        counters = lt.editorial_counters([record], decisions=decisions)
        self.assertEqual(counters["targeted_ocr_pending"], 1)
        self.assertEqual(counters["awaiting_editorial_decision"], 0)
        plan = lt.minimal_provider_set([record], decisions=decisions)
        self.assertEqual(plan["estimated_requests"], 0)
        self.assertEqual(plan["awaiting_editorial_count"], 0)

    def test_ruling_on_every_open_question_unblocks_authorization(self):
        records = self._corpus()
        pending = lt.pending_editorial_decisions(records)
        decisions = {i["region_id"]: {"decision": "translate"} for i in pending["items"]}
        self.assertFalse(lt.editorial_counters(records, decisions=decisions)["authorization_blocked"])
        self.assertEqual(lt.pending_editorial_decisions(records, decisions=decisions)["item_count"], 0)

    def test_the_ocr_queue_marks_nothing_by_itself(self):
        records = self._corpus()
        result = lt.ocr_invalid_candidates(records)
        self.assertFalse(result["ocr_executed"])
        self.assertEqual(result["confirmed_count"], 0)
        for item in result["auto_markable"] + result["review_required"]:
            self.assertEqual(item["human_decision"], "")

    def test_only_unambiguous_evidence_reaches_the_bulk_group(self):
        records = self._corpus()
        result = lt.ocr_invalid_candidates(records)
        for item in result["auto_markable"]:
            self.assertTrue(item["ocr_assessment"]["auto_markable"])
            self.assertTrue(item["ocr_assessment"]["strong_evidence"])
        for item in result["review_required"]:
            self.assertFalse(item["ocr_assessment"]["auto_markable"])

    def test_a_confirmed_region_moves_to_the_confirmed_group(self):
        record = self._record(tax.DIALOGUE_TRANSLATE, "S0METHING")
        result = lt.ocr_invalid_candidates(
            [record], decisions={record["region_id"]: {"decision": "ocr_invalid"}})
        self.assertEqual(result["confirmed_count"], 1)
        self.assertEqual(result["auto_markable_count"], 0)

    def test_queues_and_counters_agree(self):
        records = self._corpus()
        counters = lt.editorial_counters(records)
        self.assertEqual(lt.pending_editorial_decisions(records)["item_count"],
                         counters["awaiting_editorial_decision"])
        self.assertEqual(lt.minimal_provider_set(records)["estimated_requests"],
                         counters["ready_for_ai_review"])

    def test_the_queues_are_deterministic(self):
        records = self._corpus()
        self.assertEqual(lt.pending_editorial_decisions(records),
                         lt.pending_editorial_decisions(records))
        self.assertEqual(lt.editorial_counters(records), lt.editorial_counters(records))


if __name__ == "__main__":
    unittest.main()


class CacheProposalContracts(unittest.TestCase):
    """"There is a cache entry" is not "there is a fix" — and not "must re-ask"."""

    def _eval(self, source, current, review, region="p001:R1"):
        return lt.evaluate_cache_proposal(source_text=source, current_translation=current,
                                          cached_review=review, region_id=region)

    def test_a_real_correction_is_usable_and_answered(self):
        result = self._eval(_sentence(), "tradução antiga",
                            {"region_id": "p001:R1", "action": "rewrite",
                             "revised_translation": "uma correção nova e diferente"})
        self.assertEqual(result["state"], lt.CACHE_USABLE)
        self.assertTrue(result["usable"])
        self.assertTrue(result["answered"])

    def test_a_keep_answer_is_answered_but_offers_no_correction(self):
        result = self._eval(_sentence(), "tradução aceita",
                            {"region_id": "p001:R1", "action": "keep"})
        self.assertEqual(result["state"], lt.CACHE_ANSWERED_NO_CHANGE)
        self.assertFalse(result["usable"])
        self.assertTrue(result["answered"])   # re-asking buys nothing

    def test_a_deferred_answer_is_answered_and_needs_a_human(self):
        result = self._eval(_sentence(), "", {"region_id": "p001:R1", "action": "manual_review"})
        self.assertEqual(result["state"], lt.CACHE_ANSWERED_DEFERRED)
        self.assertFalse(result["usable"])
        self.assertTrue(result["answered"])

    def test_no_cached_answer_means_unanswered(self):
        result = self._eval(_sentence(), "", None)
        self.assertEqual(result["state"], lt.CACHE_ABSENT)
        self.assertFalse(result["answered"])

    def test_a_proposal_equal_to_source_or_current_is_not_usable(self):
        text = _sentence()
        equal_source = self._eval(text, "algo", {"region_id": "p001:R1", "action": "rewrite",
                                                 "revised_translation": text})
        self.assertFalse(equal_source["usable"])
        already = self._eval(text, "mesma coisa", {"region_id": "p001:R1", "action": "rewrite",
                                                   "revised_translation": "mesma coisa"})
        self.assertFalse(already["usable"])
        self.assertEqual(already["reason"], "cached_proposal_already_applied")

    def test_an_answer_recorded_for_another_region_is_never_reused(self):
        result = self._eval(_sentence(), "", {"region_id": "p099:OTHER", "action": "rewrite",
                                              "revised_translation": "algo"}, region="p001:R1")
        self.assertEqual(result["state"], lt.CACHE_ABSENT)
        self.assertFalse(result["usable"])
        self.assertFalse(result["answered"])


class OcrCandidateContracts(unittest.TestCase):
    def _record(self, category, **kw):
        return {"region_id": f"p{random.randint(1,99):03d}:{_rand_id()}",
                "page_id": f"p{random.randint(1,99):03d}",
                "classification_normalized": category,
                "source_text": kw.get("source", _sentence()),
                "confidence": kw.get("confidence", 0.9),
                "reason_codes": [],
                "linguistic_gate": {"status": kw.get("gate", lt.PASSED),
                                    "reason_codes": kw.get("gate_reasons", [])}}

    def test_unreadable_class_and_human_verdict_both_qualify(self):
        unreadable = self._record(tax.OCR_INVALID, gate=lt.NEEDS_REVIEW,
                                  gate_reasons=["source_text_unreadable"])
        decided = self._record(tax.DIALOGUE_TRANSLATE)
        clean = self._record(tax.DIALOGUE_TRANSLATE)
        result = lt.ocr_reprocessing_candidates(
            [unreadable, decided, clean],
            decisions={decided["region_id"]: {"decision": "ocr_invalid"}})
        ids = {c["region_id"] for c in result["candidates"]}
        self.assertIn(unreadable["region_id"], ids)
        self.assertIn(decided["region_id"], ids)
        self.assertNotIn(clean["region_id"], ids)
        self.assertFalse(result["ocr_executed"])
        self.assertEqual(result["candidate_count"], 2)

    def test_every_candidate_asks_for_targeted_ocr_only(self):
        record = self._record(tax.OCR_INVALID, gate=lt.NEEDS_REVIEW,
                              gate_reasons=["source_text_unreadable"])
        result = lt.ocr_reprocessing_candidates([record])
        self.assertTrue(all(c["requested_action"] == "targeted_ocr" for c in result["candidates"]))

    def test_an_empty_report_yields_no_candidates(self):
        result = lt.ocr_reprocessing_candidates([])
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["pages"], [])


class CacheDidNotResolveContracts(unittest.TestCase):
    """A cached answer that left the region broken has not resolved it."""

    def _record(self, gate_status, **kw):
        return {"region_id": f"p{random.randint(1,99):03d}:{_rand_id()}",
                "page_id": f"p{random.randint(1,99):03d}",
                "classification_normalized": tax.DIALOGUE_TRANSLATE,
                "translatable": True, "preservable": False,
                "provider_required": kw.get("provider", False),
                "cache_status": kw.get("cache", "answered"),
                "source_text": _sentence(),
                "linguistic_gate": {"status": gate_status}}

    def test_a_cached_answer_with_a_failed_gate_still_needs_the_provider(self):
        broken = self._record(lt.FAILED, provider=False, cache="answered")
        result = lt.minimal_provider_set([broken])
        self.assertEqual([i["region_id"] for i in result["items"]], [broken["region_id"]])

    def test_a_cached_answer_that_holds_up_is_excluded(self):
        fine = self._record(lt.PASSED, provider=False, cache="answered")
        result = lt.minimal_provider_set([fine])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["excluded"][0]["excluded_reason"], "resolved_by_cache")

    def test_a_needs_review_gate_with_cache_is_still_excluded(self):
        soft = self._record(lt.NEEDS_REVIEW, provider=False, cache="answered")
        self.assertEqual(lt.minimal_provider_set([soft])["items"], [])
