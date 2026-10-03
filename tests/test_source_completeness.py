"""TDD #33 - group source completeness and physical source-truth contract.

The historical defect: the raw OCR line held ``THAT I LOSTCONTROL``, the group
carried ``THAT I LOST``, the rendered page still showed ``NTRO!``, and the
post-render validator never looked for ``CONTROL`` because it asked the group
text - the very representation that had already lost it - what to look for.

These tests fix both halves of the contract: what counts as loss, and where the
physical expectation comes from.  ``CONTROL`` appears here only as fixture shape;
nothing in the implementation knows the word.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import ocr_line_provenance as provenance
import source_completeness as completeness


def line(text, box, line_id=None, parent_ids=(), raw_text=None):
    return {
        "line_id": line_id or f"L{abs(hash((text, box))) % 10**12:012d}",
        "text": text,
        "raw_text": raw_text if raw_text is not None else text,
        "bbox": list(box),
        "parent_ids": list(parent_ids),
    }


class TokenNormalizationTest(unittest.TestCase):
    def test_spacing_repair_is_not_content_loss(self):
        result = completeness.check(
            [line("I'VETURNED INTOABOSS", (10, 10, 300, 30))],
            downstream_text="I'VE TURNED INTO A BOSS",
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)
        self.assertEqual(result["unexplained_missing_tokens"], [])

    def test_compact_source_is_covered_by_spaced_downstream(self):
        result = completeness.check(
            [line("THAT I LOSTCONTROL", (260, 2075, 376, 33))],
            downstream_text="THAT I LOST CONTROL",
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)

    def test_case_normalization_is_not_content_loss(self):
        result = completeness.check(
            [line("KillIng you", (10, 10, 200, 30))],
            downstream_text="killing you",
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)

    def test_line_break_merge_keeps_every_token(self):
        result = completeness.check(
            [
                line("I WILL", (10, 10, 100, 30)),
                line("GO NOW", (10, 40, 100, 30)),
            ],
            downstream_text="I WILL GO NOW",
            downstream_boxes=[(10, 10, 100, 60)],
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)

    def test_punctuation_normalization_is_not_lexical_loss(self):
        result = completeness.check(
            [line("NO... WAIT!", (10, 10, 200, 30))],
            downstream_text="NO WAIT",
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)

    def test_split_and_merge_ancestry_still_accounts_for_the_content(self):
        parent = line("THAT I LOST CONTROL", (260, 2075, 376, 33), line_id="Lparent")
        left = line("THAT I LOST", (260, 2075, 180, 33), parent_ids=["Lparent"])
        right = line("CONTROL", (446, 2075, 190, 33), parent_ids=["Lparent"])
        resolved = completeness.resolve_source_lines([left, right], [parent])
        self.assertEqual([item["line_id"] for item in resolved], ["Lparent"])
        result = completeness.check(
            resolved,
            downstream_text="THAT I LOST CONTROL",
            downstream_boxes=[(260, 2075, 376, 33)],
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)


class SilentLossTest(unittest.TestCase):
    def test_token_lost_without_any_provenance_event_fails(self):
        result = completeness.check(
            [line("THAT I LOST CONTROL", (260, 2075, 376, 33), line_id="La")],
            group_id="BALAO_3",
            downstream_text="THAT I LOST",
            downstream_lines=[line("THAT I LOST", (260, 2075, 376, 33), line_id="La")],
        )
        self.assertEqual(result["status"], completeness.STATUS_FAIL)
        self.assertIn("CONTROL", result["unexplained_missing_tokens"])
        self.assertIn("LOST", result["represented_tokens"])
        self.assertEqual(result["expected_source_basis"], "ocr_provenance_ancestry")

    def test_explicitly_filtered_line_is_an_explained_removal(self):
        result = completeness.check(
            [
                line("REAL DIALOGUE HERE", (10, 10, 200, 30), line_id="Lkeep"),
                line("SMUDGE ARTIFACT", (900, 10, 40, 12), line_id="Ldrop"),
            ],
            downstream_text="REAL DIALOGUE HERE",
            downstream_boxes=[(0, 0, 2000, 2000)],
            discards={"Ldrop": "low_confidence"},
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)
        self.assertIn("SMUDGE", result["explained_removed_tokens"])
        self.assertEqual(result["unexplained_missing_tokens"], [])

    def test_unknown_discard_reason_is_never_a_pass(self):
        for reason in ("", "ignored_by_validator", "because_inconvenient"):
            with self.subTest(reason=reason):
                result = completeness.check(
                    [
                        line("REAL DIALOGUE HERE", (10, 10, 200, 30), line_id="Lkeep"),
                        line("MISSING PHRASE", (10, 40, 200, 30), line_id="Ldrop"),
                    ],
                    downstream_text="REAL DIALOGUE HERE",
                    downstream_boxes=[(0, 0, 2000, 2000)],
                    discards={"Ldrop": reason},
                )
                self.assertNotEqual(result["status"], completeness.STATUS_PASS)
                self.assertIn("MISSING", result["unexplained_missing_tokens"])

    def test_generic_reason_cannot_be_invented_to_satisfy_the_check(self):
        self.assertFalse(completeness.discard_is_explained("ignored_by_validator"))
        self.assertTrue(completeness.discard_is_explained("low_confidence"))
        self.assertFalse(
            # "something else took this line's place" is not evidence that the
            # replacement kept the line's content.  It was the blanket excuse
            # that let a truncating retry validate against its own survivor.
            completeness.discard_is_explained("replaced_by_speech_container_reocr")
        )
        self.assertTrue(completeness.discard_is_explained("recovery_noise_removed"))


def _full7_shaped_page():
    """One page in the exact provenance shape a truncating retry produces.

    A wide predecessor line is superseded by a narrower re-read of the same
    region; the final group holds only the survivor.  Nothing in this fixture is
    specific to the run it was taken from - it is the general shape of any
    destructive recovery.
    """

    predecessor = line("THAT I LOSTCONTROL", (260, 2075, 376, 33), line_id="Lpred")
    neighbour = line("A DIFFERENT BALLOON", (900, 2075, 300, 33), line_id="Lneigh")
    survivor = line("THAT I LOST", (260, 2075, 212, 32), line_id="Lnew")
    return {
        "page": 2,
        "raw_lines": [predecessor, neighbour, survivor],
        "events": [
            {
                "operation": "group_member",
                "group_id": "BALAO_3",
                "parent_ids": ["Lpred"],
                "after": {"text": "THAT I LOSTCONTROL", "bbox": [260, 2075, 376, 33]},
            },
            {
                "operation": "group_member",
                "group_id": "BALAO_9",
                "parent_ids": ["Lneigh"],
                "after": {"text": "A DIFFERENT BALLOON", "bbox": [900, 2075, 300, 33]},
            },
            {
                "operation": "line_filtered",
                "line_id": "Lpred",
                "reason": "replaced_by_rapidocr_region_recovery",
            },
            {
                "operation": "group_geometry",
                "group_id": "BALAO_3",
                "parent_ids": ["Lnew"],
                "before": {"text": "THAT I LOSTCONTROL", "bbox": [260, 2075, 376, 33]},
                "after": {"text": "THAT I LOST", "bbox": [260, 2075, 212, 32]},
                "reason": "regrouped",
            },
        ],
        "groups": [
            {
                "group_id": "BALAO_3",
                "member_line_ids": ["Lnew"],
                "member_lines": [survivor],
                "text": "THAT I LOST",
                "bbox": [260, 2075, 212, 32],
            },
            {
                "group_id": "BALAO_9",
                "member_line_ids": ["Lneigh"],
                "member_lines": [neighbour],
                "text": "A DIFFERENT BALLOON",
                "bbox": [900, 2075, 300, 33],
            },
        ],
        "render_inputs": [
            {
                "group_id": "BALAO_3",
                "member_line_ids": ["Lnew"],
                "render_input_lines": [survivor],
                "render_input_text": "THAT I LOST",
                "render_input_bbox": [260, 2075, 212, 32],
                "draw_box": [260, 2075, 212, 32],
            },
            {
                "group_id": "BALAO_9",
                "member_line_ids": ["Lneigh"],
                "render_input_lines": [neighbour],
                "render_input_text": "A DIFFERENT BALLOON",
                "render_input_bbox": [900, 2075, 300, 33],
                "draw_box": [900, 2075, 300, 33],
            },
        ],
    }


class AncestryOwnershipTest(unittest.TestCase):
    """TDD #35: expected source is the ancestry closure, not the survivors."""

    def setUp(self):
        self.page = _full7_shaped_page()
        self.results = {
            item["group_id"]: item for item in completeness.check_page(self.page)
        }

    def test_surviving_member_basis_would_have_passed(self):
        # The mandatory false-pass regression: scored against only the lines the
        # recovery left behind, the destroyed content is not even expected, so
        # the old basis reports a clean group for a group that lost a word.
        survivor_only = completeness.check(
            self.page["groups"][0]["member_lines"],
            group_id="BALAO_3",
            downstream_text="THAT I LOST",
            downstream_boxes=[[260, 2075, 212, 32]],
        )
        self.assertEqual(survivor_only["status"], completeness.STATUS_PASS)
        self.assertNotIn("CONTROL", survivor_only["expected_tokens"])

    def test_ancestry_basis_detects_the_destroyed_predecessor_token(self):
        result = self.results["BALAO_3"]
        # The predecessor was read without its space, so the token it owns is
        # the joined one.  What matters is that the destroyed tail is expected
        # at all and is reported missing, instead of never being asked about.
        self.assertIn("LOSTCONTROL", result["expected_tokens"])
        self.assertIn("LOSTCONTROL", result["unexplained_missing_tokens"])
        self.assertEqual(result["status"], completeness.STATUS_FAIL)

    def test_ancestry_basis_keeps_the_predecessor_geometry(self):
        result = self.results["BALAO_3"]
        x, y, width, height = result["source_bbox"]
        self.assertLessEqual(x, 260)
        self.assertGreaterEqual(x + width, 260 + 376)

    def test_neighbouring_balloon_is_never_pulled_into_the_ancestry(self):
        result = self.results["BALAO_3"]
        self.assertNotIn("Lneigh", result["source_line_ids"])
        self.assertNotIn("BALLOON", result["expected_tokens"])
        self.assertEqual(self.results["BALAO_9"]["status"], completeness.STATUS_PASS)

    def test_reported_basis_names_the_ancestry(self):
        self.assertEqual(
            self.results["BALAO_3"]["expected_source_basis"],
            "ocr_provenance_ancestry",
        )

    def test_a_reconciled_recovery_still_passes(self):
        page = _full7_shaped_page()
        for holder in (page["groups"][0], page["render_inputs"][0]):
            for key in ("text", "render_input_text"):
                if key in holder:
                    holder[key] = "THAT I LOST CONTROL"
            for key in ("bbox", "render_input_bbox", "draw_box"):
                if key in holder:
                    holder[key] = [260, 2075, 376, 33]
        page["groups"][0]["member_lines"][0]["text"] = "THAT I LOST CONTROL"
        page["groups"][0]["member_lines"][0]["bbox"] = [260, 2075, 376, 33]
        results = {item["group_id"]: item for item in completeness.check_page(page)}
        self.assertEqual(results["BALAO_3"]["status"], completeness.STATUS_PASS)


