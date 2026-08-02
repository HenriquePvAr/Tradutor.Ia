"""Contracts for canonical pipeline progress and safe review bulk actions."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chapter_quality_revision import (
    ChapterQualityRevision,
    ContextualNvidiaReviewer,
    REVIEW_SCHEMA_VERSION,
    stable_region_key,
)
from job_store import JobStatus, JobStore


ROOT = Path(__file__).resolve().parent
JS = ROOT / "static" / "tradutor_ui.js"
SHELL = ROOT / "ui" / "ui_shell.html"


class RevisionLifecycleContracts(unittest.TestCase):
    def _revision_with_status(self, root: Path, status: str) -> ChapterQualityRevision:
        output = root / "output"
        (output / "quality_revision" / "rev1").mkdir(parents=True)
        manifest = output / "quality_revision" / "rev1" / "revision_manifest.json"
        manifest.write_text(json.dumps({"revision_id": "rev1", "status": status, "phase": "x"}), encoding="utf-8")
        (output / "quality_revision" / "latest_revision.json").write_text(
            json.dumps({"revision_id": "rev1", "manifest_path": str(manifest)}), encoding="utf-8")
        return ChapterQualityRevision(output, job_id="job-1", run_id="run-1")

    def test_lost_in_flight_revision_becomes_interrupted_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision_with_status(Path(folder), "running")
            marked = revision.mark_interrupted()
            self.assertEqual(marked["status"], "interrupted")
            self.assertEqual(marked["reason_code"], "revision_process_lost")
            self.assertTrue(marked["resumable"])

    def test_marking_interrupted_is_idempotent_for_terminal_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision_with_status(Path(folder), "finished")
            marked = revision.mark_interrupted()
            # A terminal revision is returned untouched, never rewritten.
            self.assertEqual(marked["status"], "finished")
            self.assertNotIn("reason_code", marked)

    def test_background_nvidia_child_never_opens_a_console_on_windows(self) -> None:
        import os
        import subprocess

        from process_options import hidden_console_options

        options = hidden_console_options()
        if os.name == "nt":
            self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertIn("startupinfo", options)
        else:
            self.assertEqual(options, {})
        # Output must still be captured, so the helper must not fix the streams.
        self.assertNotIn("stdout", options)
        self.assertNotIn("stderr", options)


class RevisionCancelAndResumeContracts(unittest.TestCase):
    def _revision(self, root: Path, status: str, reviews: list | None = None) -> ChapterQualityRevision:
        output = root / "output"
        folder = output / "quality_revision" / "rev1"
        folder.mkdir(parents=True)
        manifest = folder / "revision_manifest.json"
        manifest.write_text(json.dumps({"revision_id": "rev1", "status": status}), encoding="utf-8")
        if reviews is not None:
            (folder / "nvidia_revision_checkpoint.json").write_text(
                json.dumps({"completed_reviews": reviews}), encoding="utf-8")
        (output / "quality_revision" / "latest_revision.json").write_text(
            json.dumps({"revision_id": "rev1", "manifest_path": str(manifest)}), encoding="utf-8")
        return ChapterQualityRevision(output, job_id="job-1", run_id="run-1")

    def test_cancelling_is_idempotent_and_only_moves_in_flight_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder), "running")
            first = revision.mark_cancelling()
            self.assertEqual(first["status"], "cancelling")
            self.assertEqual(first["reason_code"], "user_cancelled")
            self.assertEqual(revision.mark_cancelling()["status"], "cancelling")
        with tempfile.TemporaryDirectory() as folder:
            done = self._revision(Path(folder), "finished")
            self.assertEqual(done.mark_cancelling()["status"], "finished")

    def test_resume_reuses_answers_only_from_a_stopped_revision(self) -> None:
        answers = [{"region_id": "p1:R1", "action": "keep"}, {"region_id": "p1:R2", "action": "rewrite"}]
        for status in ("cancelled", "interrupted", "failed"):
            with tempfile.TemporaryDirectory() as folder:
                revision = self._revision(Path(folder), status, answers)
                self.assertEqual(sorted(revision.resume_reviews()), ["p1:R1", "p1:R2"], status)
        for status in ("finished", "running"):
            with tempfile.TemporaryDirectory() as folder:
                revision = self._revision(Path(folder), status, answers)
                self.assertEqual(revision.resume_reviews(), {}, status)

    def test_stale_revisions_outside_the_latest_pointer_are_settled(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            for name, status in (("old", "running"), ("live", "running"), ("done", "finished")):
                target = output / "quality_revision" / name
                target.mkdir(parents=True)
                (target / "revision_manifest.json").write_text(
                    json.dumps({"revision_id": name, "status": status}), encoding="utf-8")
            revision = ChapterQualityRevision(output, job_id="job-1", run_id="run-1")
            healed = revision.sweep_stale_revisions(keep_revision_id="live")
            self.assertEqual(healed, ["old"])
            settled = json.loads((output / "quality_revision" / "old" / "revision_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(settled["status"], "interrupted")
            self.assertTrue(settled["resumable"])
            # The live revision and terminal ones are never touched.
            kept = json.loads((output / "quality_revision" / "live" / "revision_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(kept["status"], "running")
            done = json.loads((output / "quality_revision" / "done" / "revision_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(done["status"], "finished")

    def test_cancel_predicate_failure_never_aborts_a_revision(self) -> None:
        def broken() -> bool:
            raise RuntimeError("predicate exploded")

        revision = ChapterQualityRevision("unused", job_id="j", run_id="r", should_cancel=broken)
        self.assertFalse(revision.cancel_requested())

    def test_ui_exposes_cancel_and_resume_actions(self) -> None:
        js = JS.read_text(encoding="utf-8")
        shell = SHELL.read_text(encoding="utf-8")
        self.assertIn('id="cancelRevisionAction"', shell)
        self.assertIn('id="resumeRevisionAction"', shell)
        self.assertIn("/api/ui/quality-review/revision/cancel", js)
        self.assertIn("REVISION_CANCELLABLE_STATES", js)
        self.assertIn("REVISION_RESUMABLE_STATES", js)


class IncrementalRenderBaseContracts(unittest.TestCase):
    """The reviewed page must be an edit of the translated page.

    Rebuilding it from the English scan is what made earlier revisions show the
    source text again, overlap two languages and regress whole pages.
    """

    def _page(self, root: Path, *, with_output: bool = True):
        import numpy as np

        import cv2

        english = root / "smart_input_pages"
        translated = root / "pages"
        english.mkdir(parents=True, exist_ok=True)
        translated.mkdir(parents=True, exist_ok=True)
        source = english / "page_003.png"
        output = translated / "page_003.png"
        # Two regions: the top one will change, the bottom one must not.
        src = np.full((200, 300, 3), 255, dtype=np.uint8)
        cv2.rectangle(src, (10, 10), (290, 90), (0, 0, 0), -1)      # english region A
        cv2.rectangle(src, (10, 110), (290, 190), (0, 0, 0), -1)    # english region B
        cv2.imwrite(str(source), src)
        trans = np.full((200, 300, 3), 255, dtype=np.uint8)
        cv2.rectangle(trans, (10, 10), (290, 90), (40, 40, 40), -1)   # translated A
        cv2.rectangle(trans, (10, 110), (290, 190), (80, 80, 80), -1)  # translated B
        if with_output:
            cv2.imwrite(str(output), trans)
        page = {
            "index": 3,
            "image_path": str(source),
            "output_path": str(output) if with_output else "",
            # Region geometry is (x, y, width, height), matching the pipeline.
            "debug_data": {"items": [
                {"region_id": "REGION_001", "page": 3, "draw_box": [10, 10, 280, 80]},
                {"region_id": "REGION_002", "page": 3, "draw_box": [10, 110, 280, 80]},
            ]},
        }
        return page, source, output

    def _revision(self, root: Path) -> ChapterQualityRevision:
        return ChapterQualityRevision(root, job_id="job-1", run_id="run-1")

    def test_base_is_the_translated_output_never_the_english_scan(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page, source, output = self._page(root)
            resolved = ChapterQualityRevision._resolve_incremental_render_base(page)
            self.assertEqual(resolved["base_kind"], "translated_pipeline_output")
            self.assertEqual(Path(resolved["path"]), output)
            self.assertNotEqual(Path(resolved["path"]), source)

    def test_missing_translated_output_fails_closed_without_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page, source, _ = self._page(root, with_output=False)
            resolved = ChapterQualityRevision._resolve_incremental_render_base(page)
            self.assertEqual(resolved["base_kind"], "unavailable")
            self.assertEqual(resolved["reason_code"], "translated_render_base_unavailable")
            # The English scan must never be offered as a fallback.
            self.assertEqual(resolved["path"], "")
            self.assertNotEqual(resolved["path"], str(source))

    def test_only_changed_regions_are_boxed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page, _, _ = self._page(root)
            boxes, rejected = ChapterQualityRevision._changed_region_boxes(
                page, [{"region_id": stable_region_key(3, page["debug_data"]["items"][0])}],
                width=300, height=200)
            self.assertEqual(len(boxes), 1)
            # (x, y, w, h) = (10, 10, 280, 80) becomes the rect (10, 10)-(290, 90).
            self.assertEqual(boxes[0], (10, 10, 290, 90))
            self.assertEqual(rejected, [])

    def test_degenerate_boxes_are_rejected_not_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page, _, _ = self._page(root)
            page["debug_data"]["items"][0]["draw_box"] = [10, 10, 0, 0]
            boxes, rejected = ChapterQualityRevision._changed_region_boxes(
                page, [{"region_id": stable_region_key(3, page["debug_data"]["items"][0])}],
                width=300, height=200)
            self.assertEqual(boxes, [])
            self.assertEqual(len(rejected), 1)

    def test_untouched_pixels_are_detected_when_something_rewrites_the_page(self) -> None:
        import numpy as np

        base = np.zeros((40, 40, 3), dtype=np.uint8)
        same = base.copy()
        boxes = [(0, 0, 10, 10)]
        self.assertEqual(ChapterQualityRevision._pixels_outside_boxes_changed(base, same, boxes), 0)
        # A change inside the allowed box is fine...
        inside = base.copy()
        inside[0:10, 0:10] = 255
        self.assertEqual(ChapterQualityRevision._pixels_outside_boxes_changed(base, inside, boxes), 0)
        # ...but anything outside it means the page was rebuilt, not edited.
        outside = base.copy()
        outside[20:30, 20:30] = 255
        self.assertEqual(ChapterQualityRevision._pixels_outside_boxes_changed(base, outside, boxes), 100)

    def test_overlapping_text_is_visible_as_extra_ink(self) -> None:
        import numpy as np

        clean = np.full((20, 20, 3), 255, dtype=np.uint8)
        clean[5:8, :] = 0                     # one line of text
        overlapped = np.full((20, 20, 3), 255, dtype=np.uint8)
        overlapped[5:8, :] = 0
        overlapped[10:16, :] = 0              # second language drawn over it
        before = ChapterQualityRevision._ink(clean)
        after = ChapterQualityRevision._ink(overlapped)
        self.assertGreater(after, before * ChapterQualityRevision.RENDER_OVERLAP_INK_RATIO)


class RegionGeometryContracts(unittest.TestCase):
    """Region boxes are (x, y, width, height), never (left, top, right, bottom)."""

    def test_page_bottom_balloon_keeps_its_bottom_below_its_top(self) -> None:
        # The real page-8 lower balloon on an 800x1911 page.
        rect = ChapterQualityRevision._xywh_to_ltrb([297, 1660, 333, 42])
        self.assertEqual(rect, (297, 1660, 630, 1702))
        left, top, right, bottom = rect
        self.assertGreater(bottom, top)
        self.assertGreater(right, left)

    def test_zero_or_negative_extent_is_rejected(self) -> None:
        for box in ([10, 10, 0, 40], [10, 10, 40, 0], [10, 10, -5, 40], ["a", 1, 2, 3], []):
            self.assertIsNone(ChapterQualityRevision._xywh_to_ltrb(box), box)

    def test_clipping_keeps_the_box_inside_the_page(self) -> None:
        clipped = ChapterQualityRevision._clip_ltrb((-20, -10, 900, 2000), width=800, height=1911)
        self.assertEqual(clipped, (0, 0, 800, 1911))
        # A box entirely outside the page collapses and is rejected.
        self.assertIsNone(ChapterQualityRevision._clip_ltrb((900, 2000, 950, 2050), width=800, height=1911))
        # So is a sliver too small to hold text.
        self.assertIsNone(ChapterQualityRevision._clip_ltrb((10, 10, 11, 11), width=800, height=1911))


class GroupPolygonGeometryContracts(unittest.TestCase):
    """A reconstructed group's polygon must be its own box, not a page-tall sliver."""

    def _revision(self, folder):
        return ChapterQualityRevision(str(folder), job_id="p", run_id="p")

    def test_polygon_comes_from_xywh_corners(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            # bounding_box is (x, y, w, h); here x=302,y=1608 with w<y, the case
            # that used to produce a full-height x301-324 sliver.
            page = {"index": 8, "debug_data": {"items": [
                {"id": "REGION_002", "region_id": "p008:REGION_002",
                 "bounding_box": [302, 1608, 323, 146], "text": "TSK.",
                 "translation": "TSK."}]}}
            group = revision._groups_from_page_items(page)[0]
            poly = group.lines[0].polygon.tolist()
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            self.assertEqual((min(xs), max(xs)), (302, 302 + 323))
            self.assertEqual((min(ys), max(ys)), (1608, 1608 + 146))
            # The sliver bug spanned from y=146 up to y=1608; never again.
            self.assertGreaterEqual(min(ys), 1608)


class VisualGateStateContracts(unittest.TestCase):
    """Every reviewed region ends in exactly one state the UI can show."""

    def _states(self, reviews, changed_by_page, records):
        return ChapterQualityRevision._region_visual_states(reviews, changed_by_page, records)

    def test_a_region_the_gate_accepted_is_applied(self) -> None:
        states = self._states(
            [{"region_id": "p008:a", "action": "replace"}],
            {8: [{"region_id": "p008:a"}]},
            [{"page": 8, "status": "rendered", "changed_regions": ["p008:a"], "rejected_regions": []}])
        self.assertEqual(states["p008:a"]["state"], "applied")

    def test_a_rejected_page_marks_its_regions_as_visual_regressions(self) -> None:
        states = self._states(
            [{"region_id": "p008:a", "action": "replace"}],
            {8: [{"region_id": "p008:a"}]},
            [{"page": 8, "status": "rejected",
              "reason_code": "unexpected_pixels_outside_changed_regions",
              "changed_regions": ["p008:a"]}])
        self.assertEqual(states["p008:a"]["state"], "rejected_visual_regression")
        self.assertEqual(states["p008:a"]["reason_code"], "unexpected_pixels_outside_changed_regions")

    def test_a_region_rejected_inside_an_accepted_page_is_not_reported_as_applied(self) -> None:
        states = self._states(
            [{"region_id": "p008:a", "action": "replace"}, {"region_id": "p008:b", "action": "replace"}],
            {8: [{"region_id": "p008:a"}, {"region_id": "p008:b"}]},
            [{"page": 8, "status": "rendered", "reason_code": "",
              "changed_regions": ["p008:a", "p008:b"], "rejected_regions": ["p008:b"]}])
        self.assertEqual(states["p008:a"]["state"], "applied")
        self.assertEqual(states["p008:b"]["state"], "rejected_visual_regression")

    def test_manual_review_and_unchanged_are_kept_apart(self) -> None:
        states = self._states(
            [{"region_id": "p001:a", "action": "manual_review", "reason_code": "high_risk"},
             {"region_id": "p001:b", "action": "keep"}],
            {}, [])
        self.assertEqual(states["p001:a"]["state"], "manual_review")
        self.assertEqual(states["p001:a"]["reason_code"], "high_risk")
        self.assertEqual(states["p001:b"]["state"], "unchanged")

    def test_preserve_original_is_unchanged_not_pending(self) -> None:
        states = self._states([{"region_id": "p001:a", "action": "preserve_original"}], {}, [])
        self.assertEqual(states["p001:a"]["state"], "unchanged")

    def test_a_declined_proposal_is_held_for_manual_review(self) -> None:
        # Reviewer proposed a rewrite, but the safe-apply step never submitted it
        # to the renderer (e.g. adding text where there was none). It must not
        # vanish into "pending"; a human decides.
        states = self._states([{"region_id": "p004:a", "action": "rewrite",
                                "revised_translation": "novo"}], {}, [])
        self.assertEqual(states["p004:a"]["state"], "manual_review")

    def test_a_region_the_renderer_never_saw_stays_pending(self) -> None:
        states = self._states([{"region_id": "p003:a", "action": "replace"}],
                              {3: [{"region_id": "p003:a"}]}, [])
        self.assertEqual(states["p003:a"]["state"], "pending")

    def test_review_summary_counts_each_outcome_separately(self) -> None:
        reviews = [{"region_id": "p001:a", "action": "manual_review"},
                   {"region_id": "p001:b", "action": "keep"},
                   {"region_id": "p008:a", "action": "replace"},
                   {"region_id": "p008:b", "action": "replace"}]
        changed = {8: [{"region_id": "p008:a", "revised_translation": "x"},
                       {"region_id": "p008:b", "revised_translation": "y"}]}
        records = [{"page": 8, "status": "rendered",
                    "changed_regions": ["p008:a", "p008:b"], "rejected_regions": ["p008:b"]}]
        states = self._states(reviews, changed, records)
        summary = ChapterQualityRevision._review_summary(
            reviews, states, records, changed, [{"index": 8}, {"index": 1}])
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(summary["rejected_visual_gate"], 1)
        self.assertEqual(summary["manual_review"], 1)
        self.assertEqual(summary["unchanged"], 1)
        self.assertEqual(summary["pages_changed"], 1)

    def test_every_state_is_one_of_the_declared_states_and_the_summary_totals_match(self) -> None:
        reviews = [{"region_id": "p001:a", "action": "manual_review"},
                   {"region_id": "p001:b", "action": "keep"},
                   {"region_id": "p008:a", "action": "replace"},
                   {"region_id": "p008:b", "action": "replace"},
                   {"region_id": "p003:a", "action": "replace"}]
        states = self._states(
            reviews,
            {8: [{"region_id": "p008:a"}, {"region_id": "p008:b"}], 3: [{"region_id": "p003:a"}]},
            [{"page": 8, "status": "rendered", "changed_regions": ["p008:a", "p008:b"],
              "rejected_regions": ["p008:b"]}])
        for region_id, entry in states.items():
            self.assertIn(entry["state"], ChapterQualityRevision.VISUAL_STATES, region_id)
        summary = ChapterQualityRevision._visual_state_summary(states)
        self.assertEqual(sum(summary.values()), len(reviews))
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(summary["rejected_visual_regression"], 1)
        self.assertEqual(summary["pending"], 1)


class VisualGateUiContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
        self.shell = Path("ui/ui_shell.html").read_text(encoding="utf-8")

    def test_every_gate_reason_code_has_a_pt_br_label(self) -> None:
        # A gate the user cannot read is a gate the user cannot act on.
        for code in ("translated_render_base_unavailable", "unsafe_incremental_mask",
                     "unexpected_pixels_outside_changed_regions", "text_overlap_regression_detected",
                     "clean_region_background_unavailable", "excessive_cleanup_mask",
                     "ambiguous_text_polarity", "unsafe_inverted_cleanup_mask",
                     "dark_region_background_reconstruction_failed",
                     "light_text_components_not_isolated",
                     "empty_previous_text_mask", "cleanup_mask_crosses_artwork"):
            self.assertIn(f"{code}:", self.js, code)

    def test_every_visual_state_has_a_label_and_a_filter(self) -> None:
        for state in ChapterQualityRevision.VISUAL_STATES:
            self.assertIn(f"{state}:", self.js, state)
        for state in ("applied", "rejected_visual_regression", "manual_review", "unchanged"):
            self.assertIn(f'data-review-filter="{state}"', self.shell, state)

    def test_the_comparison_action_is_offered(self) -> None:
        self.assertIn("data-review-compare", self.js)
        self.assertIn("ABRIR COMPARAÇÃO", self.js)
        self.assertIn("visualComparisonDialog", self.shell)


class LocalizedCleanupContracts(unittest.TestCase):
    """The previous translation must be erased before the new one is drawn."""

    def _revision(self, root: Path) -> ChapterQualityRevision:
        return ChapterQualityRevision(root, job_id="job-1", run_id="run-1")

    def _page_with_text(self):
        import numpy as np

        # Real text is many small glyphs, not one solid bar.
        page = np.full((200, 300, 3), 255, dtype=np.uint8)
        for x in range(20, 280, 20):
            page[42:52, x:x + 10] = 20      # previous translation, region A
            page[142:152, x:x + 10] = 20    # untouched text, region B
        page[95:100, :] = 0                 # balloon border between them
        return page

    def test_previous_text_is_erased_and_neighbours_are_untouched(self) -> None:
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            page = self._page_with_text()
            box = (10, 30, 290, 70)          # only region A
            cleaned, metrics, reason = revision._clean_previous_translation(page, [box])
            self.assertEqual(reason, "")
            self.assertIsNotNone(cleaned)
            # The old translation is gone from the changed region...
            self.assertEqual(revision._ink(cleaned[30:70, 10:290]), 0)
            # ...while the other text, the border and everything else stay put.
            np.testing.assert_array_equal(cleaned[142:152, 20:280], page[142:152, 20:280])
            np.testing.assert_array_equal(cleaned[95:100, :], page[95:100, :])
            self.assertEqual(
                ChapterQualityRevision._pixels_outside_boxes_changed(page, cleaned, [box]), 0)
            self.assertGreater(metrics[0]["pixels_removed"], 0)
            self.assertGreater(metrics[0]["pixels_preserved"], 0)

    def test_cleanup_is_idempotent(self) -> None:
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            page = self._page_with_text()
            box = (10, 30, 290, 70)
            once, _, _ = revision._clean_previous_translation(page, [box])
            twice, _, _ = revision._clean_previous_translation(once, [box])
            np.testing.assert_array_equal(once, twice)

    @staticmethod
    def _dark_balloon() -> "object":
        import numpy as np

        page = np.full((200, 300, 3), 235, dtype=np.uint8)
        page[20:180, 20:280] = 25                     # dark narration box
        for x in range(40, 260, 24):                  # light glyphs on it
            page[90:120, x:x + 12] = 235
        return page

    def test_light_text_on_a_dark_balloon_is_erased_without_touching_the_balloon(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            page = self._dark_balloon()
            box = (30, 80, 270, 130)
            polarity, _ = revision._detect_text_polarity(page[80:130, 30:270])
            self.assertEqual(polarity, "light_text_on_dark_background")
            cleaned, metrics, reason = revision._clean_previous_translation(page, [box])
            self.assertEqual(reason, "")
            self.assertIsNotNone(cleaned)
            self.assertEqual(metrics[-1]["polarity"], "light_text_on_dark_background")
            # The light glyphs are gone; the balloon they sat on is still dark.
            self.assertEqual(revision._ink(cleaned[80:130, 30:270], polarity), 0)
            self.assertGreater(revision._ink(cleaned[80:130, 30:270]), 0)

    def test_residual_ink_is_measured_in_the_polarity_of_the_erased_text(self) -> None:
        # On a dark balloon the balloon itself is dark, so a dark-ink measure
        # would report a clean region as still full of the old translation.
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            page = self._dark_balloon()
            cleaned, _, _ = revision._clean_previous_translation(page, [(30, 80, 270, 130)])
            crop = cleaned[80:130, 30:270]
            self.assertEqual(revision._ink(crop, "light_text_on_dark_background"), 0)
            self.assertGreater(revision._ink(crop, "dark_text_on_light_background"), 0)

    def test_a_solid_dark_panel_is_artwork_and_fails_closed(self) -> None:
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            # A solid dark panel is artwork, not glyphs on a balloon: neither side
            # of the histogram looks like text, so refuse instead of erasing it.
            page = np.full((100, 100, 3), 255, dtype=np.uint8)
            page[10:90, 10:90] = 10
            cleaned, metrics, reason = revision._clean_previous_translation(page, [(5, 5, 95, 95)])
            self.assertIsNone(cleaned)
            # One big blob is a panel or the page around a balloon, not glyphs.
            self.assertIn(reason, {"ambiguous_text_polarity", "light_text_components_not_isolated"})
            self.assertEqual(metrics[-1]["reason_code"], reason)

    def test_ambiguous_polarity_tries_automatic_segmentation_before_review(self) -> None:
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            page = np.full((80, 180, 3), 235, dtype=np.uint8)
            mask = np.zeros((60, 160), dtype=np.uint8)
            mask[20:35, 35:55] = 255
            mask[20:35, 75:95] = 255
            halo = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
            candidate = page[10:70, 10:170].copy()
            candidate[mask > 0] = 235
            masks = {
                "valid": True,
                "combined_inpainting_mask": mask,
                "source_residual_mask": mask,
                "validation_halo": halo,
                "protected_edge_mask": np.zeros(mask.shape, dtype=np.uint8),
                "mask_hash": "auto-mask",
                "reason_codes": [],
            }
            proposals = [
                {"method": "telea", "radius": 2, "image": candidate, "overall_score": 0.91},
                {"method": "navier_stokes", "radius": 3, "image": candidate, "overall_score": 0.89},
            ]
            with patch.object(revision, "_detect_text_polarity", return_value=("ambiguous", {})), \
                    patch("chapter_quality_revision.art_text_inpainting.build_text_masks", return_value=masks), \
                    patch("chapter_quality_revision.art_text_inpainting.generate_inpainting_candidates", return_value=proposals), \
                    patch("chapter_quality_revision.art_text_inpainting.select_best_candidate", return_value={
                        "status": "passed", "method": "telea", "radius": 2,
                    }):
                cleaned, metrics, reason = revision._clean_previous_translation(
                    page, [(10, 10, 170, 70)])
            self.assertEqual(reason, "")
            self.assertIsNotNone(cleaned)
            self.assertEqual(metrics[0]["mask_source"], "automatic_text_segmentation")
            self.assertEqual(len(metrics[0]["inpainting_candidates"]), 2)

    def test_clear_polarity_also_tries_scored_neighbor_pixel_strategies(self) -> None:
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            page = np.full((80, 180, 3), 235, dtype=np.uint8)
            mask = np.zeros((60, 160), dtype=np.uint8)
            mask[20:35, 35:55] = 255
            halo = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
            candidate = page[10:70, 10:170].copy()
            masks = {
                "valid": True,
                "combined_inpainting_mask": mask,
                "source_residual_mask": mask,
                "validation_halo": halo,
                "protected_edge_mask": np.zeros(mask.shape, dtype=np.uint8),
                "mask_hash": "clear-mask",
                "reason_codes": [],
            }
            proposals = [
                {"method": "local_color", "radius": None, "image": candidate,
                 "overall_score": 0.92},
                {"method": "local_gradient", "radius": None, "image": candidate,
                 "overall_score": 0.91},
                {"method": "navier_stokes", "radius": 2, "image": candidate,
                 "overall_score": 0.90},
            ]
            with patch.object(
                    revision, "_detect_text_polarity",
                    return_value=("dark_text_on_light_background", {})), \
                    patch("chapter_quality_revision.art_text_inpainting.build_text_masks",
                          return_value=masks) as build_masks, \
                    patch("chapter_quality_revision.art_text_inpainting.generate_inpainting_candidates",
                          return_value=proposals) as generate, \
                    patch("chapter_quality_revision.art_text_inpainting.select_best_candidate",
                          return_value={"status": "passed", "method": "local_color",
                                        "radius": None}):
                cleaned, metrics, reason = revision._clean_previous_translation(
                    page, [(10, 10, 170, 70)])
            self.assertEqual(reason, "")
            self.assertIsNotNone(cleaned)
            build_masks.assert_called_once()
            generate.assert_called_once()
            self.assertEqual(metrics[0]["selected_inpainting"]["method"], "local_color")
            self.assertEqual(len(metrics[0]["inpainting_candidates"]), 3)

    def test_a_mask_covering_most_of_a_light_region_is_rejected(self) -> None:
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            # Light background overall, but dense hatching: once the halo is
            # closed the mask covers most of the region, so it is artwork.
            page = np.full((100, 100, 3), 255, dtype=np.uint8)
            page[::3, :] = 10
            cleaned, metrics, reason = revision._clean_previous_translation(page, [(5, 5, 98, 98)])
            self.assertIsNone(cleaned)
            self.assertEqual(reason, "excessive_cleanup_mask")

    def test_blank_region_reports_an_empty_mask_without_failing(self) -> None:
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            page = np.full((100, 100, 3), 255, dtype=np.uint8)
            cleaned, metrics, reason = revision._clean_previous_translation(page, [(10, 10, 90, 90)])
            self.assertEqual(reason, "")
            self.assertIsNotNone(cleaned)
            self.assertEqual(metrics[0]["reason_code"], "empty_previous_text_mask")


class ReviewedPdfVersionGateContracts(unittest.TestCase):
    def _revision(self, root: Path) -> ChapterQualityRevision:
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)
        return ChapterQualityRevision(output, job_id="job-1", run_id="run-1")

    def _changes(self, translation: str = "Corra!", source: str = "Run now"):
        return {3: [{"region_id": "p003:R1", "action": "rewrite",
                     "revised_translation": translation, "source_text": source}]}

    def test_semantic_hash_ignores_ordering_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            one = {1: [{"region_id": "p001:R1", "action": "rewrite", "revised_translation": "A", "source_text": "a"}],
                   2: [{"region_id": "p002:R2", "action": "rewrite", "revised_translation": "B", "source_text": "b"}]}
            other = {2: [{"region_id": "p002:R2", "action": "rewrite", "revised_translation": "B", "source_text": "b"}],
                     1: [{"region_id": "p001:R1", "action": "rewrite", "revised_translation": "A", "source_text": "a"}]}
            self.assertEqual(revision._semantic_revision_hash(one),
                             revision._semantic_revision_hash(other))

    def test_same_corrections_are_materially_equivalent(self) -> None:
        # Scenario A/D: identical corrections, different run -> no new version.
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            first = revision._semantic_revision_hash(self._changes())
            second = revision._semantic_revision_hash(self._changes())
            self.assertEqual(first, second)

    def test_changed_translation_or_source_creates_a_new_version(self) -> None:
        # Scenario B (new text) and C (OCR corrected -> different source_text).
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            base = revision._semantic_revision_hash(self._changes())
            self.assertNotEqual(revision._semantic_revision_hash(self._changes(translation="Fuja!")), base)
            self.assertNotEqual(revision._semantic_revision_hash(self._changes(source="Run n0w")), base)

    def test_render_pipeline_version_participates_in_the_hash(self) -> None:
        # A renderer fix that changes pixels but not the reviewed text must still
        # count as a new material revision, so it can supersede an old PDF.
        import chapter_quality_revision as cqr
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            base = revision._semantic_revision_hash(self._changes())
            original = cqr.RENDER_PIPELINE_VERSION
            try:
                cqr.RENDER_PIPELINE_VERSION = original + "-next"
                self.assertNotEqual(revision._semantic_revision_hash(self._changes()), base)
            finally:
                cqr.RENDER_PIPELINE_VERSION = original

    def test_version_manifest_records_and_reports_the_latest_existing_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            self.assertIsNone(revision._latest_pdf_version())
            pdf = revision.output_dir / "chapter_reviewed_v2.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            revision._record_pdf_version(
                pdf_path=pdf,
                manifest={"revision_id": "rev1", "reviewed_pdf_sha256": "abc"},
                semantic_hash="hash-1",
                changed_by_page=self._changes(),
                parent=None,
                page_count=68,
            )
            latest = revision._latest_pdf_version()
            self.assertEqual(latest["semantic_revision_hash"], "hash-1")
            self.assertEqual(latest["changed_region_ids"], ["p003:R1"])
            self.assertEqual(latest["page_count"], 68)
            self.assertTrue(latest["contains_new_material_changes"])

    def test_a_missing_pdf_file_is_not_offered_as_the_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision(Path(folder))
            pdf = revision.output_dir / "gone.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            revision._record_pdf_version(
                pdf_path=pdf, manifest={"revision_id": "rev1"}, semantic_hash="h",
                changed_by_page=self._changes(), parent=None, page_count=68)
            pdf.unlink()
            self.assertIsNone(revision._latest_pdf_version())

    def test_gate_is_wired_into_pdf_generation(self) -> None:
        source = (ROOT / "chapter_quality_revision.py").read_text(encoding="utf-8")
        self.assertIn("no_new_material_changes", source)
        self.assertIn("materially_equivalent_to", source)
        self.assertIn("contains_new_material_changes", source)


