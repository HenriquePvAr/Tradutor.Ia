"""Artifact Contract V2 — the versioned foundation for Yomu Sekai output artifacts.

This module defines what a job may produce, where each artifact canonically belongs, how
artifacts are identified, which output profile requested them, and what this build can produce.
Artifact Contract V2 is consumed by the pipeline, canonical image/PSD exporters, and the
Professional QC review-manifest writer.

Capabilities reflect the REAL current implementation, never an aspiration:
  * FINAL_PDF / TRANSLATED_IMAGE / CLEAN_IMAGE / QUALITY_REPORT are produced today.
  * EDITABLE_PSD (native Photoshop Type Layers) is produced by ``editable_psd_writer`` in the
    PROFESSIONAL_QC profile (Mission 6); the existing raster export is a separate
    LEGACY_RASTER_PSD type and must never be reported as EDITABLE_PSD.
  * REVIEW_MANIFEST is generated for PROFESSIONAL_QC (Mission 10).

Legacy outputs (runs without ``contract_version``) are treated as V1 and stay readable.
"""
from __future__ import annotations

import re
from pathlib import Path

ARTIFACT_CONTRACT_VERSION = 2
LEGACY_CONTRACT_VERSION = 1


# --- artifact types ----------------------------------------------------------------------
class ArtifactType:
    # user-facing artifacts
    FINAL_PDF = "final_pdf"
    TRANSLATED_IMAGE = "translated_image"
    CLEAN_IMAGE = "clean_image"
    EDITABLE_PSD = "editable_psd"                 # native Type Layers -- SUPPORTED
    LEGACY_RASTER_PSD = "legacy_raster_psd"       # existing raster PSD -- distinct type
    QUALITY_REPORT = "quality_report"
    REVIEW_MANIFEST = "review_manifest"
    # internal / diagnostic artifacts
    OCR_PROVENANCE = "ocr_provenance"
    REGION_MANIFEST = "region_manifest"
    TRANSLATION_MANIFEST = "translation_manifest"
    DOWNLOAD_REPORT = "download_report"
    ERROR_REPORT = "error_report"
    RUN_MANIFEST = "run_manifest"


USER_ARTIFACTS = frozenset({
    ArtifactType.FINAL_PDF, ArtifactType.TRANSLATED_IMAGE, ArtifactType.CLEAN_IMAGE,
    ArtifactType.EDITABLE_PSD, ArtifactType.LEGACY_RASTER_PSD, ArtifactType.QUALITY_REPORT,
    ArtifactType.REVIEW_MANIFEST,
})
INTERNAL_DIAGNOSTIC_ARTIFACTS = frozenset({
    ArtifactType.OCR_PROVENANCE, ArtifactType.REGION_MANIFEST, ArtifactType.TRANSLATION_MANIFEST,
    ArtifactType.DOWNLOAD_REPORT, ArtifactType.ERROR_REPORT, ArtifactType.RUN_MANIFEST,
})
_KNOWN_TYPES = USER_ARTIFACTS | INTERNAL_DIAGNOSTIC_ARTIFACTS
# Artifacts identified per logical page (the rest are single per-run documents).
_PAGED_TYPES = frozenset({
    ArtifactType.TRANSLATED_IMAGE, ArtifactType.CLEAN_IMAGE,
    ArtifactType.EDITABLE_PSD, ArtifactType.LEGACY_RASTER_PSD,
})


# --- output profiles ---------------------------------------------------------------------
class OutputProfile:
    AUTOMATIC = "automatic"
    PROFESSIONAL_QC = "professional_qc"


PROFILE_ARTIFACTS = {
    OutputProfile.AUTOMATIC: (
        ArtifactType.FINAL_PDF, ArtifactType.TRANSLATED_IMAGE,
    ),
    OutputProfile.PROFESSIONAL_QC: (
        ArtifactType.FINAL_PDF, ArtifactType.TRANSLATED_IMAGE, ArtifactType.CLEAN_IMAGE,
        ArtifactType.EDITABLE_PSD, ArtifactType.QUALITY_REPORT, ArtifactType.REVIEW_MANIFEST,
    ),
}