class GeometryCompletenessTest(unittest.TestCase):
    def test_truncated_render_geometry_is_a_violation(self):
        result = completeness.check(
            [line("THAT I LOST CONTROL", (260, 2075, 376, 33), line_id="La")],
            downstream_text="THAT I LOST CONTROL",
            downstream_boxes=[(260, 2075, 354, 33)],
        )
        self.assertEqual(result["status"], completeness.STATUS_REVIEW)
        self.assertEqual(result["uncovered_source_line_ids"], ["La"])
        self.assertEqual(result["source_bbox"], [260, 2075, 376, 33])

    def test_small_padding_difference_still_passes(self):
        result = completeness.check(
            [line("THAT I LOST CONTROL", (260, 2075, 376, 33), line_id="La")],
            downstream_text="THAT I LOST CONTROL",
            downstream_boxes=[(262, 2077, 372, 30)],
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)
        self.assertEqual(result["uncovered_source_line_ids"], [])


class LegacyRunTest(unittest.TestCase):
    def test_missing_provenance_is_unavailable_not_pass(self):
        result = completeness.check([], group_id="BALAO_1", downstream_text="ANYTHING")
        self.assertEqual(result["status"], completeness.STATUS_UNAVAILABLE)
        self.assertEqual(result["expected_tokens"], [])

    def test_legacy_artifact_is_never_scored_from_final_groups(self):
        summary = completeness.check_artifact(
            {"status": "unavailable", "reason": "no_provenance_artifact"}
        )
        self.assertEqual(summary["status"], completeness.STATUS_UNAVAILABLE)
        self.assertEqual(summary["pass"], 0)
        self.assertEqual(summary["checked_groups"], 0)


