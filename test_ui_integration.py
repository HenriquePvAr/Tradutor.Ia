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

    def test_history_publication_is_explicit_and_metadata_driven(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        for marker in (
            "publicationEligibility",
            "publicationModalOverlay",
            "publicationConfirm",
            "publicationBusy",
            "Atualizar publicação",
            "manifest_verified",
            "manual_review_count",
        ):
            self.assertIn(marker, source + shell, marker)
        self.assertNotIn("window.prompt", source)

    def test_publication_click_has_visible_validation_trace_and_finally(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        for marker in (
            "publish_click_received",
            "publication_request_started",
            "publication_response_received",
            "publication_completed",
            "publication_failed",
            "loading_cleared",
            "validatePublicationForm",
            "publicationError",
            "X-Tradutor-Correlation-ID",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("if (!$('#publicationConfirm')?.checked) return;", source)

    def test_publication_waits_for_owner_and_exposes_claim_state(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("artifact_has_no_owner", source)
        self.assertIn("community_ownership", source)
        self.assertIn("claim_completed", source)
        self.assertIn("ownerReady", source)

    def test_community_feed_includes_unreviewed_published_posts(self):
        source = (ROOT / "community_service.py").read_text(encoding="utf-8")
        store = (ROOT / "community_store.py").read_text(encoding="utf-8")
        authorization = (ROOT / "community_authorization.py").read_text(encoding="utf-8")
        self.assertIn("require_moderation=False", source)
        self.assertIn("moderation_status IN (?, ?)", store)
        self.assertIn("Moderation.PENDING", authorization)

    def test_community_pdf_uses_authenticated_blob_flow(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        for marker in (
            "openAuthenticatedCommunityPdf",
            "rawResponse",
            "response.blob()",
            "URL.createObjectURL",
            "URL.revokeObjectURL",
            "window.open('', '_blank', 'noopener')",
            "Abrindo...",
            "authorization_header_present",
        ):
            self.assertIn(marker, source, marker)
        self.assertNotIn('target="_blank"', source[source.index("function renderCommunityCard"):source.index("function loadRecordIntoForm")])

    def test_publication_payload_has_no_local_or_secret_fields(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        publish = source[source.index("async function publishToCommunity"):source.index("async function loadCommunityFeed")]
        for forbidden in ("output_folder", "pdf_path", "NVIDIA_API_KEY", "cookie", "local_path"):
            self.assertNotIn(forbidden, publish, forbidden)
        self.assertIn("source_job_id", publish)

    def test_frontend_has_a_sanitized_source_review_confirmation_path(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        app_source = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        self.assertIn("awaiting_source_review", source)
        self.assertIn("/api/ui/source/confirm", source)
        self.assertIn("data-source-candidate-id", source)
        self.assertIn("data-source-move", source)
        self.assertIn("refreshSourceReviewOrder", source)
        self.assertIn("shouldRenderSourceReview", source)
        self.assertIn("safeReviewThumbnail", source)
        self.assertIn("item.thumbnail", source)
        self.assertIn("source_provenance", source)
        self.assertIn("safeReason", source)
        self.assertIn("transporte:", source)
        self.assertIn("appState.sourceReview?.job_id", source)
        self.assertIn("sourceProfileToggle", source)
        self.assertIn('id="sourceProfileToggle"', shell)
        self.assertIn('id="sourceReviewPanel"', shell)
        self.assertIn("api_source_confirm", app_source)
        self.assertNotIn("item.url", source[source.index("function renderSourceReview"):source.index("function renderResult")])

    def test_frontend_submits_local_folder_only_as_an_explicit_source_type(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        bridge = (ROOT / "ui_bridge.py").read_text(encoding="utf-8")
        app_source = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        self.assertIn('data-source-type="local_folder"', shell)
        self.assertIn('id="localFolderInput"', shell)
        self.assertIn("source_type: appState.selectedSourceType", source)
        self.assertIn("payload.local_folder", source)
        self.assertIn("$('#localFolderInput').value = '';", source)
        self.assertIn("local_folder_requires_loopback_ui", bridge)
        self.assertIn("local_folder_ui_allowed", app_source)
        # The browser must never receive a snapshot workspace path as a form field.
        self.assertNotIn("snapshot_ref", source)

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
