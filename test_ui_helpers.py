import tempfile
import unittest
from pathlib import Path

from ui_helpers import (
    ProgressSnapshot,
    build_run_command,
    mask_secrets,
    parse_progress_line,
    sanitize_output_name,
    suggest_chapter_details,
)
from ui_history import UIHistoryStore


LOOKISM_URL = (
    "https://m.webtoons.com/en/drama/lookism/ep-50/viewer"
    "?title_no=1049&episode_no=50"
)


class UIHelpersTests(unittest.TestCase):
    def test_url_suggestion_is_human_and_safe(self):
        details = suggest_chapter_details(LOOKISM_URL)
        self.assertEqual(details["title"], "Lookism - EP 50")
        self.assertEqual(details["slug"], "lookism_ep_50")

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

    def test_secret_masking(self):
        text = "NVIDIA_API_KEY=nvapi-this-should-never-appear"
        masked = mask_secrets(text)
        self.assertNotIn("this-should-never-appear", masked)
        self.assertIn("MASCARADO", masked)

    def test_progress_parser_extracts_stage_and_fraction(self):
        snapshot = parse_progress_line(
            "Baixando imagens: 81/713",
            ProgressSnapshot(),
        )
        self.assertEqual(snapshot.stage, "Baixando imagens")
        self.assertEqual((snapshot.current, snapshot.total), (81, 713))
        self.assertGreater(snapshot.percent, 0)

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

    def test_output_name_is_sanitized(self):
        self.assertEqual(sanitize_output_name("  Olá / Capítulo 01  "), "ol_cap_tulo_01")


if __name__ == "__main__":
    unittest.main()
