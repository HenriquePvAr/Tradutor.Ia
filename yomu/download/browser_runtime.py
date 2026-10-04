"""Deterministic local browser discovery for isolated source analysis."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class BrowserRuntime:
    engine: str
    executable_path: str
    driver_path: str
    headless_mode: str
    runtime_source: str
    availability_status: str
    policy_hash: str

    def public(self) -> dict[str, str]:
        value = asdict(self)
        value["executable_path"] = Path(self.executable_path).name
        value["driver_path"] = Path(self.driver_path).name if self.driver_path else ""
        return value


def _policy_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class BrowserRuntimeResolver:
    """Resolve configured, managed, then system runtimes without downloading."""

    def __init__(self, *, environment: Mapping[str, str] | None = None,
                 platform_name: str | None = None):
        self.environment = {
            str(key).upper(): str(value)
            for key, value in (os.environ if environment is None else environment).items()
        }
        self.platform_name = platform_name or platform.system()

    def _system_candidates(self) -> list[tuple[str, str]]:
        if self.platform_name == "Windows":
            local = self.environment.get("LOCALAPPDATA", "")
            program = self.environment.get("PROGRAMFILES", "")
            program_x86 = self.environment.get("PROGRAMFILES(X86)", "")
            return [
                ("chrome", str(Path(program) / "Google/Chrome/Application/chrome.exe")),
                ("chrome", str(Path(program_x86) / "Google/Chrome/Application/chrome.exe")),
                ("chrome", str(Path(local) / "Google/Chrome/Application/chrome.exe")),
                ("edge", str(Path(program) / "Microsoft/Edge/Application/msedge.exe")),
                ("edge", str(Path(program_x86) / "Microsoft/Edge/Application/msedge.exe")),
            ]
        return [
            ("chrome", shutil.which("google-chrome") or ""),
            ("chrome", shutil.which("chromium") or ""),
            ("edge", shutil.which("microsoft-edge") or ""),
        ]

    def resolve(self, *, operation: str, preferred_engine: str = "",
                configured_executable: str = "", configured_driver: str = "",
                headless: bool = True) -> BrowserRuntime:
        configured = str(configured_executable or "").strip()
        if configured and not os.path.isfile(configured):
            raise ValueError("browser_executable_not_found")
        candidates: list[tuple[str, str, str]] = []
        if configured:
            candidates.append((preferred_engine or "chrome", configured, "configured"))
        for engine, path in self._system_candidates():
            if path:
                candidates.append((engine, path, "system"))
        selected = next(
            ((engine, path, source) for engine, path, source in candidates
             if os.path.isfile(path) and (not preferred_engine or engine == preferred_engine)),
            None)
        if not selected and str(configured_driver or "").strip():
            selected = ("chrome", "", "driver_implicit")
        if not selected:
            raise ValueError("browser_runtime_unavailable")
        engine, executable, source = selected
        driver = str(configured_driver or "").strip()
        if driver and not os.path.isfile(driver):
            raise ValueError("browser_driver_unavailable")
        if not driver:
            driver = (
                shutil.which("chromedriver") if engine == "chrome"
                else shutil.which("msedgedriver")
            ) or ""
        policy = {
            "operation": operation, "engine": engine, "headless": bool(headless),
            "resolution_order": ["configured", "system_chrome", "system_edge"],
            "platform": self.platform_name,
        }
        return BrowserRuntime(
            engine=engine, executable_path=executable, driver_path=driver,
            headless_mode="new" if headless else "disabled",
            runtime_source=source, availability_status="available",
            policy_hash=_policy_hash(policy),
        )