class NeighbourIsolationTest(unittest.TestCase):
    def test_source_owned_by_another_group_is_not_expected_here(self):
        result = completeness.check(
            [line("THAT I LOST CONTROL", (260, 2075, 376, 33))],
            downstream_text="THAT I LOST CONTROL",
            downstream_boxes=[(260, 2075, 376, 33)],
        )
        self.assertEqual(result["status"], completeness.STATUS_PASS)
        self.assertNotIn("BARELY", result["expected_tokens"])


def _split_recovery_page():
    """One recovery attempt that legitimately yields two final groups.

    A single pre-recovery group holds a garbled short line stacked above a
    longer sentence.  The recovery re-reads the region and the regrouping keeps
    the short line in the original group while the sentence becomes a group of
    its own.  Both halves are legitimate; neither may be charged with the
    other's lexical obligations.
    """

    old_short = line("iK3H", (379, 2076, 89, 43), line_id="Lold_short")
    old_a = line("SHOUT AT", (325, 2123, 195, 40), line_id="Lold_a")
    old_b = line("THE MOUNTAIN", (314, 2172, 217, 38), line_id="Lold_b")
    new_short = line("HEY!", (382, 2079, 80, 36), line_id="Lnew_short")
    new_a = line("SHOUT AT", (328, 2128, 190, 34), line_id="Lnew_a")
    new_b = line("THE MOUNTAIN", (317, 2175, 213, 33), line_id="Lnew_b")
    return {
        "page": 76,
        "raw_lines": [old_short, old_a, old_b, new_short, new_a, new_b],
        "events": [
            {
                "operation": "group_member",
                "group_id": "BALAO_3",
                "parent_ids": ["Lold_short", "Lold_a", "Lold_b"],
            },
            {
                "operation": "line_filtered",
                "line_id": "Lold_short",
                "reason": "replaced_by_rapidocr_region_recovery",
            },
            {
                "operation": "line_filtered",
                "line_id": "Lold_a",
                "reason": "replaced_by_rapidocr_region_recovery",
            },
            {
                "operation": "line_filtered",
                "line_id": "Lold_b",
                "reason": "replaced_by_rapidocr_region_recovery",
            },
            {
                "operation": "group_geometry",
                "group_id": "BALAO_3",
                "parent_ids": ["Lnew_short"],
                "reason": "regrouped",
            },
            {
                "operation": "group_member",
                "group_id": "BALAO_4",
                "parent_ids": ["Lnew_a", "Lnew_b"],
            },
        ],
        "groups": [
            {
                "group_id": "BALAO_3",
                "member_line_ids": ["Lnew_short"],
                "member_lines": [new_short],
                "text": "HEY!",
                "bbox": [382, 2079, 80, 36],
            },
            {
                "group_id": "BALAO_4",
                "member_line_ids": ["Lnew_a", "Lnew_b"],
                "member_lines": [new_a, new_b],
                "text": "SHOUT AT THE MOUNTAIN",
                "bbox": [317, 2128, 213, 80],
            },
        ],
        "render_inputs": [
            {
                "group_id": "BALAO_3",
                "render_input_lines": [new_short],
                "render_input_text": "HEY!",
                "render_input_bbox": [382, 2079, 80, 36],
                "draw_box": [382, 2079, 80, 36],
            },
            {
                "group_id": "BALAO_4",
                "render_input_lines": [new_a, new_b],
                "render_input_text": "SHOUT AT THE MOUNTAIN",
                "render_input_bbox": [317, 2128, 213, 80],
                "draw_box": [317, 2128, 213, 80],
            },
        ],
    }


