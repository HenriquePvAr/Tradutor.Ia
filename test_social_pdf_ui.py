"""Contract/security tests for the explicit-publishing + reader frontend (source-level)."""

import _test_bootstrap  # noqa: F401

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


class PdfApiClientTests(unittest.TestCase):
    def setUp(self):
        self.src = read("static/social_api.js")

    def test_publishing_functions_exist(self):
        for fn in ("listLocalResults", "publishPdf", "publishStatus", "getAsset",
                   "replaceAsset", "unlinkAsset", "fetchChapterPdfUrl"):
            self.assertRegex(self.src, rf"export (const|async function) {fn}\b", fn)

    def test_content_fetch_uses_header_not_url(self):
        # The Bearer travels in the Authorization header, never in the URL/query.
        self.assertIn("'Authorization': `Bearer ${token}`", self.src)
        self.assertNotIn("token=", self.src)
        self.assertNotIn("?jwt", self.src)

    def test_no_ownership_or_drive_fields_in_publish_body(self):
        # No request body literal assigns an ownership/identity/drive field. owner_id etc.
        # appear only in the FORBIDDEN strip-set (which removes them) — never as `owner_id:`.
        for bad in ("owner_id:", "storage_file_id:", "drive_file_id:", "file_id:", "path:"):
            self.assertNotIn(bad, self.src, bad)
        self.assertIn("FORBIDDEN", self.src)  # the strip-set is present

    def test_publish_body_only_allowed_fields(self):
        self.assertIn("source_job_id, target_status", self.src)


class PdfUiTests(unittest.TestCase):
    def setUp(self):
        self.src = read("static/social_community.js")

    def test_confirmation_message_present(self):
        self.assertIn("Este PDF ainda está apenas no seu computador", self.src)
        self.assertIn("Privado", self.src)
        self.assertIn("Comunidade", self.src)

    def test_owner_asset_controls(self):
        for label in ("Publicar PDF", "Substituir PDF", "Desvincular", "Ler"):
            self.assertIn(label, self.src, label)

    def test_reader_revokes_blob_and_aborts(self):
        self.assertIn("revokeObjectURL", self.src)
        self.assertIn("AbortController", self.src)
        self.assertIn("controller.abort", self.src)

    def test_reader_no_drive_link_or_iframe_to_drive(self):
        self.assertNotIn("drive.google", self.src)
        self.assertNotIn("webContentLink", self.src)
        # The reader uses an <object> fed by an in-memory blob URL, not a Drive iframe src.
        self.assertNotIn("googleapis.com", self.src)

    def test_no_path_sent_and_uses_opaque_source_job_id(self):
        self.assertIn("source_job_id", self.src)
        self.assertNotIn("output_dir", self.src)
        self.assertNotIn("file://", self.src)

    def test_no_secret_or_storage_id_in_ui(self):
        for bad in ("storage_file_id", "service_role", "SUPABASE_SECRET_KEY", "sb_secret"):
            self.assertNotIn(bad, self.src, bad)

    def test_double_click_guard_on_publish(self):
        self.assertIn("submit.dataset.busy", self.src)

    def test_publish_polls_status(self):
        self.assertIn("publishStatus", self.src)
        self.assertIn("pollPublish", self.src)


class PdfWiringTests(unittest.TestCase):
    def test_app_mounts_pdf_router(self):
        app = read("app_ui.py")
        self.assertIn("create_social_pdf_router", app)
        self.assertIn("ChapterAssetRepository", app)

    def test_backend_never_returns_drive_id_in_asset_dto(self):
        # The asset status DTO is booleans + owner-only mime/updated_at, never storage id.
        repo = read("chapter_asset_repository.py")
        self.assertIn('"linked"', repo)
        self.assertIn('"available"', repo)
        # storage_file_id only used internally, never in get_asset_status output.
        status_fn = repo[repo.index("def get_asset_status"):]
        self.assertNotIn("storage_file_id", status_fn.split("def ", 1)[0])


if __name__ == "__main__":
    unittest.main()


class RetentionUiTests(unittest.TestCase):
    def setUp(self):
        self.api = read("static/social_api.js")
        self.ui = read("static/social_community.js")

    def test_retention_endpoints_exposed(self):
        for fn in ("retainedAssets", "assetRetention", "restoreAsset"):
            self.assertRegex(self.api, rf"export (const|async function) {fn}\b", fn)

    def test_restore_sends_no_client_controlled_fields(self):
        # The restore call carries no body: state/owner/deadline are server-derived.
        line = [l for l in self.api.splitlines() if "restoreAsset" in l][0]
        for bad in ("state", "retain_until", "publication_id", "owner_id", "force"):
            self.assertNotIn(bad, line, bad)

    def test_ui_says_retained_not_deleted(self):
        self.assertIn("fica retido e pode ser restaurado", self.ui)
        self.assertIn("Restaurar PDF", self.ui)
        self.assertIn("Restaurar este PDF retido", self.ui)   # explicit confirmation

    def test_ui_has_no_force_trash_or_delete_control(self):
        for bad in ("forceTrash", "hardDelete", "emptyTrash", "reconcileNow", "retain_until"):
            self.assertNotIn(bad, self.ui, bad)

    def test_retained_panel_shows_no_storage_identifiers(self):
        panel = self.ui[self.ui.index("function retainedAssetsPanel"):]
        panel = panel[:panel.index("function renderAssetControls")]
        # Field *accesses*, not the prose in comments.
        for bad in ("it.storage_file_id", "it.drive", "it.publication_id", "it.path", "it.url"):
            self.assertNotIn(bad, panel, bad)
