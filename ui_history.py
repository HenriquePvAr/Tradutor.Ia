"""Local execution history for the NiceGUI application."""

from __future__ import annotations

import json
import hashlib
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
    def __init__(self, path: Path = HISTORY_PATH, hidden_path: Path | None = None):
        self.path = Path(path).resolve()
        self.hidden_path = Path(hidden_path).resolve() if hidden_path else self.path.with_name("ui_hidden_history.json")

    def load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            value = []
        records = value if isinstance(value, list) else []
        hidden = self._hidden_records()
        return sorted(
            (
                record for record in records
                if isinstance(record, dict) and not self._is_hidden(record, hidden)
            ),
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
        hidden = self._hidden_records()
        records = [
            self._enrich_record(self._with_output_verification(record))
            for record in self.load()
        ]
        records = [record for record in records if not self._is_hidden(record, hidden)]
        known = {str(Path(item.get("output_folder") or "").resolve()) for item in records}
        if not OUTPUT_ROOT.is_dir():
            return records
        # A canonical run manifest is sufficient discovery evidence.  Older outputs still
        # need a timing report, but a valid newer run must not disappear just because a
        # diagnostic timing file is absent or was retained elsewhere.
        for candidate in OUTPUT_ROOT.iterdir():
            if not candidate.is_dir():
                continue
            folder = candidate.resolve()
            if str(folder) in known:
                continue
            if self._is_hidden({"id": f"discovered-{folder.name}", "output_folder": str(folder)}, hidden):
                continue
            timing_path = folder / "timing_report.json"
            report = load_json(timing_path) if timing_path.is_file() else {}
            verification, manifest = self._output_verification(folder)
            if not manifest and not report:
                continue
            artifacts = find_output_artifacts(folder)
            if manifest:
                record = self._record_from_verified_manifest(
                    folder, manifest, report, artifacts, timing_path)
            else:
                quality = report.get("quality_validation") or {}
                status = derive_final_run_status(
                    technical_success=bool(artifacts.get("pdf_path")),
                    quality_validation=quality,
                )
                record = {
                    "id": f"discovered-{folder.name}",
                    "chapter_name": folder.name.replace("_", " ").title(),
                    "slug": folder.name,
                    "url": report.get("url", ""),
                    "mode": "fast" if report.get("ocr_engine") == "rapidocr" else "quality",
                    "scope": report.get("mode", ""),
                    "cache_mode": "force" if report.get("force") else "cache",
                    "started_at": self._discovery_timestamp(timing_path, folder),
                    "finished_at": self._discovery_timestamp(timing_path, folder),
                    "total_seconds": report.get("total_seconds", 0),
                    "status": status,
                    "output_folder": str(folder),
                    **artifacts,
                    "output_verification": verification,
                    "manifest_path": "",
                    "job_id": self._job_id_from_manifest(folder),
                    "pages_processed": report.get("processed_images", 0),
                    "groups_translated": report.get("groups_translated", 0),
                    "errors": report.get("pages_with_error", 0),
                    "quality_gate": quality.get("passed", ""),
                }
            records.append(
                self._enrich_record(record)
            )
        return self._sort_records(records)

    def hide_record(self, record: dict[str, Any]) -> None:
        """Hide a local history card without deleting its output artifacts."""

        hidden = self._hidden_records()
        record_id = str(record.get("id") or "").strip()
        folder = str(record.get("output_folder") or "").strip()
        if record_id:
            hidden.setdefault("ids", [])
            if record_id not in hidden["ids"]:
                hidden["ids"].append(record_id)
        if folder:
            try:
                folder = str(Path(folder).resolve())
            except OSError:
                folder = str(record.get("output_folder") or "")
            hidden.setdefault("folders", [])
            if folder not in hidden["folders"]:
                hidden["folders"].append(folder)
        self._write_hidden(hidden)

    def _hidden_records(self) -> dict[str, list[str]]:
        try:
            value = json.loads(self.hidden_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            value = {}
        if not isinstance(value, dict):
            return {"ids": [], "folders": []}
        ids = [str(item) for item in value.get("ids", []) if str(item or "").strip()]
        folders: list[str] = []
        for item in value.get("folders", []):
            try:
                folders.append(str(Path(str(item)).resolve()))
            except OSError:
                folders.append(str(item))
        return {"ids": ids, "folders": folders}

    def _write_hidden(self, hidden: dict[str, list[str]]) -> None:
        self.hidden_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.hidden_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(hidden, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.hidden_path)

    @staticmethod
    def _is_hidden(record: dict[str, Any], hidden: dict[str, list[str]]) -> bool:
        record_id = str(record.get("id") or "")
        if record_id and record_id in set(hidden.get("ids", [])):
            return True
        folder = str(record.get("output_folder") or "")
        if not folder:
            return False
        try:
            folder = str(Path(folder).resolve())
        except OSError:
            pass
        return folder in set(hidden.get("folders", []))

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
    def _discovery_timestamp(timing_path: Path, folder: Path) -> str:
        try:
            stamp = (timing_path if timing_path.is_file() else folder).stat().st_mtime
        except OSError:
            return ""
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _manifest_pdf_path(folder: Path, manifest: dict[str, Any]) -> str:
        """Validate the manifest PDF as an artifact confined to its own run folder."""

        candidate = str(manifest.get("pdf_path") or manifest.get("pdf_filename") or "")
        if not candidate:
            return ""
        path = Path(candidate)
        if not path.is_absolute():
            path = folder / path
        try:
            resolved = path.resolve()
            resolved.relative_to(folder.resolve())
        except (OSError, ValueError):
            return ""
        return str(resolved) if resolved.is_file() else ""

    @staticmethod
    def _sha256_file(path: str) -> str:
        if not path:
            return ""
        digest = hashlib.sha256()
        try:
            with Path(path).open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            return ""
        return digest.hexdigest()

    @classmethod
    def _record_from_verified_manifest(
        cls,
        folder: Path,
        manifest: dict[str, Any],
        report: dict[str, Any],
        artifacts: dict[str, str],
        timing_path: Path,
    ) -> dict[str, Any]:
        """Make the canonical manifest the terminal source of truth for a run card."""

        slug = str(manifest.get("slug") or folder.name)
        created_at = str(manifest.get("created_at") or cls._discovery_timestamp(timing_path, folder))
        final_status = str(manifest.get("final_status") or "review_required")
        # A technically produced PDF with a false quality gate is a review outcome even if
        # a pre-schema writer mistakenly labelled it finished.
        if final_status == "finished" and manifest.get("quality_passed") is not True:
            final_status = "review_required"
        identity = cls._job_identity_from_manifest(folder)
        pdf_path = cls._manifest_pdf_path(folder, manifest)
        return {
            "id": f"discovered-{folder.name}",
            "chapter_name": slug.replace("_", " ").title(),
            "slug": slug,
            "url": manifest.get("source_url", ""),
            "mode": "fast" if report.get("ocr_engine") == "rapidocr" else "quality",
            "scope": report.get("mode", ""),
            "cache_mode": "force" if report.get("force") else "cache",
            "started_at": created_at,
            "finished_at": created_at,
            "total_seconds": report.get("total_seconds", 0),
            "status": final_status,
            "output_folder": str(folder),
            **artifacts,
            "pdf_path": pdf_path,
            "output_verification": "manifest_verified",
            "manifest_path": str(folder / MANIFEST_FILENAME),
            "job_id": identity["job_id"],
            # The job manifest carries the database run id used by authenticated
            # publication/claim APIs.  The run manifest id is an evidence id and may
            # legitimately differ on older outputs.
            "run_id": identity["run_id"] or manifest.get("run_id", ""),
            "commit_hash": manifest.get("commit_hash", ""),
            "branch": manifest.get("branch", ""),
            "pipeline_version": manifest.get("pipeline_version", ""),
            "pages_processed": report.get("processed_images", 0),
            "groups_translated": report.get("groups_translated", 0),
            "manual_review_count": report.get("manual_review_required_groups", 0),
            "rejected_count": report.get("translations_rejected", report.get("rejected_count", 0)),
            "errors": report.get("pages_with_error", 0),
            "quality_gate": manifest.get("quality_passed"),
            "pdf_sha256": manifest.get("pdf_sha256") or cls._sha256_file(pdf_path),
            "publication_status": manifest.get("publication_status", ""),
            "publication_pdf_sha256": manifest.get("publication_pdf_sha256", ""),
            "published_at": manifest.get("published_at", ""),
        }

    @classmethod
    def _output_verification(cls, folder: Path) -> tuple[str, dict[str, Any]]:
        manifest = load_verified_run_manifest(folder)
        if manifest and cls._manifest_pdf_path(folder, manifest):
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
        if not folder.is_dir():
            return enriched
        verification, manifest = self._output_verification(folder)
        enriched["output_verification"] = verification
        enriched["manifest_path"] = str(folder / MANIFEST_FILENAME) if manifest else ""
        if manifest:
            identity = self._job_identity_from_manifest(folder)
            enriched["run_id"] = identity["run_id"] or manifest.get("run_id", "")
            for key in ("commit_hash", "branch", "pipeline_version"):
                enriched[key] = manifest.get(key, "")
            enriched["slug"] = manifest.get("slug") or enriched.get("slug") or folder.name
            enriched["url"] = manifest.get("source_url") or enriched.get("url", "")
            enriched["pdf_path"] = self._manifest_pdf_path(folder, manifest)
            enriched["pdf_sha256"] = manifest.get("pdf_sha256") or self._sha256_file(enriched["pdf_path"])
            status = str(manifest.get("final_status") or "review_required")
            enriched["status"] = (
                "review_required"
                if status == "finished" and manifest.get("quality_passed") is not True
                else status
            )
            enriched["quality_gate"] = manifest.get("quality_passed")
        if not enriched.get("job_id"):
            enriched["job_id"] = self._job_id_from_manifest(folder)
        if not manifest and (folder / "timing_report.json").is_file() and enriched.get("status") not in {"running", "cancelled", "error"}:
            report = load_json(folder / "timing_report.json")
            enriched["status"] = derive_final_run_status(
                technical_success=bool(enriched.get("pdf_path")),
                quality_validation=report.get("quality_validation") or {},
            )
        return enriched

    @staticmethod
    def _job_id_from_manifest(folder: Path) -> str:
        """Expose only a syntactically valid runner job id from server output metadata."""
        try:
            manifest = json.loads((folder / "job_manifest.json").read_text(encoding="utf-8"))
            candidate = str(manifest.get("job_id") or "") if isinstance(manifest, dict) else ""
        except (OSError, ValueError, TypeError):
            return ""
        if len(candidate) != 32 or any(char not in "0123456789abcdef" for char in candidate):
            return ""
        return candidate

    @staticmethod
    def _job_identity_from_manifest(folder: Path) -> dict[str, str]:
        """Read the canonical job/run identity without trusting filesystem names."""
        try:
            value = json.loads((folder / "job_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"job_id": "", "run_id": ""}
        if not isinstance(value, dict):
            return {"job_id": "", "run_id": ""}
        job_id = str(value.get("job_id") or "")
        if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
            job_id = ""
        run_id = str(value.get("run_id") or "").strip()
        return {"job_id": job_id, "run_id": run_id}

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
            "job_id",
            "community_ownership",
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
            "manual_review_count",
            "rejected_count",
            "pdf_sha256",
            "publication_status",
            "publication_pdf_sha256",
            "published_at",
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
        job_id = str(result.get("job_id") or "")
        if job_id and (
            len(job_id) != 32
            or any(char not in "0123456789abcdef" for char in job_id)
        ):
            result.pop("job_id", None)
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

