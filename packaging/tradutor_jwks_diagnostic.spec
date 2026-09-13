# Console-enabled diagnostic variant; production packaging remains windowless.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent if Path(SPECPATH).name.lower() == "packaging" else Path(SPECPATH).resolve().parent.parent
if (ROOT / "desktop_app.py").is_file() is False:
    ROOT = Path(SPECPATH).resolve()
RAPIDOCR_ROOT = ROOT / ".venv-beta/Lib/site-packages/rapidocr_onnxruntime"
datas = [(str(ROOT / "ui"), "ui"), (str(ROOT / "static"), "static"),
         (str(ROOT / "assets" / "branding"), "assets/branding"),
         (str(ROOT / "config" / "public-runtime.json"), "config"),
         (str(RAPIDOCR_ROOT / "models"), "rapidocr_onnxruntime/models"),
         (str(RAPIDOCR_ROOT / "config.yaml"), "rapidocr_onnxruntime")]
hiddenimports = ["rapidocr_onnxruntime", "selenium.webdriver.chrome.webdriver", *collect_submodules("rapidocr_onnxruntime")]
a = Analysis([str(ROOT / "desktop_app.py")], pathex=[str(ROOT)], datas=datas,
             hiddenimports=hiddenimports, excludes=["paddle", "paddleocr", "paddlex", "torch", "transformers", "test", "pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], [], name="YomuSekai", icon=str(ROOT / "assets" / "branding" / "generated" / "yomu-sekai.ico"), console=True, exclude_binaries=True)
COLLECT(exe, a.binaries, a.datas, name="YomuSekai")