class AtomicStateWriteContracts(unittest.TestCase):
    def test_progress_writes_survive_a_concurrent_reader(self) -> None:
        # The UI polls these files for live progress. On Windows an open reader
        # blocks the atomic swap, which used to fail whole revisions.
        import threading

        from chapter_quality_revision import write_json

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "checkpoint.json"
            write_json(path, {"tick": -1})
            stop = threading.Event()

            def reader() -> None:
                while not stop.is_set():
                    try:
                        path.read_text(encoding="utf-8")
                    except OSError:
                        pass

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            try:
                for tick in range(40):
                    write_json(path, {"tick": tick})
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tick"], 39)
            self.assertFalse(path.with_suffix(".json.tmp").exists())


class RevisionResponseCacheContracts(unittest.TestCase):
    def _record(self, **overrides):
        record = {
            "region_id": "p1:R1", "source_text": "Run now",
            "current_translation": "Corra agora", "text_type": "dialogue",
            "previous_context": "antes", "next_context": "depois",
            "constraints": {"max_characters": 80, "max_lines": 3},
        }
        record.update(overrides)
        return record

    def _kwargs(self, **overrides):
        kwargs = {"provider": "nvidia", "model": "m1", "endpoint": "https://e/v1",
                  "glossary": {"terms": []}, "ocr_text": "Run now"}
        kwargs.update(overrides)
        return kwargs

    def _answer(self):
        return {"region_id": "p1:R1", "action": "rewrite", "revised_translation": "Corra!",
                "risk": "low", "confidence": 0.97, "reason_code": "ok", "terminology": []}

    def test_identical_inputs_hit_the_cache_across_processes(self) -> None:
        from chapter_quality_revision import RevisionResponseCache

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cache.jsonl"
            first = RevisionResponseCache(path)
            key, hashes = first.build_key(self._record(), **self._kwargs())
            self.assertIsNone(first.lookup(key, "p1:R1"))
            first.store(key, "p1:R1", self._answer(), hashes)
            # A fresh instance must reuse the answer written to disk.
            second = RevisionResponseCache(path)
            again, _ = second.build_key(self._record(), **self._kwargs())
            self.assertEqual(again, key)
            self.assertEqual((second.lookup(again, "p1:R1") or {})["revised_translation"], "Corra!")
            self.assertEqual(second.stats()["cache_hits"], 1)
            self.assertEqual(second.stats()["provider_requests_avoided"], 1)

    def test_any_changed_input_invalidates_the_cached_answer(self) -> None:
        from chapter_quality_revision import RevisionResponseCache

        mutations = {
            "source_text": {"source_text": "Run away"},
            "current_translation": {"current_translation": "Corra ja"},
            "previous_context": {"previous_context": "outro"},
            "next_context": {"next_context": "outro"},
            "constraints": {"constraints": {"max_characters": 40, "max_lines": 2}},
        }
        for label, override in mutations.items():
            with tempfile.TemporaryDirectory() as folder:
                cache = RevisionResponseCache(Path(folder) / "cache.jsonl")
                key, hashes = cache.build_key(self._record(), **self._kwargs())
                cache.store(key, "p1:R1", self._answer(), hashes)
                changed, _ = cache.build_key(self._record(**override), **self._kwargs())
                self.assertNotEqual(changed, key, label)
                self.assertIsNone(cache.lookup(changed, "p1:R1"), label)
                self.assertEqual(cache.invalidations, 1, label)
                self.assertIn("input_changed", cache.invalidation_reasons)

    def test_model_ocr_and_glossary_are_part_of_the_key(self) -> None:
        from chapter_quality_revision import RevisionResponseCache

        with tempfile.TemporaryDirectory() as folder:
            cache = RevisionResponseCache(Path(folder) / "cache.jsonl")
            base, _ = cache.build_key(self._record(), **self._kwargs())
            for override in ({"model": "m2"}, {"ocr_text": "Run n0w"},
                             {"glossary": {"terms": [{"term": "Sunny"}]}},
                             {"endpoint": "https://other/v1"}):
                other, _ = cache.build_key(self._record(), **self._kwargs(**override))
                self.assertNotEqual(other, base, override)

    def test_manual_review_is_never_cached(self) -> None:
        from chapter_quality_revision import RevisionResponseCache

        with tempfile.TemporaryDirectory() as folder:
            cache = RevisionResponseCache(Path(folder) / "cache.jsonl")
            key, hashes = cache.build_key(self._record(), **self._kwargs())
            stored = cache.store(key, "p1:R1", {"region_id": "p1:R1", "action": "manual_review"}, hashes)
            self.assertFalse(stored)
            self.assertIsNone(cache.lookup(key, "p1:R1"))

    def test_cache_file_never_stores_secrets_and_survives_corruption(self) -> None:
        from chapter_quality_revision import RevisionResponseCache

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cache.jsonl"
            cache = RevisionResponseCache(path)
            key, hashes = cache.build_key(self._record(), **self._kwargs())
            cache.store(key, "p1:R1", self._answer(), hashes)
            raw = path.read_text(encoding="utf-8").lower()
            for secret in ("authorization", "bearer", "api_key", "apikey", "cookie", "password"):
                self.assertNotIn(secret, raw, secret)
            # A truncated trailing line must not break loading the good entries.
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"cache_key": "broken", "regi')
            recovered = RevisionResponseCache(path)
            self.assertIsNotNone(recovered.lookup(key, "p1:R1"))


