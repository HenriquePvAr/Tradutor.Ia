# PyInstaller ONEDIR candidate for the frozen Beta worktree.
# This is packaging configuration only; no runtime semantics are changed.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

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

datas = [
    (str(ROOT / "ui"), "ui"),
    (str(ROOT / "static"), "static"),
    (str(ROOT / "assets" / "branding"), "assets/branding"),
    (str(ROOT / "config" / "public-runtime.json"), "config"),
    (str(RAPIDOCR_ROOT / "models"), "rapidocr_onnxruntime/models"),
    (str(RAPIDOCR_ROOT / "config.yaml"), "rapidocr_onnxruntime"),
]

hiddenimports = [
    "performance_validation_harness",
    "rapidocr_onnxruntime",
    # Selenium resolves the Chrome WebDriver lazily through __getattr__; keep
    # the concrete driver module in the frozen bundle for source adapters.
    "selenium.webdriver.chrome.webdriver",
    *collect_submodules("rapidocr_onnxruntime"),
]

a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "paddle", "paddleocr", "paddlex", "torch", "transformers",
        "test", "pytest", "pytest_cov", "coverage",
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
