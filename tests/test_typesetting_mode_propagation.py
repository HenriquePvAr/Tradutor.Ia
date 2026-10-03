"""typesetting_mode must survive UI -> config -> command argv, incl. the post-analysis
rebuild that historically dropped --output-format. PSD only; never emitted for pdf/png."""
import unittest

from source_analysis_phase import command_with_source_selection
from ui_helpers import build_run_command
from local_folder_job import build_local_job_command

URL = "https://comix.to/title/qk31-example/11378175-chapter-70"


def _ts(cmd):
    return cmd[cmd.index("--typesetting-mode") + 1] if "--typesetting-mode" in cmd else None


class BuildRunCommandTypesettingTests(unittest.TestCase):
    def test_psd_emits_mode_pdf_png_do_not(self):
        on = build_run_command(url=URL, mode="quality", output="s/i", full=True, max_images=None,
                               use_cache=False, force=True, use_context=True, output_format="psd",
                               typesetting_mode="on", python_executable="py")
        self.assertEqual(_ts(on), "on")
        off = build_run_command(url=URL, mode="quality", output="s/i", full=True, max_images=None,
                                use_cache=False, force=True, use_context=True, output_format="psd",
                                typesetting_mode="off", python_executable="py")
        self.assertEqual(_ts(off), "off")
        for fmt in ("pdf", "png"):
            cmd = build_run_command(url=URL, mode="quality", output="s/i", full=True, max_images=None,
                                    use_cache=False, force=True, use_context=True, output_format=fmt,
                                    typesetting_mode="off", python_executable="py")
            self.assertIsNone(_ts(cmd))  # only PSD carries a typesetting mode

    def test_default_is_on(self):
        cmd = build_run_command(url=URL, mode="quality", output="s/i", full=True, max_images=None,
                                use_cache=False, force=True, use_context=True, output_format="psd",
                                python_executable="py")
        self.assertEqual(_ts(cmd), "on")


class LocalCommandTypesettingTests(unittest.TestCase):
    def test_local_psd_carries_mode(self):
        cmd = build_local_job_command(snapshot_ref="ref", output="s/i", mode="quality",
                                      logical_pages=True, use_cache=False, force=True,
                                      use_context=False, output_format="psd", typesetting_mode="off",
                                      python_executable="py")
        self.assertEqual(_ts(cmd), "off")
        pdf = build_local_job_command(snapshot_ref="ref", output="s/i", mode="quality",
                                      logical_pages=True, use_cache=False, force=True,
                                      use_context=False, output_format="pdf", typesetting_mode="off",
                                      python_executable="py")
        self.assertIsNone(_ts(pdf))


class RebuildPreservesModeTests(unittest.TestCase):
    def _job(self, mode):
        return {
            "source_url": URL, "output_dir": r"C:\tmp\out\slug\run-1", "run_id": "run-1",
            "configuration": {"mode": "quality", "output_format": "psd", "typesetting_mode": mode,
                              "full": True, "download_only": False, "chapter_slug": "slug",
                              "provider_provenance": {"provider_requested": "deepl"},
                              "translation_provider": "deepl"},
        }

    def test_post_analysis_rebuild_keeps_mode(self):
        rebuilt = command_with_source_selection(
            self._job("off"), {"candidate_ids": ["a1", "b2"], "selected_ids": ["a1", "b2"]})
        self.assertEqual(_ts(rebuilt), "off")


if __name__ == "__main__":
    unittest.main()
