"""#84F53R - the Beta build must refuse an ambiguous OpenCV/Paddle environment.

pip cannot express "must not be installed" and does not detect that `opencv-python`
and `opencv-contrib-python` ship the same top-level `cv2/` directory. The guard is the
only thing standing between a silent provider swap and a shipped Beta, so its decisions
are asserted here rather than only observed on the build machine.
"""

import _test_bootstrap  # noqa: F401

import unittest
from unittest.mock import patch

from scripts import check_runtime_profile as guard


def _run(installed, cv2_version="5.0.0"):
    fake_cv2 = type("cv2", (), {"__version__": cv2_version, "__file__": "<fake>"})
    with patch.object(guard, "_installed", lambda name: installed.get(name)), \
            patch.dict("sys.modules", {"cv2": fake_cv2}), \
            patch.object(guard, "REQUIRED_IMPORTS", ("cv2",)):
        return guard.check()


CONTRACTUAL = {"opencv-python": "5.0.0.93", "rapidocr-onnxruntime": "1.4.4"}


class RuntimeProfileGuardTests(unittest.TestCase):
    def test_contractual_environment_reports_no_problem(self):
        self.assertEqual(_run(CONTRACTUAL), [])

    def test_two_opencv_distributions_are_rejected(self):
        problems = _run({**CONTRACTUAL, "opencv-contrib-python": "4.10.0.84"})

        self.assertTrue(any("same `cv2` import name" in p for p in problems), problems)

    def test_paddle_is_rejected_even_when_only_the_transitive_edge_is_present(self):
        # `paddlex` is what pins opencv-contrib-python, so it must fail on its own.
        problems = _run({**CONTRACTUAL, "paddlex": "3.7.2"})

        self.assertTrue(any("paddlex==3.7.2" in p for p in problems), problems)

    def test_overwritten_cv2_tree_is_caught_by_the_loaded_module(self):
        # The state observed in the development venv: pip still records
        # opencv-python==5.0.0.93 while the files on disk are the contrib 4.10 build.
        problems = _run(CONTRACTUAL, cv2_version="4.10.0")

        self.assertTrue(any("was overwritten" in p for p in problems), problems)

    def test_the_wheel_build_component_is_not_mistaken_for_a_mismatch(self):
        # opencv-python==5.0.0.93 exposes cv2.__version__ == "5.0.0": three components,
        # not four. Comparing the strings directly fails a correct environment.
        self.assertEqual(_run(CONTRACTUAL, cv2_version="5.0.0"), [])

    def test_missing_opencv_is_a_failure_not_a_pass(self):
        self.assertIn("no OpenCV distribution installed", _run({"rapidocr-onnxruntime": "1.4.4"}))


if __name__ == "__main__":
    unittest.main()
