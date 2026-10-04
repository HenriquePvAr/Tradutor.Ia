"""Artifact-backed completion summary contracts for Mission 12."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from yomu.runtime.job_completion import build_job_completion_summary
from yomu.ui.ui_bridge import UiBridge


def _write(path: Path, payload: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _manifest(output: Path, profile: str, statuses: dict[str, str], pages: int = 2) -> None:
    payload = {
        "contract_version": 2,
        "output_profile": profile,
        "logical_page_count": pages,
        "requested_artifacts": list(statuses),
        "artifact_status": statuses,
    }
    (output / "artifact_manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _base_record(output: Path, profile="automatic", status="finished", pages=2):
    return {
        "id": "job-123",
        "job_id": "job-123",
        "run_id": "run-123",
        "status": status,
        "output_profile": profile,
        "output_folder": str(output),
        "pdf_path": str(output / "final" / "chapter_ptbr.pdf"),
        "pages_processed": pages,
        "download_only": False,
    }


def test_automatic_finished_requires_real_manifest_artifacts(tmp_path):
    root = tmp_path / "output"
    output = root / "chapter"
    output.mkdir(parents=True)
    pdf = _write(output / "final" / "chapter_ptbr.pdf")
    for index in (1, 2):
        _write(output / "images" / "translated" / f"{index:03}.png")
    _manifest(output, "automatic", {"final_pdf": "produced", "translated_image": "produced"})

    result = build_job_completion_summary(_base_record(output), root)
    assert result["terminal_state"] == "finished"
    assert result["logical_pages"] == 2
    assert result["actions"] == {
        "pdf": True, "output_folder": True, "review": False,
        "psd_folder": False, "qc_folder": False,
    }
    assert result["artifacts"]["final_pdf"]["available"] is True
    assert pdf.is_file()


def test_automatic_review_required_exposes_review_without_psd(tmp_path):
    root = tmp_path / "output"
    output = root / "chapter"
    output.mkdir(parents=True)
    _write(output / "final" / "chapter_ptbr.pdf")
    for index in (1, 2):
        _write(output / "images" / "translated" / f"{index:03}.png")
    _manifest(output, "automatic", {"final_pdf": "produced", "translated_image": "produced"})
    record = _base_record(output, status="review_required")
    record["manual_review_count"] = 3

    result = build_job_completion_summary(record, root)
    assert result["review_required"] is True
    assert result["review_pending"] == 3
    assert result["actions"]["review"] is True
    assert result["actions"]["psd_folder"] is False


def test_professional_finished_shows_qc_and_psd_only_when_files_exist(tmp_path):
    root = tmp_path / "output"
    output = root / "chapter"
    output.mkdir(parents=True)
    _write(output / "final" / "chapter_ptbr.pdf")
    for index in (1, 2):
        _write(output / "images" / "translated" / f"{index:03}.png")
        _write(output / "images" / "clean" / f"{index:03}.png")
        _write(output / "psd" / f"{index:03}.psd")
    _write(output / "qc" / "quality_report.json", b"{}")
    _write(output / "qc" / "review_manifest.json", json.dumps({"summary": {"items_pending": 0}}).encode())
    _manifest(output, "professional_qc", {
        "final_pdf": "produced", "translated_image": "produced", "clean_image": "produced",
        "editable_psd": "produced", "quality_report": "produced", "review_manifest": "produced",
    })

    result = build_job_completion_summary(_base_record(output, "professional_qc"), root)
    assert result["output_profile"] == "professional_qc"
    assert result["review_pending"] == 0
    assert result["actions"]["psd_folder"] is True
    assert result["actions"]["qc_folder"] is True
    assert result["actions"]["review"] is True
    assert all(result["artifacts"][key]["available"] for key in (
        "final_pdf", "translated_image", "clean_image", "editable_psd", "quality_report",
    ))


@pytest.mark.parametrize("status,title", [("failed", "failed"), ("cancelled", "cancelled")])
def test_structural_failure_and_cancelled_are_not_success(status, title, tmp_path):
    root = tmp_path / "output"
    output = root / "chapter"
    output.mkdir(parents=True)
    record = _base_record(output, status=status)
    result = build_job_completion_summary(record, root)
    assert result["terminal_state"] == title
    assert result["actions"]["pdf"] is False


def test_download_only_has_folder_only_and_uses_download_count(tmp_path):
    root = tmp_path / "output"
    output = root / "chapter"
    output.mkdir(parents=True)
    record = _base_record(output, pages=7)
    record.update({"download_only": True, "pages_processed": 7})
    result = build_job_completion_summary(record, root)
    assert result["download_only"] is True
    assert result["logical_pages"] == 7
    assert result["actions"] == {
        "pdf": False, "output_folder": True, "review": False,
        "psd_folder": False, "qc_folder": False,
    }


def test_missing_manifest_produced_file_is_not_openable(tmp_path):
    root = tmp_path / "output"
    output = root / "chapter"
    output.mkdir(parents=True)
    _manifest(output, "professional_qc", {"final_pdf": "produced", "editable_psd": "produced"})
    result = build_job_completion_summary(_base_record(output, "professional_qc"), root)
    assert result["artifacts"]["final_pdf"]["status"] == "missing"
    assert result["actions"]["pdf"] is False
    assert result["actions"]["psd_folder"] is False
    assert result["actions"]["qc_folder"] is False


def test_legacy_job_keeps_existing_pdf_without_v2_manifest(tmp_path):
    root = tmp_path / "output"
    output = root / "legacy"
    output.mkdir(parents=True)
    pdf = _write(output / "old_result.pdf")
    record = _base_record(output)
    record["pdf_path"] = str(pdf)
    result = build_job_completion_summary(record, root)
    assert result["legacy"] is True
    assert result["actions"]["pdf"] is True


def test_output_outside_allowed_root_cannot_produce_open_actions(tmp_path):
    root = tmp_path / "output"
    outside = tmp_path / "outside"
    outside.mkdir()
    pdf = _write(outside / "private.pdf")
    record = _base_record(outside)
    record["pdf_path"] = str(pdf)
    result = build_job_completion_summary(record, root)
    assert result["actions"]["pdf"] is False
    assert result["actions"]["output_folder"] is False


def test_open_path_helper_rejects_traversal_and_absolute_outside_path(tmp_path):
    allowed = tmp_path / "output"
    allowed.mkdir()
    bridge = object.__new__(UiBridge)
    bridge.output_root = allowed
    for path in (allowed / ".." / ".." / "Windows", Path("C:/Windows"), Path("//server/share")):
        with pytest.raises(ValueError):
            bridge._open_artifact_path(str(path))


def test_open_psd_and_qc_folder_use_only_opaque_allowlisted_actions(tmp_path):
    root = tmp_path / "output"
    output = root / "chapter"
    output.mkdir(parents=True)
    _write(output / "psd" / "001.psd")
    _write(output / "psd" / "002.psd")
    _write(output / "qc" / "quality_report.json", b"{}")
    _write(output / "qc" / "review_manifest.json", b"{}")
    _manifest(output, "professional_qc", {"editable_psd": "produced", "quality_report": "produced"})
    record = _base_record(output, "professional_qc")
    record["completion_summary"] = build_job_completion_summary(record, root)
    bridge = object.__new__(UiBridge)
    bridge.output_root = root
    bridge.store = SimpleNamespace(get_job_for_owner=lambda owner, job_id: {"id": job_id})
    bridge._job_record = lambda _job: record
    opened = []
    bridge._open_artifact_path = lambda path, **_kwargs: opened.append(Path(path))

    bridge.open_artifact_for_owner("owner", "job-123", "psd_folder")
    bridge.open_artifact_for_owner("owner", "job-123", "qc_folder")
    assert opened == [(output / "psd").resolve(), (output / "qc").resolve()]
    with pytest.raises(ValueError):
        bridge.open_artifact_for_owner("owner", "job-123", "../../outside")


def test_completion_ui_copy_and_actions_are_local_artifact_driven():
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    html = Path("ui/ui_shell.html").read_text(encoding="utf-8")
    assert "record?.completion_summary" in js
    assert "model.actions?.pdf" in js and "model.actions?.psd_folder" in js
    assert "model.actions?.qc_folder" in js and "model.actions?.review" in js
    assert "Tradução concluída" in js and "Revisão recomendada" in js
    assert "Não foi possível concluir a tradução" in js and "Tradução cancelada" in js
    assert "data-completion-action" in js and "jobCompletionArtifacts" in html
    assert "fetch(" not in js[js.index("function renderJobCompletion(record)"):js.index("$('#jobCompletionActions')?.addEventListener")]


def test_review_count_copy_singular_plural_and_profile_scoping():
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    assert "count === 1 ? 'item recomendado para revisão.' : 'itens recomendados para revisão.'" in js
    assert "['final_pdf', 'translated_image', 'clean_image', 'editable_psd', 'quality_report']" in js
    assert "['final_pdf', 'translated_image']" in js


def test_completion_renderer_terminal_profiles_and_actions_with_dom_stub():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not available for the DOM contract harness")
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`missing ${name}`);
  const open = source.indexOf('{', start); let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unterminated ${name}`);
}
class Element {
  constructor() { this.hidden = false; this.textContent = ''; this.dataset = {}; this.children = []; }
  setAttribute(key, value) { this[key] = value; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  replaceChildren(...items) { this.children = items; }
}
const scenarios = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = scenarios.map(record => {
  const elements = new Map();
  const selectors = ['#jobCompletionPanel','#jobCompletionTitle','#jobCompletionBadge','#jobCompletionPages',
    '#jobCompletionMessage','#jobCompletionReview','#jobCompletionArtifacts','#jobCompletionActions',
    '#runStatusHuman','#runProgressHuman','#runEtaHuman','#runUpdatedHuman','#runNextHuman','#runRetryAction'];
  selectors.forEach(key => elements.set(key, new Element()));
  const context = {
    document: {createElement: () => new Element()},
    $: selector => elements.get(selector) || null,
    terminalRunStatuses: new Set(['finished','review_required','review_completed','failed','cancelled']),
    reasonText: code => code ? `Falha: ${code}` : '',
  };
  vm.createContext(context);
  vm.runInContext(`${extract('completionReviewCountText')}\n${extract('renderJobCompletion')}\nglobalThis.run = renderJobCompletion;`, context);
  context.run(record);
  const get = key => elements.get(key);
  return {
    title: get('#jobCompletionTitle').textContent,
    badgeHidden: get('#jobCompletionBadge').hidden,
    badge: get('#jobCompletionBadge').textContent,
    pages: get('#jobCompletionPages').textContent,
    message: get('#jobCompletionMessage').textContent,
    reviewHidden: get('#jobCompletionReview').hidden,
    review: get('#jobCompletionReview').textContent,
    artifacts: get('#jobCompletionArtifacts').children.map(item => item.children.map(child => child.textContent).join('')),
    actions: get('#jobCompletionActions').children.map(item => item.textContent),
    statusHidden: get('#runStatusHuman').hidden,
  };
});
process.stdout.write(JSON.stringify(results));
"""
    base = {
        "id": "job-abc", "job_id": "job-abc", "status": "finished",
        "completion_summary": {
            "job_id": "job-abc", "terminal_state": "finished", "output_profile": "automatic",
            "logical_pages": 208, "download_only": False, "review_required": False,
            "review_pending": 0, "artifacts": {
                "final_pdf": {"label": "PDF traduzido", "status": "produced", "available": True},
                "translated_image": {"label": "Imagens traduzidas", "status": "produced", "available": True},
                "clean_image": {"label": "Imagens limpas", "status": "not_requested", "available": False},
                "editable_psd": {"label": "PSDs editáveis", "status": "not_requested", "available": False},
                "quality_report": {"label": "Relatório de QC", "status": "not_requested", "available": False},
            },
            "actions": {"pdf": True, "output_folder": True, "review": False, "psd_folder": False, "qc_folder": False},
        },
    }
    automatic_review = json.loads(json.dumps(base))
    automatic_review["status"] = "review_required"
    automatic_review["completion_summary"].update({
        "terminal_state": "review_required", "review_required": True, "review_pending": 3,
    })
    automatic_review["completion_summary"]["actions"]["review"] = True
    professional = json.loads(json.dumps(base))
    professional["completion_summary"].update({
        "output_profile": "professional_qc", "review_required": False,
        "artifacts": {key: {"label": label, "status": "produced", "available": True} for key, label in (
            ("final_pdf", "PDF traduzido"), ("translated_image", "Imagens traduzidas"),
            ("clean_image", "Imagens limpas"), ("editable_psd", "PSDs editáveis"),
            ("quality_report", "Relatório de QC"),
        )},
        "actions": {"pdf": True, "output_folder": True, "review": False, "psd_folder": True, "qc_folder": True},
    })
    professional_review = json.loads(json.dumps(professional))
    professional_review["status"] = "review_required"
    professional_review["completion_summary"].update({"terminal_state": "review_required", "review_required": True, "review_pending": 12})
    professional_review["completion_summary"]["actions"]["review"] = True
    failed = json.loads(json.dumps(base))
    failed["status"] = "failed"
    failed["reason_code"] = "worker_failed"
    failed["completion_summary"].update({"terminal_state": "failed", "actions": {"pdf": False, "output_folder": False, "review": False, "psd_folder": False, "qc_folder": False}})
    cancelled = json.loads(json.dumps(base))
    cancelled["status"] = "cancelled"
    cancelled["completion_summary"].update({"terminal_state": "cancelled", "actions": {"pdf": False, "output_folder": True, "review": False, "psd_folder": False, "qc_folder": False}})
    download = json.loads(json.dumps(base))
    download["completion_summary"].update({"download_only": True, "logical_pages": 7, "actions": {"pdf": False, "output_folder": True, "review": False, "psd_folder": False, "qc_folder": False}})
    missing = json.loads(json.dumps(base))
    missing["completion_summary"]["actions"]["pdf"] = False
    missing["completion_summary"]["artifacts"]["final_pdf"].update({"status": "missing", "available": False})
    records = [base, automatic_review, professional, professional_review, failed, cancelled, download, missing]
    completed = subprocess.run(
        [node, "-e", harness, str(Path("static/tradutor_ui.js").resolve())],
        input=json.dumps(records), text=True, encoding="utf-8", capture_output=True, check=True,
    )
    results = json.loads(completed.stdout)
    assert results[0]["title"] == "Tradução concluída"
    assert results[0]["pages"] == "208 páginas processadas"
    assert results[0]["actions"] == ["Abrir PDF", "Abrir pasta", "Nova tradução"]
    assert results[1]["review"] == "3 itens recomendados para revisão."
    assert results[1]["actions"] == ["Abrir PDF", "Abrir Review", "Abrir pasta", "Nova tradução"]
    assert results[2]["actions"] == ["Abrir PDF", "Abrir PSDs", "Abrir pasta de QC", "Abrir pasta", "Nova tradução"]
    assert "12 itens recomendados para revisão." == results[3]["review"]
    assert results[4]["title"] == "Não foi possível concluir a tradução"
    assert "Tradução concluída" not in results[4]["title"]
    assert results[4]["actions"] == []
    assert results[5]["title"] == "Tradução cancelada"
    assert "Nova tradução" in results[5]["actions"]
    assert results[6]["title"] == "Download concluído"
    assert results[6]["pages"] == "7 imagens salvas"
    assert results[6]["actions"] == ["Abrir pasta", "Nova tradução"]
    assert "Abrir PDF" not in results[7]["actions"]
