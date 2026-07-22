from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from output_manifest import build_run_manifest, load_verified_run_manifest, sanitize_source_url
from chapter_source import SourceError
from ui_helpers import (
    ProgressSnapshot,
    build_run_command,
    derive_final_run_status,
    find_output_artifacts,
    infer_series_details,
    mask_secrets,
    parse_progress_line,
    quality_requires_review,
    sanitize_output_name,
    sanitize_diagnostic_text,
    suggest_chapter_details,
)
from ui_history import UIHistoryStore


LOOKISM_URL = (
    "https://m.webtoons.com/en/drama/lookism/ep-50/viewer"
    "?title_no=1049&episode_no=50"
)
JUNGLE_URL = (
    "https://www.webtoons.com/en/action/jungle-juice/episode-1/viewer"
    "?title_no=2480&episode_no=1"
)


class UIHelpersTests(unittest.TestCase):
    def test_url_suggestion_is_human_and_safe(self):
        details = suggest_chapter_details(LOOKISM_URL)
        self.assertEqual(details["title"], "Lookism - EP 50")
        self.assertEqual(details["slug"], "lookism_ep_50")

    def test_jungle_juice_url_suggestion(self):
        details = suggest_chapter_details(JUNGLE_URL)
        self.assertEqual(details["title"], "Jungle Juice - Episode 1")
        self.assertEqual(details["slug"], "jungle_juice_episode_1")

    def test_command_is_a_shell_free_argument_list(self):
        command = build_run_command(
            url=LOOKISM_URL,
            mode="fast",
            output="Lookism EP 50",
            full=False,
            max_images=3,
            use_cache=False,
            force=True,
            use_context=True,
            python_executable="python.exe",
        )
        self.assertEqual(command[0], "python.exe")
        self.assertIn("--force", command)
        self.assertEqual(command[-2:], ["--max-images", "3"])
        self.assertIn("lookism_ep_50", command)

    def test_cache_and_force_cannot_be_combined(self):
        with self.assertRaises(ValueError):
            build_run_command(
                url=LOOKISM_URL,
                mode="fast",
                output="lookism",
                full=True,
                max_images=None,
                use_cache=True,
                force=True,
                use_context=True,
            )

    def test_specific_host_series_index_cannot_be_queued_as_a_chapter(self):
        with self.assertRaisesRegex(SourceError, "not_a_chapter_url"):
            build_run_command(
                url="https://www.webtoons.com/en/action/jungle-juice/list",
                mode="fast",
                output="not-a-chapter",
                full=True,
                max_images=None,
                use_cache=False,
                force=True,
                use_context=True,
            )

    def test_secret_masking(self):
        text = "NVIDIA_API_KEY=valor_de_teste_nao_secreto"
        masked = mask_secrets(text)
        self.assertNotIn("valor_de_teste_nao_secreto", masked)
        self.assertIn("MASCARADO", masked)

    def test_diagnostic_url_redaction_removes_query_and_credentials(self):
        text = "erro em https://user:pass@example.test/chapter?token=secret-value"
        safe = sanitize_diagnostic_text(text)
        self.assertNotIn("secret-value", safe)
        self.assertNotIn("user:pass", safe)
        self.assertNotIn("/chapter", safe)
        self.assertRegex(safe, r"https://example\.test/path-[0-9a-f]{12}")
        sanitized = sanitize_source_url(
            "https://user:pass@example.test/chapter?token=secret-value")
        self.assertNotIn("user:pass", sanitized)
        self.assertNotIn("secret-value", sanitized)
        self.assertNotIn("/chapter", sanitized)
        self.assertRegex(sanitized, r"^https://example\.test/path-[0-9a-f]{12}$")

    def test_progress_parser_extracts_stage_and_fraction(self):
        snapshot = parse_progress_line(
            "Baixando imagens: 81/713",
            ProgressSnapshot(),
        )
        self.assertEqual(snapshot.stage, "Baixando imagens")
        self.assertEqual((snapshot.current, snapshot.total), (81, 713))
        self.assertGreater(snapshot.percent, 0)

    def test_progress_parser_does_not_treat_engine_name_as_ocr_stage(self):
        snapshot = parse_progress_line("Modo: fast (rapidocr)", ProgressSnapshot())
        self.assertEqual(snapshot.stage, "Preparando")

    def test_progress_parser_keeps_ocr_for_nvidia_provider_setup_line(self):
        snapshot = ProgressSnapshot(stage="OCR", percent=0.32)
        parse_progress_line("Usando NVIDIA API para tradução", snapshot)
        self.assertEqual(snapshot.stage, "OCR")

    def test_progress_parser_enters_nvidia_only_on_explicit_phase_marker(self):
        snapshot = ProgressSnapshot(stage="OCR", percent=0.32)
        parse_progress_line("Tradução NVIDIA: iniciando", snapshot)
        self.assertEqual(snapshot.stage, "Tradução NVIDIA")

    def test_progress_does_not_regress_to_download(self):
        snapshot = ProgressSnapshot(stage="Tradução NVIDIA", percent=0.64)
        parse_progress_line("Baixando um modelo auxiliar", snapshot)
        self.assertEqual(snapshot.stage, "Tradução NVIDIA")
        self.assertEqual(snapshot.percent, 0.64)

    def test_quality_mode_is_forwarded(self):
        command = build_run_command(
            url=LOOKISM_URL,
            mode="quality",
            output="quality run",
            full=True,
            max_images=None,
            use_cache=True,
            force=False,
            use_context=False,
            python_executable="python.exe",
        )
        self.assertEqual(command[command.index("--mode") + 1], "quality")
        self.assertIn("--cache", command)
        self.assertIn("--no-context", command)

    def test_history_drops_unknown_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            store = UIHistoryStore(Path(folder) / "history.json")
            store.upsert({"id": "one", "status": "finished", "secret": "never"})
            record = store.load()[0]
            self.assertNotIn("secret", record)
            self.assertEqual(record["id"], "one")

    def test_final_status_distinguishes_quality_review_from_technical_success(self):
        self.assertEqual(
            derive_final_run_status(
                technical_success=True,
                quality_validation={"passed": True, "manual_review_required_groups": 0},
            ),
            "finished",
        )
        self.assertEqual(
            derive_final_run_status(
                technical_success=True,
                quality_validation={"passed": False, "manual_review_required_groups": 1},
            ),
            "review_required",
        )
        self.assertEqual(
            derive_final_run_status(
                technical_success=True,
                quality_validation={"passed": False, "manual_review_required_groups": 0},
            ),
            "review_required",
        )
        self.assertEqual(
            derive_final_run_status(technical_success=False, quality_validation={"passed": True}),
            "error",
        )
        self.assertEqual(
            derive_final_run_status(
                technical_success=True,
                cancelled=True,
                quality_validation={"passed": False, "manual_review_required_groups": 1},
            ),
            "cancelled",
        )
        self.assertEqual(
            derive_final_run_status(
                technical_success=True,
                quality_validation={"passed": None},
            ),
            "finished",
        )
        self.assertEqual(
            derive_final_run_status(technical_success=True, quality_validation={}),
            "finished",
        )

    def test_quality_review_bool_coercion_is_conservative(self):
        clean_values = [True, 1, "true", "True", "1", "yes"]
        for value in clean_values:
            with self.subTest(value=value):
                self.assertFalse(quality_requires_review({"passed": value}))

        failed_values = [False, 0, "false", "False", "0", "no"]
        for value in failed_values:
            with self.subTest(value=value):
                self.assertTrue(quality_requires_review({"passed": value}))

        unknown_values = [None, "", "maybe", [], {}]
        for value in unknown_values:
            with self.subTest(value=value):
                self.assertFalse(quality_requires_review({"passed": value}))

        self.assertTrue(quality_requires_review({"passed": True}, manual_review_count=1))
        self.assertTrue(quality_requires_review({"passed": True}, manual_review_count="1"))

    def test_discovered_history_marks_gate_failure_as_review_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output_root = root / "output"
            chapter = output_root / "chapter"
            chapter.mkdir(parents=True)
            pdf_path = chapter / "chapter.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            timing = {
                "url": LOOKISM_URL,
                "ocr_engine": "rapidocr",
                "force": False,
                "total_seconds": 3.5,
                "processed_images": 2,
                "groups_translated": 1,
                "pages_with_error": 0,
                "pdf_path": str(pdf_path),
                "quality_validation": {
                    "passed": False,
                    "manual_review_required_groups": 1,
                },
            }
            (chapter / "timing_report.json").write_text(
                json.dumps(timing),
                encoding="utf-8",
            )
            store = UIHistoryStore(root / "history.json")
            with patch("ui_history.OUTPUT_ROOT", output_root):
                records = store.discover_outputs()

            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["status"], "review_required")
            self.assertFalse(record["quality_gate"])
            self.assertEqual(record["pdf_path"], str(pdf_path.resolve()))

    def test_manifest_only_output_is_discovered_from_its_canonical_run_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output_root = root / "output"
            chapter = output_root / "verified_only"
            chapter.mkdir(parents=True)
            pdf_path = chapter / "chapter.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            manifest = build_run_manifest(
                run_id="verified-only",
                created_at="2026-01-01T00:00:00+00:00",
                source_url=LOOKISM_URL,
                commit_hash="abc123",
                branch="feature",
                pipeline_version="pipeline-v1",
                model="fake",
                final_status="review_required",
                quality_passed=False,
                manual_review_count=1,
                rejected_count=0,
                pdf_path=str(pdf_path),
                slug="verified_only",
            )
            (chapter / "run_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            store = UIHistoryStore(root / "history.json")
            with patch("ui_history.OUTPUT_ROOT", output_root):
                records = store.discover_outputs()

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["output_verification"], "manifest_verified")
            self.assertEqual(records[0]["slug"], "verified_only")
            self.assertEqual(records[0]["status"], "review_required")
            self.assertFalse(records[0]["quality_gate"])
            self.assertEqual(records[0]["pdf_path"], str(pdf_path.resolve()))

    def test_verified_manifest_is_authoritative_over_a_stale_timing_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output_root = root / "output"
            chapter = output_root / "canonical_status"
            chapter.mkdir(parents=True)
            pdf_path = chapter / "chapter.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            (chapter / "timing_report.json").write_text(
                json.dumps({
                    "ocr_engine": "rapidocr",
                    "pdf_path": str(pdf_path),
                    "quality_validation": {"passed": True},
                }), encoding="utf-8"
            )
            manifest = build_run_manifest(
                run_id="canonical-status",
                created_at="2026-01-01T00:00:00+00:00",
                source_url=LOOKISM_URL,
                commit_hash="abc123",
                branch="feature",
                pipeline_version="pipeline-v1",
                model="fake",
                final_status="review_required",
                quality_passed=False,
                manual_review_count=1,
                rejected_count=0,
                pdf_path=str(pdf_path),
                slug="canonical_status",
            )
            (chapter / "run_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            store = UIHistoryStore(root / "history.json")
            with patch("ui_history.OUTPUT_ROOT", output_root):
                record = store.discover_outputs()[0]

            self.assertEqual(record["status"], "review_required")
            self.assertFalse(record["quality_gate"])

    def test_output_artifacts_refuse_a_manifest_path_outside_the_run_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "run"
            run.mkdir()
            outside = root / "outside.pdf"
            outside.write_bytes(b"%PDF-1.4\n")
            (run / "run_manifest.json").write_text(
                json.dumps({"pdf_path": str(outside)}), encoding="utf-8"
            )
            self.assertFalse(find_output_artifacts(run)["pdf_path"])

    def test_discovered_history_maps_only_valid_runner_job_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output_root = root / "output"
            chapter = output_root / "chapter"
            chapter.mkdir(parents=True)
            pdf_path = chapter / "chapter.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            (chapter / "timing_report.json").write_text(
                json.dumps({
                    "ocr_engine": "rapidocr",
                    "pdf_path": str(pdf_path),
                    "quality_validation": {"passed": True},
                }),
                encoding="utf-8",
            )
            job_id = "a" * 32
            (chapter / "job_manifest.json").write_text(
                json.dumps({"job_id": job_id}),
                encoding="utf-8",
            )
            store = UIHistoryStore(root / "history.json")
            with patch("ui_history.OUTPUT_ROOT", output_root):
                record = store.discover_outputs()[0]
            self.assertEqual(record["id"], "discovered-chapter")
            self.assertEqual(record["job_id"], job_id)

            (chapter / "job_manifest.json").write_text(
                json.dumps({"job_id": "discovered-chapter"}),
                encoding="utf-8",
            )
            with patch("ui_history.OUTPUT_ROOT", output_root):
                invalid = store.discover_outputs()[0]
            self.assertFalse(invalid.get("job_id"))

    def test_discovery_prioritizes_verified_manifest_and_separates_legacy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output_root = root / "output"
            verified = output_root / "verified_run"
            legacy = output_root / "legacy_run"
            verified.mkdir(parents=True)
            legacy.mkdir(parents=True)

            def write_output(directory, *, manifest=False):
                pdf_path = directory / "chapter.pdf"
                pdf_path.write_bytes(b"%PDF-1.4\n")
                (directory / "timing_report.json").write_text(
                    json.dumps(
                        {
                            "ocr_engine": "rapidocr",
                            "processed_images": 2,
                            "pdf_path": str(pdf_path),
                            "quality_validation": {"passed": True},
                        }
                    ),
                    encoding="utf-8",
                )
                if manifest:
                    (directory / "run_manifest.json").write_text(
                        json.dumps(
                            {
                                "manifest_version": 1,
                                "run_id": "run-safe-id",
                                "created_at": "2026-01-01T00:00:00+00:00",
                                "source_url": "https://example.test/chapter",
                                "commit_hash": "abc123",
                                "branch": "feature",
                                "pipeline_version": "pipeline-v1",
                                "model": "model-id",
                                "final_status": "finished",
                                "quality_passed": True,
                                "manual_review_count": 0,
                                "rejected_count": 0,
                                "pdf_path": str(pdf_path),
                            }
                        ),
                        encoding="utf-8",
                    )

            write_output(verified, manifest=True)
            write_output(legacy)
            os.utime(legacy / "timing_report.json", (2_000_000_000, 2_000_000_000))

            store = UIHistoryStore(root / "history.json")
            with patch("ui_history.OUTPUT_ROOT", output_root):
                records = store.discover_outputs()

            self.assertEqual(records[0]["slug"], "verified_run")
            self.assertEqual(records[0]["output_verification"], "manifest_verified")
            self.assertEqual(records[1]["slug"], "legacy_run")
            self.assertEqual(records[1]["status"], "finished")
            self.assertEqual(records[1]["output_verification"], "legacy_unverified")

    def test_discovery_recognizes_generic_e2e_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output_root = root / "output"
            chapter = output_root / "e2e_run"
            chapter.mkdir(parents=True)
            pdf_path = chapter / "chapter.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            (chapter / "timing_report.json").write_text(
                json.dumps(
                    {
                        "ocr_engine": "rapidocr",
                        "pdf_path": str(pdf_path),
                        "quality_validation": {"passed": True},
                    }
                ),
                encoding="utf-8",
            )
            runtime = root / ".cache" / "e2e_runtime" / chapter.name
            runtime.mkdir(parents=True)
            (runtime / "exit_code.txt").write_text("0\n", encoding="utf-8")
            (runtime / "end_time.txt").write_text("2026-01-01T00:00:00+00:00\n", encoding="utf-8")

            store = UIHistoryStore(root / "history.json")
            with patch("ui_history.OUTPUT_ROOT", output_root), patch("ui_history.REPO_ROOT", root):
                records = store.discover_outputs()

            self.assertEqual(records[0]["output_verification"], "e2e_evidence")
            self.assertEqual(records[0]["status"], "finished")

    def test_manifest_schema_sanitizes_source_url(self):
        with tempfile.TemporaryDirectory() as folder:
            output_folder = Path(folder)
            manifest = build_run_manifest(
                run_id="safe-run-id",
                created_at="2026-01-01T00:00:00+00:00",
                source_url="https://example.test/series/chapter?token=secret",
                commit_hash="abc123",
                branch="feature",
                pipeline_version="pipeline-v1",
                model="model-id",
                final_status="finished",
                quality_passed=True,
                manual_review_count=0,
                rejected_count=0,
                pdf_path=str(output_folder / "chapter.pdf"),
            )
            (output_folder / "run_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            loaded = load_verified_run_manifest(output_folder)

            self.assertNotIn("/series/chapter", loaded["source_url"])
            self.assertRegex(loaded["source_url"], r"^https://example\.test/path-[0-9a-f]{12}$")
            self.assertTrue(loaded["quality_passed"])

    def test_saved_history_with_failed_quality_gate_is_not_clean_success(self):
        with tempfile.TemporaryDirectory() as folder:
            store = UIHistoryStore(Path(folder) / "history.json")
            store.upsert(
                {
                    "id": "needs-review",
                    "status": "finished",
                    "quality_gate": False,
                    "pdf_path": str(Path(folder) / "chapter.pdf"),
                }
            )
            record = store.load()[0]
            self.assertEqual(record["status"], "review_required")

    def test_saved_history_with_string_false_gate_is_not_clean_success(self):
        with tempfile.TemporaryDirectory() as folder:
            store = UIHistoryStore(Path(folder) / "history.json")
            store.upsert(
                {
                    "id": "needs-review",
                    "status": "finished",
                    "quality_gate": "false",
                    "pdf_path": str(Path(folder) / "chapter.pdf"),
                }
            )
            record = store.load()[0]
            self.assertEqual(record["status"], "review_required")

    def test_output_name_is_sanitized(self):
        self.assertEqual(sanitize_output_name("  Olá / Capítulo 01  "), "ola_capitulo_01")

    def test_series_grouping_prefers_url_over_technical_output_name(self):
        details = infer_series_details(
            url=JUNGLE_URL,
            chapter_name="Jungle Juice Quality Page4 Recovery",
            output_slug="jungle_juice_quality_page4_recovery",
        )
        self.assertEqual(details["name"], "Jungle Juice")
        self.assertEqual(details["slug"], "jungle_juice")


if __name__ == "__main__":
    unittest.main()
