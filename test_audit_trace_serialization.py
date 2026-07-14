"""Regressions for the audit trails that never reached an artifact.

Both mechanisms already decided correctly in production, but only a counter survived
into the reports, so no decision could be traced back afterwards: the selective
re-OCR left no event at all, and an unsafe smart split left a count with no list.

Nothing here touches the network, a chapter, or an existing output.
"""

import json
import unittest

from benchmark_pipeline import _build_quality_report, _serializable_state
from ocr_balloon import summarize_speech_container_reocr
from pdf import smart_split_audit


def _reocr_record(
    trigger="speech_line_underread",
    accepted=True,
    page=3,
    container_id=7,
    duration_ms=12.5,
):
    record = {
        "trigger": trigger,
        "page": page,
        "container_id": container_id,
        "box": [10, 20, 120, 30],
        "crop_box": [6, 16, 128, 38],
        "previous_engine": "rapidocr",
        "previous_text": "PARTIAL",
        "previous_confidence": 0.91,
        "engines_attempted": ["rapidocr", "paddle"],
        "candidates": [
            {"engine": "rapidocr", "text": "PARTIAL", "confidence": 0.91, "overlap": 0.8},
            {"engine": "paddle", "text": "PARTIAL ROW", "confidence": 0.96, "overlap": 0.9},
        ],
        "accepted": accepted,
        "reason": (
            "selective_reocr_recovered_text"
            if accepted
            else "selective_reocr_no_confident_candidate"
        ),
        "duration_ms": duration_ms,
    }
    if accepted:
        record["selected_engine"] = "paddle"
        record["selected_confidence"] = 0.96
        record["new_text"] = "PARTIAL ROW"
    return record


def _state(index=3, reocr=None):
    return {
        "index": index,
        "sequence_index": index,
        "original_index": index,
        "status": "completed",
        "output_path": "",
        "image_path": "",
        "timings": {},
        "speech_container_reocr": list(reocr or []),
        "debug_data": {"items": [], "selective_ocr_fallbacks": [], "classification_counts": {}},
    }


class SpeechContainerReocrSerializationTests(unittest.TestCase):
    def test_accepted_underread_event_reaches_progress(self):
        state = _state(reocr=[_reocr_record()])
        serialized = _serializable_state(state)

        self.assertIn("speech_container_reocr", serialized)
        event = serialized["speech_container_reocr"][0]
        self.assertEqual(event["trigger"], "speech_line_underread")
        self.assertEqual(event["container_id"], 7)
        self.assertTrue(event["accepted"])
        self.assertEqual(event["selected_engine"], "paddle")
        self.assertEqual(event["previous_text"], "PARTIAL")
        self.assertEqual(event["new_text"], "PARTIAL ROW")

    def test_accepted_gap_event_reaches_progress(self):
        state = _state(
            reocr=[_reocr_record(trigger="speech_container_uncovered_text")]
        )
        serialized = _serializable_state(state)

        event = serialized["speech_container_reocr"][0]
        self.assertEqual(event["trigger"], "speech_container_uncovered_text")
        self.assertTrue(event["accepted"])

    def test_rejected_event_is_kept_with_its_reason(self):
        state = _state(reocr=[_reocr_record(accepted=False)])
        event = _serializable_state(state)["speech_container_reocr"][0]

        self.assertFalse(event["accepted"])
        self.assertEqual(event["reason"], "selective_reocr_no_confident_candidate")
        self.assertNotIn("selected_engine", event)

    def test_every_attempted_engine_is_recorded(self):
        event = _serializable_state(_state(reocr=[_reocr_record()]))[
            "speech_container_reocr"
        ][0]

        self.assertEqual(event["engines_attempted"], ["rapidocr", "paddle"])
        self.assertEqual(len(event["candidates"]), 2)

    def test_events_are_json_serializable(self):
        state = _serializable_state(_state(reocr=[_reocr_record()]))
        # A numpy scalar or an OCRLine leaking into the record would break the run
        # at write time, after all the work was done.
        json.dumps(state)

    def test_quality_report_carries_the_summary(self):
        states = [
            _state(
                index=1,
                reocr=[
                    _reocr_record(duration_ms=10.0),
                    _reocr_record(
                        trigger="speech_container_uncovered_text",
                        accepted=False,
                        duration_ms=5.0,
                    ),
                ],
            )
        ]
        report = _build_quality_report({}, states, [])
        summary = report["totals"]["speech_container_reocr"]

        self.assertEqual(summary["containers_evaluated"], 2)
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["underread_recovered"], 1)
        self.assertEqual(summary["uncovered_text_recovered"], 0)
        self.assertEqual(summary["total_duration_ms"], 15.0)
        self.assertEqual(summary["triggers"]["speech_line_underread"], 1)
        self.assertEqual(report["pages"][0]["speech_container_reocr"], states[0][
            "speech_container_reocr"
        ])

    def test_summary_of_no_events_is_empty_not_missing(self):
        summary = summarize_speech_container_reocr([])

        self.assertEqual(summary["containers_evaluated"], 0)
        self.assertEqual(summary["accepted"], 0)
        self.assertEqual(summary["total_duration_ms"], 0)

    def test_older_state_without_the_field_still_serializes(self):
        # Outputs produced before this field existed are not migrated, so every
        # reader must tolerate its absence.
        state = _state()
        state.pop("speech_container_reocr")
        serialized = _serializable_state(state)

        self.assertNotIn("speech_container_reocr", serialized)
        report = _build_quality_report({}, [state], [])
        self.assertEqual(report["pages"][0]["speech_container_reocr"], [])
        self.assertEqual(report["totals"]["speech_container_reocr"]["containers_evaluated"], 0)


