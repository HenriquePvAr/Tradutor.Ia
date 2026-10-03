"""output_format must survive UI -> config -> command_json -> runner argv, for pdf/png/psd.

Physically observed bug (job 95fa97e8): the submit built a command WITH ``--output-format
psd`` but ``source_candidate_ids=[]``; the worker then rebuilt the command via
``command_with_source_selection`` to carry the 105 candidate ids and, because that rebuild
never passed ``output_format``, silently dropped ``--output-format psd``.  The runner argv
defaulted to pdf, so a psd job produced a pdf while still consuming 1 YK.  ``config`` said
``psd`` the whole time -> the divergence is exactly what these tests pin.
"""
import unittest

from source_analysis_phase import command_with_source_selection
from ui_helpers import (
    assert_command_output_format,
    build_run_command,
    command_output_format,
)

URL = "https://comix.to/title/qk31-example/11378175-chapter-70"


def _job(fmt, *, download_only=False):
    return {
        "source_url": URL,
        "output_dir": r"C:\tmp\out\slug\run-1",
        "run_id": "run-1",
        "configuration": {
            "mode": "quality",
            "output_format": fmt,
            "full": True,
            "download_only": download_only,
            "chapter_slug": "slug",
            "provider_provenance": {"provider_requested": "deepl"},
            "translation_provider": "deepl",
        },
    }


class BuildRunCommandFormatTests(unittest.TestCase):
    def test_build_emits_flag_only_for_png_and_psd(self):
        for fmt in ("png", "psd"):
            cmd = build_run_command(url=URL, mode="quality", output="s/i", full=True,
                                    max_images=None, use_cache=False, force=True,
                                    use_context=True, output_format=fmt, python_executable="py")
            self.assertIn("--output-format", cmd)
            self.assertEqual(command_output_format(cmd), fmt)
        pdf = build_run_command(url=URL, mode="quality", output="s/i", full=True,
                                max_images=None, use_cache=False, force=True,
                                use_context=True, output_format="pdf", python_executable="py")
        self.assertNotIn("--output-format", pdf)      # pdf is the canonical default
        self.assertEqual(command_output_format(pdf), "pdf")


class RebuildPreservesFormatTests(unittest.TestCase):
    """The exact regression: the post-analysis rebuild must not drop the format."""

    def _rebuilt(self, fmt, **kw):
        return command_with_source_selection(
            _job(fmt, **kw), {"candidate_ids": ["a1", "b2", "c3"], "selected_ids": ["a1", "b2", "c3"]})

    def test_psd_survives_rebuild_with_candidates(self):
        cmd = self._rebuilt("psd")
        self.assertEqual(command_output_format(cmd), "psd")
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd.count("--source-candidate-id"), 3)  # candidates carried too

    def test_png_survives_rebuild(self):
        self.assertEqual(command_output_format(self._rebuilt("png")), "png")

    def test_pdf_rebuild_stays_pdf(self):
        cmd = self._rebuilt("pdf")
        self.assertEqual(command_output_format(cmd), "pdf")
        self.assertNotIn("--output-format", cmd)

    def test_download_only_psd_survives_rebuild(self):
        cmd = self._rebuilt("psd", download_only=True)
        self.assertEqual(command_output_format(cmd), "psd")
        self.assertIn("--download-only", cmd)


class OutputFormatInvariantTests(unittest.TestCase):
    """Fail closed BEFORE expensive processing on any config/command divergence."""

    def test_matching_format_passes(self):
        for fmt in ("pdf", "png", "psd"):
            cmd = build_run_command(url=URL, mode="quality", output="s/i", full=True,
                                    max_images=None, use_cache=False, force=True,
                                    use_context=True, output_format=fmt, python_executable="py")
            assert_command_output_format(cmd, {"output_format": fmt})  # must not raise

    def test_psd_config_but_pdf_command_is_rejected(self):
        pdf_cmd = build_run_command(url=URL, mode="quality", output="s/i", full=True,
                                    max_images=None, use_cache=False, force=True,
                                    use_context=True, output_format="pdf", python_executable="py")
        with self.assertRaises(ValueError) as ctx:
            assert_command_output_format(pdf_cmd, {"output_format": "psd"})  # the real bug
        self.assertEqual(str(ctx.exception), "output_format_mismatch")

    def test_png_config_but_psd_command_is_rejected(self):
        psd_cmd = build_run_command(url=URL, mode="quality", output="s/i", full=True,
                                    max_images=None, use_cache=False, force=True,
                                    use_context=True, output_format="psd", python_executable="py")
        with self.assertRaises(ValueError):
            assert_command_output_format(psd_cmd, {"output_format": "png"})

    def test_rebuild_result_satisfies_the_invariant(self):
        # End-to-end: the rebuild output is self-consistent for every format.
        for fmt in ("pdf", "png", "psd"):
            cmd = command_with_source_selection(
                _job(fmt), {"candidate_ids": ["x"], "selected_ids": ["x"]})
            assert_command_output_format(cmd, {"output_format": fmt})  # must not raise


if __name__ == "__main__":
    unittest.main()