class SiblingOwnershipTest(unittest.TestCase):
    """TDD #41: ancestry is scoped by ownership, not by shared history.

    One recovery event produced two legitimate groups.  The predecessor lines of
    the half that moved out belong to the group that now owns that geometry, so
    charging them to the group that kept only the short line is a false failure -
    and it widens that group's searched region over its neighbour's still
    untranslated balloon.
    """

    def setUp(self):
        self.results = {
            item["group_id"]: item
            for item in completeness.check_page(_split_recovery_page())
        }

    def test_predecessor_of_the_sibling_half_is_not_charged_here(self):
        result = self.results["BALAO_3"]
        self.assertNotIn("MOUNTAIN", result["expected_tokens"])
        self.assertNotIn("SHOUT", result["expected_tokens"])
        self.assertEqual(result["unexplained_missing_tokens"], [])
        self.assertEqual(result["status"], completeness.STATUS_PASS)

    def test_owned_predecessor_of_the_short_line_is_still_charged(self):
        self.assertIn("Lold_short", self.results["BALAO_3"]["source_line_ids"])

    def test_the_contaminated_region_no_longer_reaches_the_sibling(self):
        source_box = self.results["BALAO_3"]["source_bbox"]
        self.assertLess(source_box[1] + source_box[3], 2128)

    def test_the_sibling_group_keeps_its_own_obligations(self):
        result = self.results["BALAO_4"]
        self.assertIn("MOUNTAIN", result["expected_tokens"])
        self.assertEqual(result["status"], completeness.STATUS_PASS)

    def test_a_narrowing_retry_in_place_is_still_a_loss(self):
        # The destructive-recovery regression must survive the scoping: the
        # predecessor that was re-read over the *same* geometry has no sibling
        # owner, so it stays this group's obligation.
        results = {
            item["group_id"]: item
            for item in completeness.check_page(_full7_shaped_page())
        }
        self.assertEqual(results["BALAO_3"]["status"], completeness.STATUS_FAIL)
        self.assertIn("LOSTCONTROL", results["BALAO_3"]["unexplained_missing_tokens"])