class LiveRevisionProgressContracts(unittest.TestCase):
    def _revision_with_checkpoint(self, root: Path, checkpoint: dict) -> ChapterQualityRevision:
        output = root / "output"
        folder = output / "quality_revision" / "rev1"
        folder.mkdir(parents=True)
        manifest = folder / "revision_manifest.json"
        manifest.write_text(json.dumps({"revision_id": "rev1", "status": "running", "requests": 0}), encoding="utf-8")
        (folder / "nvidia_revision_checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        (output / "quality_revision" / "latest_revision.json").write_text(
            json.dumps({"revision_id": "rev1", "manifest_path": str(manifest)}), encoding="utf-8")
        return ChapterQualityRevision(output, job_id="job-1", run_id="run-1")

    def test_running_revision_reports_real_counters_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            revision = self._revision_with_checkpoint(Path(folder), {
                "requests_used": 27, "regions_completed": 27, "regions_pending": 46,
                "suspicious_regions": 73, "skipped_unchanged_regions": 35,
                "resumed_regions": 0, "valid": 26, "manual": 3, "elapsed_ms": 91000,
                "risk_counts": {"low": 20, "medium": 4, "high": 3},
            })
            progress = revision.live_progress()
            # The manifest still says 0 requests; the loop's checkpoint is authoritative.
            self.assertEqual(progress["requests"], 27)
            self.assertEqual(progress["regions_completed"], 27)
            self.assertEqual(progress["regions_pending"], 46)
            self.assertEqual(progress["suspicious_regions"], 73)
            self.assertEqual(progress["risk_counts"], {"low": 20, "medium": 4, "high": 3})
            self.assertEqual(progress["elapsed_ms"], 91000)

    def test_live_progress_is_empty_without_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            (output / "quality_revision").mkdir(parents=True)
            revision = ChapterQualityRevision(output, job_id="job-1", run_id="run-1")
            self.assertEqual(revision.live_progress(), {})

    def test_ui_panel_renders_live_counters(self) -> None:
        js = JS.read_text(encoding="utf-8")
        self.assertIn("regions_completed", js)
        self.assertIn("regions_pending", js)


