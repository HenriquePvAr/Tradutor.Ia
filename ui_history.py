"""Local execution history for the NiceGUI application."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ui_helpers import (
    HISTORY_PATH,
    OUTPUT_ROOT,
    REPO_ROOT,
    derive_final_run_status,
    find_output_artifacts,
    infer_series_details,
    load_json,
    quality_requires_review,
)
from output_manifest import MANIFEST_FILENAME, load_verified_run_manifest


_OUTPUT_VERIFICATION_RANK = {
    "manifest_verified": 0,
    "e2e_evidence": 1,
    "legacy_unverified": 2,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UIHistoryStore:
    def __init__(self, path: Path = HISTORY_PATH):
        self.path = Path(path).resolve()

    def load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            value = []
        records = value if isinstance(value, list) else []
        return sorted(
            (record for record in records if isinstance(record, dict)),
            key=lambda record: str(record.get("started_at") or ""),
            reverse=True,
        )

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        safe_record = self._enrich_record(record)
        records = [item for item in self.load() if item.get("id") != safe_record.get("id")]
        records.insert(0, safe_record)
        self._write(records[:300])
        return safe_record

    def discover_outputs(self) -> list[dict[str, Any]]:
        records = [
            self._enrich_record(self._with_output_verification(record))
            for record in self.load()
        ]
        known = {str(Path(item.get("output_folder") or "").resolve()) for item in records}
        if not OUTPUT_ROOT.is_dir():
            return records
        for timing_path in OUTPUT_ROOT.glob("*/timing_report.json"):
            folder = timing_path.parent.resolve()
            if str(folder) in known:
                continue
            report = load_json(timing_path)
            artifacts = find_output_artifacts(folder)
            quality = report.get("quality_validation") or {}
            verification, manifest = self._output_verification(folder)
            status = derive_final_run_status(
                technical_success=bool(artifacts.get("pdf_path")),
                quality_validation=quality,
            )
            records.append(
                self._enrich_record(
                    {
                        "id": f"discovered-{folder.name}",
                        "chapter_name": folder.name.replace("_", " ").title(),
                        "slug": folder.name,
                        "url": report.get("url", ""),
                        "mode": "fast" if report.get("ocr_engine") == "rapidocr" else "quality",
                        "scope": report.get("mode", ""),
                        "cache_mode": "force" if report.get("force") else "cache",
                        "started_at": datetime.fromtimestamp(
                            timing_path.stat().st_mtime, timezone.utc
                        ).isoformat(timespec="seconds"),
                        "finished_at": datetime.fromtimestamp(
                            timing_path.stat().st_mtime, timezone.utc
                        ).isoformat(timespec="seconds"),
                        "total_seconds": report.get("total_seconds", 0),
                        "status": status,
                        "output_folder": str(folder),
                        **artifacts,
                        "output_verification": verification,
                        "manifest_path": str(folder / MANIFEST_FILENAME)
                        if manifest
                        else "",
                        "run_id": manifest.get("run_id", ""),
                        "commit_hash": manifest.get("commit_hash", ""),
                        "branch": manifest.get("branch", ""),
                        "pipeline_version": manifest.get("pipeline_version", ""),
                        "pages_processed": report.get("processed_images", 0),
                        "groups_translated": report.get("groups_translated", 0),
                        "errors": report.get("pages_with_error", 0),
                        "quality_gate": quality.get("passed", ""),
                    }
                )
            )
        return self._sort_records(records)

    @staticmethod
    def _sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(
            records,
            key=lambda item: str(item.get("started_at") or ""),
            reverse=True,
        )
        return sorted(
            ordered,
            key=lambda item: _OUTPUT_VERIFICATION_RANK.get(
                str(item.get("output_verification") or "legacy_unverified"),
                2,
            ),
        )

    @staticmethod
    def _output_verification(folder: Path) -> tuple[str, dict[str, Any]]:
        manifest = load_verified_run_manifest(folder)
        if manifest:
            return "manifest_verified", manifest
        runtime = REPO_ROOT / ".cache" / "e2e_runtime" / folder.name
        try:
            exit_code = (runtime / "exit_code.txt").read_text(encoding="utf-8").strip()
            ended = (runtime / "end_time.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return "legacy_unverified", {}
        if exit_code == "0" and ended:
            return "e2e_evidence", {}
        return "legacy_unverified", {}

    def _with_output_verification(self, record: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(record)
        folder = Path(str(enriched.get("output_folder") or ""))
        if not folder.is_dir() or not (folder / "timing_report.json").is_file():
            return enriched
        verification, manifest = self._output_verification(folder)
        enriched["output_verification"] = verification
        enriched["manifest_path"] = str(folder / MANIFEST_FILENAME) if manifest else ""
        if manifest:
            for key in ("run_id", "commit_hash", "branch", "pipeline_version"):
                enriched[key] = manifest.get(key, "")
        if enriched.get("status") not in {"running", "cancelled", "error"}:
            report = load_json(folder / "timing_report.json")
            enriched["status"] = derive_final_run_status(
                technical_success=bool(enriched.get("pdf_path")),
                quality_validation=report.get("quality_validation") or {},
            )
        return enriched

    def _write(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def _safe_record(record: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "id",
            "chapter_name",
            "slug",
            "url",
            "mode",
            "scope",
            "max_images",
            "cache_mode",
            "started_at",
            "finished_at",
            "total_seconds",
            "status",
            "output_folder",
            "pdf_path",
            "quality_report_path",
            "compare_sheet_path",
            "contact_sheet_path",
            "session_context_path",
            "timing_report_path",
            "pages_processed",
            "groups_translated",
            "sfx_preserved",
            "errors",
            "quality_gate",
            "last_message",
            "series_name",
            "series_slug",
            "output_verification",
            "manifest_path",
            "run_id",
            "commit_hash",
            "branch",
            "pipeline_version",
        }
        result = {key: value for key, value in record.items() if key in allowed}
        result.setdefault("status", "unknown")
        return result

    @classmethod
    def _enrich_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        safe = cls._safe_record(record)
        if safe.get("status") == "finished" and quality_requires_review(
            {"passed": safe.get("quality_gate")}
        ):
            safe["status"] = "review_required"
        series = infer_series_details(
            url=str(safe.get("url") or ""),
            chapter_name=str(safe.get("chapter_name") or ""),
            output_slug=str(safe.get("slug") or ""),
        )
        safe["series_name"] = str(safe.get("series_name") or series["name"])
        safe["series_slug"] = str(safe.get("series_slug") or series["slug"])
        return safe

