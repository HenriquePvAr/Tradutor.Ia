"""Output-preset UI: the six-card selector, custom mode, and the preset->OutputPlan mapping.

Two layers:
  * UI content: the preset cards, custom checkboxes and payload wiring exist in the shipped
    ui_shell.html / tradutor_ui.js (same style as test_output_profile_selection.py).
  * Contract: each preset payload, run through the real server-side normalizer, yields the
    correct effective operations / knobs. The backend stays the single source of truth.
"""
from pathlib import Path

import pytest

import yomu.runtime.artifact_contract as ac
import yomu.ui.ui_bridge as ui_bridge
from yomu.runtime.output_plan import normalize_output_plan


# ---- UI content -------------------------------------------------------------------------
def test_preset_selector_has_the_six_options():
    html = Path("ui/ui_shell.html").read_text(encoding="utf-8")
    assert 'id="outputPlanField"' in html
    assert "O que você quer gerar?" in html
    for value in ("traducao_pronta", "apenas_clean", "apenas_traducao",
                  "traducao_clean", "profissional", "personalizado"):
        assert f'name="output_plan" value="{value}"' in html
    assert 'name="output_plan" value="traducao_pronta" checked' in html  # default
    # custom operation checkboxes
    for op_id in ("opTranslate", "opClean", "opTypeset", "opPdf", "opPsd", "opQc"):
        assert f'id="{op_id}"' in html


def test_preset_cards_do_not_leak_internal_terms():
    html = Path("ui/ui_shell.html").read_text(encoding="utf-8")
    # The output section must never expose internal strategy names anywhere (these are not
    # form values, so any occurrence is a user-facing leak). "typeset"/"output_plan" are
    # legitimate form value/name attributes (the visible label is "Aplicar texto traduzido").
    start = html.index('id="outputPlanField"')
    end = html.index("</fieldset>", html.index("outputCustomOps"))
    section = html[start:end].lower()
    for forbidden in ("gemini", "oracle", "chrome", "cdp", "deepl", "provider", "ocr"):
        assert forbidden not in section, f"leaked internal term: {forbidden}"


def test_js_sends_output_plan_and_custom_operations():
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    assert "output_plan: appState.outputPlan" in js
    assert "operations: planOps" in js
    assert "typeof effectivePlanOps === 'function' ? effectivePlanOps()" in js
    # dependency closure + default + download-only disabling exist
    assert "function closePlanDeps" in js
    assert "'traducao_pronta'" in js
    assert "syncOutputPlanField" in js


# ---- preset -> effective plan (backend is source of truth) ------------------------------
@pytest.mark.parametrize("preset,ops,translation,fmt", [
    ("traducao_pronta", {"translate", "clean", "typeset", "pdf"}, True, "pdf"),
    ("apenas_clean", {"clean"}, False, "png"),
    ("apenas_traducao", {"translate"}, True, "translation"),
    ("traducao_clean", {"translate", "clean"}, True, "png"),
    ("profissional", {"translate", "clean", "typeset", "pdf", "psd", "qc"}, True, "psd"),
])
def test_preset_maps_to_expected_plan(preset, ops, translation, fmt):
    plan = normalize_output_plan(preset)
    assert set(plan.operations) == ops
    assert plan.translation_enabled is translation
    assert plan.output_format == fmt
    assert plan.executable is True


def _normalize(monkeypatch, payload):
    bridge = object.__new__(ui_bridge.UiBridge)
    monkeypatch.setattr(ui_bridge, "env_status", lambda: {"env_exists": True, "nvidia_configured": True})
    monkeypatch.setattr(ui_bridge, "build_run_command", lambda **_: [])
    return bridge._normalize_payload({"url": "https://example.com/chapter-1", **payload})


@pytest.mark.parametrize("preset,translation_enabled,download_only,output_format", [
    ("traducao_pronta", True, False, "pdf"),
    ("apenas_clean", False, False, "png"),
    ("apenas_traducao", True, False, "translation"),
    ("traducao_clean", True, False, "png"),
    ("profissional", True, False, "psd"),
])
def test_preset_payload_normalizes_server_side(monkeypatch, preset, translation_enabled, download_only, output_format):
    cfg = _normalize(monkeypatch, {"output_plan": preset})
    assert cfg["output_plan"] == preset
    assert cfg["translation_enabled"] is translation_enabled
    assert cfg["download_only"] is download_only
    assert cfg["output_format"] == output_format


def test_custom_payload_with_operations_normalizes(monkeypatch):
    # custom typeset alone -> closes to translate+typeset (backend authoritative)
    cfg = _normalize(monkeypatch, {"output_plan": "personalizado", "operations": ["typeset"]})
    assert cfg["output_plan"] == "personalizado"
    assert set(cfg["effective_operations"]) == {"translate", "typeset"}
    assert cfg["translation_enabled"] is True


def test_apenas_clean_sends_zero_provider_contract(monkeypatch):
    cfg = _normalize(monkeypatch, {"output_plan": "apenas_clean"})
    assert cfg["translation_enabled"] is False  # 0 provider / 0 YK by construction
    assert cfg["effective_operations"] == ["clean"]