class RecordedPageTest(unittest.TestCase):
    """The contract over a real recorder page, live and replayed."""

    def setUp(self):
        self.recorder = provenance.activate()
        self.addCleanup(provenance.deactivate)

    def record(self, raw_text, group_text, group_box, member_box=None):
        from test_ocr_line_provenance import FakeGroup, make_line

        source = make_line(raw_text, (260, 2075, 376, 33))
        group = FakeGroup("BALAO_3", [source], text=raw_text)
        with provenance.page(2):
            provenance.record_input_lines([source])
            source.text = group_text
            if member_box:
                source.box = member_box
            group.text = group_text
            provenance.record_group(group)
            provenance.record_render_input(group, bbox=group_box)
            live = completeness.check_live_group(group)
        return group, live

    def test_live_check_detects_the_historical_truncation_class(self):
        _group, live = self.record(
            "THAT I LOSTCONTROL", "THAT I LOST", (260, 2075, 354, 33)
        )
        self.assertEqual(live["status"], completeness.STATUS_FAIL)
        self.assertIn("LOSTCONTROL", live["unexplained_missing_tokens"])

    def test_live_check_passes_when_the_content_survived(self):
        _group, live = self.record(
            "THAT I LOSTCONTROL", "THAT I LOST CONTROL", (260, 2075, 376, 33)
        )
        self.assertEqual(live["status"], completeness.STATUS_PASS)
        self.assertEqual(live["unexplained_missing_tokens"], [])

    def test_replay_of_the_recorded_page_matches_the_live_answer(self):
        self.record("THAT I LOSTCONTROL", "THAT I LOST", (260, 2075, 354, 33))
        summary = completeness.check_artifact(
            {"status": "available", **self.recorder.to_dict()}
        )
        self.assertEqual(summary["status"], completeness.STATUS_FAIL)
        self.assertEqual(summary["fail"], 1)
        self.assertIn("LOSTCONTROL", summary["missing_lexical_tokens"])

    def test_live_check_is_unavailable_without_a_recorder(self):
        from test_ocr_line_provenance import FakeGroup, make_line

        provenance.deactivate()
        group = FakeGroup("BALAO_3", [make_line("ANY TEXT", (0, 0, 10, 10))])
        result = completeness.check_live_group(group)
        self.assertEqual(result["status"], completeness.STATUS_UNAVAILABLE)


