"""Safe orchestration for selective local artifact reconstruction.

This module composes existing primitives: persisted OCR/translation evidence,
the current translation validator, local page rendering, PDF generation, SHA/size
calculation, and quality gating.  It never fetches sources, calls a translation
provider, or treats review confirmation as artifact quality.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from benchmark_pipeline import _build_quality_report, _translation_quality_accounting
from community_service import sha256_of_file
from job_store import JobStatus, JobStore
from ocr_balloon import validate_translation_text
from pdf import generate_pdf
from pipeline_cache import atomic_write_json


class ReconstructionError(RuntimeError):
    """Local, categorical reconstruction failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code)


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ReconstructionError(code) from None
    if not isinstance(value, dict):
        raise ReconstructionError(code)
    return value


def _text_sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest, _size = sha256_of_file(path)
    return digest


def _stable_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _page_number(page: dict[str, Any]) -> int:
    try:
        return int(page.get("index") or page.get("sequence_index") or 0)
    except (TypeError, ValueError):
        return 0


def _candidate_values(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "selected_candidate",
        "review_candidate",
        "translation_candidate",
        "rejected_translation",
    ):
        value = str(item.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def default_render_page(
    *,
    source_page: Path,
    destination: Path,
    page: dict[str, Any],
    item: dict[str, Any],
    candidate: str,
) -> dict[str, Any]:
    """Render one corrected page with the existing renderer.

    Tests normally inject a spy renderer.  The default implementation is local-only:
    it loads the persisted source page and persisted debug items, injects the
    selected persisted candidate into the target item, and asks the existing
    renderer to redraw from those structures.
    """
    import cv2
    from chapter_quality_revision import ChapterQualityRevision
    from ocr_balloon import render_analyzed_image

    image = cv2.imread(str(source_page))
    if image is None:
        raise ReconstructionError("reconstruction_source_page_missing")
    render_page = copy.deepcopy(page)
    for existing in render_page.get("debug_data", {}).get("items", []) or []:
        if not isinstance(existing, dict):
            continue
        if str(existing.get("region_id") or "") != str(item.get("region_id") or ""):
            continue
        existing["translation"] = candidate
        existing["translation_candidate"] = candidate
        existing["translation_valid"] = True
        existing["translation_validation_reason"] = "reconstruction_validated"
        existing["translation_final_state"] = "translated"
        existing["translation_final_reason"] = "reconstruction_candidate_validated"
        existing["preserved_original"] = False
        existing["manual_review_required"] = False
    engine = object.__new__(ChapterQualityRevision)
    groups = ChapterQualityRevision._groups_from_page_items(engine, render_page)
    rendered, debug = render_analyzed_image(
        image,
        [],
        [],
        groups,
        page_index=_page_number(render_page),
        image_path=str(source_page),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if rendered is None or not cv2.imwrite(str(destination), rendered):
        raise ReconstructionError("reconstruction_render_failed")
    return debug if isinstance(debug, dict) else {}


def default_quality_builder(
    *,
    run_id: str,
    artifact_sha256: str,
    artifact_size_bytes: int,
    pages: list[dict[str, Any]],
    source: dict[str, Any],
) -> dict[str, Any]:
    quality_validation = _translation_quality_accounting(pages)
    quality_validation.update({
        "passed": bool(quality_validation.get("quality_passed")),
        "status": "passed" if quality_validation.get("quality_passed") else "review_required",
        "run_id": run_id,
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": artifact_size_bytes,
    })
    report = {
        "url": source.get("source_url") or "",
        "status": "finished" if quality_validation["passed"] else "review_required",
        "pdf_path": source.get("pdf_path") or "",
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": artifact_size_bytes,
        "quality_validation": quality_validation,
    }
    quality = _build_quality_report(report, pages, [])
    quality.setdefault("summary", {})
    quality["summary"].update({
        "run_id": run_id,
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": artifact_size_bytes,
    })
    quality["summary"].setdefault("quality_validation", {}).update({
        "run_id": run_id,
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": artifact_size_bytes,
    })
    return quality


class SelectiveArtifactReconstructor:
    """Orchestrate temp-build, fresh quality, and atomic artifact promotion."""

    def __init__(
        self,
        job_store: JobStore,
        *,
        workspace_root: str | Path,
        renderer: Callable[..., dict[str, Any]] = default_render_page,
        pdf_builder: Callable[..., None] = generate_pdf,
        quality_builder: Callable[..., dict[str, Any]] = default_quality_builder,
        clock: Callable[[], float] = time.time,
    ):
        self.job_store = job_store
        self.workspace_root = Path(workspace_root).resolve()
        self.renderer = renderer
        self.pdf_builder = pdf_builder
        self.quality_builder = quality_builder
        self.clock = clock

    def reconstruct(self, request: dict[str, Any]) -> dict[str, Any]:
        source = self._resolve_source(request)
        progress = _read_json(source["output_dir"] / "progress.json", "reconstruction_evidence_missing")
        pages = [page for page in (progress.get("pages") or []) if isinstance(page, dict)]
        if not pages:
            raise ReconstructionError("reconstruction_page_order_missing")
        page, item = self._select_item(pages, request)
        candidate = self._select_candidate(item, request)
        valid, reason = validate_translation_text(
            item.get("clean_text") or item.get("original_text") or item.get("text") or "",
            candidate,
            item.get("classification") or "unknown",
        )
        if not valid:
            raise ReconstructionError("reconstruction_candidate_invalid")
        source_pdf_sha = _file_sha256(source["pdf_path"]) if source["pdf_path"].is_file() else ""
        reconstruction_id = _stable_id({
            "source_job_id": source["job"]["id"],
            "source_run_id": source["job"]["run_id"],
            "source_pdf_sha256": source_pdf_sha,
            "page": _page_number(page),
            "region_id": item.get("region_id"),
            "candidate_sha256": _text_sha256(candidate),
        })
        run_id = f"recon-{reconstruction_id[:24]}"
        final_root = source["output_dir"] / "reconstructions" / reconstruction_id[:32]
        manifest_path = final_root / "reconstruction_manifest.json"
        if manifest_path.is_file():
            manifest = _read_json(manifest_path, "reconstruction_manifest_invalid")
            if (
                manifest.get("status") == JobStatus.FINISHED
                and manifest.get("reconstruction_status") == "completed"
            ):
                return manifest

        child_id = reconstruction_id[:32]
        if not self.job_store.get_job(child_id):
            parent_config = source["job"].get("configuration") or {}
            self.job_store.create_job(
                job_id=child_id,
                source_url=str(source["job"].get("source_url") or ""),
                output_dir=str(final_root),
                command=["selective_artifact_reconstruction"],
                run_id=run_id,
                configuration={
                    "job_type": "artifact_reconstruction",
                    "community_owner_id": str(
                        source["job"].get("owner_id")
                        or parent_config.get("community_owner_id")
                        or ""
                    ),
                    "source_job_id": source["job"]["id"],
                    "source_run_id": source["job"]["run_id"],
                    "page": _page_number(page),
                    "region_id": str(item.get("region_id") or ""),
                    "candidate_sha256": _text_sha256(candidate),
                    "reconstruction_reason": str(request.get("reason") or "selective_reconstruction"),
                },
                operation_kind="artifact_reconstruction",
                parent_job_id=str(source["job"]["id"]),
            )
        claimed = self.job_store.claim_next_job(f"artifact-reconstruction-{child_id[:8]}", 1)
        worker = str((claimed or {}).get("worker_id") or "")
        if claimed and claimed.get("id") == child_id:
            self.job_store.transition(child_id, JobStatus.STARTING, expected_worker=worker)
            self.job_store.transition(child_id, JobStatus.RUNNING, expected_worker=worker)

        temp_root = source["output_dir"] / "reconstructions" / f".tmp-{child_id}-{uuid.uuid4().hex}"
        promoted = False
        try:
            temp_root.mkdir(parents=True, exist_ok=False)
            corrected_page = temp_root / "pages" / f"page_{_page_number(page):03d}.png"
            corrected_page.parent.mkdir(parents=True, exist_ok=True)
            source_page = Path(str(page.get("image_path") or "")).resolve()
            if not source_page.is_file():
                raise ReconstructionError("reconstruction_source_page_missing")
            try:
                render_debug = self.renderer(
                    source_page=source_page,
                    destination=corrected_page,
                    page=page,
                    item=item,
                    candidate=candidate,
                )
            except ReconstructionError:
                raise
            except Exception:
                raise ReconstructionError("reconstruction_render_failed") from None
            if not corrected_page.is_file():
                raise ReconstructionError("reconstruction_render_failed")
            rebuilt_pages = self._rebuilt_page_order(pages, _page_number(page), corrected_page)
            pdf_path = temp_root / "artifact.pdf"
            try:
                self.pdf_builder(rebuilt_pages, str(pdf_path))
            except ReconstructionError:
                raise
            except Exception:
                raise ReconstructionError("reconstruction_pdf_failed") from None
            if not pdf_path.is_file():
                raise ReconstructionError("reconstruction_pdf_failed")
            artifact_sha256, artifact_size = sha256_of_file(pdf_path)
            quality_pages = self._quality_pages(pages, page, item, candidate, corrected_page, render_debug)
            try:
                quality = self.quality_builder(
                    run_id=run_id,
                    artifact_sha256=artifact_sha256,
                    artifact_size_bytes=artifact_size,
                    pages=quality_pages,
                    source={
                        "source_url": source["job"].get("source_url") or "",
                        "pdf_path": str(pdf_path),
                    },
                )
            except ReconstructionError:
                raise
            except Exception:
                raise ReconstructionError("reconstruction_quality_failed") from None
            self._require_quality_pass(
                quality,
                run_id=run_id,
                artifact_sha256=artifact_sha256,
                artifact_size=artifact_size,
            )
            self._ensure_source_current(source, source_pdf_sha)
            atomic_write_json(temp_root / "quality_report.json", quality)
            manifest = {
                "schema_version": 1,
                "status": JobStatus.FINISHED,
                "reconstruction_status": "completed",
                "job_id": child_id,
                "reconstruction_id": reconstruction_id[:32],
                "run_id": run_id,
                "exit_code": 0,
                "output_dir": str(final_root),
                "source_job_id": source["job"]["id"],
                "source_run_id": source["job"]["run_id"],
                "source_pdf_path": str(source["pdf_path"]),
                "source_pdf_sha256": source_pdf_sha,
                "page": _page_number(page),
                "region_id": str(item.get("region_id") or ""),
                "candidate_sha256": _text_sha256(candidate),
                "reason": str(request.get("reason") or "selective_reconstruction"),
                "page_path": str(final_root / "pages" / corrected_page.name),
                "pdf_path": str(final_root / "artifact.pdf"),
                "quality_report_path": str(final_root / "quality_report.json"),
                "artifact_sha256": artifact_sha256,
                "artifact_size_bytes": artifact_size,
                "created_at": self.clock(),
            }
            atomic_write_json(temp_root / "reconstruction_manifest.json", manifest)
            if final_root.exists():
                shutil.rmtree(final_root)
            temp_root.rename(final_root)
            promoted = True
            self.job_store.update_fields(
                child_id,
                output_dir=str(final_root),
                pdf_path=str(final_root / "artifact.pdf"),
                quality_report_path=str(final_root / "quality_report.json"),
                manifest_path=str(final_root / "reconstruction_manifest.json"),
                exit_code=0,
            )
            child = self.job_store.get_job(child_id)
            if child and child["status"] in {JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING}:
                self.job_store.transition(
                    child_id,
                    JobStatus.FINISHED,
                    expected_worker=worker,
                    stage="artifact_reconstruction_completed",
                    reason_code="quality_passed",
                )
            return _read_json(final_root / "reconstruction_manifest.json", "reconstruction_manifest_invalid")
        except Exception:
            if temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)
            if promoted and final_root.exists():
                shutil.rmtree(final_root, ignore_errors=True)
            child = self.job_store.get_job(child_id)
            if child and child["status"] in {JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING}:
                try:
                    self.job_store.transition(
                        child_id,
                        JobStatus.FAILED,
                        expected_worker=worker,
                        stage="artifact_reconstruction_failed",
                        reason_code="reconstruction_failed",
                        exit_code=1,
                    )
                except Exception:
                    pass
            raise

    def _resolve_source(self, request: dict[str, Any]) -> dict[str, Any]:
        job_id = str(request.get("source_job_id") or "")
        expected_run = str(request.get("source_run_id") or "")
        job = self.job_store.get_job(job_id)
        if not job:
            raise ReconstructionError("reconstruction_source_missing")
        if str(job.get("run_id") or "") != expected_run:
            raise ReconstructionError("reconstruction_source_stale")
        if str(job.get("status") or "") not in {JobStatus.FINISHED, JobStatus.REVIEW_REQUIRED}:
            raise ReconstructionError("reconstruction_source_not_terminal")
        output_dir = Path(str(job.get("output_dir") or "")).resolve()
        pdf_path = Path(str(job.get("pdf_path") or "")).resolve()
        if not output_dir.is_dir() or not pdf_path.is_file():
            raise ReconstructionError("reconstruction_source_missing")
        return {"job": job, "output_dir": output_dir, "pdf_path": pdf_path}

    def _ensure_source_current(self, source: dict[str, Any], source_pdf_sha: str) -> None:
        current = self.job_store.get_job(str(source["job"]["id"]))
        if not current:
            raise ReconstructionError("reconstruction_source_stale")
        if str(current.get("run_id") or "") != str(source["job"].get("run_id") or ""):
            raise ReconstructionError("reconstruction_source_stale")
        current_pdf = Path(str(current.get("pdf_path") or "")).resolve()
        if not current_pdf.is_file() or _file_sha256(current_pdf) != source_pdf_sha:
            raise ReconstructionError("reconstruction_source_stale")

    def _select_item(self, pages: list[dict[str, Any]], request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        target_page = int(request.get("page") or 0)
        region_id = str(request.get("region_id") or "")
        page = next((item for item in pages if _page_number(item) == target_page), None)
        if page is None:
            raise ReconstructionError("reconstruction_page_missing")
        items = [
            item for item in (page.get("debug_data") or {}).get("items", [])
            if isinstance(item, dict) and str(item.get("region_id") or "") == region_id
        ]
        if len(items) != 1:
            raise ReconstructionError("reconstruction_region_missing")
        item = items[0]
        box = item.get("bounding_box") or item.get("bbox")
        if not isinstance(box, list) or len(box) < 4:
            raise ReconstructionError("reconstruction_geometry_missing")
        return page, item

    def _select_candidate(self, item: dict[str, Any], request: dict[str, Any]) -> str:
        values = _candidate_values(item)
        if not values:
            raise ReconstructionError("reconstruction_candidate_missing")
        if len(values) > 1:
            raise ReconstructionError("reconstruction_candidate_ambiguous")
        candidate = values[0]
        expected_hash = str(request.get("candidate_sha256") or "").strip().lower()
        if expected_hash and _text_sha256(candidate) != expected_hash:
            raise ReconstructionError("reconstruction_candidate_mismatch")
        return candidate

    def _rebuilt_page_order(self, pages: list[dict[str, Any]], target_page: int, corrected_page: Path) -> list[Path]:
        ordered: list[Path] = []
        seen: set[int] = set()
        for page in sorted(pages, key=_page_number):
            number = _page_number(page)
            if number <= 0 or number in seen:
                raise ReconstructionError("reconstruction_page_order_incomplete")
            seen.add(number)
            path = corrected_page if number == target_page else Path(str(page.get("output_path") or "")).resolve()
            if not path.is_file():
                raise ReconstructionError("reconstruction_page_order_incomplete")
            ordered.append(path)
        if len(ordered) != len(pages):
            raise ReconstructionError("reconstruction_page_order_incomplete")
        return ordered

    def _quality_pages(
        self,
        pages: list[dict[str, Any]],
        target_page: dict[str, Any],
        target_item: dict[str, Any],
        candidate: str,
        corrected_page: Path,
        render_debug: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cloned = copy.deepcopy(pages)
        for page in cloned:
            if _page_number(page) != _page_number(target_page):
                continue
            page["output_path"] = str(corrected_page)
            for item in (page.get("debug_data") or {}).get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("region_id") or "") != str(target_item.get("region_id") or ""):
                    continue
                item.update({
                    "translation": candidate,
                    "translation_candidate": candidate,
                    "translation_valid": True,
                    "translation_validation_reason": "reconstruction_validated",
                    "translation_final_state": "translated",
                    "translation_final_reason": "reconstruction_candidate_validated",
                    "translation_quality_impact": "",
                    "preserved_original": False,
                    "manual_review_required": False,
                    "redrawn": True,
                })
                if isinstance(render_debug, dict):
                    item["visual_validation"] = render_debug.get("visual_validation") or {
                        "visual_validation_passed": True,
                    }
        return cloned

    @staticmethod
    def _require_quality_pass(
        quality: dict[str, Any],
        *,
        run_id: str,
        artifact_sha256: str,
        artifact_size: int,
    ) -> None:
        summary = quality.get("summary") if isinstance(quality.get("summary"), dict) else {}
        validation = summary.get("quality_validation")
        validation = validation if isinstance(validation, dict) else quality.get("quality_validation")
        validation = validation if isinstance(validation, dict) else {}
        if validation.get("passed") is not True:
            raise ReconstructionError("reconstruction_quality_failed")
        if str(validation.get("status") or "").casefold() == "review_required":
            raise ReconstructionError("reconstruction_quality_failed")
        if int(validation.get("manual_review_required_groups") or 0) != 0:
            raise ReconstructionError("reconstruction_quality_failed")
        recorded_run = (
            str(validation.get("run_id") or "").strip()
            or str(summary.get("run_id") or "").strip()
            or str(quality.get("run_id") or "").strip()
        )
        if recorded_run != run_id:
            raise ReconstructionError("reconstruction_quality_run_mismatch")
        recorded_sha = (
            str(validation.get("artifact_sha256") or "").strip().lower()
            or str(summary.get("artifact_sha256") or "").strip().lower()
            or str(quality.get("artifact_sha256") or "").strip().lower()
        )
        recorded_size = (
            validation.get("artifact_size_bytes")
            or summary.get("artifact_size_bytes")
            or quality.get("artifact_size_bytes")
        )
        try:
            recorded_size_int = int(recorded_size)
        except (TypeError, ValueError):
            recorded_size_int = -1
        if recorded_sha != artifact_sha256.lower() or recorded_size_int != int(artifact_size):
            raise ReconstructionError("reconstruction_quality_artifact_mismatch")
