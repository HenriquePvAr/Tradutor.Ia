# PyInstaller ONEDIR candidate for the frozen Beta worktree.
# This is packaging configuration only; no runtime semantics are changed.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# PyInstaller exposes SPECPATH differently across invocation styles (directory
# in older versions, spec-file path in newer ones). Resolve both forms so data
# files are always sourced from the repository root rather than packaging/.
_spec_path = Path(SPECPATH).resolve()
# PyInstaller has exposed SPECPATH as the current project directory, the
# ``packaging`` directory, or (in programmatic invocations) the spec file.
# Resolve by a stable project marker rather than assuming one representation.
if (_spec_path / "desktop_app.py").is_file():
    ROOT = _spec_path
elif _spec_path.name.lower() == "packaging":
    ROOT = _spec_path.parent
elif _spec_path.is_file():
    ROOT = _spec_path.parent.parent
else:
    ROOT = _spec_path.parent
RAPIDOCR_ROOT = ROOT / ".venv-beta/Lib/site-packages/rapidocr_onnxruntime"
PLAYWRIGHT_ROOT = ROOT / ".venv-beta/Lib/site-packages/playwright"
VALIDATION_BUILD = bool(globals().get("VALIDATION_BUILD", False))

datas = [
    (str(ROOT / "ui"), "ui"),
    (str(ROOT / "static"), "static"),
    (str(ROOT / "assets" / "branding"), "assets/branding"),
    (str(ROOT / "config" / "public-runtime.json"), "config"),
    (str(ROOT / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(RAPIDOCR_ROOT / "models"), "rapidocr_onnxruntime/models"),
    (str(RAPIDOCR_ROOT / "config.yaml"), "rapidocr_onnxruntime"),
]

hiddenimports = [
    "rapidocr_onnxruntime",
    # Selenium resolves the Chrome WebDriver lazily through __getattr__; keep
    # the concrete driver module in the frozen bundle for source adapters.
    "selenium.webdriver.chrome.webdriver",
    "psd_tools",
    # Scrapling resolves DynamicFetcher lazily through ``scrapling.fetchers`` and
    # then imports its browser driver stack dynamically.  Keep the complete frozen
    # capability explicit: otherwise the optional import is swallowed by the resolver
    # and the UI falls through to a misleading source_access_denied result.
    "scrapling_reader_resolver",
    "dynamic_resolver_process",
    "scrapling.fetchers",
    "scrapling.fetchers.chrome",
    "scrapling.core.storage",
    "sqlite3",
    "patchright",
    "patchright.sync_api",
    "playwright",
    "playwright.sync_api",
    "orjson",
    *collect_submodules("rapidocr_onnxruntime"),
    *collect_submodules("scrapling"),
    *collect_submodules("patchright"),
    *collect_submodules("playwright"),
    *collect_submodules("browserforge"),
]
datas += collect_data_files("scrapling")
datas += collect_data_files("browserforge")
datas += collect_data_files("apify_fingerprint_datapoints")
# Scrapling's DynamicFetcher imports the regular Playwright sync API.  Its driver
# (including node.exe and cli.js) is runtime data, not a Python hidden import.
# Keep it beside the frozen playwright package so the browser controller can start.
datas.append((str(PLAYWRIGHT_ROOT / "driver"), "playwright/driver"))
if VALIDATION_BUILD:
    hiddenimports.append("performance_validation_harness")

a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / ("runtime_profile_validation.py" if VALIDATION_BUILD else "runtime_profile_production.py"))],
    excludes=[
        "paddle", "paddleocr", "paddlex", "torch", "transformers",
        "test", "pytest", "pytest_cov", "coverage",
        "performance_validation_harness" if not VALIDATION_BUILD else "__never_exclude__",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    [],
    name="YomuSekai",
    icon=str(ROOT / "assets" / "branding" / "generated" / "yomu-sekai.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    exclude_binaries=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YomuSekai",
)