class PhysicalExpectationSourceTest(unittest.TestCase):
    """Contract B: the expectation comes from provenance, not from group text."""

    def setUp(self):
        self.recorder = provenance.activate()
        self.addCleanup(provenance.deactivate)

    def build(self, raw_text, group_text):
        from test_ocr_line_provenance import FakeGroup, make_line

        source = make_line(raw_text, (260, 2075, 376, 33))
        group = FakeGroup("BALAO_3", [source], text=raw_text)
        with provenance.page(2):
            provenance.record_input_lines([source])
            source.text = group_text
            group.text = group_text
            provenance.record_group(group)
            tokens, result = completeness.expected_physical_tokens(group)
        return tokens, result

    def test_expectation_keeps_a_token_the_group_text_lost(self):
        tokens, result = self.build("THAT I LOST CONTROL", "THAT I LOST")
        self.assertIn("CONTROL", tokens)
        self.assertEqual(result["expected_source_basis"], "ocr_provenance_ancestry")
        self.assertTrue(result["source_line_ids"])

    def test_group_text_alone_would_have_missed_it(self):
        # The pre-patch expectation, reproduced: this is the blind spot.
        self.assertNotIn(
            "CONTROL", set(completeness.meaningful_tokens("THAT I LOST"))
        )

    def test_intact_group_text_yields_the_same_expectation(self):
        tokens, result = self.build("THAT I LOST CONTROL", "THAT I LOST CONTROL")
        self.assertIn("CONTROL", tokens)
        self.assertEqual(result["status"], completeness.STATUS_PASS)


class PhysicalRegionAccountingTest(unittest.TestCase):
    def test_completeness_violation_blocks_the_physical_gate(self):
        import benchmark_pipeline

        states = [
            {
                "index": 2,
                "debug_data": {
                    "items": [
                        {
                            "id": "BALAO_3",
                            "classification": "speech",
                            "translation": "QUE EU PERDI O CONTROLE",
                            "translation_valid": True,
                            "redrawn": True,
                            "translation_final_state": "translated",
                            "translation_final_reason": "ok",
                            "source_completeness_status": "fail",
                            "source_completeness": {
                                "unexplained_missing_tokens": ["CONTROL"]
                            },
                        }
                    ]
                },
            }
        ]
        result = benchmark_pipeline._physical_residual_accounting(states)
        self.assertFalse(result["physical_gate_passed"])
        self.assertEqual(result["physical_source_residual_count"], 1)
        self.assertEqual(result["source_completeness"]["fail"], 1)
        self.assertEqual(result["source_completeness"]["missing_tokens"], ["CONTROL"])
        self.assertEqual(result["source_completeness"]["group_ids"], ["p002:BALAO_3"])

    def test_a_clean_group_still_passes_the_physical_gate(self):
        import benchmark_pipeline

        states = [
            {
                "index": 2,
                "debug_data": {
                    "items": [
                        {
                            "id": "BALAO_3",
                            "classification": "speech",
                            "translation": "QUE EU PERDI O CONTROLE",
                            "translation_valid": True,
                            "redrawn": True,
                            "translation_final_state": "translated",
                            "translation_final_reason": "ok",
                            "source_completeness_status": "pass",
                        }
                    ]
                },
            }
        ]
        result = benchmark_pipeline._physical_residual_accounting(states)
        self.assertTrue(result["physical_gate_passed"])
        self.assertEqual(result["physical_source_residual_count"], 0)
        self.assertEqual(result["source_completeness"]["pass"], 1)

    def test_a_legacy_run_without_provenance_adds_no_completeness_block(self):
        import benchmark_pipeline

        states = [
            {
                "index": 2,
                "debug_data": {
                    "items": [
                        {
                            "id": "BALAO_3",
                            "classification": "speech",
                            "translation": "QUE EU PERDI O CONTROLE",
                            "translation_valid": True,
                            "redrawn": True,
                            "translation_final_state": "translated",
                            "translation_final_reason": "ok",
                        }
                    ]
                },
            }
        ]
        result = benchmark_pipeline._physical_residual_accounting(states)
        self.assertNotIn("source_completeness", result)
        self.assertTrue(result["physical_gate_passed"])