# --- capabilities (REAL current implementation, verified 2026-09-28) ---------------------
class Capability:
    SUPPORTED = "supported"       # produced by the current build
    PLANNED = "planned"           # defined by the contract, not yet implemented
    UNAVAILABLE = "unavailable"   # not producible in this build/config


CAPABILITIES = {
    ArtifactType.FINAL_PDF: Capability.SUPPORTED,
    ArtifactType.TRANSLATED_IMAGE: Capability.SUPPORTED,
    # Persisted today as per-page ``*_cleaned.png``; canonical images/clean/ relocation is M4.
    ArtifactType.CLEAN_IMAGE: Capability.SUPPORTED,
    # Native Type-Layer PSD is implemented (Mission 6, editable_psd_writer). Physically verified
    # editable in Adobe Photoshop. Distinct from the raster export below.
    ArtifactType.EDITABLE_PSD: Capability.SUPPORTED,
    ArtifactType.LEGACY_RASTER_PSD: Capability.SUPPORTED,
    ArtifactType.QUALITY_REPORT: Capability.SUPPORTED,
    ArtifactType.REVIEW_MANIFEST: Capability.SUPPORTED,
    ArtifactType.OCR_PROVENANCE: Capability.SUPPORTED,
    ArtifactType.REGION_MANIFEST: Capability.SUPPORTED,
    ArtifactType.TRANSLATION_MANIFEST: Capability.SUPPORTED,
    ArtifactType.DOWNLOAD_REPORT: Capability.SUPPORTED,
    ArtifactType.ERROR_REPORT: Capability.SUPPORTED,
    ArtifactType.RUN_MANIFEST: Capability.SUPPORTED,
}


# --- artifact status (per run) -----------------------------------------------------------
class ArtifactStatus:
    REQUESTED = "requested"        # asked for, outcome not yet decided
    PRODUCED = "produced"          # written to disk this run
    SKIPPED = "skipped"            # not requested by this profile
    UNAVAILABLE = "unavailable"    # requested but the capability is planned/unavailable
    FAILED = "failed"             # requested and supported, but production errored


def capability_of(artifact_type: str) -> str:
    return CAPABILITIES.get(str(artifact_type), Capability.UNAVAILABLE)


def is_supported(artifact_type: str) -> bool:
    return capability_of(artifact_type) == Capability.SUPPORTED


def profile_artifacts(profile: str) -> tuple:
    if profile not in PROFILE_ARTIFACTS:
        raise ValueError(f"unknown output profile: {profile!r}")
    return PROFILE_ARTIFACTS[profile]


# --- logical page naming -----------------------------------------------------------------
def logical_page_name(page: int, total_pages: int | None = None) -> str:
    """Zero-padded logical page name. Width is >=3 and grows for chapters over 999 pages,
    so a 1000-page chapter is stable ("1000"), not truncated. Identity is the logical index,
    never a filesystem enumeration."""
    page = int(page)
    if page < 1:
        raise ValueError("logical page index must be >= 1")
    width = 3
    if total_pages:
        width = max(width, len(str(int(total_pages))))
    width = max(width, len(str(page)))
    return f"{page:0{width}d}"


# --- chapter slug safety -----------------------------------------------------------------
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul",
                     *(f"com{i}" for i in range(1, 10)),
                     *(f"lpt{i}" for i in range(1, 10))}


def safe_chapter_slug(chapter: str) -> str:
    """A filesystem-safe, deterministic slug. Unicode letters/digits are preserved; path
    separators, traversal and control characters cannot survive; Windows reserved device
    names are neutralized; length is bounded so a long title cannot produce an absurd path."""
    raw = str(chapter or "")
    # Replace anything that is not a Unicode word char, dot or hyphen with a single '_'.
    slug = re.sub(r"[^\w.-]+", "_", raw, flags=re.UNICODE)
    slug = re.sub(r"\.{2,}", "_", slug)          # no ".." runs
    slug = re.sub(r"_{2,}", "_", slug).strip("._-")
    if not slug:
        slug = "chapter"
    if slug.split(".")[0].lower() in _WINDOWS_RESERVED:
        slug = f"_{slug}"
    return slug[:80]


