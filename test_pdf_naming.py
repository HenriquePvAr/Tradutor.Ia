from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import tempfile
import unittest
from pathlib import Path

from output_manifest import (
    MANIFEST_FILENAME,
    build_run_manifest,
    load_verified_run_manifest,
)
from pdf_naming import (
    build_pdf_filename,
    episode_number_from_url,
    sanitize_filename_component,
    series_slug_from_url,
)
from ui_helpers import find_output_artifacts


_HORROR_URL = (
    "https://www.webtoons.com/en/horror/platfrom-zero/episode-2/viewer"
    "?title_no=10488&episode_no=2"
)
_ROMANCE_URL = (
    "https://www.webtoons.com/en/romance/the-villainess-just-wants-to-live-in-peace"
    "/s2-episode-85/viewer?title_no=6879&episode_no=85"
)


class SanitizeComponentTests(unittest.TestCase):
    def test_lowercases_and_underscores_spaces(self):
        self.assertEqual(
            sanitize_filename_component("The Villainess Just Wants"),
            "the_villainess_just_wants",
        )

    def test_normalizes_hyphens_and_collapses_underscores(self):
        self.assertEqual(sanitize_filename_component("platfrom--zero"), "platfrom_zero")
        self.assertEqual(sanitize_filename_component("a __ b"), "a_b")

    def test_strips_accents(self):
        self.assertEqual(sanitize_filename_component("Coração Ardente"), "coracao_ardente")

    def test_removes_windows_invalid_characters(self):
        self.assertEqual(
            sanitize_filename_component('a<b>c:d"e/f\\g|h?i*j'),
            "abcdefghij",
        )

    def test_blocks_path_traversal_and_dots(self):
        self.assertEqual(sanitize_filename_component("../../etc/passwd"), "etcpasswd")
        self.assertEqual(sanitize_filename_component(".."), "")

    def test_trims_edge_underscores(self):
        self.assertEqual(sanitize_filename_component("  -hello-  "), "hello")

    def test_empty_input_returns_empty(self):
        self.assertEqual(sanitize_filename_component("   "), "")


class BuildPdfFilenameTests(unittest.TestCase):
    def test_series_and_episode_from_url(self):
        self.assertEqual(
            build_pdf_filename(source_url=_HORROR_URL),
            "platfrom_zero_capitulo_2.pdf",
        )

    def test_long_series_slug_from_url(self):
        self.assertEqual(
            build_pdf_filename(source_url=_ROMANCE_URL),
            "the_villainess_just_wants_to_live_in_peace_capitulo_85.pdf",
        )

    def test_explicit_series_title_wins_over_slug(self):
        self.assertEqual(
            build_pdf_filename(
                source_url=_HORROR_URL,
                series_title="Platform Zero",
            ),
            "platform_zero_capitulo_2.pdf",
        )

    def test_explicit_episode_number_wins_over_query(self):
        self.assertEqual(
            build_pdf_filename(source_url=_HORROR_URL, episode_number=7),
            "platfrom_zero_capitulo_7.pdf",
        )

    def test_accented_title_is_sanitized(self):
        self.assertEqual(
            build_pdf_filename(series_title="Coração Ardente", episode_number=4),
            "coracao_ardente_capitulo_4.pdf",
        )

    def test_missing_series_falls_back_to_generic_name(self):
        self.assertEqual(build_pdf_filename(episode_number=3), "obra_capitulo_3.pdf")

    def test_missing_episode_falls_back_to_run_id(self):
        name = build_pdf_filename(series_title="Some Series", fallback_id="a4309411f85c")
        self.assertTrue(name.startswith("some_series_capitulo_"))
        self.assertTrue(name.endswith(".pdf"))
        self.assertNotIn("capitulo_.pdf", name)

    def test_no_series_and_no_episode_still_produces_a_safe_name(self):
        name = build_pdf_filename()
        self.assertTrue(name.endswith(".pdf"))
        self.assertGreater(len(name), len(".pdf"))
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)

    def test_title_is_length_limited(self):
        name = build_pdf_filename(series_title="x" * 400, episode_number=1)
        self.assertLessEqual(len(name), 120)
        self.assertTrue(name.endswith("_capitulo_1.pdf"))

    def test_reserved_windows_name_is_escaped(self):
        name = build_pdf_filename(series_title="CON", episode_number=1)
        self.assertNotEqual(name.split("_capitulo_")[0].lower(), "con")
        self.assertTrue(name.endswith(".pdf"))

    def test_path_traversal_in_title_cannot_escape(self):
        name = build_pdf_filename(series_title="../../evil", episode_number=1)
        self.assertNotIn("..", name)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)

    def test_extension_appears_once(self):
        name = build_pdf_filename(series_title="Some.pdf", episode_number=1)
        self.assertEqual(name.count(".pdf"), 1)

    def test_two_series_do_not_collide(self):
        self.assertNotEqual(
            build_pdf_filename(source_url=_HORROR_URL),
            build_pdf_filename(source_url=_ROMANCE_URL),
        )

    def test_same_chapter_is_deterministic(self):
        self.assertEqual(
            build_pdf_filename(source_url=_HORROR_URL),
            build_pdf_filename(source_url=_HORROR_URL),
        )

    def test_episode_segment_is_not_used_as_the_series_name(self):
        # "s2-episode-85" is the chapter segment, never the work's name.
        name = build_pdf_filename(source_url=_ROMANCE_URL)
        self.assertFalse(name.startswith("s2_episode"))

    def test_never_returns_the_legacy_generic_name(self):
        self.assertNotEqual(
            build_pdf_filename(source_url=_HORROR_URL),
            "capitulo_completo_traduzido.pdf",
        )


