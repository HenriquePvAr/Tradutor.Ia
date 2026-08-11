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

from benchmark_pipeline import _build_quality_report, _translation_quality_accounting, _validate_quality
from community_service import sha256_of_file
from job_store import JobStatus, JobStore
from ocr_balloon import validate_translation_text
from pdf import generate_pdf
from pipeline_cache import atomic_write_json, valid_image


_NO_CONTENT_PRECHECK_REASONS = frozenset({
    "nearly_flat_low_edges",
    "extreme_brightness_low_variation",
    "no_text_like_components",
})
_BLANK_EXCLUSION_REASONS = frozenset({"invalid_or_blank_logical_page"})


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


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return _read_json(path, "reconstruction_evidence_missing")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest, _size = sha256_of_file(path)
    return digest


def _file_identity(path: Path) -> dict[str, Any]:
    digest, size = sha256_of_file(path)
    return {"sha256": digest, "size_bytes": int(size)}


def _stable_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_strict_visual_validation(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict) or "visual_validation_passed" not in evidence:
        raise ReconstructionError("reconstruction_visual_evidence_missing")
    passed = evidence.get("visual_validation_passed")
    if passed is not True and passed is not False:
        raise ReconstructionError("reconstruction_visual_validation_invalid")
    return dict(evidence)


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


def _debug_items(page: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (page.get("debug_data") or {}).get("items", []) or []
        if isinstance(item, dict)
    ]


def _page_count_trace(timing_report: dict[str, Any]) -> dict[str, Any]:
    trace = timing_report.get("page_count_trace")
    return trace if isinstance(trace, dict) else {}


def _int_set(values: Any) -> set[int]:
    if not isinstance(values, list):
        return set()
    parsed: set[int] = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            parsed.add(number)
    return parsed


