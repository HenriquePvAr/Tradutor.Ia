"""Fail a Beta build whose environment does not match BETA1_OCR_RUNTIME_CONTRACT.

A requirements file can pin a version but cannot express "must not be installed",
and pip does not report the `cv2/` directory collision between `opencv-python` and
`opencv-contrib-python`: both own the same import name, so the last install wins and
`cv2.__version__` changes without any resolver error. This guard is the enforcement
point for that contract.

It is packaging infrastructure, deliberately kept out of the OCR pipeline: the
pipeline must not decide whether its own environment is legitimate.

    python -s scripts/check_runtime_profile.py

Exits 0 when the environment is unambiguous, 1 otherwise.
"""

from __future__ import annotations

import importlib.metadata as metadata
import sys

# Every distribution that installs a top-level ``cv2`` package. Exactly one may be
# present, and it must be the contractual one.
OPENCV_DISTRIBUTIONS = (
    "opencv-python",
    "opencv-contrib-python",
    "opencv-python-headless",
    "opencv-contrib-python-headless",
)
REQUIRED_OPENCV = ("opencv-python", "5.0.0.93")

# Not bundled with Beta 1. `paddlex` is listed because it is the transitive edge that
# drags in `opencv-contrib-python`, so it must be reported even when `paddleocr` is not.
FORBIDDEN_DISTRIBUTIONS = ("paddleocr", "paddlepaddle", "paddlex")

# RapidOCR is primary, so its absence is a build failure, not a degraded mode.
REQUIRED_IMPORTS = ("rapidocr_onnxruntime", "onnxruntime", "cv2")


def _installed(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def check() -> list[str]:
    problems: list[str] = []

    present = [(n, v) for n in OPENCV_DISTRIBUTIONS if (v := _installed(n))]
    if not present:
        problems.append("no OpenCV distribution installed")
    elif len(present) > 1:
        listed = ", ".join(f"{n}=={v}" for n, v in present)
        problems.append(
            f"{len(present)} OpenCV distributions share the same `cv2` import name: {listed}. "
            "The effective provider is whichever installed last, not whichever is declared."
        )
    elif present[0] != REQUIRED_OPENCV:
        problems.append(
            f"OpenCV is {present[0][0]}=={present[0][1]}, contract requires "
            f"{REQUIRED_OPENCV[0]}=={REQUIRED_OPENCV[1]}"
        )

    for name in FORBIDDEN_DISTRIBUTIONS:
        version = _installed(name)
        if version:
            problems.append(f"{name}=={version} is installed; Beta 1 does not bundle Paddle")

    for name in REQUIRED_IMPORTS:
        try:
            __import__(name)
        except ImportError as exc:
            problems.append(f"required import `{name}` failed: {exc}")

    # The metadata above only proves what pip recorded. This proves what Python loads.
    try:
        import cv2
    except ImportError:
        pass
    else:
        # `cv2.__version__` carries only major.minor.patch; the wheel adds a fourth
        # build component (5.0.0.93 -> "5.0.0"), so compare the first three.
        expected = ".".join(REQUIRED_OPENCV[1].split(".")[:3])
        if cv2.__version__.split(".")[:3] != expected.split("."):
            problems.append(
                f"cv2.__version__ is {cv2.__version__} but the recorded distribution claims "
                f"{REQUIRED_OPENCV[1]}: the `cv2/` tree was overwritten by another distribution"
            )

    return problems


def main() -> int:
    problems = check()
    if problems:
        print("BETA RUNTIME PROFILE: FAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    import cv2

    print("BETA RUNTIME PROFILE: OK")
    print(f"  python              {sys.version.split()[0]} ({sys.executable})")
    print(f"  cv2                 {cv2.__version__} <- {cv2.__file__}")
    print(f"  opencv distribution {REQUIRED_OPENCV[0]}=={_installed(REQUIRED_OPENCV[0])}")
    print(f"  rapidocr            {_installed('rapidocr-onnxruntime')}")
    print("  paddle              not installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
