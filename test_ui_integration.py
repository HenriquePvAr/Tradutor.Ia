from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from ui_helpers import build_run_command, suggest_chapter_details
from ui_bridge import UiBridge, _profile_default


ROOT = Path(__file__).resolve().parent
URL = (
    "https://www.webtoons.com/en/action/jungle-juice/episode-1/viewer"
    "?title_no=2480&episode_no=1"
)


class UiIntegrationTests(unittest.TestCase):
    def test_reference_shell_is_split_from_python(self):
        app_source = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        self.assertTrue((ROOT / "ui" / "ui_shell.html").is_file())
        self.assertTrue((ROOT / "static" / "tradutor_ui.css").is_file())
        self.assertTrue((ROOT / "static" / "tradutor_ui.js").is_file())
        self.assertNotIn("<section class=", app_source)

    def test_fast_force_command(self):
        command = build_run_command(
            url=URL,
            mode="fast",
            output=suggest_chapter_details(URL)["slug"],
            full=False,
            max_images=3,
            use_cache=False,
            force=True,
            use_context=True,
            python_executable="python.exe",
        )
        self.assertEqual(command[command.index("--mode") + 1], "fast")
        self.assertIn("--force", command)
        self.assertEqual(command[-2:], ["--max-images", "3"])

    def test_quality_cache_command(self):
        command = build_run_command(
            url=URL,
            mode="quality",
            output="chapter",
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
        self.assertNotIn("--max-images", command)

    def test_frontend_contains_no_pipeline_or_queue_simulation(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        forbidden = (
            "simulateQueueItem",
            "runStage(",
            "historyData =",
            "window.storage",
            "Math.random()*22",
        )
        for marker in forbidden:
            self.assertNotIn(marker, source)

    def test_frontend_labels_review_required_status(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("review_required", source)
        self.assertIn("revisão necessária", source)

    def test_frontend_has_a_sanitized_source_review_confirmation_path(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        app_source = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        self.assertIn("awaiting_source_review", source)
        self.assertIn("/api/ui/source/confirm", source)
        self.assertIn("data-source-candidate-id", source)
        self.assertIn('id="sourceReviewPanel"', shell)
        self.assertIn("api_source_confirm", app_source)
        self.assertNotIn("item.url", source[source.index("function renderSourceReview"):source.index("function renderResult")])

    def test_frontend_never_uses_presentation_record_id_as_source_job(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertNotIn("record.job_id || record.id", source)
        self.assertIn("if (trustedJobId) payload.source_job_id = trustedJobId", source)

    def test_shell_contains_no_seed_chapter(self):
        source = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        for marker in ("Lookism", "Jungle Juice", "Plus One"):
            self.assertNotIn(marker, source)

    def test_secret_values_are_not_embedded(self):
        sources = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "app_ui.py",
                "ui_bridge.py",
                "static/tradutor_ui.js",
                "ui/ui_shell.html",
            )
        )
        self.assertNotRegex(sources, r"nvapi-[A-Za-z0-9_-]{12,}")
        self.assertNotRegex(sources, r"sk-[A-Za-z0-9_-]{12,}")

    def test_profile_media_is_stored_as_local_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bridge = UiBridge.__new__(UiBridge)
            bridge.profile = _profile_default()
            with (
                patch("ui_bridge.PROFILE_PATH", root / "ui_profile.json"),
                patch("ui_bridge.PROFILE_MEDIA_DIR", root / "ui_profile"),
            ):
                payload = bridge.save_profile_media(
                    "avatar",
                    filename="avatar.png",
                    content_type="image/png",
                    content=b"local-test-image",
                )
                self.assertTrue((root / "ui_profile" / "avatar.png").is_file())
                self.assertEqual(payload["avatar_mode"], "image")
                self.assertTrue(payload["avatar_media_url"].startswith("/api/ui/profile/media/avatar"))
                self.assertNotIn("local-test-image", str(payload))

    def test_standard_emoji_are_not_embedded_in_ui(self):
        sources = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in ("ui/ui_shell.html", "static/tradutor_ui.js", "static/tradutor_ui.css")
        )
        self.assertFalse(any(ord(character) >= 0x1F000 for character in sources))


if __name__ == "__main__":
    unittest.main()
