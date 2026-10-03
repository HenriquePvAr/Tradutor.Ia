"""TDD #32: OCR line provenance from raw detection to renderer input.

These tests observe the transformation chain.  They deliberately do not fix any
text/geometry loss they expose: correctness of the transformation itself belongs
to a later mission, observability belongs here.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import ocr_line_provenance as provenance
from ocr_engine import OCRLine


def make_line(text, box, confidence=0.99, engine="rapidocr", page=2):
    x, y, w, h = box
    polygon = np.asarray(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32
    )
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=polygon,
        box=(x, y, w, h),
        raw_text=text,
        engine=engine,
        page=page,
    )


class FakeGroup:
    def __init__(self, group_id, lines, text="", translation="", draw_box=None):
        self.group_id = group_id
        self.lines = list(lines)
        self.text = text or " ".join(line.text for line in lines)
        self.translation = translation
        self.draw_box = draw_box

    @property
    def box(self):
        boxes = [line.box for line in self.lines]
        if not boxes:
            return (0, 0, 0, 0)
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes)
        y1 = max(b[1] + b[3] for b in boxes)
        return (x0, y0, x1 - x0, y1 - y0)


class LineIdentityTest(unittest.TestCase):
    def test_identity_is_deterministic_and_not_index_based(self):
        first = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        second = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        (id_a, _), = provenance.ensure_line_ids([first], page=2)
        (id_b, _), = provenance.ensure_line_ids([second], page=2)
        self.assertTrue(id_a.startswith("L"))
        self.assertEqual(id_a, id_b)

        moved = make_line("THAT I LOST CONTROL", (260, 2075, 354, 33))
        (id_c, _), = provenance.ensure_line_ids([moved], page=2)
        self.assertNotEqual(id_a, id_c)

    def test_reassigned_identity_keeps_the_parent(self):
        line = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        (original_id, parent), = provenance.ensure_line_ids([line], page=2)
        self.assertEqual(parent, "")
        line.box = (260, 2075, 354, 33)
        (derived_id, derived_parent), = provenance.ensure_line_ids([line], page=2)
        self.assertNotEqual(derived_id, original_id)
        self.assertEqual(derived_parent, original_id)


class ProvenanceRecordingTest(unittest.TestCase):
    def setUp(self):
        self.recorder = provenance.activate()
        self.addCleanup(provenance.deactivate)

    def events(self, page=2, operation=None):
        payload = self.recorder.to_dict()
        entries = next(item for item in payload["pages"] if item["page"] == page)["events"]
        if operation is None:
            return entries
        return [event for event in entries if event["operation"] == operation]

    def test_raw_snapshot_survives_later_in_place_mutation(self):
        line = make_line("THAT I LOSTCONTROL", (260, 2075, 376, 33))
        with provenance.page(2):
            provenance.record_input_lines([line])
            # The pipeline rewrites the very same object further down the chain.
            line.text = "THAT I LOST CONT"
            line.box = (260, 2075, 354, 33)
            provenance.record_group(FakeGroup("BALAO_3", [line]))

        payload = self.recorder.to_dict()
        raw = next(item for item in payload["pages"] if item["page"] == 2)["raw_lines"][0]
        self.assertEqual(raw["text"], "THAT I LOSTCONTROL")
        self.assertEqual(raw["bbox"], [260, 2075, 376, 33])

    def test_geometry_change_records_both_boxes(self):
        line = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        group = FakeGroup("BALAO_3", [line])
        with provenance.page(2):
            provenance.record_input_lines([line])
            provenance.record_group(group)
            line.box = (260, 2075, 354, 33)
            provenance.record_group(group)

        geometry = self.events(operation="group_geometry")
        self.assertEqual(len(geometry), 1)
        self.assertEqual(geometry[0]["before"]["bbox"], [260, 2075, 376, 33])
        self.assertEqual(geometry[0]["after"]["bbox"], [260, 2075, 354, 33])
        self.assertEqual(geometry[0]["reason"], "regrouped")

    def test_text_truncation_is_visible_without_being_repaired(self):
        line = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        with provenance.page(2):
            provenance.record_input_lines([line])
            provenance.record_normalization(
                line, "THAT I LOST CONTROL", "THAT I LOST CONT", reason="normalize"
            )

        normalized = self.events(operation="ocr_normalized")
        self.assertEqual(normalized[0]["before"]["text"], "THAT I LOST CONTROL")
        self.assertEqual(normalized[0]["after"]["text"], "THAT I LOST CONT")

    def test_engine_level_repair_is_recorded_with_both_texts(self):
        line = make_line("THAT I LOSTCONTROL", (260, 2075, 376, 33))
        line.raw_text = "THATI LOSTCONTROL"
        line.repair_reason = "segment_compact_english_word"
        with provenance.page(2):
            provenance.record_input_lines([line])

        normalized = self.events(operation="ocr_normalized")
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["before"]["text"], "THATI LOSTCONTROL")
        self.assertEqual(normalized[0]["after"]["text"], "THAT I LOSTCONTROL")
        self.assertEqual(normalized[0]["reason"], "segment_compact_english_word")

    def test_split_lines_reference_their_parent(self):
        parent = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        with provenance.page(2):
            (parent_id, _), = provenance.ensure_line_ids([parent], page=2)
            provenance.record_input_lines([parent])
            left = make_line("THAT I LOST", (260, 2075, 180, 33))
            right = make_line("CONTROL", (446, 2075, 190, 33))
            for child in (left, right):
                (child_id, _), = provenance.ensure_line_ids([child], page=2)
                provenance.record_event(
                    "line_split",
                    line_id=child_id,
                    parent_ids=[parent_id],
                    reason="sentence_boundary",
                )

        splits = self.events(operation="line_split")
        self.assertEqual(len(splits), 2)
        for event in splits:
            self.assertEqual(event["parent_ids"], [parent_id])

    def test_merged_line_references_every_parent(self):
        left = make_line("THAT I LOST", (260, 2075, 180, 33))
        right = make_line("CONTROL", (446, 2075, 190, 33))
        with provenance.page(2):
            ids = [lid for lid, _ in provenance.ensure_line_ids([left, right], page=2)]
            provenance.record_input_lines([left, right])
            merged = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
            (merged_id, _), = provenance.ensure_line_ids([merged], page=2)
            provenance.record_event(
                "line_merge", line_id=merged_id, parent_ids=ids, reason="same_row"
            )

        merges = self.events(operation="line_merge")
        self.assertEqual(merges[0]["parent_ids"], ids)

    def test_filtered_line_records_an_explicit_reason(self):
        kept = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        dropped = make_line("!!", (700, 2075, 20, 12), confidence=0.2)
        with provenance.page(2):
            provenance.record_input_lines([kept, dropped])
            provenance.record_filtered(dropped, "low_confidence")

        filtered = self.events(operation="line_filtered")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["reason"], "low_confidence")
        self.assertEqual(filtered[0]["before"]["text"], "!!")

    def test_injected_line_is_never_anonymous(self):
        original = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        with provenance.page(2):
            provenance.record_input_lines([original])
            injected = make_line("AGAIN AND ALMOST", (282, 2121, 333, 33))
            provenance.record_replacement(
                [original], [original, injected], reason="speech_container_reocr"
            )

        injections = self.events(operation="line_injected")
        self.assertEqual(len(injections), 1)
        self.assertEqual(injections[0]["reason"], "speech_container_reocr")
        self.assertTrue(injections[0]["line_id"])
        replacements = self.events(operation="ocr_retry_replacement")
        self.assertEqual(replacements[0]["before"]["line_count"], 1)
        self.assertEqual(replacements[0]["after"]["line_count"], 2)

    def test_group_membership_identifies_contributing_lines(self):
        lines = [
            make_line("IT'S NOT JUST", (325, 2028, 244, 37)),
            make_line("THAT I LOST CONTROL", (260, 2075, 376, 33)),
        ]
        other = make_line("I BARELY", (535, 2609, 134, 35))
        with provenance.page(2):
            provenance.record_input_lines(lines + [other])
            snapshot = provenance.record_group(FakeGroup("BALAO_3", lines))

        expected = [lid for lid, _ in provenance.ensure_line_ids(lines, page=2)]
        self.assertEqual(snapshot["member_line_ids"], expected)
        (other_id, _), = provenance.ensure_line_ids([other], page=2)
        self.assertNotIn(other_id, snapshot["member_line_ids"])

    def test_render_input_snapshot_matches_what_the_renderer_received(self):
        lines = [make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))]
        group = FakeGroup("BALAO_3", lines, translation="QUE EU PERDI O CONTROLE")
        rendered = {}

        def fake_renderer(target):
            rendered["text"] = target.text
            rendered["bbox"] = tuple(target.box)

        with provenance.page(2):
            provenance.record_input_lines(lines)
            entry = provenance.record_render_input(group)
            fake_renderer(group)

        self.assertEqual(entry["render_input_text"], rendered["text"])
        self.assertEqual(tuple(entry["render_input_bbox"]), rendered["bbox"])
        self.assertEqual(entry["render_input_translation"], "QUE EU PERDI O CONTROLE")
        self.assertEqual(
            entry["member_line_ids"],
            [lid for lid, _ in provenance.ensure_line_ids(lines, page=2)],
        )

    def test_raw_line_complete_while_render_input_truncated_is_observable(self):
        # TDD #32 observes the defect class, it does not repair it.
        line = make_line("THAT I LOSTCONTROL", (260, 2075, 376, 33))
        group = FakeGroup("BALAO_3", [line])
        with provenance.page(2):
            provenance.record_input_lines([line])
            group.text = "THAT I LOST"
            line.box = (260, 2075, 354, 33)
            provenance.record_render_input(group)

        payload = self.recorder.to_dict()
        page = next(item for item in payload["pages"] if item["page"] == 2)
        self.assertIn("CONTROL", page["raw_lines"][0]["text"])
        self.assertNotIn("CONTROL", page["render_inputs"][0]["render_input_text"])
        self.assertEqual(page["raw_lines"][0]["bbox"], [260, 2075, 376, 33])
        self.assertEqual(page["render_inputs"][0]["render_input_bbox"], [260, 2075, 354, 33])

    def test_unknown_operation_is_rejected(self):
        with provenance.page(2):
            with self.assertRaises(ValueError):
                provenance.record_event("invented_operation")

    def test_recording_is_inert_without_an_active_recorder(self):
        provenance.deactivate()
        line = make_line("THAT I LOST CONTROL", (260, 2075, 376, 33))
        with provenance.page(2) as current:
            self.assertIsNone(current)
            self.assertEqual(provenance.record_input_lines([line]), [])
            self.assertIsNone(provenance.record_event("ocr_raw"))
            self.assertIsNone(provenance.record_group(FakeGroup("BALAO_1", [line])))


class ArtifactTest(unittest.TestCase):
    def test_artifact_roundtrip_and_summary(self):
        recorder = provenance.activate()
        self.addCleanup(provenance.deactivate)
        lines = [
            make_line("IT'S NOT JUST", (325, 2028, 244, 37)),
            make_line("THAT I LOSTCONTROL", (260, 2075, 376, 33)),
        ]
        group = FakeGroup("BALAO_3", lines)
        with provenance.page(2):
            provenance.record_input_lines(lines)
            provenance.record_group(group)
            provenance.record_render_input(group)

        with tempfile.TemporaryDirectory() as folder:
            summary = provenance.write_artifact(folder)
            self.assertEqual(summary["raw_lines"], 2)
            self.assertEqual(summary["group_members"], 2)
            self.assertEqual(summary["traceable_group_members"], 2)
            self.assertEqual(summary["render_inputs"], 1)
            self.assertEqual(summary["traceable_render_inputs"], 1)
            self.assertGreater(summary["artifact_bytes"], 0)

            loaded = provenance.load_artifact(folder)
            self.assertEqual(loaded["status"], "available")
            self.assertEqual(loaded["schema_version"], provenance.SCHEMA_VERSION)
            self.assertEqual(len(loaded["pages"][0]["raw_lines"]), 2)

    def test_legacy_run_reports_unavailable_without_reconstructing(self):
        with tempfile.TemporaryDirectory() as folder:
            loaded = provenance.load_artifact(folder)
            self.assertEqual(loaded["status"], "unavailable")
            self.assertEqual(loaded["reason"], "no_provenance_artifact")
            self.assertNotIn("pages", loaded)

    def test_unsupported_schema_version_is_unavailable_not_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / provenance.ARTIFACT_FILENAME
            path.write_text(json.dumps({"schema_version": 999, "pages": []}), encoding="utf-8")
            loaded = provenance.load_artifact(folder)
            self.assertEqual(loaded["status"], "unavailable")
            self.assertEqual(loaded["reason"], "unsupported_schema_version")

    def test_artifact_carries_no_credential_shaped_fields(self):
        recorder = provenance.activate()
        self.addCleanup(provenance.deactivate)
        with provenance.page(2):
            provenance.record_input_lines([make_line("HELLO", (10, 10, 50, 20))])
        blob = json.dumps(recorder.to_dict()).lower()
        for forbidden in ("api_key", "token", "cookie", "password", "secret", "base64"):
            self.assertNotIn(forbidden, blob)


class ManifestReferenceTest(unittest.TestCase):
    def build(self, **extra):
        import output_manifest

        return output_manifest.build_run_manifest(
            run_id="r1",
            created_at="2026-08-15T00:00:00+00:00",
            source_url="https://example.com/a/b",
            commit_hash="abc123",
            branch="fix/main-e2e-findings",
            pipeline_version="1",
            model="deepl",
            final_status="completed",
            quality_passed=False,
            manual_review_count=0,
            rejected_count=0,
            pdf_path="/out/run/x.pdf",
            slug="run",
            **extra,
        )

    def test_manifest_carries_a_bounded_provenance_index(self):
        import output_manifest

        manifest = self.build(
            ocr_line_provenance={
                "schema_version": 1,
                "pages": 2,
                "raw_lines": 34,
                "group_members": 30,
                "traceable_group_members": 30,
                "render_inputs": 9,
                "traceable_render_inputs": 9,
                "events": 80,
                "artifact": provenance.ARTIFACT_FILENAME,
                "artifact_bytes": 4096,
            }
        )
        index = manifest["ocr_line_provenance"]
        self.assertEqual(index["schema_version"], 1)
        self.assertEqual(index["raw_lines"], 34)
        self.assertEqual(index["artifact"], provenance.ARTIFACT_FILENAME)
        # The manifest must stay an index, never a dump of the line evidence.
        self.assertNotIn("pages_detail", index)
        self.assertLess(len(json.dumps(index)), 400)
        self.assertEqual(
            index, output_manifest.sanitize_ocr_line_provenance(index)
        )

    def test_legacy_manifest_without_provenance_stays_valid(self):
        import output_manifest

        manifest = self.build()
        self.assertNotIn("ocr_line_provenance", manifest)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / output_manifest.MANIFEST_FILENAME
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = output_manifest.load_verified_run_manifest(Path(folder))
            self.assertEqual(loaded.get("run_id"), "r1")
            self.assertNotIn("ocr_line_provenance", loaded)


class PipelineInstrumentationTest(unittest.TestCase):
    """The real production boundaries must feed the recorder."""

    def setUp(self):
        self.recorder = provenance.activate()
        self.addCleanup(provenance.deactivate)

    def test_analyze_image_array_records_raw_lines_groups_and_filters(self):
        import ocr_balloon

        image = np.full((400, 800, 3), 255, dtype=np.uint8)
        lines = [
            make_line("EVERY TIME I GO", (181, 154, 287, 33), page=1),
            make_line("INTO A DUNGEON", (178, 199, 290, 30), page=1),
            make_line("!!", (700, 350, 20, 10), confidence=0.2, page=1),
        ]
        ocr_balloon.analyze_image_array(image, lines, page_index=1)

        payload = self.recorder.to_dict()
        page = next(item for item in payload["pages"] if item["page"] == 1)
        self.assertEqual(len(page["raw_lines"]), 3)
        self.assertTrue(page["groups"])
        self.assertTrue(
            any(event["operation"] == "line_filtered" for event in page["events"])
        )
        for group in page["groups"]:
            self.assertTrue(group["member_line_ids"])

    def test_render_analyzed_image_records_the_render_input(self):
        import ocr_balloon

        image = np.full((400, 800, 3), 255, dtype=np.uint8)
        line = make_line("EVERY TIME I GO", (181, 154, 287, 33), page=1)
        group = ocr_balloon.TextGroup(group_id="BALAO_1", lines=[line])
        group.text = line.text
        group.translation = "TODA VEZ QUE EU VOU"
        group.sent_to_translation = True
        group.classification = "speech"
        ocr_balloon.render_analyzed_image(image, [line], [], [group], page_index=1)

        payload = self.recorder.to_dict()
        page = next(item for item in payload["pages"] if item["page"] == 1)
        self.assertTrue(page["render_inputs"])
        entry = page["render_inputs"][0]
        self.assertEqual(entry["group_id"], "BALAO_1")
        self.assertEqual(entry["render_input_text"], "EVERY TIME I GO")
        self.assertEqual(entry["render_input_bbox"], [181, 154, 287, 33])
        self.assertEqual(
            entry["member_line_ids"],
            [lid for lid, _ in provenance.ensure_line_ids([line], page=1)],
        )


if __name__ == "__main__":
    unittest.main()
