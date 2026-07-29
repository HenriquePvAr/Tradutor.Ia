"""Hermetic synthetic quality fixture aligned with the JobStore runner contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# This file is an absolute test entrypoint and is intentionally executable from an
# unrelated cwd. The bootstrap is local to this test helper; production sys.path and
# the developer environment are never modified.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class OutputContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExpectedJobOutputContract:
    output_root: Path
    job_id: str
    run_id: str
    owner_id: str
    expected_outputs: frozenset[str]
    terminal: bool = True

    def __post_init__(self) -> None:
        root = Path(self.output_root)
        if not root.is_absolute():
            raise OutputContractError("contract_invalid")
        for relative in self.expected_outputs:
            _validate_relative_path(relative)


def _validate_relative_path(value: str) -> None:
    path = Path(str(value or ""))
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or str(path).replace("\\", "/").startswith("/")
    ):
        raise OutputContractError("manifest_invalid")


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _contains_sensitive_key(value: Any) -> bool:
    sensitive = {"token", "secret", "password", "cookie", "authorization", "csrf"}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in sensitive):
                return True
            if _contains_sensitive_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _valid_time(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0


def _validate_manifest(path: Path, contract: ExpectedJobOutputContract) -> dict[str, Any]:
    root = contract.output_root.resolve()
    if path.is_symlink() or not path.is_file() or not _within(path, root):
        raise OutputContractError("manifest_invalid")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OutputContractError("manifest_invalid") from None
    if not isinstance(manifest, dict) or _contains_sensitive_key(manifest):
        raise OutputContractError("manifest_invalid")
    if (
        manifest.get("job_manifest_version") != 1
        or manifest.get("job_id") != contract.job_id
        or manifest.get("run_id") != contract.run_id
        or not _valid_time(manifest.get("created_at"))
        or not _valid_time(manifest.get("updated_at"))
    ):
        raise OutputContractError("manifest_invalid")
    started_at = manifest.get("started_at")
    if started_at is not None and not _valid_time(started_at):
        raise OutputContractError("manifest_invalid")
    source = str(manifest.get("source_url") or "")
    if source and not source.startswith("local-folder:"):
        raise OutputContractError("manifest_invalid")

    # The current JobStore schema records these two trusted server-side paths as
    # absolute values. They are accepted only when they resolve exactly inside this
    # contract root; no client-supplied or arbitrary absolute path is accepted.
    output_dir = Path(str(manifest.get("output_dir") or ""))
    if output_dir.resolve() != root:
        raise OutputContractError("manifest_invalid")
    pdf_value = str(manifest.get("pdf_path") or "")
    if pdf_value:
        pdf_path = Path(pdf_value)
        if not pdf_path.is_absolute():
            _validate_relative_path(pdf_value)
            pdf_path = root / pdf_path
        if not _within(pdf_path, root) or pdf_path.name not in {
            Path(item).name for item in contract.expected_outputs if item.endswith(".pdf")
        }:
            raise OutputContractError("manifest_invalid")
    return manifest


def validate_job_output_contract(
    contract: ExpectedJobOutputContract,
    *,
    job_configuration: dict[str, Any],
) -> dict[str, Any]:
    root = contract.output_root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise OutputContractError("manifest_invalid")
    if str(job_configuration.get("community_owner_id") or "") != contract.owner_id:
        raise OutputContractError("manifest_invalid")
    _validate_manifest(root / "job_manifest.json", contract)

    structural = {"job_manifest.json"}
    allowed = structural | set(contract.expected_outputs)
    fixture_outputs: list[str] = []
    for item in root.rglob("*"):
        relative = item.relative_to(root).as_posix()
        if item.is_symlink():
            raise OutputContractError("unexpected_output")
        if item.is_dir():
            if not any(
                declared == relative or declared.startswith(f"{relative}/")
                for declared in allowed
            ):
                raise OutputContractError("unexpected_output")
            continue
        if relative not in allowed:
            if not contract.terminal and relative.endswith(".partial"):
                continue
            raise OutputContractError("unexpected_output")
        if relative not in structural:
            fixture_outputs.append(relative)
    if contract.terminal and set(fixture_outputs) != set(contract.expected_outputs):
        raise OutputContractError("unexpected_output")
    return {
        "valid": True,
        "structural_files": sorted(structural),
        "fixture_outputs": sorted(fixture_outputs),
    }


def _block_network() -> None:
    def denied(*_args, **_kwargs):
        raise RuntimeError("external_network_blocked")
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied


def _generate_quality(output: Path) -> int:
    _block_network()
    from PIL import Image, ImageDraw
    from output_manifest import build_run_manifest

    output = output.resolve()
    manifest_path = output / "job_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("job_manifest_missing")
    for number, color in ((1, "#f0eadf"), (2, "#e2edf6")):
        image = Image.new("RGB", (360, 540), color)
        draw = ImageDraw.Draw(image)
        draw.text((40, 250), f"Synthetic V8 {number}", fill="#202020")
        image.save(output / f"page_{number:03d}.png")
    pdf = output / "synthetic_fixture.pdf"
    _write_deterministic_two_page_pdf(pdf)
    quality = {
        "passed": True,
        "status": "passed",
        "synthetic": True,
        "local_test_only": True,
        "external_calls_allowed": False,
        "translation_accounting": {
            "detected_translatable": 4,
            "translated": 2,
            "preserved_original": 1,
            "rejected": 1,
            "accounting_closed": True,
        },
    }
    (output / "quality_report.json").write_text(
        json.dumps(quality, sort_keys=True), encoding="utf-8"
    )
    (output / "timing_report.json").write_text(
        json.dumps({"quality_validation": quality, "pdf_path": pdf.name}, sort_keys=True),
        encoding="utf-8",
    )
    run_manifest = build_run_manifest(
        run_id="synthetic-v8-offline",
        created_at="2026-07-28T00:00:00+00:00",
        source_url="local-folder:0123456789abcdef01234567",
        commit_hash="offline",
        branch="test",
        pipeline_version="synthetic-v8",
        model="none",
        final_status="finished",
        quality_passed=True,
        manual_review_count=0,
        rejected_count=1,
        pdf_path=pdf.name,
        slug=output.name,
        source_type="local_folder",
    )
    (output / "run_manifest.json").write_text(
        json.dumps(run_manifest, sort_keys=True), encoding="utf-8"
    )
    return 0


def _write_deterministic_two_page_pdf(path: Path) -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 360 540] "
        b"/Resources << /Font << /F1 7 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 360 540] "
        b"/Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        b"<< /Length 45 >>\nstream\nBT /F1 14 Tf 40 270 Td (Synthetic V8 1) Tj ET\nendstream",
        b"<< /Length 45 >>\nstream\nBT /F1 14 Tf 40 270 Td (Synthetic V8 2) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(payload)


def _remove_exact_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((item for item in root.rglob("*") if item.is_file()), reverse=True):
        path.unlink()
    for path in sorted((item for item in root.rglob("*") if item.is_dir()),
                       key=lambda item: len(item.parts), reverse=True):
        path.rmdir()
    root.rmdir()


def _run_quality(workspace: Path) -> dict[str, Any]:
    _block_network()
    from job_runner import run_job
    from job_store import JobStore

    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    output = workspace / "output"
    db = workspace / "jobs.sqlite3"
    log = workspace / "runner.log"
    owner = "synthetic-owner-v8"
    store = JobStore(db)
    try:
        job_id = store.create_job(
            source_url="",
            output_dir=str(output),
            command=[
                sys.executable,
                str(Path(__file__).resolve()),
                "generate-quality",
                "--output",
                str(output),
            ],
            configuration={
                "job_type": "translation",
                "community_owner_id": owner,
                "chapter_name": "Synthetic Quality Matrix V8",
                "synthetic": True,
                "local_test_only": True,
                "external_calls_allowed": False,
            },
        )
        claimed = store.claim_next_job("synthetic-harness-v8", os.getpid())
        if not claimed or claimed["id"] != job_id:
            raise RuntimeError("job_claim_failed")
        code = run_job(job_id, str(db), "synthetic-harness-v8", str(log))
        row = store.get_job(job_id)
        contract = ExpectedJobOutputContract(
            output_root=output,
            job_id=job_id,
            run_id=row["run_id"],
            owner_id=owner,
            expected_outputs=frozenset({
                "page_001.png",
                "page_002.png",
                "quality_report.json",
                "timing_report.json",
                "run_manifest.json",
                "synthetic_fixture.pdf",
            }),
            terminal=True,
        )
        validation = validate_job_output_contract(
            contract, job_configuration=row["configuration"]
        )
        hashes = {
            relative: hashlib.sha256((output / relative).read_bytes()).hexdigest()
            for relative in sorted(contract.expected_outputs)
        }
        result = {
            **validation,
            "runner_exit_code": code,
            "job_status": row["status"],
            "external_calls": 0,
            "hashes": hashes,
        }
    finally:
        store.close()
    _remove_exact_tree(workspace)
    result["cleanup_complete"] = not workspace.exists()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate-quality")
    generate.add_argument("--output", required=True)
    run = sub.add_parser("run-quality")
    run.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    if args.command == "generate-quality":
        return _generate_quality(Path(args.output))
    result = _run_quality(Path(args.workspace))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
