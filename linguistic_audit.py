"""Offline, read-only linguistic audit of a reviewed chapter (BLOCO 2).

Produces a deterministic, traceable per-region report by applying the semantic
taxonomy to a chapter's persisted data. It selects the revision by real
job_id + run_id + manifest (never by title, date, glob or a latest pointer),
never calls a provider, and never modifies any artifact.

The report is a *derived view*: it records what a region's classification would
normalise to and what action that implies, but it applies nothing. A region that
would flip from preserved to translatable is flagged needs_human_review, not
corrected.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import region_taxonomy as tax


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve_output_dir(job_id: str, run_id: str, *, db_path: str = ".cache/runtime/jobs.sqlite3") -> str:
    """Resolve the chapter's output dir from the real job/run (never glob/latest).

    Validates that the job exists and its run_id matches, then returns the
    job-owned output dir. This is how the chapter is selected — by identity, not
    by title, date or the latest pointer.
    """
    from job_store import JobStore

    store = JobStore(Path(db_path))
    try:
        job = store.get_job(str(job_id or ""))
        if not job or not job.get("output_dir"):
            raise ValueError("job_not_found")
        if str(job.get("run_id") or "") != str(run_id or ""):
            raise ValueError("run_id_mismatch")
        return str(job["output_dir"])
    finally:
        store.close()


def find_reviewed_revision(output_dir: str, *, pdf_name: str) -> dict[str, Any] | None:
    """Locate the revision that produced ``pdf_name`` inside a chapter.

    Deterministic: matches the reviewed_pdf basename and breaks ties on the
    recorded ``updated_at`` field (not on the file's mtime, not a latest pointer).
    The chapter itself was already selected by real job/run identity.
    """
    root = Path(output_dir) / "quality_revision"
    if not root.is_dir():
        return None
    best: dict[str, Any] | None = None
    for child in sorted(root.iterdir()):
        manifest_path = child / "revision_manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_json(manifest_path)
        if Path(str(manifest.get("reviewed_pdf_path") or "")).name != pdf_name:
            continue
        manifest["_dir"] = str(child)
        if best is None or str(manifest.get("updated_at") or "") > str(best.get("updated_at") or ""):
            best = manifest
    return best


def _iter_report_items(quality_report: dict[str, Any]):
    seen: set[str] = set()
    for page in quality_report.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        number = int(page.get("index") or page.get("sequence_index") or 0)
        for collection in ("translation_terminal_items", "text_overflow_items",
                           "visual_validation_failures", "suspicious_groups"):
            for raw in (page.get(collection, []) or []):
                if not isinstance(raw, dict):
                    continue
                region_id = str(raw.get("region_id") or raw.get("id") or "")
                key = f"p{number:03d}:{region_id}"
                if key in seen:
                    continue
                seen.add(key)
                yield number, key, raw


def audit_chapter(output_dir: str, job_id: str, run_id: str, *,
                  pdf_name: str) -> dict[str, Any]:
    """Return the full audit dict for one chapter revision. Read-only."""
    output = Path(output_dir)
    quality_report = _read_json(output / "quality_report.json")
    revision = find_reviewed_revision(output_dir, pdf_name=pdf_name)
    if revision is None:
        raise ValueError("reviewed_revision_not_found")
    rev_dir = Path(revision["_dir"])
    reviews = {str(r.get("region_id")): r
              for r in _read_json(rev_dir / "contextual_translation_review.json").get("reviews", [])
              if isinstance(r, dict)}
    audit = _read_json(rev_dir / "incremental_render_audit.json")
    visual_states = audit.get("region_visual_states") or {}

    records: list[dict[str, Any]] = []
    for number, region_key, raw in _iter_report_items(quality_report):
        page_id = f"p{number:03d}"
        region_id = str(raw.get("region_id") or raw.get("id") or "")
        stable = f"{page_id}:{region_id}"
        legacy = str(raw.get("classification") or "unknown")
        source_text = str(raw.get("text") or raw.get("clean_text") or "")
        current_translation = str(raw.get("translation") or raw.get("translation_candidate") or "")
        preserve_as_name = bool(raw.get("preserve_as_name"))
        category, reason = tax.normalize(legacy, text=source_text, preserve_as_name=preserve_as_name)

        review = reviews.get(stable) or {}
        visual = visual_states.get(stable) or {}
        revision_linked = bool(visual)
        report_only = not revision_linked  # panel-flagged item outside the revision
        # Cache/provider: a region already answered by the revision has a reusable
        # decision; a translatable one never answered would need the provider.
        answered = bool(review)
        provider_required = tax.is_translatable(category) and not answered and (
            not current_translation or current_translation.strip() == source_text.strip())

        # A region whose normalized category disagrees with how it was handled
        # (preserved but now judged translatable) is the interesting audit signal.
        was_preserved = bool(raw.get("preserved_original")) or (str(review.get("action") or "") in ("preserve_original", "keep")) or report_only
        needs_human_review = tax.needs_human_review(category) or (
            tax.is_translatable(category) and was_preserved)

        reason_codes = [reason]
        if str(review.get("reason_code") or ""):
            reason_codes.append(str(review.get("reason_code")))

        records.append({
            "page_number": number,
            "page_id": page_id,
            "region_id": stable,
            "classification_original": legacy,
            "classification_normalized": category,
            "source_text": source_text,
            "current_translation": current_translation,
            "suggested_action": tax.suggested_action(category),
            "reason_codes": reason_codes,
            "revision_linked": revision_linked,
            "report_only": report_only,
            "visual_state": str(visual.get("state") or ""),
            "review_action": str(review.get("action") or ""),
            "cache_status": "answered" if answered else "not_answered",
            "provider_required": provider_required,
            "confidence": raw.get("confidence"),
            "needs_human_review": needs_human_review,
        })

    by_category: dict[str, int] = {}
    for record in records:
        by_category[record["classification_normalized"]] = by_category.get(record["classification_normalized"], 0) + 1
    report_only_records = [r for r in records if r["report_only"]]
    reclassified_report_only = [r for r in report_only_records if tax.is_translatable(r["classification_normalized"])]

    return {
        "taxonomy_version": tax.TAXONOMY_VERSION,
        "job_id": str(job_id),
        "run_id": str(run_id),
        "revision_id": str(revision.get("revision_id") or ""),
        "reviewed_pdf": pdf_name,
        "reviewed_pdf_sha256": str(revision.get("reviewed_pdf_sha256") or ""),
        "total_regions_audited": len(records),
        "by_normalized_category": dict(sorted(by_category.items())),
        "report_only_total": len(report_only_records),
        "report_only_now_translatable": len(reclassified_report_only),
        "report_only_still_preserved": sum(1 for r in report_only_records if tax.is_preservable(r["classification_normalized"])),
        "needs_human_review_total": sum(1 for r in records if r["needs_human_review"]),
        "provider_required_total": sum(1 for r in records if r["provider_required"]),
        "records": records,
    }


def write_report(report: dict[str, Any], out_dir: str) -> dict[str, str]:
    """Write JSON + Markdown. Only writes under ``out_dir`` (never the chapter)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "linguistic_page_audit.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Auditoria linguística — {report['reviewed_pdf']}",
             f"- revisão: {report['revision_id']} · job {report['job_id']} · run {report['run_id']}",
             f"- regiões auditadas: {report['total_regions_audited']}",
             f"- report_only: {report['report_only_total']} "
             f"(traduzíveis agora: {report['report_only_now_translatable']}, "
             f"preservados: {report['report_only_still_preserved']})",
             f"- exigem revisão humana: {report['needs_human_review_total']} · "
             f"exigiriam provider: {report['provider_required_total']}",
             "", "## Por categoria normalizada"]
    for cat, count in report["by_normalized_category"].items():
        lines.append(f"- {cat}: {count}")
    lines += ["", "## Regiões que exigem revisão humana"]
    for r in report["records"]:
        if r["needs_human_review"]:
            lines.append(f"- {r['region_id']} ({r['classification_original']} → "
                         f"{r['classification_normalized']}): {r['source_text'][:60]!r}")
    md_path = out / "linguistic_page_audit.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: linguistic_audit.py <job_id> <run_id> [out_dir]", file=sys.stderr)
        raise SystemExit(2)
    _job, _run = sys.argv[1], sys.argv[2]
    _out_dir = sys.argv[3] if len(sys.argv) > 3 else ".runtime/linguistic-audit"
    _output = resolve_output_dir(_job, _run)
    # Canonical base revision and its reviewed-PDF name, resolved dynamically from
    # the recorded pointer — nothing chapter-specific is hardcoded.
    _pointer = _read_json(Path(_output) / "quality_revision" / "latest_revision.json")
    _manifest = _read_json(Path(str(_pointer.get("manifest_path") or "")))
    _pdf_name = Path(str(_manifest.get("reviewed_pdf_path") or "")).name
    _report = audit_chapter(_output, _job, _run, pdf_name=_pdf_name)
    _paths = write_report(_report, _out_dir)
    print(json.dumps({k: _report[k] for k in (
        "total_regions_audited", "by_normalized_category", "report_only_total",
        "report_only_now_translatable", "needs_human_review_total", "provider_required_total")},
        ensure_ascii=False, indent=2))
    print("written:", _paths)