def _split(page, safe_band=True, height=1800):
    record = {
        "page": page,
        "height": height,
        "safe_band": safe_band,
        "reason": "white_gutter" if safe_band else "lowest_risk_band",
        "orientation": "horizontal",
        "band_score": 4.2 if safe_band else 0.7,
        "white_ratio": 0.97 if safe_band else 0.49,
        "dark_ratio": 0.0,
        "texture": 2.1 if safe_band else 23.8,
        "horizontal_edges": 0.4 if safe_band else 1.6,
    }
    return record


def _split_report(splits):
    return {
        "source_images": 12,
        "pdf_pages": len(splits),
        "splits": splits,
        "unsafe_split_count": sum(not s.get("safe_band") for s in splits),
    }


class SmartSplitSerializationTests(unittest.TestCase):
    def test_safe_chapter_reports_no_details(self):
        audit = smart_split_audit(_split_report([_split(1), _split(2)]))

        self.assertTrue(audit["safe"])
        self.assertEqual(audit["unsafe_count"], 0)
        self.assertEqual(audit["details_count"], 0)
        self.assertEqual(audit["details"], [])

    def test_one_unsafe_cut_is_fully_described(self):
        audit = smart_split_audit(
            _split_report([_split(1), _split(2, safe_band=False, height=1287)])
        )

        self.assertFalse(audit["safe"])
        self.assertEqual(audit["unsafe_count"], 1)
        detail = audit["details"][0]
        self.assertEqual(detail["page"], 2)
        self.assertEqual(detail["logical_pages"], [2, 3])
        self.assertEqual(detail["split_y"], 1287)
        self.assertEqual(detail["orientation"], "horizontal")
        self.assertEqual(detail["reason"], "lowest_risk_band")
        self.assertEqual(detail["fallback_decision"], "kept_lowest_risk_band")
        self.assertTrue(detail["accepted"])
        self.assertTrue(detail["requires_review"])
        self.assertEqual(detail["texture"], 23.8)

    def test_two_unsafe_cuts_yield_two_details(self):
        audit = smart_split_audit(
            _split_report(
                [
                    _split(1),
                    _split(2, safe_band=False),
                    _split(3, safe_band=False),
                    _split(4),
                ]
            )
        )

        self.assertEqual(audit["unsafe_count"], 2)
        self.assertEqual(audit["details_count"], 2)
        self.assertEqual([d["page"] for d in audit["details"]], [2, 3])

    def test_counter_can_never_exist_without_its_list(self):
        for splits in (
            [],
            [_split(1)],
            [_split(1, safe_band=False)],
            [_split(1, safe_band=False), _split(2, safe_band=False)],
        ):
            with self.subTest(splits=len(splits)):
                audit = smart_split_audit(_split_report(splits))
                self.assertEqual(audit["unsafe_count"], len(audit["details"]))
                self.assertEqual(audit["details_count"], audit["unsafe_count"])
                self.assertEqual(audit["safe"], audit["unsafe_count"] == 0)

    def test_details_are_json_serializable(self):
        audit = smart_split_audit(_split_report([_split(1, safe_band=False)]))
        json.dumps(audit)

    def test_report_without_splits_is_tolerated(self):
        # A run with the smart split disabled carries no split list at all.
        audit = smart_split_audit({"unsafe_split_count": 0})

        self.assertTrue(audit["safe"])
        self.assertEqual(audit["details"], [])


if __name__ == "__main__":
    unittest.main()
