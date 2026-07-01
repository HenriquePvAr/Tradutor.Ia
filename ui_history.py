"""Local execution history for the NiceGUI application."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ui_helpers import HISTORY_PATH, OUTPUT_ROOT, find_output_artifacts, load_json


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
        safe_record = self._safe_record(record)
        records = [item for item in self.load() if item.get("id") != safe_record.get("id")]
        records.insert(0, safe_record)
        self._write(records[:300])
        return safe_record

    def discover_outputs(self) -> list[dict[str, Any]]:
        records = self.load()
        known = {str(Path(item.get("output_folder") or "").resolve()) for item in records}
        if not OUTPUT_ROOT.is_dir():
            return records
        for timing_path in OUTPUT_ROOT.glob("*/timing_report.json"):
            folder = timing_path.parent.resolve()
            if str(folder) in known:
                continue
            report = load_json(timing_path)
            artifacts = find_output_artifacts(folder)
            records.append(
                self._safe_record(
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
                        "status": "finished",
                        "output_folder": str(folder),
                        **artifacts,
                        "pages_processed": report.get("processed_images", 0),
                        "groups_translated": report.get("groups_translated", 0),
                        "errors": report.get("pages_with_error", 0),
                        "quality_gate": (report.get("quality_validation") or {}).get("passed", ""),
                    }
                )
            )
        return sorted(records, key=lambda item: str(item.get("started_at") or ""), reverse=True)

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
        }
        result = {key: value for key, value in record.items() if key in allowed}
        result.setdefault("status", "unknown")
        return result