class SuspiciousRegionSelection(unittest.TestCase):
    def _revision(self) -> ChapterQualityRevision:
        return ChapterQualityRevision("unused", job_id="job-1", run_id="run-1")

    def test_clean_translation_is_not_suspicious(self) -> None:
        record = {"region_id": "p1:R1", "source_text": "You think you can defeat me?",
                  "current_translation": "Voce acha que pode me derrotar?", "ocr_confidence": 0.95,
                  "quality_reasons": []}
        self.assertEqual(self._revision()._suspicious_reasons(record), [])

    def test_generic_quality_signals_flag_regions(self) -> None:
        revision = self._revision()
        expectations = {
            "already_flagged": {"source_text": "Master", "current_translation": "Mestre",
                                "ocr_confidence": 0.99, "quality_reasons": ["needs_context"]},
            "low_ocr_confidence": {"source_text": "The shadow rose", "current_translation": "A sombra se ergueu",
                                   "ocr_confidence": 0.4, "quality_reasons": []},
            "empty_translation": {"source_text": "Hello there", "current_translation": "",
                                  "ocr_confidence": 0.9, "quality_reasons": []},
            "untranslated_literal": {"source_text": "Run away now", "current_translation": "Run away now",
                                     "ocr_confidence": 0.95, "quality_reasons": []},
            "suspicious_truncation": {"source_text": "The nightmare spell shattered the sky above them",
                                      "current_translation": "O feitico", "ocr_confidence": 0.95,
                                      "quality_reasons": []},
        }
        for expected, record in expectations.items():
            record = {"region_id": "p1:R", **record}
            self.assertIn(expected, revision._suspicious_reasons(record), expected)

    def test_partition_keeps_unsuspicious_regions_out_of_the_model(self) -> None:
        revision = self._revision()
        clean = {"region_id": "p1:R1", "source_text": "Go now", "current_translation": "Va agora",
                 "ocr_confidence": 0.98, "quality_reasons": []}
        flagged = {"region_id": "p1:R2", "source_text": "Go now", "current_translation": "",
                   "ocr_confidence": 0.98, "quality_reasons": []}
        suspicious, skipped = revision._partition_suspicious([clean, flagged])
        self.assertEqual([r["region_id"] for r in suspicious], ["p1:R2"])
        self.assertEqual([r["region_id"] for r in skipped], ["p1:R1"])
        # A skipped region is explicitly unchanged, never "approved by the model".
        unchanged = revision._unchanged_review(skipped[0])
        self.assertEqual(unchanged["action"], "keep")
        self.assertEqual(unchanged["reason_code"], "not_suspicious_unchanged")
        self.assertEqual(unchanged["contract_path"], "not_reviewed")

    def test_preserved_classes_never_reach_the_suspicious_heuristic(self) -> None:
        revision = self._revision()
        for classification in ("sfx", "credit", "watermark", "decorative", "editorial"):
            self.assertFalse(revision._is_reviewable({"classification": classification, "source_text": "BOOM"}),
                             classification)