class ManifestAndDiscoveryTests(unittest.TestCase):
    def _manifest(self, pdf_path):
        return build_run_manifest(
            run_id="r1",
            created_at="2026-01-01T00:00:00+00:00",
            source_url=_ROMANCE_URL,
            commit_hash="abc123",
            branch="main",
            pipeline_version="v1",
            model="m",
            final_status="review_required",
            quality_passed=False,
            manual_review_count=0,
            rejected_count=0,
            pdf_path=pdf_path,
            series_slug=series_slug_from_url(_ROMANCE_URL),
            episode_number=episode_number_from_url(_ROMANCE_URL),
        )

    def test_manifest_records_the_descriptive_name_and_stays_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = build_pdf_filename(source_url=_ROMANCE_URL)
            pdf = root / name
            pdf.write_bytes(b"%PDF-1.4\n")
            manifest = self._manifest(str(pdf))
            (root / MANIFEST_FILENAME).write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            self.assertEqual(manifest["pdf_filename"], name)
            self.assertEqual(manifest["episode_number"], "85")
            self.assertTrue(manifest["series_slug"])
            # The schema still validates with the new optional fields present.
            self.assertTrue(load_verified_run_manifest(root))

    def test_ui_opens_the_pdf_recorded_by_the_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = build_pdf_filename(source_url=_ROMANCE_URL)
            pdf = root / name
            pdf.write_bytes(b"%PDF-1.4\n")
            (root / MANIFEST_FILENAME).write_text(
                json.dumps(self._manifest(str(pdf))), encoding="utf-8"
            )

            found = find_output_artifacts(root)["pdf_path"]
            self.assertEqual(Path(found).name, name)

    def test_legacy_output_without_a_manifest_is_still_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / "capitulo_completo_traduzido.pdf"
            legacy.write_bytes(b"%PDF-1.4\n")

            found = find_output_artifacts(root)["pdf_path"]
            self.assertEqual(Path(found).name, "capitulo_completo_traduzido.pdf")

    def test_legacy_manifest_without_the_new_fields_stays_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy_pdf = root / "capitulo_completo_traduzido.pdf"
            legacy_pdf.write_bytes(b"%PDF-1.4\n")
            manifest = self._manifest(str(legacy_pdf))
            for field in ("pdf_filename", "series_slug", "episode_number"):
                manifest.pop(field, None)
            (root / MANIFEST_FILENAME).write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            self.assertTrue(load_verified_run_manifest(root))
            found = find_output_artifacts(root)["pdf_path"]
            self.assertEqual(Path(found).name, "capitulo_completo_traduzido.pdf")

    def test_same_chapter_in_two_runs_keeps_one_name_per_folder(self):
        # The filename is deterministic, so two runs of the same chapter carry the
        # same name; their own output folders keep them from overwriting.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = build_pdf_filename(source_url=_HORROR_URL)
            for run in ("run_a", "run_b"):
                directory = root / run
                directory.mkdir()
                (directory / name).write_bytes(b"%PDF-1.4\n")
            self.assertEqual(
                sorted(p.name for p in root.glob("*/*.pdf")), [name, name]
            )


if __name__ == "__main__":
    unittest.main()
