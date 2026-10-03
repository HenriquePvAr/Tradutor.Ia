import json
from pathlib import Path
import pytest
from experiments.kaggle_qwen_fallback_lab.run import is_m20, main

def test_rejects_m20():
    assert is_m20(Path("/tmp/M20/page.png"))
    assert not is_m20(Path("/tmp/non_m19/page.png"))

def test_dry_run_creates_report(tmp_path):
    image=tmp_path/"page.png"; image.write_bytes(b"fake-image")
    report=tmp_path/"report.json"
    assert main(["--image",str(image),"--prompt","audit","--kaggle-slug","user/lab-x","--dry-run","--output",str(report)]) == 0
    data=json.loads(report.read_text())
    assert data["status"] == "dry_run"
    assert data["cleanup"]["staging_deleted"] is True

def test_rejects_missing_image(tmp_path):
    with pytest.raises(SystemExit): main(["--image",str(tmp_path/"missing.png"),"--prompt","x","--kaggle-slug","u/x"])