class ReviewModeNavigationContracts(unittest.TestCase):
    def test_exit_review_mode_releases_the_new_translation_form(self) -> None:
        source = JS.read_text(encoding="utf-8")
        self.assertIn("function exitReviewMode", source)
        self.assertIn("if (start) { start.hidden = false; start.disabled = false; }", source)
        self.assertIn("['view', 'job_id', 'run_id'].forEach(key => url.searchParams.delete(key));", source)
        # Choosing Nova tradução from the rail must leave review_mode.
        self.assertIn("if (tab.dataset.tab === 'nova' && appState.reviewMode) exitReviewMode();", source)

    def test_opening_review_mode_does_not_mark_the_selected_job_as_a_fresh_draft(self) -> None:
        source = JS.read_text(encoding="utf-8")
        self.assertIn(
            "name === 'nova' && !appState.reviewMode",
            source,
        )

    def test_review_mode_hides_unrelated_source_and_live_pipeline_surfaces(self) -> None:
        source = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "tradutor_ui.css").read_text(encoding="utf-8")
        self.assertIn(
            '<!-- form -->\n        <div class="panel" id="newTranslationFormPanel">',
            source,
        )
        self.assertIn('id="translationPreviewPanel"', source)
        for selector in (
            "#newTranslationFormPanel",
            "#translationPreviewPanel",
            "#loadingSurface",
            "#stageList",
            "#runSummary",
            "#runStatusCard",
            "#sourceReviewPanel",
            "#sourceReadyPanel",
        ):
            self.assertIn(f'html[data-review-mode="1"] {selector}', styles)
        self.assertIn(
            'html[data-review-mode="1"] .nt-grid{grid-template-columns:minmax(0,1fr)}',
            styles,
        )

    def test_deep_review_actions_are_visible_in_the_review_panel(self) -> None:
        source = JS.read_text(encoding="utf-8")
        for action in (
            "edited", "reviewed", "rejected", "preserved_original", "manual_review",
        ):
            self.assertIn(
                f'class="btn-ghost show" data-review-deep-action="{action}"',
                source,
            )

    def test_auth_identity_transition_clears_private_review_before_refresh(self) -> None:
        source = JS.read_text(encoding="utf-8")
        self.assertIn("function clearPrivateUiForAuthTransition", source)
        self.assertIn("exitReviewMode();", source)
        listener = source[
            source.index("window.addEventListener('tradutor-auth-changed', event => {"):
            source.index("applyCanonicalAuthSurface(getGlobal('__tradutorAuthState')")
        ]
        self.assertIn("clearPrivateUiForAuthTransition", listener)
        self.assertLess(
            listener.index("clearPrivateUiForAuthTransition"),
            listener.index("void refreshBootstrap()"),
        )


