"""Guards the NiceGUI runtime-dependency declaration against re-drifting.

Root cause this covers: `app_ui.py` imports NiceGUI unconditionally at module load
(`from nicegui import app, ui`), but `requirements.txt` (the only manifest the
"Hermetic Tests" CI workflow installs) did not declare it, so a CI runner that installs
strictly from the manifests fails `pytest --collect-only` with
``ModuleNotFoundError: No module named 'nicegui'`` while a developer machine with
NiceGUI installed out-of-band never notices. See requirements.txt and
.github/workflows/tests.yml.

This does not gate on unrelated, pre-existing runtime configuration (e.g. the community
auth provider's Supabase settings) that `app_ui.py` also requires at import time --
that is a separate concern from the dependency-declaration gap this test guards.
"""

import _test_bootstrap  # noqa: F401

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_UI_SOURCE = (ROOT / "app_ui.py").read_text(encoding="utf-8")
REQUIREMENTS_TXT = (ROOT / "requirements.txt").read_text(encoding="utf-8")
REQUIREMENTS_DEV_TXT = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

_PIN_RE = re.compile(r"(?im)^\s*nicegui\s*==\s*([^\s#]+)\s*$")


def _manifest_pins(text: str) -> list[str]:
    return _PIN_RE.findall(text)


class UiRuntimeDependencyTests(unittest.TestCase):
    def test_app_ui_imports_nicegui_at_module_scope(self) -> None:
        self.assertRegex(
            APP_UI_SOURCE,
            r"(?m)^from nicegui import ",
            "app_ui.py must keep importing nicegui at module scope for this guard "
            "to remain meaningful; if the import moved, update this test too.",
        )

    def test_requirements_txt_declares_nicegui_pinned_exactly(self) -> None:
        pins = _manifest_pins(REQUIREMENTS_TXT)
        self.assertEqual(
            len(pins), 1,
            "requirements.txt must declare exactly one `nicegui==<version>` line "
            f"(found {pins!r}); app_ui.py is production code that CI compiles and "
            "imports during test collection.",
        )

    def test_no_conflicting_nicegui_pin_in_requirements_dev(self) -> None:
        dev_pins = _manifest_pins(REQUIREMENTS_DEV_TXT)
        [prod_pin] = _manifest_pins(REQUIREMENTS_TXT)
        for dev_pin in dev_pins:
            self.assertEqual(
                dev_pin, prod_pin,
                "requirements-dev.txt declares a nicegui version that conflicts with "
                "requirements.txt.",
            )

    def test_ci_installs_requirements_txt(self) -> None:
        self.assertIn(
            "pip install -r requirements.txt", WORKFLOW,
            "Hermetic Tests workflow must install requirements.txt for the nicegui "
            "pin to reach the CI runner.",
        )

    def test_nicegui_is_importable_in_this_interpreter(self) -> None:
        try:
            import nicegui  # noqa: F401
        except ModuleNotFoundError as exc:
            self.fail(
                "nicegui is not importable in this interpreter even though "
                f"requirements.txt declares it; pip install may not have run: {exc}"
            )

    def test_clean_subprocess_import_of_app_ui_does_not_fail_on_nicegui(self) -> None:
        """A fresh process importing app_ui must not fail for a *missing nicegui*.

        It may still fail for unrelated, pre-existing environment preconditions
        (e.g. community auth provider configuration) -- that is out of scope for
        this dependency-declaration guard.
        """
        result = subprocess.run(
            [sys.executable, "-c", "import app_ui"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            self.assertNotIn(
                "No module named 'nicegui'", result.stderr,
                "app_ui import failed due to a missing nicegui install:\n"
                f"{result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