# --- canonical relative paths ------------------------------------------------------------
def artifact_relative_path(artifact_type: str, *, page: int | None = None,
                           total_pages: int | None = None, chapter: str = "chapter") -> str:
    """Canonical POSIX-style relative path for an artifact under the output root.

    Deterministic: the same (type, page, chapter) always yields the same path, so a retry or
    re-export overwrites in place instead of accumulating ``001_final_v2`` junk.
    """
    at = str(artifact_type)
    if at not in _KNOWN_TYPES:
        raise ValueError(f"unknown artifact type: {artifact_type!r}")
    if at in _PAGED_TYPES:
        if page is None:
            raise ValueError(f"{at} is a per-page artifact and requires a page index")
        name = logical_page_name(page, total_pages)
        if at == ArtifactType.TRANSLATED_IMAGE:
            return f"images/translated/{name}.png"
        if at == ArtifactType.CLEAN_IMAGE:
            return f"images/clean/{name}.png"
        # editable and legacy raster PSD both live under psd/ (distinct types, same slot)
        return f"psd/{name}.psd"
    if at == ArtifactType.FINAL_PDF:
        return f"final/{safe_chapter_slug(chapter)}_ptbr.pdf"
    if at == ArtifactType.QUALITY_REPORT:
        return "qc/quality_report.json"
    if at == ArtifactType.REVIEW_MANIFEST:
        return "qc/review_manifest.json"
    # internal / diagnostic
    return f"diagnostics/{at}.json"


def artifact_path(output_root, artifact_type: str, *, page: int | None = None,
                  total_pages: int | None = None, chapter: str = "chapter") -> Path:
    """Absolute path for an artifact, guaranteed to stay within ``output_root``."""
    rel = artifact_relative_path(artifact_type, page=page, total_pages=total_pages, chapter=chapter)
    root = Path(output_root).resolve()
    full = (root / rel).resolve()
    if full != root and root not in full.parents:
        raise ValueError("resolved artifact path escapes the output root")
    return full


# --- manifest ----------------------------------------------------------------------------
def build_artifact_manifest(*, output_profile: str, output_root, logical_page_count: int,
                            produced: dict | None = None, run_id: str = "",
                            status: str = "", created_at=None) -> dict:
    """Build the per-run artifact manifest (contract V2).

    ``produced`` maps artifact_type -> True when that artifact was actually written this run.
    Requested artifacts that are not supported by this build are reported as UNAVAILABLE, so
    a PROFESSIONAL_QC run before Mission 6 truthfully shows editable_psd as requested +
    unavailable rather than pretending it was produced.
    """
    requested = profile_artifacts(output_profile)
    produced = produced or {}
    produced_artifacts, unavailable_artifacts, statuses = [], [], {}
    for art in requested:
        if produced.get(art):
            statuses[art] = ArtifactStatus.PRODUCED
            produced_artifacts.append(art)
        elif capability_of(art) != Capability.SUPPORTED:
            statuses[art] = ArtifactStatus.UNAVAILABLE
            unavailable_artifacts.append(art)
        else:
            statuses[art] = ArtifactStatus.REQUESTED
    manifest = {
        "contract_version": ARTIFACT_CONTRACT_VERSION,
        "output_profile": output_profile,
        "output_root": str(output_root),
        "logical_page_count": int(logical_page_count),
        "requested_artifacts": list(requested),
        "produced_artifacts": produced_artifacts,
        "unavailable_artifacts": unavailable_artifacts,
        "artifact_status": statuses,
        "capabilities": {art: capability_of(art) for art in requested},
        "run_id": str(run_id or ""),
        "status": str(status or ""),
    }
    if created_at is not None:
        manifest["created_at"] = created_at
    return manifest


def contract_version_of(manifest: dict) -> int:
    """Version of an existing manifest; a run without the field is legacy V1 (never an error)."""
    if not isinstance(manifest, dict):
        return LEGACY_CONTRACT_VERSION
    try:
        return int(manifest.get("contract_version", LEGACY_CONTRACT_VERSION))
    except (TypeError, ValueError):
        return LEGACY_CONTRACT_VERSION


def is_legacy_manifest(manifest: dict) -> bool:
    return contract_version_of(manifest) < ARTIFACT_CONTRACT_VERSION