class PipelineCanonicalUiContracts(unittest.TestCase):
    def test_pipeline_uses_canonical_stage_aliases_for_new_backend_stages(self) -> None:
        source = JS.read_text(encoding="utf-8")
        for marker in (
            "const stageAliases = {",
            "reading_text: 'ocr'",
            "translating: 'translate'",
            "redrawing: 'render'",
            "generating_pdf: 'pdf'",
            "quality_review: 'quality_review'",
            "review_required: 'quality_review'",
        ):
            self.assertIn(marker, source)

    def test_preview_is_rendered_from_canonical_pipeline_state(self) -> None:
        source = JS.read_text(encoding="utf-8")
        render_progress = source[
            source.index("function renderProgress("):source.index("function shouldRenderSourceReview")
        ]
        self.assertIn("renderPipelinePreview(state);", render_progress)
        self.assertNotIn("aguardando início", render_progress)
        self.assertIn("buildPipelineState(runtime, visibleProgress)", source)
        self.assertIn("appState.currentPipelineState = pipelineState", source)

    def test_stage_list_contains_quality_review_step_and_explicit_states(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        self.assertIn('data-stage="quality_review"', shell)
        self.assertIn("item.dataset.state =", source)
        self.assertIn("'completed'", source)
        self.assertIn("'active'", source)
        self.assertIn("'future'", source)


class QualityReviewBulkContracts(unittest.TestCase):
    def test_frontend_exposes_safe_bulk_controls(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        for marker in (
            "qualityReviewSelectAll",
            "acceptLowRiskReview",
            "acceptAllReview",
            "undoBulkReview",
            "globalAiReview",
            "/api/ui/quality-review/bulk-action",
            "Aceitar baixo risco",
            "Esta seleção inclui itens de alto risco",
        ):
            self.assertIn(marker, shell + source)

    def test_job_store_bulk_review_actions_are_atomic_and_restore_previous_values(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            try:
                job_id = store.create_job(
                    source_url="https://example.test/chapter",
                    output_dir=str(Path(folder) / "output"),
                    command=["python"],
                    configuration={"job_type": "translation"},
                )
                store.transition(job_id, JobStatus.CLAIMING)
                store.transition(job_id, JobStatus.STARTING)
                store.transition(job_id, JobStatus.RUNNING)
                store.transition(job_id, JobStatus.REVIEW_REQUIRED)
                store.record_review_actions_bulk(job_id, {"p1:i1": "reviewed", "p1:i2": "preserved_original"})
                self.assertEqual(
                    store.review_actions(job_id),
                    {"p1:i1": "reviewed", "p1:i2": "preserved_original"},
                )
                store.record_review_actions_bulk(job_id, {"p1:i1": "pending", "p1:i2": "reviewed"})
                self.assertEqual(store.review_actions(job_id), {"p1:i2": "reviewed"})
            finally:
                store.close()

    def test_no_chapter_specific_translation_hardcode_in_runtime(self) -> None:
        runtime = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (ROOT / "static").glob("*.js")
        ) + "\n" + (ROOT / "ui_bridge.py").read_text(encoding="utf-8", errors="ignore")
        runtime += "\n" + (ROOT / "chapter_quality_revision.py").read_text(encoding="utf-8", errors="ignore")
        forbidden = (
            "REAL COFFEE",
            "PRECINCT",
            "CODE BLACK",
            "I AM",
            "THE INFECTED",
            "THE NIGHTMARE SPELL",
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, runtime)
        self.assertNotIn("shadow_slave", runtime.lower())
        self.assertNotIn('if text ==', runtime)
        self.assertNotIn('if source_text ==', runtime)


class FakeContextualReviewer:
    model = "fake-contextual-reviewer"

    def __init__(self) -> None:
        self.requests = 0

    def review_batch(self, records, glossary):
        self.requests += 1
        result = []
        for record in reversed(records):
            result.append({
                "region_id": record["region_id"],
                "action": "manual_review" if record["region_id"].endswith("REGION_002") else "keep",
                "revised_translation": record["current_translation"],
                "reason_code": "fake_review",
                "confidence": 0.99,
                "risk": "high" if record["region_id"].endswith("REGION_002") else "low",
                "terminology": [],
            })
        return result


class FakeRewriteReviewer:
    model = "fake-rewrite-reviewer"

    def __init__(self) -> None:
        self.requests = 0

    def review_batch(self, records, glossary, **_kwargs):
        self.requests += 1
        result = []
        for record in records:
            result.append({
                "region_id": record["region_id"],
                "action": "rewrite" if record["region_id"].endswith("REGION_001") else "manual_review",
                "revised_translation": "Olá de verdade.",
                "reason_code": "safe_test_rewrite" if record["region_id"].endswith("REGION_001") else "needs_review",
                "confidence": 0.99,
                "risk": "low" if record["region_id"].endswith("REGION_001") else "high",
                "terminology": [],
            })
        return result


class ReviewContractParserContracts(unittest.TestCase):
    def _records(self):
        return [
            {"region_id": "p001:REGION_001", "source_text": "HELLO", "current_translation": "Olá"},
            {"region_id": "p001:REGION_002", "source_text": "WAIT", "current_translation": "Espere"},
        ]

    def _envelope(self, results=None, batch_id="batch-1"):
        if results is None:
            results = [
                {
                    "region_id": "p001:REGION_001",
                    "action": "keep",
                    "revised_translation": "",
                    "reason_code": "already_good",
                    "confidence": 0.98,
                    "risk": "low",
                    "terminology": [],
                },
                {
                    "region_id": "p001:REGION_002",
                    "action": "rewrite",
                    "revised_translation": "Espere um pouco.",
                    "reason_code": "natural_ptbr",
                    "confidence": 0.99,
                    "risk": "low",
                    "terminology": [],
                },
            ]
        return {"schema_version": REVIEW_SCHEMA_VERSION, "batch_id": batch_id, "results": results}

    def _parse(self, payload, batch_id="batch-1"):
        reviewer = ContextualNvidiaReviewer()
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return reviewer._parse_contract_response(text, self._records(), batch_id)

    def test_structured_contract_accepts_valid_json_and_out_of_order_ids(self) -> None:
        envelope = self._envelope(results=list(reversed(self._envelope()["results"])))
        parsed = self._parse(envelope)
        self.assertTrue(parsed.valid)
        self.assertEqual({item["region_id"] for item in parsed.items}, {"p001:REGION_001", "p001:REGION_002"})

    def test_structured_contract_tolerates_single_markdown_fence_and_prose(self) -> None:
        parsed = self._parse("Observação.\n```json\n" + json.dumps(self._envelope(), ensure_ascii=False) + "\n```\nFim.")
        self.assertTrue(parsed.valid)
        self.assertIn("prose_before_json", parsed.categories)
        self.assertIn("prose_after_json", parsed.categories)

    def test_structured_contract_rejects_truncated_json(self) -> None:
        parsed = self._parse('{"schema_version": "1.0", "batch_id": "batch-1", "results": [')
        self.assertFalse(parsed.valid)
        self.assertIn("truncated_json", parsed.categories)

    def test_structured_contract_rejects_wrong_root(self) -> None:
        parsed = self._parse([])
        self.assertFalse(parsed.valid)
        self.assertIn("wrong_root_type", parsed.categories)

    def test_structured_contract_requires_exact_region_id_set(self) -> None:
        results = self._envelope()["results"][:1]
        parsed = self._parse(self._envelope(results=results))
        self.assertFalse(parsed.valid)
        self.assertIn("missing_regions", parsed.categories)

    def test_structured_contract_rejects_extra_duplicate_and_unknown_ids(self) -> None:
        results = self._envelope()["results"]
        results = results + [{**results[0]}, {**results[0], "region_id": "p999:REGION_X"}]
        parsed = self._parse(self._envelope(results=results))
        self.assertFalse(parsed.valid)
        self.assertIn("duplicate_region_id", parsed.categories)
        self.assertIn("unknown_region_id", parsed.categories)
        self.assertIn("extra_regions", parsed.categories)

    def test_structured_contract_rejects_invalid_fields(self) -> None:
        bad = self._envelope()["results"]
        bad[0]["action"] = "accept"
        bad[0]["confidence"] = 1.2
        bad[0]["risk"] = "certain"
        bad[1]["revised_translation"] = ""
        parsed = self._parse(self._envelope(results=bad))
        self.assertFalse(parsed.valid)
        self.assertIn("invalid_action", parsed.categories)
        self.assertIn("invalid_confidence", parsed.categories)
        self.assertIn("invalid_risk", parsed.categories)
        self.assertIn("empty_translation", parsed.categories)

    def test_structured_contract_never_maps_by_position(self) -> None:
        results = [
            {
                "region_id": "unknown",
                "action": "rewrite",
                "revised_translation": "Texto posicional.",
                "reason_code": "bad_id",
                "confidence": 0.99,
                "risk": "low",
                "terminology": [],
            }
        ]
        parsed = self._parse(self._envelope(results=results))
        self.assertFalse(parsed.valid)
        self.assertEqual(parsed.items, [])
        self.assertIn("unknown_region_id", parsed.categories)


class FakeProviderReviewer(ContextualNvidiaReviewer):
    def __init__(self, responses):
        super().__init__()
        self.api_key = "configured-for-test"
        self.responses = list(responses)
        self.sent = []

    def _post_chat_completion(self, request_payload, started, purpose, **kwargs):
        self.requests += 1
        self.sent.append(purpose)
        content = self.responses.pop(0)
        return {
            "purpose": purpose,
            "status_http": 200,
            "duration_seconds": 0.01,
            "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            "raw_body": "",
            "finish_reason": "stop",
            "provider_error": None,
        }


class FakeTransportFailureReviewer(FakeProviderReviewer):
    def _post_chat_completion(self, request_payload, started, purpose, **kwargs):
        self.requests += 1
        self.sent.append(purpose)
        return {
            "purpose": purpose,
            "status_http": None,
            "duration_seconds": 0.01,
            "content": "",
            "raw_body": "",
            "finish_reason": None,
            "provider_error": "nvidia_review_request_failed",
            "provider_error_detail": "synthetic transport failure",
        }


class ReviewContractRecoveryContracts(unittest.TestCase):
    def _records(self):
        return [
            {"region_id": "p001:REGION_001", "source_text": "HELLO", "current_translation": "Olá"},
            {"region_id": "p001:REGION_002", "source_text": "WAIT", "current_translation": "Espere"},
        ]

    def _envelope(self, ids=None, batch_id="batch-1"):
        ids = ids or ["p001:REGION_001", "p001:REGION_002"]
        return {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": batch_id,
            "results": [
                {
                    "region_id": rid,
                    "action": "keep",
                    "revised_translation": "",
                    "reason_code": "already_good",
                    "confidence": 0.97,
                    "risk": "low",
                    "terminology": [],
                }
                for rid in ids
            ],
        }

    def test_repair_is_attempted_once_for_invalid_batch(self) -> None:
        reviewer = FakeProviderReviewer(["not json", self._envelope()])
        reviews = reviewer.review_batch(self._records(), {}, batch_id="batch-1")
        self.assertEqual(reviewer.sent, ["review", "repair"])
        self.assertEqual(reviewer.repaired_batches, 1)
        self.assertEqual({item["region_id"] for item in reviews}, {"p001:REGION_001", "p001:REGION_002"})

    def test_batch_falls_back_to_individual_regions_after_failed_repair(self) -> None:
        reviewer = FakeProviderReviewer([
            "not json",
            "still not json",
            self._envelope(ids=["p001:REGION_001"], batch_id="batch-1-region-01"),
            self._envelope(ids=["p001:REGION_002"], batch_id="batch-1-region-02"),
        ])
        reviews = reviewer.review_batch(self._records(), {}, batch_id="batch-1")
        self.assertEqual(reviewer.sent, ["review", "repair", "individual_fallback", "individual_fallback"])
        self.assertEqual(reviewer.fallback_individual, 2)
        self.assertTrue(all(item["contract_path"] == "individual_fallback" for item in reviews))

    def test_failed_contract_returns_manual_review_without_creating_translation(self) -> None:
        reviewer = FakeProviderReviewer(["not json", "still not json"])
        reviews = reviewer.review_batch([self._records()[0]], {}, batch_id="batch-1")
        self.assertEqual(reviewer.sent, ["review", "repair"])
        self.assertEqual(reviews[0]["action"], "manual_review")
        self.assertEqual(reviews[0]["revised_translation"], "")
        self.assertIn("invalid_json", reviews[0]["contract_categories"])

    def test_transport_failure_does_not_attempt_repair_or_region_fallback(self) -> None:
        reviewer = FakeTransportFailureReviewer([])
        reviews = reviewer.review_batch(self._records(), {}, batch_id="batch-1")
        self.assertEqual(reviewer.sent, ["review"])
        self.assertEqual(reviewer.requests, 1)
        self.assertTrue(all(item["action"] == "manual_review" for item in reviews))
        self.assertTrue(all(item["reason_code"] == "nvidia_review_request_failed" for item in reviews))

    def test_provider_timeout_is_classified_without_json_parse_noise(self) -> None:
        self.assertEqual(ContextualNvidiaReviewer._provider_error_categories("nvidia_review_timeout"), ["timeout"])

    def test_subprocess_uses_current_environment_python_not_base_python(self) -> None:
        executable = ContextualNvidiaReviewer._subprocess_python_executable()
        self.assertEqual(Path(executable), Path(sys.executable))

    def test_diagnostic_mode_disables_repair_and_individual_fallback(self) -> None:
        reviewer = FakeProviderReviewer(["not json"])
        reviews = reviewer.review_batch(self._records(), {}, batch_id="batch-1", diagnostic_mode=True)
        self.assertEqual(reviewer.sent, ["review"])
        self.assertEqual(reviewer.requests, 1)
        self.assertEqual(reviewer.invalid_batches, 1)
        self.assertTrue(all(item["action"] == "manual_review" for item in reviews))

    def test_timeout_config_keeps_subprocess_above_http_read_timeout(self) -> None:
        reviewer = ContextualNvidiaReviewer()
        config = reviewer._timeout_config()
        self.assertGreater(config["subprocess_seconds"], config["read_seconds"])
        self.assertLess(config["connect_seconds"], config["read_seconds"])

    def test_prompt_records_are_compact_without_cutting_core_text(self) -> None:
        reviewer = ContextualNvidiaReviewer()
        record = {
            "region_id": "p001:REGION_001",
            "source_text": "A" * 220,
            "current_translation": "B" * 180,
            "previous_context": "P" * 500,
            "next_context": "N" * 500,
            "text_type": "dialogue",
            "ocr_confidence": 0.81234,
        }
        compact = reviewer._prompt_record(record)
        self.assertEqual(compact["source_text"], record["source_text"])
        self.assertEqual(compact["current_translation"], record["current_translation"])
        self.assertLessEqual(len(compact["previous_context"]), 161)
        self.assertLessEqual(len(compact["next_context"]), 161)
        self.assertNotIn("page_id", compact)
        self.assertNotIn("glossary", compact)

    def test_glossary_is_filtered_to_terms_used_by_the_batch(self) -> None:
        reviewer = ContextualNvidiaReviewer()
        glossary = {"terms": [
            {"term": "Sunny", "category": "name", "count": 9, "policy": "preserve"},
            {"term": "UnusedTerm", "category": "name", "count": 9, "policy": "preserve"},
        ]}
        compact = reviewer._compact_glossary(glossary, [{"source_text": "Sunny speaks.", "current_translation": ""}])
        self.assertEqual([item["term"] for item in compact["terms"]], ["Sunny"])

    def test_minimal_health_check_uses_same_structured_contract(self) -> None:
        reviewer = FakeProviderReviewer([{
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": "health-check",
            "results": [],
        }])
        result = reviewer.health_check(batch_id="health-check")
        self.assertTrue(result["ok"])
        self.assertEqual(reviewer.sent, ["health_check"])

    def test_requests_use_honored_structured_output_mechanism(self) -> None:
        captured: list[dict] = []

        class RecordingReviewer(FakeProviderReviewer):
            def _post_chat_completion(self, request_payload, started, purpose, **kwargs):
                captured.append(request_payload)
                return super()._post_chat_completion(request_payload, started, purpose, **kwargs)

        reviewer = RecordingReviewer([{
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": "batch-1",
            "results": [{
                "region_id": "p001:REGION_001",
                "action": "keep",
                "revised_translation": "",
                "reason_code": "ok",
                "confidence": 0.9,
                "risk": "low",
                "terminology": [],
            }],
        }])
        reviewer.review_batch(self._records(), {}, batch_id="batch-1", diagnostic_mode=True)
        payload = captured[0]
        # The endpoint ignores nvext.guided_json for this model but honors
        # response_format json_schema strict, so the payload must use it.
        self.assertNotIn("nvext", payload)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertFalse(payload["chat_template_kwargs"]["thinking"])


class FullChapterQualityRevisionContracts(unittest.TestCase):
    def _fixture_output(self, root: Path) -> Path:
        from PIL import Image

        output = root / "output"
        output.mkdir()
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% fake hermetic pdf\n" + b"0" * 2048)
        image = output / "page_001.jpg"
        Image.new("RGB", (240, 320), "white").save(image, "JPEG")
        progress = {
            "pdf_path": str(pdf),
            "pages": [
                {
                    "index": 1,
                    "sequence_index": 1,
                    "image_path": str(image),
                    "output_path": str(image),
                    "debug_data": {
                        "image_path": str(image),
                        "items": [
                            {
                                "id": "BALAO_1",
                                "region_id": "REGION_001",
                                "clean_text": "HELLO THERE",
                                "translation": "Olá.",
                                "classification": "speech",
                                "confidence": 0.99,
                                "bounding_box": [20, 20, 160, 80],
                                "sent_to_nvidia": True,
                                "redrawn": True,
                                "translation_final_state": "translated",
                            },
                            {
                                "id": "BALAO_2",
                                "region_id": "REGION_002",
                                "clean_text": "WHAT IS THIS?",
                                "translation": "WHAT IS THIS?",
                                "classification": "speech",
                                "confidence": 0.91,
                                "bounding_box": [20, 100, 180, 160],
                                "sent_to_nvidia": True,
                                "redrawn": False,
                                "manual_review_required": True,
                                "translation_final_state": "manual_review",
                            },
                            {
                                "id": "SFX_1",
                                "region_id": "REGION_003",
                                "clean_text": "BANG",
                                "translation": "BANG",
                                "classification": "sfx",
                                "confidence": 0.95,
                                "bounding_box": [20, 180, 180, 220],
                                "preserved_original": True,
                            },
                        ],
                    },
                }
            ],
        }
        quality = {
            "summary": {
                "pdf_path": str(pdf),
                "quality_validation": {
                    "smart_split_details": [
                        {"page": 1, "requires_review": True, "safe_band": False, "band_score": 1.2}
                    ]
                },
            },
            "pages": [{"index": 1}],
        }
        (output / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        (output / "quality_report.json").write_text(json.dumps(quality), encoding="utf-8")
        return output

    def test_revision_run_uses_existing_artifacts_and_preserves_original_pdf_without_identical_copy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            original = output / "chapter.pdf"
            before = original.read_bytes()
            revision = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            )
            status = revision.start()
            self.assertEqual(status["parent_job_id"], "job-1")
            self.assertEqual(status["parent_run_id"], "run-1")
            self.assertEqual(status["source_pdf_path"], str(original))
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(status["reviewed_pdf_path"], "")
            self.assertEqual(status["reviewed_pdf_sha256"], "")
            self.assertEqual(status["no_reviewed_pdf_reason"], "no_safe_changes_applied")
            self.assertEqual(status["total_pages"], 1)
            self.assertEqual(status["total_regions"], 3)
            self.assertEqual(status["publication_created"], False)

    def test_revision_creates_versioned_pdf_only_when_safe_changes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            original = output / "chapter.pdf"
            status = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeRewriteReviewer,
            ).start()
            reviewed = Path(status["reviewed_pdf_path"])
            self.assertTrue(reviewed.is_file())
            self.assertEqual(reviewed.name, "chapter_reviewed_v2.pdf")
            self.assertNotEqual(reviewed.name, original.name)
            self.assertEqual(status["safe_changes_applied"], 1)
            self.assertEqual(status["publication_created"], False)

    def test_revision_sends_one_region_per_request(self) -> None:
        # The model is 100% ID-complete only with one region per request; batching
        # lets it silently omit regions, so both canary and full revision use bs=1.
        revision = ChapterQualityRevision("unused", job_id="job-1", run_id="run-1")
        records = [{"region_id": f"p001:REGION_{idx:03d}", "source_text": "HELLO", "current_translation": "Olá"} for idx in range(10)]
        self.assertEqual(revision._revision_batch_size(records[:1], canary=True), 1)
        self.assertEqual(revision._revision_batch_size(records[:10], canary=True), 1)
        self.assertEqual(revision._revision_batch_size(records[:10], canary=False), 1)

    def test_revision_filters_progress_pages_to_quality_report_pages(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            progress_path = output / "progress.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            extra = json.loads(json.dumps(progress["pages"][0]))
            extra["index"] = 2
            extra["sequence_index"] = 2
            extra["debug_data"]["items"][0]["region_id"] = "REGION_EXTRA"
            progress["pages"].append(extra)
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
            status = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            ).start()
            self.assertEqual(status["total_pages"], 1)
            self.assertEqual(status["total_regions"], 3)

    def test_revision_validates_region_ids_and_keeps_high_risk_manual(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            status = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            ).start()
            latest = json.loads((output / "quality_revision" / "latest_revision.json").read_text(encoding="utf-8"))
            contextual = json.loads(Path(latest["manifest_path"]).with_name("contextual_translation_review.json").read_text(encoding="utf-8"))
            by_id = {item["region_id"]: item for item in contextual["reviews"]}
            self.assertIn("p001:REGION_001", by_id)
            self.assertEqual(by_id["p001:REGION_002"]["action"], "manual_review")
            self.assertEqual(status["safe_changes_applied"], 0)
            self.assertEqual(status["status"], "review_required")

    def test_frontend_exposes_full_chapter_revision_action(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        self.assertIn("REVISAR CAPÍTULO INTEIRO", shell)
        self.assertIn("/api/ui/quality-review/revision/start", source)
        self.assertIn("pollQualityRevisionStatus", source)
        self.assertIn("qualityRevisionStatus", shell)

    def test_frontend_exposes_developer_only_contract_canary_action(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        self.assertIn("TESTAR CONTRATO NVIDIA", shell)
        self.assertIn('id="nvidiaContractCanary" hidden', shell)
        self.assertIn("qualityReviewDeveloperMode", source)
        self.assertIn("/api/ui/quality-review/revision/canary/start", source)
        self.assertIn("tradutorDeveloperMode", source)
        self.assertIn("maxRegions = passed && reviewed >= 3", source)
        self.assertIn("passed && reviewed >= 1 ? 3 : 1", source)

    def test_nvidia_review_accepts_region_id_keyed_json(self) -> None:
        parsed = {
            "p001:REGION_002": {
                "action": "manual_review",
                "revised_translation": "",
                "reason_code": "needs_context",
                "confidence": 0.2,
                "risk": "high",
            }
        }
        items = ContextualNvidiaReviewer.review_items_from_parsed(parsed)
        self.assertEqual(items[0]["region_id"], "p001:REGION_002")
        self.assertEqual(items[0]["action"], "manual_review")

    def test_nvidia_review_translation_only_mapping_is_fail_closed(self) -> None:
        items = ContextualNvidiaReviewer.review_items_from_parsed({
            "p001:REGION_002": "Tradução sem contrato estruturado."
        })
        self.assertEqual(items[0]["region_id"], "p001:REGION_002")
        self.assertEqual(items[0]["action"], "manual_review")
        self.assertEqual(items[0]["risk"], "high")
        self.assertEqual(items[0]["reason_code"], "non_contract_translation_only_response")


class RaisingReviewer:
    """Reviewer that fails if the provider is ever called — proves cache-only."""
    model = "raising-reviewer"

    def __init__(self) -> None:
        self.requests = 0

    def review_batch(self, records, glossary, **_kwargs):
        raise AssertionError("provider must not be called without authorization")


class TargetedPageRevisionContracts(unittest.TestCase):
    def _two_page_output(self, root: Path) -> Path:
        from PIL import Image

        output = root / "output"
        output.mkdir()
        (output / "chapter.pdf").write_bytes(b"%PDF-1.4\n% fake\n" + b"0" * 1024)
        pages = []
        for index in (1, 2):
            image = output / f"page_{index:03d}.jpg"
            Image.new("RGB", (240, 320), "white").save(image, "JPEG")
            pages.append({
                "index": index, "sequence_index": index,
                "image_path": str(image), "output_path": str(image),
                "debug_data": {"image_path": str(image), "items": [
                    {"id": "BALAO_1", "region_id": "REGION_001", "clean_text": "HELLO THERE",
                     "translation": "Olá.", "classification": "speech", "confidence": 0.9,
                     "bounding_box": [20, 20, 160, 80], "sent_to_nvidia": True, "redrawn": True},
                    {"id": "SFX_1", "region_id": "REGION_002", "clean_text": "BANG",
                     "translation": "BANG", "classification": "sfx", "confidence": 0.9,
                     "bounding_box": [20, 200, 160, 60], "preserved_original": True},
                ]},
            })
        (output / "progress.json").write_text(json.dumps({"pdf_path": str(output / "chapter.pdf"), "pages": pages}), encoding="utf-8")
        (output / "quality_report.json").write_text(json.dumps({"pages": [{"index": 1}, {"index": 2}]}), encoding="utf-8")
        return output

    def _revision(self, output: Path) -> ChapterQualityRevision:
        return ChapterQualityRevision(output, job_id="job-1", run_id="run-1", reviewer_factory=FakeRewriteReviewer)

    def test_page_revision_drafts_only_the_target_page(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            page2_before = (output / "page_002.jpg").read_bytes()
            manifest = self._revision(output).revise_page(1)
            self.assertEqual(manifest["status"], "draft_ready")
            self.assertEqual(manifest["page_id"], "p001")
            self.assertIn("p001:REGION_001", manifest["region_ids"])
            self.assertEqual(manifest["safe_changes_applied"], 1)
            self.assertTrue(Path(manifest["draft_page_path"]).is_file())
            # Only page 1 has a draft; page 2 is never rendered.
            draft_root = Path(manifest["draft_page_path"]).parent
            self.assertFalse((draft_root / "page_002.png").exists())
            # No reviewed PDF is produced by a page revision.
            self.assertEqual(list(output.glob("*_reviewed_*.pdf")), [])
            # Other pages stay byte-identical.
            self.assertEqual((output / "page_002.jpg").read_bytes(), page2_before)

    def test_page_revision_records_lineage_and_is_resumable_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            revision = self._revision(output)
            manifest = revision.revise_page(1)
            self.assertIn("page_revision_id", manifest)
            self.assertIn("parent_revision_id", manifest)
            # The per-page pointer resolves back to this manifest (F5/resume).
            latest = revision.latest_page_revision(1)
            self.assertEqual(latest["page_revision_id"], manifest["page_revision_id"])

    def test_page_revision_can_scope_to_one_region(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            manifest = self._revision(output).revise_page(1, region_ids=["p001:REGION_001"])
            self.assertEqual(manifest["region_ids"], ["p001:REGION_001"])

    def test_page_revision_reports_when_nothing_is_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            # Scope to the preserved sfx region only: nothing reviewable remains.
            manifest = self._revision(output).revise_page(1, region_ids=["p001:REGION_002"])
            self.assertEqual(manifest["status"], "no_reviewable_regions")
            self.assertEqual(manifest["region_ids"], [])

    def test_cache_only_never_calls_the_provider_and_flags_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            revision = ChapterQualityRevision(output, job_id="job-1", run_id="run-1",
                                              reviewer_factory=RaisingReviewer)
            # No cache exists, so the one reviewable region needs authorization.
            manifest = revision.revise_page(1, cache_only=True)
            self.assertEqual(manifest["status"], "awaiting_provider_authorization")
            self.assertEqual(manifest["requests_needed"], 1)
            self.assertIn("p001:REGION_001", manifest["provider_authorization_required"])
            self.assertEqual(manifest["safe_changes_applied"], 0)
            self.assertEqual(list(output.glob("*_reviewed_*.pdf")), [])

    def test_list_page_regions_reports_cache_and_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            listing = ChapterQualityRevision(output, job_id="job-1", run_id="run-1",
                                             reviewer_factory=RaisingReviewer).list_page_regions(1)
            self.assertEqual(listing["page_id"], "p001")
            self.assertEqual(listing["reviewable_regions"], 1)
            self.assertEqual(listing["requests_needed"], 1)
            by_id = {r["region_id"]: r for r in listing["regions"]}
            self.assertTrue(by_id["p001:REGION_001"]["requires_authorization"])
            self.assertFalse(by_id["p001:REGION_002"]["reviewable"])

    def test_approve_reject_discard_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            revision = self._revision(output)
            pr = revision.revise_page(1)["page_revision_id"]
            self.assertEqual(revision.set_page_revision_outcome(pr, "approved")["status"], "approved")
            self.assertEqual(revision.set_page_revision_outcome(pr, "rejected")["status"], "rejected")
            discarded = revision.set_page_revision_outcome(pr, "discarded")
            self.assertEqual(discarded["status"], "discarded")
            self.assertEqual(discarded["draft_page_path"], "")

    def test_manual_region_validates_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            revision = self._revision(output)
            pr = revision.revise_page(1)["page_revision_id"]
            entry = revision.add_manual_region(pr, box=[10, 10, 60, 40], source_text="HELLO", region_type="speech")
            self.assertEqual(entry["box"], [10, 10, 60, 40])
            self.assertTrue(entry["region_id"].endswith("MANUAL_001"))
            with self.assertRaises(ValueError):
                revision.add_manual_region(pr, box=[10, 10, 2, 2])          # too small
            with self.assertRaises(ValueError):
                revision.add_manual_region(pr, box=[10, 10, 5000, 5000])    # out of bounds
            with self.assertRaises(ValueError):
                revision.add_manual_region(pr, box=[12, 12, 60, 40])        # overlaps MANUAL_001

    def test_forgotten_text_surfaces_candidates_without_translating(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._two_page_output(Path(folder))
            result = self._revision(output).search_forgotten_text(1)
            ids = {c["region_id"] for c in result["candidates"]}
            self.assertIn("p001:REGION_002", ids)  # preserved sfx surfaces as a candidate
            sfx = next(c for c in result["candidates"] if c["region_id"] == "p001:REGION_002")
            # A real onomatopoeia resolves to a preserve class, so the search
            # surfaces it for inspection but never proposes translating it.
            self.assertEqual(sfx["suggested_action"], "keep_preserved")
            self.assertTrue(sfx["do_not_translate"])
            self.assertEqual(sfx["classification_normalized"], "sfx_preserve")

    def test_frontend_exposes_page_revision_controls(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        js = JS.read_text(encoding="utf-8")
        for label in ("REVISAR ESTA PÁGINA", "Reprocessar pendências", "Reprocessar região",
                      "Detectar texto original restante", "Tentar reconstrução novamente", "CANCELAR REVISÃO DA PÁGINA",
                      "RETOMAR REVISÃO DA PÁGINA", "APROVAR PRÉVIA", "REJEITAR PRÉVIA", "DESCARTAR RASCUNHO"):
            self.assertIn(label, shell, label)
        for endpoint in ("/api/ui/page-revision/regions", "/api/ui/page-revision/start",
                         "/api/ui/page-revision/status", "/api/ui/page-revision/decision",
                         "/api/ui/page-revision/forgotten-text"):
            self.assertIn(endpoint, js, endpoint)
        self.assertNotIn('id="pageRevisionManual"', shell)
        # cancel/resume share one templated call.
        self.assertIn("/api/ui/page-revision/${kind}", js)
        self.assertIn("pageRevisionLifecycle('cancel')", js)
        self.assertIn("pageRevisionLifecycle('resume')", js)
        # F5 restore keeps the page revision id in the URL.
        self.assertIn("page_rev", js)
        self.assertIn("restorePageRevisionFromUrl", js)


if __name__ == "__main__":
    unittest.main()