class PhysicalSourceTruthTest(unittest.TestCase):
    """Contract B at the pixel boundary, with the post-render OCR pass stubbed.

    Only the independent pass is faked: what it observes is the input to the
    decision under test, and the decision is what these tests are about.
    """

    def setUp(self):
        import numpy as np

        import ocr_balloon

        self.ocr_balloon = ocr_balloon
        self.rendered = np.zeros((3000, 1000, 3), dtype="uint8")
        self.recorder = provenance.activate()
        self.addCleanup(provenance.deactivate)

    def stub_post_render_ocr(self, observed_text):
        from test_ocr_line_provenance import make_line

        class StubEngine:
            def __init__(self, *args, **kwargs):
                pass

            def _detect_with_rapidocr(self, crop):
                return [make_line(observed_text, (0, 0, 10, 10))]

        original = self.ocr_balloon.OCREngine
        self.ocr_balloon.OCREngine = StubEngine
        self.addCleanup(setattr, self.ocr_balloon, "OCREngine", original)

    def check(
        self,
        raw_text,
        group_text,
        translation,
        observed_text,
        proper_names=(),
        final_reason="ok",
    ):
        from test_ocr_line_provenance import make_line

        self.stub_post_render_ocr(observed_text)
        source = make_line(raw_text, (260, 2075, 376, 33))
        group = self.ocr_balloon.TextGroup(group_id="BALAO_3", lines=[source])
        group.text = raw_text
        group.classification = "speech"
        group.translation = translation
        group.detected_proper_names = list(proper_names)
        group.translation_final_reason = final_reason
        group.draw_box = (260, 2075, 354, 33)
        with provenance.page(2):
            provenance.record_input_lines([source])
            source.text = group_text
            group.text = group_text
            provenance.record_group(group)
            return self.ocr_balloon._post_render_source_text_check(
                self.rendered, group, 2
            )

    def test_token_only_provenance_remembers_is_detected_after_render(self):
        summary = self.check(
            "THAT I LOST CONTROL",
            "THAT I LOST",
            "QUE EU PERDI",
            "QUE EU PERDI CONTROL",
        )
        self.assertIn("CONTROL", summary["expected_tokens"])
        self.assertIn("CONTROL", summary["detected_residual_tokens"])
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["expected_source_basis"], "ocr_provenance_ancestry")
        self.assertTrue(summary["source_line_ids"])

    def test_group_text_complete_and_pixels_clean_still_passes(self):
        summary = self.check(
            "THAT I LOST CONTROL",
            "THAT I LOST CONTROL",
            "QUE EU PERDI O CONTROLE",
            "QUE EU PERDI O CONTROLE",
        )
        self.assertTrue(summary["passed"], summary["reason"])
        self.assertEqual(summary["detected_residual_tokens"], [])

    def test_ordinary_english_left_on_the_page_fails(self):
        summary = self.check(
            "THEY ARE COMING",
            "THEY ARE COMING",
            "ELES ESTAO VINDO",
            "THEY ARE COMING",
        )
        self.assertFalse(summary["passed"])
        self.assertTrue(summary["detected_residual_tokens"])

    def test_preserved_entity_is_excluded_not_called_residual(self):
        summary = self.check(
            "HYEON IS HERE",
            "HYEON IS HERE",
            "HYEON ESTA AQUI",
            "HYEON ESTA AQUI",
            proper_names=["HYEON"],
        )
        self.assertIn("HYEON", summary["excluded_preserved_tokens"])
        self.assertNotIn("HYEON", summary["expected_tokens"])
        self.assertNotIn("HYEON", summary["detected_residual_tokens"])

    def test_unreadable_source_keeps_its_own_taxonomy(self):
        # Provenance must not widen the expectation for a group held as
        # unreadable: whatever this pass reports about it stays exactly what the
        # pre-provenance behaviour reported, so the OCR_UNINTELLIGIBLE /
        # MANUAL_REVIEW taxonomy is never re-labelled as English residual.
        summary = self.check(
            "iHon SOMETHING ELSE",
            "iHon",
            "",
            "iHon",
            final_reason=self.ocr_balloon.OCR_UNINTELLIGIBLE_SOURCE_REASON,
        )
        self.assertEqual(summary["expected_tokens"], ["IHON"])
        self.assertNotIn("SOMETHING", summary["expected_tokens"])

    def test_provenance_widens_the_expectation_only_for_readable_source(self):
        summary = self.check(
            "iHon SOMETHING ELSE",
            "iHon",
            "",
            "iHon",
        )
        self.assertIn("SOMETHING", summary["expected_tokens"])

    def test_searched_region_is_group_owned_source_geometry(self):
        summary = self.check(
            "THAT I LOST CONTROL",
            "THAT I LOST",
            "QUE EU PERDI",
            "QUE EU PERDI",
        )
        # The region covers the group's own source line, widened past the
        # truncated draw box, and stops there: no neighbour is swept in.
        x, y, w, h = summary["searched_region"]
        self.assertLessEqual(x, 260)
        self.assertGreaterEqual(x + w, 260 + 376)
        self.assertLess(h, 200)


if __name__ == "__main__":
    unittest.main()
