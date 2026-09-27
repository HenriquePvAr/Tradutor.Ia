"""Static gate: the critical Beta runtime dependencies declared in requirements-beta.txt
must also be bundled by the PyInstaller spec.

This catches the exact class of failure seen in the Comix live smoke — a dependency
(scrapling[fetchers]) that was DECLARED in requirements-beta.txt but missing from the
runtime environment, so Comix dynamic discovery failed closed with capability_unavailable.
If a dep is declared but the frozen build does not collect it, the packaged Beta would
ship without it. Runs offline; reads files only.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQS = (ROOT / "requirements-beta.txt").read_text(encoding="utf-8")
SPEC = (ROOT / "packaging" / "tradutor_ia.spec").read_text(encoding="utf-8")

# Requirement pin (as it must appear in requirements-beta.txt) -> the import/module token
# the PyInstaller spec must reference (hiddenimports / collect_submodules / collect_data_files)
# so the module is actually bundled into the frozen app.
CRITICAL = {
    "scrapling[fetchers]==0.4.15": "scrapling",   # Comix dynamic reader resolver
    "psd-tools==1.10.9": "psd_tools",             # Professional PSD exporter (P1-P4)
}
# Runtime modules that must be collected by the spec even if declared in an included
# requirements file (selenium/rapidocr/browser backends).
SPEC_MUST_REFERENCE = ("scrapling", "psd_tools", "selenium", "rapidocr_onnxruntime",
                       "patchright", "playwright")


def test_requirements_beta_pins_critical_versions():
    for pin in CRITICAL:
        assert pin in REQS, f"requirements-beta.txt must pin {pin!r}"


def test_spec_bundles_every_declared_critical_dependency():
    for pin, module in CRITICAL.items():
        assert module in SPEC, (
            f"{pin!r} is declared in requirements-beta.txt but {module!r} is not "
            f"referenced by packaging/tradutor_ia.spec — the frozen Beta would ship without it")


def test_spec_references_all_browser_and_ocr_runtimes():
    for module in SPEC_MUST_REFERENCE:
        assert module in SPEC, f"packaging/tradutor_ia.spec must collect {module!r}"


def test_psd_tools_pin_matches_professional_psd_internal_dependency():
    # professional_psd._save_without_forced_composite relies on psd-tools save internals
    # (with a safe fallback). The pin must stay explicit so the behavior is deterministic.
    match = re.search(r"^psd-tools==([0-9.]+)$", REQS, re.MULTILINE)
    assert match, "psd-tools must be pinned to an exact version in requirements-beta.txt"
    assert match.group(1) == "1.10.9"
