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


if __name__ == "__main__":
    unittest.main()
