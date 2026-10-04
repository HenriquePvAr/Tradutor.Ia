"""Small, artifact-backed summary for a terminal translation job."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ARTIFACT_LABELS = {
    "final_pdf": "PDF traduzido",
    "translated_image": "Imagens traduzidas",
    "clean_image": "Imagens limpas",
    "editable_psd": "PSDs editáveis",
    "quality_report": "Relatório de QC",
    "review_manifest": "Dados de revisão",
    "translation_manifest": "Tradução",
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _manifest(output: Path) -> dict[str, Any]:
    try:
        path = output / "artifact_manifest.json"
        if not _inside(path, output) or not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def build_job_completion_summary(record: dict[str, Any], output_root: str | Path) -> dict[str, Any]:
    """Resolve presentation facts from the job row + V2 manifest, with a V1 PDF fallback.

    No path supplied by the browser is accepted here. All artifact locations are fixed
    relative to the already-owned output folder; the open endpoint resolves them again.
    """
    status = str(record.get("status") or "").lower()
    profile_value = record.get("output_profile")
    profile = profile_value if profile_value in {"automatic", "professional_qc"} else "automatic"
    if status not in {"finished", "review_required", "review_completed", "failed", "cancelled"}:
        return {
            "job_id": str(record.get("job_id") or record.get("id") or ""),
            "terminal_state": status,
            "output_profile": profile,
            "logical_pages": 0,
            "download_only": bool(record.get("download_only")),
            "review_required": False,
            "review_pending": 0,
            "review_available": False,
            "legacy": True,
            "artifacts": {},
            "actions": {"pdf": False, "output_folder": False, "review": False,
                        "psd_folder": False, "qc_folder": False},
        }
    try:
        root = Path(output_root).expanduser().resolve()
    except (OSError, RuntimeError):
        root = Path(output_root)
    raw_output = str(record.get("output_folder") or "").strip()
    try:
        output = Path(raw_output).expanduser().resolve() if raw_output else None
    except (OSError, RuntimeError):
        output = None
    confined = bool(output and _inside(output, root) and output.is_dir())
    output = output if confined else None
    manifest = _manifest(output) if output else {}
    try:
        v2 = int(manifest.get("contract_version") or 0) >= 2
    except (TypeError, ValueError):
        v2 = False
    statuses = manifest.get("artifact_status") if isinstance(manifest.get("artifact_status"), dict) else {}
    try:
        pages = int(manifest.get("logical_page_count") or record.get("pages_processed") or 0)
    except (TypeError, ValueError):
        pages = 0
    pages = max(0, pages)

    def has_files(relative: str, suffix: str | None = None) -> bool:
        if output is None:
            return False
        try:
            directory = (output / relative).resolve()
        except (OSError, RuntimeError):
            return False
        if not _inside(directory, output) or not directory.is_dir():
            return False
        files = [p for p in directory.iterdir() if _inside(p, output)
                 and p.is_file() and (suffix is None or p.suffix.lower() == suffix)]
        return len(files) >= pages if pages > 0 else bool(files)

    def has_file(relative: str) -> bool:
        if output is None:
            return False
        try:
            candidate = (output / relative).resolve()
        except (OSError, RuntimeError):
            return False
        return _inside(candidate, output) and candidate.is_file()

    try:
        pdf_path = Path(str(record.get("pdf_path") or "")).expanduser().resolve() if record.get("pdf_path") else None
    except (OSError, RuntimeError):
        pdf_path = None
    pdf_exists = bool(pdf_path and output and _inside(pdf_path, output) and pdf_path.is_file())
    artifact_map = {
        "final_pdf": pdf_exists,
        "translated_image": has_files("images/translated", ".png"),
        "clean_image": has_files("images/clean", ".png"),
        "editable_psd": has_files("psd", ".psd"),
        "quality_report": has_file("qc/quality_report.json"),
        "review_manifest": has_file("qc/review_manifest.json"),
        # Translate-only deliverable: the persisted translation data (no visual artifact).
        "translation_manifest": has_file("translation/translation.json"),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for kind, label in ARTIFACT_LABELS.items():
        artifact_status = str(statuses.get(kind) or "") if v2 else ""
        if kind == "translation_manifest":
            # Translate-only deliverable: on-disk presence is authoritative.
            produced = artifact_map[kind]
        else:
            produced = artifact_map[kind] and (
                artifact_status == "produced" if v2 else kind in {"final_pdf", "translated_image"})
        if produced:
            final_status = "produced"
        elif artifact_status in {"failed", "unavailable", "skipped", "requested"}:
            final_status = artifact_status if artifact_status != "requested" else "missing"
        else:
            final_status = "missing" if v2 and kind in (manifest.get("requested_artifacts") or []) else "not_requested"
        artifacts[kind] = {"label": label, "status": final_status, "available": bool(produced)}

    review_pending = None
    if output and artifact_map["review_manifest"]:
        try:
            review_path = (output / "qc" / "review_manifest.json").resolve()
            if not _inside(review_path, output):
                raise ValueError("review_manifest_outside_output")
            body = json.loads(review_path.read_text(encoding="utf-8"))
            summary = body.get("summary") if isinstance(body, dict) else None
            if isinstance(summary, dict):
                review_pending = max(0, int(summary.get("items_pending") or 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            review_pending = None
    if review_pending is None:
        try:
            review_pending = max(0, int(record.get("manual_review_count") or 0))
        except (TypeError, ValueError):
            review_pending = 0

    download_only = bool(record.get("download_only"))
    review_required = (
        status == "review_required" or bool(record.get("review_required"))
        or (v2 and manifest.get("review_required") is True)
    )
    review_available = bool(
        review_required or review_pending > 0 or artifacts["review_manifest"]["available"]
        or record.get("review_available")
    )
    return {
        "job_id": str(record.get("job_id") or record.get("id") or ""),
        "terminal_state": status,
        "output_profile": str(
            manifest.get("output_profile")
            if manifest.get("output_profile") in {"automatic", "professional_qc"}
            else record.get("output_profile") or "automatic"
        ),
        "logical_pages": pages,
        "download_only": download_only,
        "review_required": review_required,
        "review_pending": review_pending,
        "review_available": review_available,
        "legacy": not v2,
        # Output-plan provenance so the finish screen lists only requested artifacts and the
        # history shows the mode. Prefer the manifest; fall back to the job record fields.
        "output_plan": str(manifest.get("output_plan") or record.get("output_plan") or ""),
        "effective_operations": (
            list(manifest.get("effective_operations"))
            if isinstance(manifest.get("effective_operations"), list)
            else (list(record.get("effective_operations"))
                  if isinstance(record.get("effective_operations"), list) else [])),
        "requested_artifacts": (list(manifest.get("requested_artifacts"))
                                if isinstance(manifest.get("requested_artifacts"), list) else []),
        "artifacts": artifacts,
        "actions": {
            "pdf": bool(artifacts["final_pdf"]["available"]),
            "output_folder": output is not None,
            "review": review_available,
            "psd_folder": bool(artifacts["editable_psd"]["available"]),
            "qc_folder": bool(artifacts["quality_report"]["available"] or artifacts["review_manifest"]["available"]),
        },
    }