def _pdf_page_policy(pages: list[dict[str, Any]], timing_report: dict[str, Any]) -> dict[str, Any]:
    logical_pages = {_page_number(page) for page in pages if _page_number(page) > 0}
    trace = _page_count_trace(timing_report)
    excluded = _int_set(trace.get("excluded_logical_pages")) & logical_pages
    processed = _int_set(trace.get("processed_logical_pages")) & logical_pages
    included = processed if processed else logical_pages - excluded
    return {
        "included": included,
        "excluded": excluded,
        "exclusion_reason": str(trace.get("logical_page_exclusion_reason") or ""),
    }


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
    physical_quality = source.get("physical_quality") if isinstance(source.get("physical_quality"), dict) else {}
    quality_validation = _translation_quality_accounting(pages)
    quality_validation.update(physical_quality)
    quality_validation.update({
        "passed": bool(quality_validation.get("quality_passed") and physical_quality.get("passed") is True),
        "status": (
            "passed"
            if quality_validation.get("quality_passed") and physical_quality.get("passed") is True
            else "review_required"
        ),
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
        timing_report = _read_optional_json(source["output_dir"] / "timing_report.json")
        pages = [page for page in (progress.get("pages") or []) if isinstance(page, dict)]
        if not pages:
            raise ReconstructionError("reconstruction_page_order_missing")
        pdf_policy = _pdf_page_policy(pages, timing_report)
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
        page_provenance = self._collect_page_provenance(pages, _page_number(page), pdf_policy)
        page_provenance_digest = _stable_id({"pages": page_provenance})
        reconstruction_id = _stable_id({
            "source_job_id": source["job"]["id"],
            "source_run_id": source["job"]["run_id"],
            "source_pdf_sha256": source_pdf_sha,
            "page_provenance_digest": page_provenance_digest,
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
                    "page_provenance_digest": page_provenance_digest,
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
            page_snapshots = self._snapshot_reusable_pages(
                pages,
                target_page=_page_number(page),
                temp_root=temp_root,
                provenance=page_provenance,
            )
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
            rebuilt_pages = self._rebuilt_page_order(
                pages,
                _page_number(page),
                corrected_page,
                page_snapshots,
                page_provenance,
            )
            pdf_path = temp_root / "artifact.pdf"
            try:
                self.pdf_builder(rebuilt_pages, str(pdf_path))
            except ReconstructionError:
                raise
            except Exception:
                raise ReconstructionError("reconstruction_pdf_failed") from None
            if not pdf_path.is_file():
                raise ReconstructionError("reconstruction_pdf_failed")
            quality_pages = self._quality_pages(
                pages,
                page,
                item,
                candidate,
                corrected_page,
                render_debug,
                page_provenance,
            )
            physical_quality = self._validate_physical_quality(
                quality_pages,
                pdf_path=pdf_path,
                expected_page_count=len(quality_pages),
            )
            artifact_sha256, artifact_size = sha256_of_file(pdf_path)
            try:
                quality = self.quality_builder(
                    run_id=run_id,
                    artifact_sha256=artifact_sha256,
                    artifact_size_bytes=artifact_size,
                    pages=quality_pages,
                    source={
                        "source_url": source["job"].get("source_url") or "",
                        "pdf_path": str(final_root / "artifact.pdf"),
                        "expected_page_count": len(quality_pages),
                        "physical_quality": physical_quality,
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
            self._ensure_page_provenance_current(page_provenance)
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
                "page_provenance_digest": page_provenance_digest,
                "page_provenance": page_provenance,
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

    def _collect_page_provenance(
        self,
        pages: list[dict[str, Any]],
        target_page: int,
        pdf_policy: dict[str, Any],
    ) -> list[dict[str, Any]]:
        provenance: list[dict[str, Any]] = []
        included = set(pdf_policy.get("included") or set())
        excluded = set(pdf_policy.get("excluded") or set())
        for page in sorted(pages, key=_page_number):
            number = _page_number(page)
            if number <= 0:
                raise ReconstructionError("reconstruction_page_order_incomplete")
            output_path = Path(str(page.get("output_path") or "")).resolve()
            if number != target_page:
                if number in excluded:
                    identity = self._require_no_content_page_provenance(page, pdf_policy)
                    provenance.append({
                        "page": number,
                        "role": "reused_no_content_page",
                        "path": str(output_path),
                        "pdf_included": False,
                        **identity,
                    })
                    continue
                if number not in included:
                    raise ReconstructionError("reconstruction_page_provenance_mismatch")
                if not output_path.is_file() or not valid_image(str(output_path), 1, 1):
                    raise ReconstructionError("reconstruction_page_provenance_mismatch")
                identity = _file_identity(output_path)
                self._require_expected_page_identity(page, identity)
                provenance.append({
                    "page": number,
                    "role": "reused_rendered_page",
                    "path": str(output_path),
                    "pdf_included": True,
                    **identity,
                })
                continue
            source_path = Path(str(page.get("image_path") or "")).resolve()
            if not source_path.is_file() or not valid_image(str(source_path), 1, 1):
                raise ReconstructionError("reconstruction_source_page_missing")
            identity = _file_identity(source_path)
            provenance.append({
                "page": number,
                "role": "affected_source_page",
                "path": str(source_path),
                "pdf_included": True,
                **identity,
            })
        return provenance

    @classmethod
    def _require_no_content_page_provenance(
        cls,
        page: dict[str, Any],
        pdf_policy: dict[str, Any],
    ) -> dict[str, Any]:
        number = _page_number(page)
        included = set(pdf_policy.get("included") or set())
        excluded = set(pdf_policy.get("excluded") or set())
        if number <= 0 or number not in excluded or number in included:
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        reason = str(pdf_policy.get("exclusion_reason") or "")
        if reason and reason not in _BLANK_EXCLUSION_REASONS:
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        precheck = page.get("precheck") if isinstance(page.get("precheck"), dict) else {}
        if precheck.get("skip") is not True:
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        if str(precheck.get("reason") or "") not in _NO_CONTENT_PRECHECK_REASONS:
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        if str(page.get("cache_source") or "") != "no_text_precheck":
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        if _debug_items(page):
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        output_path = Path(str(page.get("output_path") or "")).resolve()
        source_path = Path(str(page.get("image_path") or "")).resolve()
        if not output_path.is_file() or not source_path.is_file():
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        rendered = _file_identity(output_path)
        source = _file_identity(source_path)
        cls._require_expected_page_identity(page, rendered)
        expected_source_hash = str(page.get("image_hash") or "").strip().lower()
        if expected_source_hash and expected_source_hash != str(source["sha256"]).lower():
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        if rendered != source:
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        return {
            **rendered,
            "source_path": str(source_path),
            "source_sha256": source["sha256"],
            "source_size_bytes": source["size_bytes"],
            "no_content_reason": str(precheck.get("reason") or ""),
        }

    @staticmethod
    def _require_expected_page_identity(page: dict[str, Any], actual: dict[str, Any]) -> None:
        expected_hash = str(
            page.get("output_sha256")
            or page.get("page_sha256")
            or page.get("sha256")
            or ""
        ).strip().lower()
        expected_size = (
            page.get("output_size_bytes")
            or page.get("page_size_bytes")
            or page.get("size_bytes")
        )
        if expected_hash and expected_hash != str(actual["sha256"]).lower():
            raise ReconstructionError("reconstruction_page_provenance_mismatch")
        if expected_size is not None:
            try:
                parsed_size = int(expected_size)
            except (TypeError, ValueError):
                raise ReconstructionError("reconstruction_page_provenance_mismatch") from None
            if parsed_size != int(actual["size_bytes"]):
                raise ReconstructionError("reconstruction_page_provenance_mismatch")

    @staticmethod
    def _snapshot_reusable_pages(
        pages: list[dict[str, Any]],
        *,
        target_page: int,
        temp_root: Path,
        provenance: list[dict[str, Any]],
    ) -> dict[int, Path]:
        expected_by_page = {
            int(item["page"]): item
            for item in provenance
            if item.get("role") in {"reused_rendered_page", "reused_no_content_page"}
        }
        snapshots: dict[int, Path] = {}
        snapshot_root = temp_root / "input_pages"
        snapshot_root.mkdir(parents=True, exist_ok=True)
        for page in pages:
            number = _page_number(page)
            if number == target_page:
                continue
            source = Path(str(page.get("output_path") or "")).resolve()
            expected = expected_by_page.get(number)
            if expected is None:
                raise ReconstructionError("reconstruction_page_provenance_missing")
            current = _file_identity(source)
            if (
                current["sha256"] != expected["sha256"]
                or int(current["size_bytes"]) != int(expected["size_bytes"])
            ):
                raise ReconstructionError("reconstruction_page_provenance_mismatch")
            source_path = expected.get("source_path")
            if source_path:
                source_identity = _file_identity(Path(str(source_path)).resolve())
                if (
                    source_identity["sha256"] != expected.get("source_sha256")
                    or int(source_identity["size_bytes"]) != int(expected.get("source_size_bytes") or -1)
                ):
                    raise ReconstructionError("reconstruction_page_provenance_mismatch")
            if expected.get("pdf_included") is False:
                continue
            destination = snapshot_root / f"page_{number:03d}{source.suffix or '.png'}"
            shutil.copyfile(source, destination)
            copied = _file_identity(destination)
            if copied != current:
                raise ReconstructionError("reconstruction_page_provenance_mismatch")
            snapshots[number] = destination
        return snapshots

    @staticmethod
    def _ensure_page_provenance_current(provenance: list[dict[str, Any]]) -> None:
        for item in provenance:
            path = Path(str(item.get("path") or "")).resolve()
            if not path.is_file():
                raise ReconstructionError("reconstruction_page_provenance_mismatch")
            current = _file_identity(path)
            if (
                current["sha256"] != item.get("sha256")
                or int(current["size_bytes"]) != int(item.get("size_bytes") or -1)
            ):
                raise ReconstructionError("reconstruction_page_provenance_mismatch")
            source_path = item.get("source_path")
            if source_path:
                source = _file_identity(Path(str(source_path)).resolve())
                if (
                    source["sha256"] != item.get("source_sha256")
                    or int(source["size_bytes"]) != int(item.get("source_size_bytes") or -1)
                ):
                    raise ReconstructionError("reconstruction_page_provenance_mismatch")

    @staticmethod
    def _validate_physical_quality(
        pages: list[dict[str, Any]],
        *,
        pdf_path: Path,
        expected_page_count: int,
    ) -> dict[str, Any]:
        try:
            quality = _validate_quality(pages, str(pdf_path), expected_page_count)
        except Exception:
            raise ReconstructionError("reconstruction_physical_quality_failed") from None
        if quality.get("passed") is not True:
            raise ReconstructionError("reconstruction_physical_quality_failed")
        return quality

    def _rebuilt_page_order(
        self,
        pages: list[dict[str, Any]],
        target_page: int,
        corrected_page: Path,
        page_snapshots: dict[int, Path],
        provenance: list[dict[str, Any]],
    ) -> list[Path]:
        ordered: list[Path] = []
        seen: set[int] = set()
        pdf_included = {
            int(item["page"])
            for item in provenance
            if item.get("pdf_included") is not False
        }
        for page in sorted(pages, key=_page_number):
            number = _page_number(page)
            if number <= 0 or number in seen:
                raise ReconstructionError("reconstruction_page_order_incomplete")
            seen.add(number)
            if number not in pdf_included:
                continue
            path = corrected_page if number == target_page else page_snapshots.get(number)
            if path is None:
                raise ReconstructionError("reconstruction_page_provenance_missing")
            if not path.is_file():
                raise ReconstructionError("reconstruction_page_order_incomplete")
            ordered.append(path)
        if len(ordered) != len(pdf_included):
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
        provenance: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        cloned = copy.deepcopy(pages)
        pdf_included = {
            int(item["page"])
            for item in provenance
            if item.get("pdf_included") is not False
        }
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
                visual_validation = render_debug.get("visual_validation") if isinstance(render_debug, dict) else None
                item["visual_validation"] = _require_strict_visual_validation(visual_validation)
        return [page for page in cloned if _page_number(page) in pdf_included]

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
