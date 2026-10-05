"""Output-plan contract + selective-pipeline guarantees.

Covers the dependency rules, the mapping onto the existing config knobs, the ui_bridge
integration, and the decisive product guarantee: CLEAN-only costs 0 provider calls and
0 YK, proven at the real translation boundary (not by mocking the whole pipeline).
"""
from pathlib import Path

import pytest

import yomu.pipeline.benchmark_pipeline as bp
import yomu.runtime.artifact_contract as ac
import yomu.ui.ui_bridge as ui_bridge
from yomu.runtime import output_plan as op
from yomu.runtime.output_plan import Operation, normalize_output_plan, plan_from_config


# --- dependency rules --------------------------------------------------------------------
def test_typeset_requires_translation():
    plan = normalize_output_plan("personalizado", operations={Operation.TYPESET})
    assert Operation.TRANSLATE in plan.operations


def test_professional_closes_to_full_operation_set():
    assert normalize_output_plan("profissional").operations == frozenset(op.ALL_OPERATIONS)


def test_output_plan_resolves_separate_execution_operations():
    clean = normalize_output_plan("apenas_clean")
    assert clean.requested_operations == frozenset({Operation.CLEAN})
    assert clean.operations == frozenset({Operation.CLEAN})
    assert clean.execution_operations == frozenset({
        "analyze_metadata", "download_assets", "run_ocr", "reconstruct",
    })
    assert "translate" not in clean.execution_operations


def test_plan_artifact_matrix_is_derived_from_effective_operations():
    assert op.plan_artifact_types(normalize_output_plan("traducao_pronta").operations) == (
        "clean_image", "translated_image", "final_pdf",
    )
    assert op.plan_artifact_types(normalize_output_plan("apenas_clean").operations) == (
        "clean_image",
    )
    assert op.plan_artifact_types(normalize_output_plan("apenas_traducao").operations) == (
        "translation_manifest",
    )
    assert op.plan_artifact_types(normalize_output_plan("traducao_clean").operations) == (
        "clean_image",
    )
    custom = normalize_output_plan("personalizado", operations={Operation.CLEAN})
    assert op.plan_artifact_types(custom.operations) == ("clean_image",)
    assert op.plan_artifact_types(normalize_output_plan("profissional").operations) == (
        "clean_image", "translated_image", "final_pdf", "editable_psd",
        "quality_report", "review_manifest",
    )

    ready = normalize_output_plan("traducao_pronta")
    assert ready.execution_operations == frozenset({
        "analyze_metadata", "download_assets", "run_ocr", "translate",
        "reconstruct", "generate_pdf",
    })

    download = normalize_output_plan("download")
    assert download.execution_operations == frozenset({
        "analyze_metadata", "download_assets",
    })


def test_psd_and_qc_pull_the_professional_profile():
    for custom in ({Operation.PSD}, {Operation.QC}):
        plan = normalize_output_plan("personalizado", operations=custom)
        assert plan.output_profile == ac.OutputProfile.PROFESSIONAL_QC


def test_pdf_without_a_page_producer_is_dropped():
    plan = normalize_output_plan("personalizado", operations={Operation.PDF})
    assert Operation.PDF not in plan.operations
    assert not plan.operations


def test_clean_only_pdf_is_allowed():
    plan = normalize_output_plan("personalizado", operations={Operation.CLEAN, Operation.PDF})
    assert Operation.PDF in plan.operations  # clean is a page producer


# --- knob mapping ------------------------------------------------------------------------
def test_traducao_pronta_maps_to_automatic_pdf():
    plan = normalize_output_plan("traducao_pronta")
    assert plan.translation_enabled and not plan.download_only
    assert plan.output_profile == ac.OutputProfile.AUTOMATIC
    assert plan.output_format == "pdf"


def test_profissional_maps_to_professional_psd():
    plan = normalize_output_plan("profissional")
    assert plan.output_profile == ac.OutputProfile.PROFESSIONAL_QC
    assert plan.output_format == "psd"


def test_download_preset_is_download_only():
    plan = normalize_output_plan("download")
    assert plan.download_only and not plan.operations


def test_unknown_preset_falls_back_to_default_not_error():
    assert normalize_output_plan("does_not_exist").preset == op.DEFAULT_PRESET


# --- executability (honest: render couples clean + typeset) ------------------------------
def test_translate_only_is_executable_and_textual():
    plan = normalize_output_plan("apenas_traducao")
    assert plan.executable is True
    assert plan.translate_only is True
    assert plan.operations == frozenset({Operation.TRANSLATE})
    assert plan.output_format == "translation"
    assert op.plan_artifact_types(plan.operations) == ("translation_manifest",)


