"""The loading screen shows a number, a time or a log line only when it has one.

The visual reference for this screen carries a percentage, per-step elapsed
times and a system log. Each of those is a thing the UI could easily fabricate,
so each has a test here proving it cannot.

The module is plain CommonJS: these run it in node and assert on real return
values. No scenario names a title, a URL, an episode, an owner or a job id — the
mapping must be generic, and a test that referenced any of those would be
proving the opposite.
"""

import _test_bootstrap  # noqa: F401

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE = ROOT / "static" / "loading_view.js"
NODE = shutil.which("node")


def run_js(body: str):
    script = (
        f"const L = require({json.dumps(str(MODULE))});\n"
        f"const out = (() => {{ {body} }})();\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def view(state: dict):
    return run_js(f"return L.mapJobStateToLoadingView({json.dumps(state)});")


@unittest.skipUnless(NODE, "node is required to execute the loading view module")
class PercentageIsNeverInvented(unittest.TestCase):
    def test_no_countable_work_means_no_number(self):
        for progress in ({}, {"total_pages": 0, "completed_pages": 0}, {"fraction": None},
                         {"completed_pages": "x"}, {"fraction": 4}):
            result = view({"status": "running", "stage": "ocr", "progress": progress})
            self.assertEqual(result["progress"]["mode"], "indeterminate", progress)
            self.assertIsNone(result["progress"]["percent"], progress)
            self.assertFalse(result["showPercent"], progress)

    def test_elapsed_time_alone_never_becomes_a_percentage(self):
        """A bar built from elapsed seconds says nothing about what remains."""
        result = view({"status": "running", "stage": "ocr",
                       "progress": {"elapsed_seconds": 120, "eta_seconds": 60}})
        self.assertEqual(result["progress"]["mode"], "indeterminate")
        self.assertIsNone(result["progress"]["percent"])

    def test_real_page_counts_produce_a_real_number(self):
        result = view({"status": "running", "stage": "download",
                       "progress": {"completed_pages": 8, "total_pages": 12}})
        self.assertEqual(result["progress"]["percent"], 67)
        self.assertEqual(result["progress"]["source"], "counts")
        self.assertEqual(result["progress"]["unit"], "pages")
        self.assertTrue(result["showPercent"])

    def test_real_block_counts_also_count(self):
        result = view({"status": "running", "stage": "translate",
                       "progress": {"completed_blocks": 2, "total_blocks": 5}})
        self.assertEqual(result["progress"]["percent"], 40)

    def test_the_backend_declaring_indeterminate_wins_over_counts(self):
        result = view({"status": "running", "stage": "ocr",
                       "progress": {"indeterminate": True, "completed_pages": 5, "total_pages": 10}})
        self.assertEqual(result["progress"]["mode"], "indeterminate")
        self.assertIsNone(result["progress"]["percent"])


@unittest.skipUnless(NODE, "node is required to execute the loading view module")
class DurationsComeOnlyFromTimestamps(unittest.TestCase):
    def test_without_timestamps_there_is_no_duration(self):
        result = run_js("return L.resolveDuration(null, null);")
        self.assertIsNone(result["seconds"])
        self.assertEqual(result["label"], "")

    def test_unusable_timestamps_produce_no_duration(self):
        result = run_js("return L.resolveDuration('2026-01-01T10:00:05Z','2026-01-01T10:00:00Z');")
        self.assertIsNone(result["seconds"])
        self.assertEqual(result["source"], "unusable_timestamps")

    def test_two_real_timestamps_produce_a_duration(self):
        result = run_js("return L.resolveDuration('2026-01-01T10:00:00Z','2026-01-01T10:00:07Z');")
        self.assertEqual(result["seconds"], 7)
        self.assertEqual(result["source"], "timestamps")


@unittest.skipUnless(NODE, "node is required to execute the loading view module")
class EventsAreSanitised(unittest.TestCase):
    def test_credential_shaped_lines_are_dropped(self):
        events = [
            {"message": "Ambiente preparado", "stage": "source_analysis", "status": "completed"},
            {"message": "Authorization: Bearer abc"},
            {"message": "Set-Cookie: session=1"},
            {"message": "Traceback (most recent call last)"},
            {"message": "leu C:\\Users\\alguem\\.env"},
            {"message": "csrf token invalido"},
            {"message": "owner_id 12345"},
            {"message": "OCR concluído", "stage": "ocr", "status": "completed"},
        ]
        result = run_js(f"return L.sanitiseEvents({json.dumps(events)});")
        self.assertEqual([entry["message"] for entry in result],
                         ["Ambiente preparado", "OCR concluído"])

    def test_a_non_list_is_not_an_error(self):
        for payload in ("null", "undefined", "'texto'", "42", "{}"):
            self.assertEqual(run_js(f"return L.sanitiseEvents({payload});"), [])

    def test_no_events_means_an_empty_list_not_a_placeholder(self):
        self.assertEqual(view({"status": "running", "stage": "ocr"})["events"], [])


@unittest.skipUnless(NODE, "node is required to execute the loading view module")
class TerminalStatesAreDistinct(unittest.TestCase):
    def test_review_required_is_not_a_failure(self):
        result = view({"status": "review_required", "stage": "quality_review",
                       "pending_review_count": 4})
        self.assertTrue(result["needsReview"])
        self.assertFalse(result["failed"])
        self.assertEqual(result["tone"], "review")
        self.assertEqual(result["pendingReviewCount"], 4)
        self.assertIn("revisão", result["description"].lower())

    def test_finished_is_success(self):
        result = view({"status": "finished", "stage": "pdf"})
        self.assertEqual(result["tone"], "success")
        self.assertFalse(result["failed"])
        self.assertFalse(result["needsReview"])

    def test_a_result_is_only_offered_when_it_exists(self):
        without = view({"status": "finished", "stage": "pdf"})
        self.assertFalse(without["hasResult"])
        self.assertFalse(without["hasPdf"])
        with_result = view({"status": "finished", "stage": "pdf",
                            "result_available": True, "pdf_available": True})
        self.assertTrue(with_result["hasResult"])
        self.assertTrue(with_result["hasPdf"])

    def test_failure_description_comes_from_the_backend_message(self):
        result = view({"status": "failed", "stage": "source_analysis",
                       "reason_code": "source_not_ready", "message": "A análise não avançou."})
        self.assertTrue(result["failed"])
        self.assertEqual(result["reasonCode"], "source_not_ready")
        self.assertEqual(result["description"], "A análise não avançou.")

    def test_retry_is_only_offered_when_the_backend_allows_it(self):
        self.assertFalse(view({"status": "failed", "stage": "ocr"})["canRetry"])
        self.assertTrue(view({"status": "failed", "stage": "ocr",
                              "retry_available": True})["canRetry"])

    def test_cancelled_is_neutral(self):
        result = view({"status": "cancelled", "stage": "ocr"})
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["failed"])
        self.assertEqual(result["tone"], "neutral")


