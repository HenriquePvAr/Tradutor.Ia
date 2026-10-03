from pathlib import Path

import artifact_contract as ac
import job_runner
import ui_bridge


def _normalize(monkeypatch, payload):
    bridge = object.__new__(ui_bridge.UiBridge)
    monkeypatch.setattr(ui_bridge, "env_status", lambda: {"env_exists": True, "nvidia_configured": True})
    monkeypatch.setattr(ui_bridge, "build_run_command", lambda **_: [])
    return bridge._normalize_payload({"url": "https://example.com/chapter-1", **payload})


def test_output_profile_cards_are_native_mutually_exclusive_radios():
    html = Path("ui/ui_shell.html").read_text(encoding="utf-8")
    assert 'role="radiogroup" aria-label="Perfil de saída"' in html
    assert 'name="output_profile" value="automatic" checked' in html
    assert 'name="output_profile" value="professional_qc"' in html
    assert "QC Profissional" in html
    css = Path("static/tradutor_ui.css").read_text(encoding="utf-8")
    assert ".output-profile-card:focus-within" in css
    assert "@media (max-width:520px){.output-profile-grid{grid-template-columns:1fr;}}" in css


def test_ui_defaults_persists_and_sends_the_profile(monkeypatch):
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    assert "outputProfile: 'automatic'" in js
    assert "yomu.outputProfile" in js
    assert "output_profile: appState.outputProfile === 'professional_qc' ? 'professional_qc' : 'automatic'" in js
    assert "output_profile: appState.selectedMode === 'download_only' ? 'automatic'" in js
    assert "form.outputProfile === 'professional_qc' ? 'professional_qc' : 'automatic'" in js


def test_switching_to_download_only_disables_profile_controls():
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    start = js.index("function applyProcessingMode(mode)")
    end = js.index("\n  function ", start + 1)
    assert "syncOutputProfileField()" in js[start:end]
    assert "input.disabled = downloadOnly" in js


def test_url_request_defaults_to_automatic_and_passes_explicit_profile(monkeypatch):
    automatic = _normalize(monkeypatch, {})
    professional = _normalize(monkeypatch, {"output_profile": ac.OutputProfile.PROFESSIONAL_QC})
    assert automatic["output_profile"] == ac.OutputProfile.AUTOMATIC
    assert professional["output_profile"] == ac.OutputProfile.PROFESSIONAL_QC


def test_legacy_explicit_psd_maps_to_professional_but_explicit_automatic_cannot_emit_psd(monkeypatch):
    legacy_psd = _normalize(monkeypatch, {"output_format": "psd"})
    automatic_psd = _normalize(monkeypatch, {
        "output_profile": ac.OutputProfile.AUTOMATIC, "output_format": "psd",
    })
    assert legacy_psd["output_profile"] == ac.OutputProfile.PROFESSIONAL_QC
    assert legacy_psd["output_format"] == "psd"
    assert automatic_psd["output_profile"] == ac.OutputProfile.AUTOMATIC
    assert automatic_psd["output_format"] == "pdf"


def test_download_only_ignores_any_professional_profile(monkeypatch):
    normalized = _normalize(monkeypatch, {
        "download_only": True,
        "output_profile": ac.OutputProfile.PROFESSIONAL_QC,
        "output_format": "psd",
    })
    assert normalized["output_profile"] == ac.OutputProfile.AUTOMATIC
    assert normalized["output_format"] == "pdf"
    assert "Download-only salva apenas o conteúdo original." in Path("ui/ui_shell.html").read_text(encoding="utf-8")


def test_each_profile_requests_only_its_frozen_artifact_set():
    automatic = ac.profile_artifacts(ac.OutputProfile.AUTOMATIC)
    professional = ac.profile_artifacts(ac.OutputProfile.PROFESSIONAL_QC)
    assert automatic == (ac.ArtifactType.FINAL_PDF, ac.ArtifactType.TRANSLATED_IMAGE)
    assert professional == (
        ac.ArtifactType.FINAL_PDF,
        ac.ArtifactType.TRANSLATED_IMAGE,
        ac.ArtifactType.CLEAN_IMAGE,
        ac.ArtifactType.EDITABLE_PSD,
        ac.ArtifactType.QUALITY_REPORT,
        ac.ArtifactType.REVIEW_MANIFEST,
    )


def test_profile_reaches_pipeline_child_and_old_jobs_fall_back_safely():
    env = {}
    job_runner._set_output_profile_environment(env, {"output_profile": "professional_qc"})
    assert env["YOMU_OUTPUT_PROFILE"] == ac.OutputProfile.PROFESSIONAL_QC
    job_runner._set_output_profile_environment(env, {})
    assert env["YOMU_OUTPUT_PROFILE"] == ac.OutputProfile.AUTOMATIC
    assert '"output_profile"' in Path("job_runner.py").read_text(encoding="utf-8")


def test_internal_quality_report_remains_separate_from_automatic_requested_artifacts():
    pipeline = Path("benchmark_pipeline.py").read_text(encoding="utf-8")
    assert "atomic_write_json(quality_json_path, quality_report)" in pipeline
    assert ac.ArtifactType.QUALITY_REPORT not in ac.profile_artifacts(ac.OutputProfile.AUTOMATIC)
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    assert "record.output_profile === 'professional_qc' ? actionButton('Relatório'" in js
    assert "record.output_profile === 'professional_qc' ? [['Qualidade', 'report'" in js