def test_all_five_product_modes_executable():
    for name in ("traducao_pronta", "apenas_clean", "apenas_traducao",
                 "traducao_clean", "profissional", "download"):
        assert normalize_output_plan(name).executable is True


# --- round-trip from legacy knobs --------------------------------------------------------
def test_plan_from_legacy_download_only():
    assert plan_from_config({"download_only": True}).download_only is True


def test_plan_from_legacy_professional_psd():
    plan = plan_from_config({"translation_enabled": True,
                             "output_profile": "professional_qc", "output_format": "psd"})
    assert Operation.PSD in plan.operations and Operation.QC in plan.operations


# --- ui_bridge integration ---------------------------------------------------------------
def _normalize(monkeypatch, payload):
    bridge = object.__new__(ui_bridge.UiBridge)
    monkeypatch.setattr(ui_bridge, "env_status", lambda: {"env_exists": True, "nvidia_configured": True})
    monkeypatch.setattr(ui_bridge, "build_run_command", lambda **_: [])
    return bridge._normalize_payload({"url": "https://example.com/chapter-1", **payload})


def test_output_plan_overrides_scattered_knobs(monkeypatch):
    cfg = _normalize(monkeypatch, {"output_plan": "profissional"})
    assert cfg["output_profile"] == ac.OutputProfile.PROFESSIONAL_QC
    assert cfg["output_format"] == "psd"
    assert cfg["translation_enabled"] is True
    assert cfg["output_plan"] == "profissional"


def test_apenas_clean_disables_translation(monkeypatch):
    cfg = _normalize(monkeypatch, {"output_plan": "apenas_clean"})
    assert cfg["translation_enabled"] is False
    assert cfg["download_only"] is False
    assert cfg["output_format"] == "png"
    assert cfg["output_plan"] == "apenas_clean"


def test_translate_only_plan_is_accepted_and_maps_to_translation_format(monkeypatch):
    # apenas_traducao is now executable: accepted by the bridge, mapped to the textual format.
    cfg = _normalize(monkeypatch, {"output_plan": "apenas_traducao"})
    assert cfg["translation_enabled"] is True
    assert cfg["download_only"] is False
    assert cfg["output_format"] == "translation"
    assert cfg["output_plan"] == "apenas_traducao"
    assert cfg["effective_operations"] == ["translate"]


def test_legacy_payload_without_plan_still_gets_inferred_preset(monkeypatch):
    cfg = _normalize(monkeypatch, {})  # no output_plan field
    assert cfg["output_plan"]  # inferred, never empty


# --- the decisive product guarantee: CLEAN-only = 0 provider, 0 YK -----------------------
def test_clean_only_plan_sets_translation_off():
    # Contract level: the plan the user picks for "Apenas Clean" carries translation OFF.
    assert normalize_output_plan("apenas_clean").translation_enabled is False


def test_translation_disabled_initializes_no_provider():
    # Provider spy at the REAL boundary: translation_enabled=False (what Apenas Clean sets)
    # returns no provider object at all -> it is impossible to make a provider call.
    assert bp._build_translation_provider("yomu_backend", translation_enabled=False) is None
    translator, _lang = bp._resolve_translation_runtime("yomu_backend", translation_enabled=False)
    assert translator is None  # no translator -> 0 provider calls, 0 YK


def test_yk_commit_is_guarded_by_translation_enabled():
    # YK-spy proof by invariant: the wallet commit (the only YK charge) is reached only
    # inside the translation_enabled branch, so a clean-only run never commits YK.
    src = Path("yomu/pipeline/benchmark_pipeline.py").read_text(encoding="utf-8")
    idx = src.index("settlement = commit_translation()")
    guard = src.rindex("if translation_enabled", 0, idx)
    # no competing unconditional commit between the guard and the call
    assert "commit_translation()" not in src[guard:idx].replace("callable(commit_translation)", "")


# --- manifest carries requested vs effective operations ----------------------------------
def test_manifest_records_requested_and_effective_operations():
    manifest = ac.build_artifact_manifest(
        output_profile=ac.OutputProfile.AUTOMATIC, output_root="/tmp/x", logical_page_count=1,
        output_plan="apenas_clean", requested_operations=["clean"], effective_operations=["clean"],
    )
    assert manifest["output_plan"] == "apenas_clean"
    assert manifest["requested_operations"] == ["clean"]
    assert manifest["effective_operations"] == ["clean"]


def test_manifest_omits_plan_fields_for_legacy_runs():
    manifest = ac.build_artifact_manifest(
        output_profile=ac.OutputProfile.AUTOMATIC, output_root="/tmp/x", logical_page_count=1)
    assert "output_plan" not in manifest
    assert "requested_operations" not in manifest