@unittest.skipUnless(NODE, "node is required to execute the loading view module")
class TwoModesAreDistinct(unittest.TestCase):
    def test_bootstrap_has_three_short_groups_and_no_pipeline(self):
        result = view({"mode": "bootstrap", "status": "running", "stage": "session"})
        self.assertEqual(result["mode"], "bootstrap")
        self.assertEqual([g["key"] for g in result["groups"]],
                         ["session", "environment", "interface"])

    def test_pipeline_has_the_full_band(self):
        result = view({"status": "running", "stage": "ocr"})
        self.assertEqual(result["mode"], "pipeline")
        self.assertEqual(len(result["groups"]), 7)
        by_key = {g["key"]: g["status"] for g in result["groups"]}
        self.assertEqual(by_key["ocr"], "active")
        self.assertEqual(by_key["preparation"], "completed")
        self.assertEqual(by_key["document"], "pending")

    def test_an_unknown_mode_falls_back_to_pipeline(self):
        self.assertEqual(view({"mode": "nonsense", "status": "running"})["mode"], "pipeline")


@unittest.skipUnless(NODE, "node is required to execute the loading view module")
class UnknownAndEmptyStatesDegradeSafely(unittest.TestCase):
    def test_an_unknown_stage_still_reads(self):
        result = view({"status": "running", "stage": "brand_new_stage"})
        self.assertFalse(result["knownStage"])
        self.assertEqual(result["title"], "Brand new stage")
        self.assertNotIn("_", result["title"])
        self.assertTrue(result["description"])

    def test_an_empty_state_does_not_throw(self):
        for payload in ({}, {"status": ""}, {"stage": None}):
            result = view(payload)
            self.assertTrue(result["title"])
            self.assertFalse(result["showPercent"])

    def test_nothing_active_leaves_every_group_pending(self):
        result = view({"status": "queued", "stage": ""})
        self.assertTrue(all(g["status"] == "pending" for g in result["groups"]))


@unittest.skipUnless(NODE, "node is required to execute the loading view module")
class LanguagesComeFromTheJob(unittest.TestCase):
    def test_absent_languages_are_not_guessed(self):
        self.assertIsNone(view({"status": "running", "stage": "ocr"})["languages"])

    def test_reported_languages_are_used_as_given(self):
        result = view({"status": "running", "stage": "translate",
                       "source_language": "ko", "target_language": "pt-br"})
        self.assertEqual(result["languages"]["source"], "KO")
        self.assertEqual(result["languages"]["target"], "PT-BR")


class ModuleCarriesNoConcreteData(unittest.TestCase):
    def test_no_chapter_url_or_identity_leaks_into_the_module(self):
        text = MODULE.read_text(encoding="utf-8")
        for needle in ("the-extras-academy", "episode-105", "title_no=", "episode_no=",
                       "REAL COFFEE", "shadow_slave", "http://", "https://", "Kayden",
                       "C:\\Users", "8081"):
            self.assertNotIn(needle, text, needle)

    def test_the_module_branches_on_no_identity(self):
        text = MODULE.read_text(encoding="utf-8")
        for pattern in (r"job_id\s*===", r"owner\s*===", r"page_id\s*===",
                        r"title\s*===\s*['\"]", r"url\s*===\s*['\"]"):
            self.assertIsNone(re.search(pattern, text), pattern)

    def test_no_percentage_literal_is_used_as_a_fallback(self):
        text = MODULE.read_text(encoding="utf-8")
        # A literal percent assigned as progress would be a fabricated position.
        self.assertIsNone(re.search(r"percent\s*[:=]\s*\d", text))


if __name__ == "__main__":
    unittest.main()
