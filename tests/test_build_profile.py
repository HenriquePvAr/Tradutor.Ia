from pathlib import Path

import build_profile
import start_tradutor


def test_build_profile_source_is_not_production():
    assert build_profile.BUILD_PROFILE == "dev"
    assert not build_profile.is_production()


def test_production_rejects_performance_validation(monkeypatch, capsys):
    monkeypatch.setattr(build_profile, "BUILD_PROFILE", "production")
    assert start_tradutor._run_internal_child("performance-validation", []) == 2
    assert "REJECTED_BY_PRODUCTION_PROFILE" in capsys.readouterr().err


def test_production_spec_excludes_validation_harness():
    spec = Path("packaging/tradutor_ia.spec").read_text(encoding="utf-8")
    assert "performance_validation_harness" not in spec.split("if VALIDATION_BUILD:", 1)[0]
    assert "runtime_profile_production.py" in spec


def test_validation_spec_keeps_harness_available():
    spec = Path("packaging/tradutor_ia_validation.spec").read_text(encoding="utf-8")
    assert "VALIDATION_BUILD = True" in spec
    assert "tradutor_ia.spec" in spec
