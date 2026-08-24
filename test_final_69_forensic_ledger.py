"""TDD #71 - final #69 forensic ledger closure.

This suite is intentionally offline.  It reads the persisted #69 evidence when
available, freezes the exact ledger sets, and replays the current local
production decisions over the same OCR provenance without calling any provider,
runner, UI, network, Supabase, Community or Drive path.
"""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import os
import unittest
from pathlib import Path

import cv2
import numpy as np

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import ocr_line_provenance
import source_completeness
from ocr_balloon import _render_analyzed_image, analyze_image_array, validate_translation_text
from ocr_engine import OCRLine


RUN_DIR = Path(
    r"C:\Projetos\Tradutor.Ia-main-promotion\output\shadow_slave_chapter_1_5"
    r"\871412d9-2151-4aa3-8804-a2fb8ee699be"
)


def _artifact_available() -> bool:
    if os.environ.get("TRADUTOR_ALLOW_FORENSIC_ARTIFACT_TESTS") != "1":
        return False
    return (
        (RUN_DIR / "quality_report.json").exists()
        and (RUN_DIR / "ocr_line_provenance.json").exists()
    )


@unittest.skipUnless(
    _artifact_available(),
    "#69 local artifact test is opt-in; set TRADUTOR_ALLOW_FORENSIC_ARTIFACT_TESTS=1",
)
class Final69ForensicLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(RUN_DIR / "quality_report.json", encoding="utf-8") as handle:
            cls.quality = json.load(handle)
        with open(RUN_DIR / "ocr_line_provenance.json", encoding="utf-8") as handle:
            cls.provenance = json.load(handle)

    def test_exact_69_physical_and_structured_review_sets_are_frozen(self):
        physical = set(
            self.quality["summary"]["quality_validation"]["physical_quality"][
                "physical_source_residual_group_ids"
            ]
        )
        terminal_review = {
            f"p{page['index']:03d}:{item['id']}"
            for page in self.quality["pages"]
            for item in page.get("translation_terminal_items", [])
            if item.get("translation_final_state") == "manual_review"
            or item.get("preserved_original")
        }
        by_reason: dict[str, set[str]] = {}
        for page in self.quality["pages"]:
            for item in page.get("translation_terminal_items", []):
                reason = item.get("translation_final_reason") or ""
                if reason and reason != "ok":
                    by_reason.setdefault(reason, set()).add(
                        f"p{page['index']:03d}:{item['id']}"
                    )

        self.assertEqual(
            physical,
            {
                "p002:BALAO_1",
                "p005:BALAO_2",
                "p006:BALAO_1",
                "p006:BALAO_2",
                "p011:BALAO_1",
                "p011:BALAO_2",
                "p013:BALAO_3",
                "p015:BALAO_4",
                "p015:BALAO_8",
                "p025:BALAO_1",
                "p030:BALAO_1",
                "p044:BALAO_1",
                "p062:BALAO_1",
                "p068:LINE_004",
            },
        )
        self.assertEqual(len(physical), 14)
        self.assertEqual(len(terminal_review), 13)
        self.assertEqual(terminal_review - physical, set())
        self.assertEqual(physical - terminal_review, {"p068:LINE_004"})
        self.assertEqual(
            by_reason["translation_not_rendered_after_validation"],
            {
                "p002:BALAO_1",
                "p006:BALAO_1",
                "p015:BALAO_4",
                "p025:BALAO_1",
                "p030:BALAO_1",
            },
        )
        self.assertEqual(
            by_reason["translation_not_selected"],
            {"p005:BALAO_2", "p013:BALAO_3", "p062:BALAO_1"},
        )
        self.assertEqual(
            by_reason["untranslated_source_after_retries"],
            {"p011:BALAO_1", "p011:BALAO_2", "p015:BALAO_8"},
        )
        self.assertEqual(
            by_reason["semantic_fidelity_failed_after_retries"],
            {"p044:BALAO_1"},
        )

    def test_current_runtime_replays_remaining_ordinary_story_roots_offline(self):
        matrix = {
            "p002:BALAO_1": ("BALAO_1", "BEFORE OUR NIGHTMARES"),
            "p005:BALAO_2": ("BALAO_2", "CHEAP SYNTHETIC"),
            "p006:BALAO_1": ("BALAO_1", "COST ME"),
            "p006:BALAO_2": ("BALAO_2", "WORTH IT"),
            "p015:BALAO_4": ("BALAO_4", "JUST"),
            "p025:BALAO_1": ("BALAO_1", "NIGHTMARE SPELL"),
            "p030:BALAO_1": ("BALAO_1", "OVERWHELMED"),
            "p044:BALAO_1": ("BALAO_1", "DEATH SENTENCE"),
            "p062:BALAO_1": ("BALAO_1", "DESPAIR"),
            "p063:BALAO_1": ("BALAO_1", "TRIALS"),
            "p068:BALAO_2": ("BALAO_2", "AWAKENED"),
        }
        synthetic_only = {"p005:BALAO_2", "p006:BALAO_2", "p062:BALAO_1"}

        for gid, (current_id, evidence) in matrix.items():
            with self.subTest(gid=gid):
                page_index = int(gid[1:4])
                group, old_item, raw_lines, image_path = self._current_group(
                    page_index,
                    current_id,
                    evidence,
                )
                candidate = old_item.get("translation_candidate") or old_item.get(
                    "translation"
                )
                if not candidate:
                    self.assertIn(gid, synthetic_only)
                    candidate = "TRADUÇÃO SINTÉTICA DE TESTE."
                valid, reason = validate_translation_text(
                    group.text,
                    candidate,
                    group.classification,
                    [],
                )
                self.assertTrue(valid, reason)
                group.translation = candidate
                group.translation_candidate = candidate
                group.translation_valid = True
                group.translation_validation_reason = "ok"
                group.sent_to_translation = True
                group.source_completeness = {"status": source_completeness.STATUS_PASS}

                image = cv2.imread(str(image_path))
                self.assertIsNotNone(image, image_path)
                with self._recorded_page(page_index, raw_lines, [group]):
                    _render_analyzed_image(
                        image,
                        raw_lines,
                        [],
                        [group],
                        page_index=page_index,
                        image_path=str(image_path),
                    )

                removal = (group.visual_validation or {}).get(
                    "source_text_removal",
                    {},
                )
                self.assertTrue(group.redrawn, group.visual_validation)
                self.assertEqual(group.translation_final_state, "translated")
                self.assertEqual(group.translation_final_reason, "ok")
                self.assertEqual(removal.get("source_text_coverage"), 1.0)

    def _current_group(self, page_index: int, current_id: str, evidence: str):
        page_quality = self.quality["pages"][page_index - 1]
        image_path = Path(page_quality["image_path"])
        raw_lines = [
            self._line_from_dict(item, page_index)
            for item in self.provenance["pages"][page_index - 1]["raw_lines"]
        ]
        image = cv2.imread(str(image_path))
        self.assertIsNotNone(image, image_path)
        with self._recorded_page(page_index, raw_lines, []):
            _candidates, groups = analyze_image_array(image, raw_lines, page_index=page_index)
        group = next(
            (
                item
                for item in groups
                if item.group_id == current_id and evidence in item.text.upper()
            ),
            None,
        )
        self.assertIsNotNone(group, [(item.group_id, item.text) for item in groups])
        old_region = current_id if page_index != 30 else "BALAO_1"
        old_item = next(
            item
            for item in page_quality["translation_terminal_items"]
            if item["id"] == old_region
        )
        return group, old_item, raw_lines, image_path

    def _line_from_dict(self, item: dict, page_index: int) -> OCRLine:
        x, y, w, h = [int(part) for part in item["bbox"]]
        line = OCRLine(
            text=item.get("text", ""),
            confidence=float(item.get("confidence", 0.95) or 0.95),
            polygon=np.array(
                [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
                dtype=np.int32,
            ),
            box=(x, y, w, h),
            raw_text=item.get("raw_text") or item.get("text", ""),
            engine=item.get("engine") or "rapidocr",
            page=page_index,
        )
        line.metadata = {"ocr_line_id": item.get("line_id", "")}
        return line

    def _recorded_page(self, page_index: int, raw_lines: list[OCRLine], groups):
        class _RecordedPage:
            def __enter__(_self):
                ocr_line_provenance.activate()
                _self._ctx = ocr_line_provenance.page(page_index)
                _self._ctx.__enter__()
                for line in raw_lines:
                    ocr_line_provenance.record_event(
                        "ocr_raw",
                        line_id=line.metadata["ocr_line_id"],
                        after={"text": line.text, "bbox": list(line.box)},
                        reason="offline_replay",
                    )
                for group in groups:
                    ocr_line_provenance.record_group(group)
                return _self

            def __exit__(_self, exc_type, exc, tb):
                try:
                    return _self._ctx.__exit__(exc_type, exc, tb)
                finally:
                    ocr_line_provenance.deactivate()

        return _RecordedPage()
