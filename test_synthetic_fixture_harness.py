"""Offline contract for synthetic JobStore fixtures and their subprocess harness."""

import _test_bootstrap  # noqa: F401

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest


def _harness():
    from test_support import synthetic_fixture_harness
    return synthetic_fixture_harness


def _contract(tmp_path: Path):
    harness = _harness()
    output = tmp_path / "output"
    output.mkdir()
    return harness.ExpectedJobOutputContract(
        output_root=output,
        job_id="a" * 32,
        run_id="b" * 32,
        owner_id="owner-fixture",
        expected_outputs=frozenset({
            "page_001.png", "page_002.png", "quality_report.json",
            "synthetic_fixture.pdf",
        }),
        terminal=True,
    )


def _valid_manifest(contract) -> dict:
    return {
        "job_manifest_version": 1,
        "job_id": contract.job_id,
        "run_id": contract.run_id,
        "status": "finished",
        "stage": "finished",
        "source_url": "",
        "output_dir": str(contract.output_root),
        "configuration": {
            "job_type": "translation",
            "chapter_name": "Synthetic Quality Matrix",
        },
        "created_at": 1.0,
        "started_at": 2.0,
        "updated_at": 3.0,
        "pdf_path": str(contract.output_root / "synthetic_fixture.pdf"),
        "exit_code": 0,
    }


def _write_manifest(contract, payload=None):
    path = contract.output_root / "job_manifest.json"
    path.write_text(json.dumps(payload or _valid_manifest(contract)), encoding="utf-8")
    return path


def test_harness_module_is_available_without_import_side_effects():
    harness = _harness()
    assert harness.ExpectedJobOutputContract


def test_valid_job_manifest_is_structural_and_expected(tmp_path):
    harness = _harness()
    contract = replace(_contract(tmp_path), expected_outputs=frozenset())
    payload = _valid_manifest(contract)
    payload["pdf_path"] = ""
    _write_manifest(contract, payload)
    result = harness.validate_job_output_contract(
        contract, job_configuration={"community_owner_id": contract.owner_id}
    )
    assert result["valid"] is True
    assert result["structural_files"] == ["job_manifest.json"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(job_manifest_version=999),
        lambda p: p.update(job_id="c" * 32),
        lambda p: p.update(run_id="d" * 32),
        lambda p: p.update(created_at="not-a-time"),
        lambda p: p.update(source_url="https://external.invalid/chapter"),
        lambda p: p.update(access_token="secret"),
        lambda p: p.update(pdf_path="../outside.pdf"),
    ],
)
def test_invalid_or_foreign_manifest_fails_closed(tmp_path, mutation):
    harness = _harness()
    contract = _contract(tmp_path)
    payload = _valid_manifest(contract)
    mutation(payload)
    _write_manifest(contract, payload)
    with pytest.raises(harness.OutputContractError, match="manifest_invalid"):
        harness.validate_job_output_contract(
            contract, job_configuration={"community_owner_id": contract.owner_id}
        )


def test_manifest_owner_is_cross_checked_against_job_configuration(tmp_path):
    harness = _harness()
    contract = _contract(tmp_path)
    _write_manifest(contract)
    with pytest.raises(harness.OutputContractError, match="manifest_invalid"):
        harness.validate_job_output_contract(
            contract, job_configuration={"community_owner_id": "other-owner"}
        )


@pytest.mark.parametrize(
    "relative",
    [
        "unexpected.bin",
        "unexpected/child.bin",
        "leftover.partial",
        "credentials.json",
        "auth.sqlite3",
        "runner.log",
    ],
)
def test_undeclared_files_and_directories_remain_blocked(tmp_path, relative):
    harness = _harness()
    contract = _contract(tmp_path)
    _write_manifest(contract)
    target = contract.output_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    with pytest.raises(harness.OutputContractError, match="unexpected_output"):
        harness.validate_job_output_contract(
            contract, job_configuration={"community_owner_id": contract.owner_id}
        )


def test_declared_outputs_are_accepted_only_inside_root(tmp_path):
    harness = _harness()
    contract = _contract(tmp_path)
    _write_manifest(contract)
    for relative in contract.expected_outputs:
        (contract.output_root / relative).write_bytes(b"%PDF" if relative.endswith(".pdf") else b"x")
    result = harness.validate_job_output_contract(
        contract, job_configuration={"community_owner_id": contract.owner_id}
    )
    assert sorted(result["fixture_outputs"]) == sorted(contract.expected_outputs)


def test_terminal_contract_rejects_partial_but_active_contract_allows_it(tmp_path):
    harness = _harness()
    terminal = _contract(tmp_path)
    _write_manifest(terminal)
    (terminal.output_root / "synthetic_fixture.pdf.partial").write_bytes(b"x")
    with pytest.raises(harness.OutputContractError, match="unexpected_output"):
        harness.validate_job_output_contract(
            terminal, job_configuration={"community_owner_id": terminal.owner_id}
        )


def test_symlink_output_is_rejected(tmp_path, monkeypatch):
    harness = _harness()
    contract = _contract(tmp_path)
    _write_manifest(contract)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    link = contract.output_root / "page_001.png"
    try:
        link.symlink_to(outside)
    except OSError:
        link.write_bytes(b"x")
        original = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda path: path == link or original(path)
        )
    with pytest.raises(harness.OutputContractError, match="unexpected_output"):
        harness.validate_job_output_contract(
            contract, job_configuration={"community_owner_id": contract.owner_id}
        )


def test_subprocess_runs_from_unrelated_cwd_and_cleans_successfully(tmp_path):
    workspace = tmp_path / "workspace"
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    entrypoint = Path(__file__).parent / "test_support" / "synthetic_fixture_harness.py"
    completed = subprocess.run(
        [sys.executable, str(entrypoint), "run-quality", "--workspace", str(workspace)],
        cwd=unrelated,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "ALLOW_NETWORK_TESTS": ""},
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["valid"] is True
    assert result["external_calls"] == 0
    assert result["cleanup_complete"] is True
    assert not workspace.exists()
