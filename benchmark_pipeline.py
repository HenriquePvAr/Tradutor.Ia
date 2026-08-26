import json
import hashlib
import math
import os
import random
import re
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import cv2
from PIL import Image, ImageDraw, ImageFont, ImageOps

import config
import region_taxonomy
from classification_profiler import (
    ClassificationProfiler,
    profile_step,
    set_active_profiler,
)
from down import download_images, force_remove
from json_utils import dumps_json
from ocr_balloon import (
    OCR_UNINTELLIGIBLE_SOURCE_REASON,
    PROPER_NAME_ONLY_REASON,
    RENDER_CLEAN,
    RENDER_WITH_REVIEW,
    RESIDUAL_SOURCE_LETTERING_REASONS,
    REVIEW_TERMINAL_STATES,
    TRANSLATION_TERMINAL_STATES,
    analyze_image_array,
    apply_rapidocr_region_recovery,
    apply_selective_ocr_fallbacks,
    apply_speech_container_reocr,
    apply_group_translations,
    enforce_rapidocr_quality_gate,
    get_translatable_groups,
    normalize_recurring_compact_names,
    recover_protected_term_boundaries,
    summarize_speech_container_reocr,
    render_analyzed_image,
    validate_and_retry_translations,
)
import ocr_line_provenance
import semantic_fidelity
import source_completeness
from ocr_parallel import detect_ocr_jobs
from ocr_engine import OCREngine
from fast_ocr_policy import FastOCRBudget
from pdf import (
    create_split_boundary_contact_sheet,
    generate_pdf,
    prepare_smart_webtoon_pages,
    smart_split_audit,
)
from pipeline_cache import (
    atomic_copy,
    atomic_write_json,
    cache_folder,
    file_sha256,
    fingerprint_files,
    load_json,
    load_ocr_cache,
    load_processed_cache,
    no_text_precheck,
    ocr_cache_key,
    processed_cache_key,
    save_ocr_cache,
    save_processed_cache,
    stable_hash,
    valid_image,
)
from session_context import SessionContextStore
from output_manifest import (
    build_run_manifest,
    sanitize_source_provenance,
    sanitize_source_url,
)
from pdf_naming import (
    build_pdf_filename,
    episode_number_from_url,
    series_slug_from_url,
)
from translator_nllb import get_translator
from translator_nvidia import PROMPT_VERSION
from resource_monitor import ResourceMonitor, detect_gpu_basic
from ui_helpers import derive_final_run_status, sanitize_diagnostic_text


BASELINE_SECONDS = 2129.41
OUTPUT_FOLDER = Path("output/full_chapter")
PIPELINE_FILES = (
    "ocr_balloon.py",
    "ocr_engine.py",
    "translator_nvidia.py",
    "pdf.py",
)
PIPELINE_MANIFEST_VERSION = "benchmark-pipeline-v1"


def _git_metadata():
    def resolve(*args):
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    return {
        "commit_hash": resolve("rev-parse", "HEAD"),
        "branch": resolve("branch", "--show-current"),
    }


def _first_source_value(*values):
    """Return the first explicitly supplied source diagnostic without treating zero as absent."""

    for value in values:
        if value is not None and value != "":
            return value
    return None


def _source_manifest_provenance(download_report):
    """Reduce a downloader report to URL/path-free scalar run evidence.

    The full downloader report is useful only inside its own restricted output directory and
    contains opaque candidate ids plus per-image information.  The canonical run manifest is
    an index/discovery artifact, so it records counts and decisions rather than a replayable
    source manifest.  ``sanitize_source_provenance`` is the final allowlist before writing.
    """

    report = download_report if isinstance(download_report, dict) else {}
    analysis = report.get("source_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    selection = report.get("source_selection")
    selection = selection if isinstance(selection, dict) else {}

    candidate_ids = selection.get("candidate_ids")
    selected_count_from_ids = (
        len(candidate_ids) if isinstance(candidate_ids, (list, tuple)) else None
    )
    raw_selection = {
        "automatic": selection.get("automatic"),
        "selected_page_count": _first_source_value(
            selection.get("fresh_candidate_count"),
            selection.get("selected_candidate_count"),
            selection.get("confirmed_candidate_count"),
            selected_count_from_ids,
        ),
        "accepted_candidate_count": _first_source_value(
            selection.get("accepted_candidate_count"),
            selection.get("observed_candidate_count"),
            analysis.get("accepted_count"),
        ),
        "manual_subset": selection.get("manual_subset"),
        "manual_reordered": selection.get("manual_reordered"),
        "reason_code": _first_source_value(
            selection.get("reason_code"),
            report.get("source_reason"),
            report.get("source_outcome"),
            analysis.get("outcome"),
        ),
    }
    raw_provenance = {
        "source_type": report.get("source_type"),
        "adapter_name": _first_source_value(
            report.get("adapter_name"), analysis.get("adapter")
        ),
        "adapter_version": _first_source_value(
            report.get("adapter_version"), analysis.get("adapter_version")
        ),
        "transport_name": report.get("transport_name"),
        "score": _first_source_value(
            report.get("source_score"),
            report.get("source_confidence"),
            analysis.get("score"),
            analysis.get("confidence"),
        ),
        "candidate_count": _first_source_value(
            report.get("candidate_count"), analysis.get("candidate_count")
        ),
        "accepted_page_count": _first_source_value(
            report.get("accepted_count"), analysis.get("accepted_count")
        ),
        "rejected_page_count": _first_source_value(
            report.get("rejected_page_count"),
            report.get("discarded_count"),
            analysis.get("rejected_count"),
            analysis.get("discarded_count"),
        ),
        "outcome": _first_source_value(
            report.get("source_outcome"), analysis.get("outcome")
        ),
        "selection": raw_selection,
    }
    return sanitize_source_provenance(raw_provenance)


def resolve_provider_provenance(translator, requested_provider=""):
    from ui_helpers import normalize_translation_provider

    requested = normalize_translation_provider(requested_provider)
    stats = getattr(translator, "stats", {})
    effective = str(stats.get("provider_name") or "").strip().lower()
    if requested and effective != requested:
        raise RuntimeError("provider_mismatch")
    if requested:
        stats["provider_requested"] = requested
        stats["provider_source"] = "run_argument"
    else:
        # A run nobody asked a provider for stays *unrequested*.  Copying the effective
        # provider here is what let a lost --translation-provider flag self-certify as
        # "requested=nemotron, effective=nemotron, mismatch=false" for a riva job.
        stats.setdefault("provider_source", "runtime_default")
    stats["provider_effective"] = effective
    stats["provider_fallback_used"] = False
    stats["provider_fallback_reason"] = ""
    return report_provider_provenance(stats)


def report_provider_provenance(translator_stats):
    """Provenance of a finished run, derived only from what was actually requested.

    ``provider_requested`` is an immutable job identity: no runtime default, environment
    variable, CLI default or effective provider may become a request after the fact.
    """
    stats = translator_stats if isinstance(translator_stats, dict) else {}
    requested = str(stats.get("provider_requested") or "").strip().lower()
    effective = str(
        stats.get("provider_effective") or stats.get("provider_name") or ""
    ).strip().lower()
    provenance = {
        "provider_requested": requested,
        "provider_effective": effective,
        "provider_model": stats.get("model", config.NVIDIA_TRANSLATION_MODEL),
        "provider_source": stats.get("provider_source", ""),
        "provider_fallback_used": bool(stats.get("provider_fallback_used", False)),
        "provider_fallback_reason": str(stats.get("provider_fallback_reason", "")),
        "provider_mismatch": bool(requested and effective and requested != effective),
    }
    # Reported only when the provider actually publishes them, so a run can never
    # invent a model type it never asked for or was never told about.  DeepL is
    # the first provider to distinguish the two; the fields stay absent for the
    # NVIDIA family rather than being back-filled from configuration.
    for key in ("provider_family", "model_type_requested", "model_type_used"):
        value = str(stats.get(key) or "").strip()
        if value:
            provenance[
                key if key == "provider_family" else f"provider_{key}"
            ] = value
    return provenance


def _safe_count(value):
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return count if count >= 0 else None


def _first_positive_count(*values):
    for value in values:
        count = _safe_count(value)
        if count and count > 0:
            return count
    return 0


def _expected_download_count(manifest):
    """Return the source/download count declared by a downloader manifest."""

    report = manifest if isinstance(manifest, dict) else {}
    gate = report.get("download_gate")
    gate = gate if isinstance(gate, dict) else {}
    analysis = report.get("source_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    selection = report.get("source_selection")
    selection = selection if isinstance(selection, dict) else {}
    expected_ids = report.get("expected_chapter_candidate_ids")
    selected_ids = selection.get("candidate_ids")
    return _first_positive_count(
        gate.get("expected_viewer_images"),
        report.get("expected_viewer_images"),
        report.get("viewer_image_count"),
        report.get("accepted_count"),
        analysis.get("accepted_count"),
        len(expected_ids) if isinstance(expected_ids, (list, tuple)) else None,
        len(selected_ids) if isinstance(selected_ids, (list, tuple)) else None,
        selection.get("fresh_candidate_count"),
        selection.get("selected_candidate_count"),
        selection.get("confirmed_candidate_count"),
    )


def _download_cache_is_complete(manifest, cached_paths):
    """A cache hit is valid only if it is explicitly complete for its source."""

    report = manifest if isinstance(manifest, dict) else {}
    gate = report.get("download_gate")
    if isinstance(gate, dict) and gate and not gate.get("passed", False):
        return False
    expected = _expected_download_count(report)
    actual = len(cached_paths or [])
    if expected and actual < expected:
        return False
    total = _safe_count(report.get("total_downloaded"))
    if total is not None and actual < total:
        return False
    return bool(actual)


def _page_count_trace(download_report, *, all_image_paths, source_image_paths,
                      image_paths, completed_states, smart_split_report):
    split = smart_split_report if isinstance(smart_split_report, dict) else {}
    logical_count = len(image_paths or [])
    processed_indices = sorted(
        {
            int(state.get("index"))
            for state in (completed_states or [])
            if isinstance(state, dict) and state.get("index") is not None
        }
    )
    processed_index_set = set(processed_indices)
    excluded_logical_pages = [
        index
        for index in range(1, logical_count + 1)
        if index not in processed_index_set
    ]
    return {
        "source_expected": _expected_download_count(download_report),
        "downloaded": len(all_image_paths or []),
        "source_images": len(source_image_paths or []),
        "logical_pages": logical_count,
        "processed_pages": len(completed_states or []),
        "processed_logical_pages": processed_indices,
        "excluded_logical_pages": excluded_logical_pages,
        "logical_page_exclusion_reason": (
            "invalid_or_blank_logical_page" if excluded_logical_pages else ""
        ),
        "smart_split_enabled": bool(split.get("enabled")),
        "smart_split_source_images": _safe_count(split.get("source_images")),
        "smart_split_pdf_pages": _safe_count(split.get("pdf_pages")),
        "smart_split_unsafe_count": _safe_count(split.get("unsafe_split_count")),
    }


def _group_count_trace(page_states):
    """Return count-only OCR/grouping provenance without exposing OCR text."""

    states = list(page_states or [])
    trace = {
        "logical_pages": len(states),
        "detector_inputs": 0,
        "regions_detected": 0,
        "ocr_inputs": 0,
        "ocr_executed": 0,
        "ocr_nonempty": 0,
        "candidate_regions": 0,
        "groups_created": 0,
        "groups_filtered": 0,
        "translation_groups": 0,
        "groups_persisted": 0,
        "pages_without_text": 0,
        "pages_with_ocr_error": 0,
    }
    for state in states:
        raw_lines = list(state.get("raw_lines") or [])
        candidates = list(state.get("candidates") or [])
        groups = list(state.get("groups") or [])
        translatable = list(state.get("translatable_groups") or [])
        debug = state.get("debug_data") if isinstance(state.get("debug_data"), dict) else {}
        metadata = state.get("ocr_metadata") if isinstance(state.get("ocr_metadata"), dict) else {}
        raw_line_count = len(raw_lines)
        if not raw_line_count:
            raw_line_count = int(debug.get("ocr_line_count") or 0)

        skipped_without_text = state.get("status") == "completed" and bool(
            (state.get("precheck") or {}).get("skipped_no_text")
            or state.get("precheck_reason")
        )
        trace["detector_inputs"] += 1
        trace["ocr_inputs"] += 0 if skipped_without_text else 1
        if (
            state.get("ocr_source") in {"run", "cache"}
            or raw_line_count
            or state.get("ocr_completed")
        ):
            trace["ocr_executed"] += 1
        if raw_line_count:
            trace["ocr_nonempty"] += 1
        elif not groups and not int(debug.get("group_count") or 0):
            trace["pages_without_text"] += 1
        if state.get("ocr_error"):
            trace["pages_with_ocr_error"] += 1

        estimated_regions = _safe_count(metadata.get("estimated_text_regions"))
        trace["regions_detected"] += (
            estimated_regions if estimated_regions is not None else raw_line_count
        )
        trace["candidate_regions"] += (
            len(candidates) if candidates else int(debug.get("ocr_line_count") or 0)
        )
        created = len(groups) if groups else int(debug.get("group_count") or 0)
        translation_count = (
            len(translatable)
            if translatable
            else int(debug.get("translated_group_count") or 0)
        )
        trace["groups_created"] += created
        trace["translation_groups"] += translation_count
        trace["groups_filtered"] += max(0, created - translation_count)
        trace["groups_persisted"] += int(debug.get("group_count") or created)
    return trace


def _output_run_manifest(output_folder, report, translator):
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    git = _git_metadata()
    run_id = str(report.get("job_run_id") or "").strip()
    if not run_id:
        run_id = stable_hash(
            {
                "run_signature": report.get("run_signature"),
                "output_folder": str(output_folder),
                "created_at": created_at,
            }
        )[:24]
    quality = report.get("quality_validation") or {}
    source_url = str(report.get("url") or "")
    source_type = str(report.get("source_type") or "url")
    return build_run_manifest(
        run_id=run_id,
        created_at=created_at,
        source_url=source_url,
        commit_hash=git["commit_hash"],
        branch=git["branch"],
        pipeline_version=PIPELINE_MANIFEST_VERSION,
        model=str(getattr(translator, "model", "") or "unknown"),
        final_status=str(report.get("status") or ""),
        quality_passed=bool(quality.get("passed")),
        manual_review_count=int(quality.get("manual_review_required_groups") or 0),
        rejected_count=int(report.get("translation_rejections") or 0),
        pdf_path=str(report.get("pdf_path") or ""),
        # ``source_url`` is deliberately sanitised before the timing report is written,
        # so it cannot safely yield a semantic series/chapter identifier.  The run slug is
        # the output directory identity; optional series metadata stays empty unless a
        # future trusted source supplies it separately.
        slug=Path(output_folder).name,
        series_slug=str(report.get("series_slug") or ""),
        episode_number=str(report.get("episode_number") or ""),
        source_type=source_type,
        adapter_name=str(report.get("adapter_name") or ""),
        adapter_version=str(report.get("adapter_version") or ""),
        transport_name=str(report.get("transport_name") or ""),
        source_provenance=report.get("source_provenance"),
        provider_provenance=report.get("provider_provenance"),
        quality_validation=quality,
        ocr_line_provenance=report.get("ocr_line_provenance"),
    )


def _sha256_and_size(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


class ImmutableArtifactConflict(RuntimeError):
    """Raised when a terminal artifact path already contains different bytes."""


def _finalize_immutable_file(temp_path, final_path):
    """Promote ``temp_path`` to ``final_path`` without overwriting different bytes.

    Re-running the same logical run with byte-identical output is idempotent.  Producing
    different bytes for an existing terminal artifact fails closed because overwriting it
    would erase the previous run's audit evidence.
    """

    temp = Path(temp_path).resolve()
    final = Path(final_path).resolve()
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        temp.relative_to(final.parent)
    except ValueError as exc:
        raise ImmutableArtifactConflict("temporary_artifact_outside_run_folder") from exc
    if final.exists():
        if not final.is_file():
            raise ImmutableArtifactConflict("artifact_path_not_file")
        temp_hash, temp_size = _sha256_and_size(temp)
        final_hash, final_size = _sha256_and_size(final)
        if temp_hash == final_hash and temp_size == final_size:
            try:
                temp.unlink()
            except OSError:
                pass
            return {"status": "idempotent", "sha256": final_hash, "size": final_size}
        raise ImmutableArtifactConflict("artifact_already_exists_with_different_bytes")
    temp.replace(final)
    final_hash, final_size = _sha256_and_size(final)
    return {"status": "created", "sha256": final_hash, "size": final_size}


def run_benchmark(args):
    started = time.perf_counter()
    # Line provenance is collected for the whole run: raw OCR lines, every list
    # replacement between passes, group membership and the exact renderer input.
    ocr_line_provenance.activate()
    output_folder = Path(getattr(args, "output_folder", OUTPUT_FOLDER)).resolve()
    pages_folder = output_folder / "pages"
    errors_folder = output_folder / "errors"
    diagnostic_folder = output_folder / "debug"
    progress_path = output_folder / "progress.json"
    timing_json_path = output_folder / "timing_report.json"
    timing_txt_path = output_folder / "timing_report.txt"
    selected_page_indices = _parse_page_indices(getattr(args, "page_indices", ""))
    targeted_regression = bool(selected_page_indices)
    contact_sheet_path = (
        output_folder / "regression_contact_sheet.jpg"
        if targeted_regression
        else output_folder / "preview_contact_sheet.jpg"
    )
    compare_sheet_path = (
        output_folder / "regression_compare_sheet.jpg"
        if targeted_regression
        else output_folder / "preview_compare_sheet.jpg"
    )
    quality_json_path = (
        output_folder / "regression_report.json"
        if targeted_regression
        else output_folder / "quality_report.json"
    )
    quality_html_path = (
        output_folder / "regression_report.html"
        if targeted_regression
        else output_folder / "quality_report.html"
    )
    context_enabled = bool(getattr(args, "use_context", False))
    fast_ocr_budget = FastOCRBudget.from_config(fast=bool(getattr(args, "fast", False)))
    session_context_path = Path(
        getattr(args, "session_context_path", output_folder / "session_context.json")
    ).resolve()
    session_context = (
        SessionContextStore(session_context_path, args.url)
        if context_enabled
        else None
    )

    output_folder.mkdir(parents=True, exist_ok=True)
    resource_monitor = ResourceMonitor(
        output_folder,
        enabled=config.RESOURCE_MONITORING,
        interval_seconds=config.RESOURCE_MONITOR_INTERVAL_SECONDS,
    )
    classification_profiler = ClassificationProfiler(
        enabled=config.CLASSIFICATION_PROFILING,
    )
    set_active_profiler(classification_profiler)
    resource_monitor.start()
    resource_monitor.set_stage("preparing")
    if args.force:
        _reset_generated_folders(pages_folder, errors_folder)
        if targeted_regression:
            _reset_generated_folders(diagnostic_folder)
    pages_folder.mkdir(parents=True, exist_ok=True)
    errors_folder.mkdir(parents=True, exist_ok=True)

    effective_debug = bool(config.DEBUG_VISUAL and not args.fast)
    max_images = None if args.full else args.max_images
    local_manifest_path = str(getattr(args, "local_manifest_path", "") or "")
    download_max_images = _resolve_download_max_images(
        max_images,
        selected_page_indices,
        local_manifest_path,
        getattr(args, "source_candidate_ids", []) or [],
    )
    run_signature = stable_hash(
        {
            "url": args.url,
            "max_images": max_images,
            "full": args.full,
            "fast": args.fast,
            "page_indices": selected_page_indices,
            "source_candidate_ids": list(getattr(args, "source_candidate_ids", []) or []),
            "pipeline": "benchmark-v2-smart-pages",
            "ocr_engine": config.OCR_ENGINE,
            "ocr_fallback_engine": config.OCR_FALLBACK_ENGINE,
            "ocr_hybrid_fallback": config.OCR_HYBRID_FALLBACK,
            "fast_ocr_mode": bool(getattr(config, "FAST_OCR_MODE", False)),
            "fast_ocr_heavy_fallback": bool(getattr(config, "FAST_OCR_HEAVY_FALLBACK", False)),
            "fast_ocr_region_timeout": float(getattr(config, "FAST_OCR_REGION_TIMEOUT_SECONDS", 0.0)),
            "fast_ocr_full_fallback_max_pages": int(
                getattr(config, "FAST_OCR_FULL_FALLBACK_MAX_PAGES", 0)
            ),
            "fast_ocr_full_fallback_max_regions": int(
                getattr(config, "FAST_OCR_FULL_FALLBACK_MAX_REGIONS", 0)
            ),
            "smart_webtoon_pdf_split": config.SMART_WEBTOON_PDF_SPLIT,
            "smart_pdf_target_height": config.SMART_PDF_TARGET_HEIGHT,
            "smart_pdf_min_height": config.SMART_PDF_MIN_HEIGHT,
            "smart_pdf_max_height": config.SMART_PDF_MAX_HEIGHT,
        }
    )
    pipeline_fingerprint = fingerprint_files(PIPELINE_FILES)
    relevant_config = _relevant_output_config(pipeline_fingerprint)

    # Console output is captured by the job runner, so never place signed reader queries in
    # it. The raw URL remains in memory only for the actual, validated download.
    print(f"Benchmark: {sanitize_source_url(args.url)}", flush=True)
    print(f"Imagens: {'capitulo completo' if args.full else max_images}", flush=True)
    if selected_page_indices:
        print(f"Paginas selecionadas: {selected_page_indices}", flush=True)
    print(f"Fast: {'sim' if args.fast else 'nao'}", flush=True)
    print(f"Force: {'sim' if args.force else 'nao'}", flush=True)
    print(f"Saida: {output_folder}", flush=True)

    resource_monitor.set_stage("downloading")
    download_started = time.perf_counter()
    all_image_paths, download_report, download_cache_hit = _download_with_cache(
        args.url,
        download_max_images,
        output_folder,
        force=bool(getattr(args, "force_download", False)),
        source_candidate_ids=list(getattr(args, "source_candidate_ids", []) or []),
        local_manifest_path=local_manifest_path,
    )
    download_wall_seconds = time.perf_counter() - download_started
    if not all_image_paths:
        raise RuntimeError("Nenhuma imagem valida encontrada para o benchmark.")
    download_gate = download_report.get("download_gate") or {}
    if download_gate and not download_gate.get("passed", False):
        raise RuntimeError(
            "Download gate reprovado: "
            + ", ".join(download_gate.get("reasons") or ["motivo desconhecido"])
        )
    resource_monitor.set_stage("validation")
    source_selection = [] if (
        config.SMART_WEBTOON_PDF_SPLIT
        and bool(download_report.get("requires_smart_split", True))
    ) else selected_page_indices
    source_entries, missing_page_indices = _select_image_entries(
        all_image_paths,
        source_selection,
    )
    if missing_page_indices:
        print(
            "Aviso: paginas selecionadas indisponiveis apos download: "
            + ",".join(str(item) for item in missing_page_indices),
            flush=True,
        )
    if not source_entries:
        raise RuntimeError("Nenhuma pagina selecionada estava disponivel para processar.")
    source_image_paths = [entry["path"] for entry in source_entries]
    smart_split_report = {
        "enabled": False,
        "reason": "disabled",
        "source_images": len(source_image_paths),
        "pdf_pages": len(source_image_paths),
        "unsafe_split_count": 0,
    }
    smart_split_seconds = 0.0
    smart_split_contact_sheet = output_folder / "smart_split_contact_sheet.jpg"
    should_rebuild_pages = bool(
        config.SMART_WEBTOON_PDF_SPLIT
        and download_report.get("viewer_image_count")
        and download_report.get("requires_smart_split", True)
    )
    if should_rebuild_pages:
        resource_monitor.set_stage("smart_split")
        split_started = time.perf_counter()
        smart_paths, smart_split_report = prepare_smart_webtoon_pages(
            source_image_paths,
            output_folder / "smart_input_pages",
            target_height=config.SMART_PDF_TARGET_HEIGHT,
            min_height=config.SMART_PDF_MIN_HEIGHT,
            max_height=config.SMART_PDF_MAX_HEIGHT,
        )
        smart_split_seconds = time.perf_counter() - split_started
        smart_split_report = {"enabled": True, **smart_split_report}
        create_split_boundary_contact_sheet(
            smart_paths,
            smart_split_report,
            smart_split_contact_sheet,
        )
        if selected_page_indices:
            image_entries, missing_page_indices = _select_image_entries(
                smart_paths,
                selected_page_indices,
            )
            selected_smart_paths = [entry["path"] for entry in image_entries]
        else:
            selected_smart_paths = (
                smart_paths if args.full else smart_paths[: max(0, int(max_images or 0))]
            )
            image_entries, _ = _select_image_entries(selected_smart_paths, [])
        print(
            "Smart split: "
            f"{len(source_image_paths)} fatias -> {len(smart_paths)} paginas logicas "
            f"({len(selected_smart_paths)} selecionadas; "
            f"{smart_split_report.get('unsafe_split_count', 0)} cortes de baixo risco)",
            flush=True,
        )
    else:
        smart_split_report["reason"] = (
            "targeted_page_selection" if selected_page_indices else "disabled"
        )
        image_entries = source_entries
    image_paths = [entry["path"] for entry in image_entries]
    resource_monitor.set_progress(pages_done=0, pages_total=len(image_paths))

    previous_progress = load_json(progress_path, default={})
    previous_records = {}
    if not args.force and previous_progress.get("run_signature") == run_signature:
        previous_records = {
            int(record["index"]): record
            for record in previous_progress.get("pages", [])
            if str(record.get("index", "")).isdigit()
        }

    from ui_helpers import normalize_translation_provider

    requested_provider = normalize_translation_provider(
        getattr(args, "translation_provider", ""))
    translator, ocr_lang = get_translator(
        "3", translation_provider=requested_provider or None
    )
    if hasattr(translator, "force_cache"):
        translator.force_cache = bool(args.force)
    resolve_provider_provenance(translator, requested_provider)

    counters = {
        "images_skipped_by_cache": 0,
        "images_skipped_by_no_text_precheck": 0,
        "ocr_runs": 0,
        "ocr_cache_hits": 0,
        "ocr_page_fallbacks": 0,
        "ocr_region_fallbacks": 0,
        "ocr_text_repairs": 0,
        "translation_retries": 0,
        "translation_rejections": 0,
        "visual_validation_failures": 0,
        "pages_with_error": 0,
    }
    validation_seconds = (
        0.0
        if download_cache_hit
        else float(download_report.get("timings", {}).get("validation_seconds", 0.0))
    )
    stage_seconds = {
        "download_collection": max(0.0, download_wall_seconds - validation_seconds),
        "image_validation": validation_seconds,
        "smart_pdf_split": smart_split_seconds,
        "no_text_precheck": 0.0,
        "ocr": 0.0,
        "ocr_cpu": 0.0,
        "ocr_selective_fallback": 0.0,
        "classification_grouping": 0.0,
        "translation": 0.0,
        "inpainting": 0.0,
        "redraw": 0.0,
        "image_save": 0.0,
        "pdf": 0.0,
        "cache_load": 0.0,
    }

    page_states = []
    ocr_jobs = []
    resource_monitor.set_stage("precheck")
    for entry in image_entries:
        index = int(entry["index"])
        image_path = entry["path"]
        image_hash = file_sha256(image_path)
        process_key = processed_cache_key(
            image_hash,
            pipeline_fingerprint,
            relevant_config,
        )
        output_path = pages_folder / f"page_{index:03}.png"
        state = {
            "index": index,
            "sequence_index": int(entry.get("sequence_index", index)),
            "original_index": int(entry.get("original_index", index)),
            "image_path": str(image_path),
            "image_hash": image_hash,
            "process_key": process_key,
            "output_path": str(output_path),
            "ocr_lang": ocr_lang,
            "timings": {},
        }

        cache_started = time.perf_counter()
        reused = _reuse_completed_page(
            state,
            previous_records.get(index),
            force=args.force,
        )
        stage_seconds["cache_load"] += time.perf_counter() - cache_started
        if reused:
            counters["images_skipped_by_cache"] += 1
            page_states.append(state)
            print(f"Pagina {index}/{len(image_paths)}: cache processado", flush=True)
            continue

        precheck_started = time.perf_counter()
        precheck = {
            "skip": False,
            "reason": "disabled_or_debug",
            "metrics": {},
        }
        if config.SKIP_NO_TEXT_IMAGES and not effective_debug:
            precheck = no_text_precheck(
                image_path,
                image_hash=image_hash,
                force=args.force,
            )
        precheck_elapsed = time.perf_counter() - precheck_started
        stage_seconds["no_text_precheck"] += precheck_elapsed
        state["precheck"] = precheck
        state["timings"]["no_text_precheck"] = precheck_elapsed

        if precheck.get("skip"):
            copy_started = time.perf_counter()
            atomic_copy(image_path, output_path)
            copy_elapsed = time.perf_counter() - copy_started
            stage_seconds["image_save"] += copy_elapsed
            state["timings"]["image_save"] = copy_elapsed
            state["status"] = "completed"
            state["ocr_completed"] = True
            state["cache_source"] = "no_text_precheck"
            state["debug_data"] = _empty_debug_data(
                image_path,
                precheck_reason=precheck.get("reason"),
            )
            counters["images_skipped_by_no_text_precheck"] += 1
            _save_page_processed_cache(state)
            page_states.append(state)
            _write_progress(
                progress_path,
                run_signature,
                args,
                len(image_paths),
                page_states,
            )
            print(
                f"Pagina {index}/{len(image_paths)}: OCR pulado "
                f"({precheck.get('reason')})",
                flush=True,
            )
            continue

        key = ocr_cache_key(image_hash, ocr_lang)
        state["ocr_cache_key"] = key
        cached_ocr = (
            load_ocr_cache(key)
            if config.ENABLE_OCR_CACHE and not args.force
            else None
        )
        if cached_ocr is not None:
            state["raw_lines"] = cached_ocr[0]
            state["ocr_metadata"] = cached_ocr[1].get("ocr_metadata", {})
            state["ocr_source"] = "cache"
            state["timings"]["ocr"] = 0.0
            counters["ocr_cache_hits"] += 1
        else:
            ocr_jobs.append({"index": index, "image_path": str(image_path)})
            state["ocr_source"] = "run"
        page_states.append(state)

    state_by_index = {state["index"]: state for state in page_states}

    def _persist_ocr_result(result):
        """Checkpoint one OCR page as soon as its worker returns."""
        state = state_by_index.get(int(result.get("index", -1)))
        if state is None:
            return
        state["raw_lines"] = result.get("lines", [])
        state["ocr_metadata"] = result.get("ocr_metadata", {})
        state["ocr_completed"] = True
        elapsed = float(result.get("elapsed_seconds", 0.0))
        state["timings"]["ocr"] = elapsed
        if result.get("error"):
            state["ocr_error"] = result["error"]
        elif config.ENABLE_OCR_CACHE:
            save_ocr_cache(
                state["ocr_cache_key"], state["image_hash"], ocr_lang,
                state["raw_lines"], elapsed, state.get("precheck", {}),
                ocr_metadata=state.get("ocr_metadata", {}),
            )
        _write_progress(progress_path, run_signature, args, len(image_paths), page_states)

    resource_monitor.set_stage("ocr")
    print(
        f"OCR: iniciando {len(ocr_jobs)} páginas com engine={config.OCR_ENGINE}",
        flush=True,
    )
    resource_monitor.set_progress(queue_depth=len(ocr_jobs), active_workers=min(config.OCR_WORKERS, 1))
    ocr_positions = {
        int(job["index"]): position
        for position, job in enumerate(ocr_jobs, start=1)
    }
    ocr_completed_positions = set()

    def _ocr_progress(item, phase):
        index = int(item.get("index", 0))
        position = ocr_positions.get(index, 0)
        engine_name = str(
            (item.get("ocr_metadata") or {}).get("final_engine")
            or config.OCR_ENGINE
        )
        if phase == "started":
            print(
                f"OCR: {position}/{len(ocr_jobs)} - pagina {index} - "
                f"engine={engine_name} - iniciando",
                flush=True,
            )
            resource_monitor.set_progress(
                pages_done=len(ocr_completed_positions),
                pages_total=len(image_paths),
                queue_depth=max(0, len(ocr_jobs) - len(ocr_completed_positions)),
                active_workers=1,
            )
        else:
            ocr_completed_positions.add(position)
            elapsed = float(item.get("elapsed_seconds", 0.0) or 0.0)
            metadata = item.get("ocr_metadata") or {}
            final_engine = metadata.get("final_engine") or engine_name
            print(
                f"OCR: {position}/{len(ocr_jobs)} - pagina {index} - "
                f"engine={final_engine} "
                f"- concluida em {elapsed:.1f}s",
                flush=True,
            )
            resource_monitor.set_progress(
                pages_done=len(ocr_completed_positions),
                pages_total=len(image_paths),
                queue_depth=max(0, len(ocr_jobs) - len(ocr_completed_positions)),
                active_workers=1 if len(ocr_completed_positions) < len(ocr_jobs) else 0,
            )

    ocr_wall_started = time.perf_counter()
    ocr_results, ocr_parallel_info = detect_ocr_jobs(
        ocr_jobs,
        ocr_lang,
        parallel=config.OCR_PARALLEL,
        workers=config.OCR_WORKERS,
        result_callback=_persist_ocr_result,
        progress_callback=_ocr_progress,
    )
    stage_seconds["ocr"] = time.perf_counter() - ocr_wall_started
    counters["ocr_runs"] = len(ocr_jobs)
    resource_monitor.register_worker_roles(
        ocr_parallel_info.get("worker_pids", []),
        "ocr-worker",
    )
    policy = ocr_parallel_info.get("memory_policy") or {}
    set_memory_policy = getattr(resource_monitor, "set_memory_policy", None)
    if callable(set_memory_policy):
        set_memory_policy(
            mode=policy.get("memory_mode", "normal"),
            pressure=policy.get("memory_pressure", "unknown"),
            ocr_workers=policy.get("workers", 0),
        )
    resource_monitor.set_progress(queue_depth=0, active_workers=0)

    for index, result in ocr_results.items():
        state = state_by_index[index]
        elapsed = float(result.get("elapsed_seconds", 0.0))
        state["timings"]["ocr"] = elapsed
        stage_seconds["ocr_cpu"] += elapsed
        state["raw_lines"] = result.get("lines", [])
        state["ocr_metadata"] = result.get("ocr_metadata", {})
        if result.get("error"):
            state["ocr_error"] = result["error"]
            continue

    translation_targets = []
    analyzable_states = []
    resource_monitor.set_stage("classification")
    for state in page_states:
        if state.get("status") == "completed":
            continue

        if state.get("ocr_error"):
            _complete_page_with_error(
                state,
                state["ocr_error"],
                errors_folder,
                stage_seconds,
                stage="ocr",
            )
            counters["pages_with_error"] += 1
            _write_progress(
                progress_path,
                run_signature,
                args,
                len(image_paths),
                page_states,
            )
            continue

        original = cv2.imread(state["image_path"])
        if original is None:
            _complete_page_with_error(
                state,
                "image_load_failed_before_classification",
                errors_folder,
                stage_seconds,
                stage="image_load",
            )
            counters["pages_with_error"] += 1
            continue

        classification_profiler.start_page(
            state["index"],
            raw_line_count=len(state.get("raw_lines", []) or []),
        )
        classify_started = time.perf_counter()
        with profile_step(
            "pipeline.analyze_initial",
            page_index=state["index"],
            items=len(state.get("raw_lines", []) or []),
        ):
            candidates, groups = analyze_image_array(
                original,
                state.get("raw_lines", []),
                page_index=state["index"],
            )
        reocr_started = time.perf_counter()
        with profile_step(
            "pipeline.speech_container_reocr",
            page_index=state["index"],
            items=len(state.get("raw_lines", []) or []),
        ):
            reocr_lines, reocr_records = apply_speech_container_reocr(
                original,
                state.get("raw_lines", []),
                ocr_lang,
                state["index"],
            )
        reocr_elapsed = time.perf_counter() - reocr_started
        if reocr_records:
            state["speech_container_reocr"] = reocr_records
            stage_seconds["ocr_selective_fallback"] += reocr_elapsed
            if any(record.get("accepted") for record in reocr_records):
                with ocr_line_provenance.page(state["index"]):
                    ocr_line_provenance.record_replacement(
                        state.get("raw_lines", []),
                        reocr_lines,
                        reason="speech_container_reocr",
                    )
                state["raw_lines"] = reocr_lines
                with profile_step(
                    "pipeline.analyze_after_speech_container_reocr",
                    page_index=state["index"],
                    items=len(reocr_lines),
                ):
                    candidates, groups = analyze_image_array(
                        original,
                        reocr_lines,
                        page_index=state["index"],
                    )
        recovery_started = time.perf_counter()
        with profile_step(
            "pipeline.rapidocr_region_recovery",
            page_index=state["index"],
            items=len(groups),
        ):
            recovery_lines, recovery_records = apply_rapidocr_region_recovery(
                original,
                state.get("raw_lines", []),
                groups,
                ocr_lang,
                state["index"],
            )
        if recovery_records:
            state["rapidocr_region_recovery"] = recovery_records
            recovery_elapsed = time.perf_counter() - recovery_started
            stage_seconds["ocr_selective_fallback"] += recovery_elapsed
            if any(record.get("selection") == "attempt_2" for record in recovery_records):
                with ocr_line_provenance.page(state["index"]):
                    ocr_line_provenance.record_replacement(
                        state.get("raw_lines", []),
                        recovery_lines,
                        reason="rapidocr_region_recovery",
                    )
                state["raw_lines"] = recovery_lines
                with profile_step(
                    "pipeline.analyze_after_rapidocr_recovery",
                    page_index=state["index"],
                    items=len(recovery_lines),
                ):
                    candidates, groups = analyze_image_array(
                        original,
                        recovery_lines,
                        page_index=state["index"],
                    )
        selective_started = time.perf_counter()
        with profile_step(
            "pipeline.selective_ocr_fallbacks",
            page_index=state["index"],
            items=len(groups),
        ):
            fallback_lines, selective_records = apply_selective_ocr_fallbacks(
                original,
                state.get("raw_lines", []),
                groups,
                ocr_lang,
                state["index"],
                fast_ocr_budget=fast_ocr_budget if fast_ocr_budget.enabled else None,
            )
        selective_elapsed = time.perf_counter() - selective_started
        if selective_records:
            state["selective_ocr_fallbacks"] = selective_records
            stage_seconds["ocr_selective_fallback"] += selective_elapsed
            state["timings"]["ocr_selective_fallback"] = selective_elapsed
            used_records = [
                record for record in selective_records if record.get("fallback_used")
            ]
            if used_records:
                with ocr_line_provenance.page(state["index"]):
                    ocr_line_provenance.record_replacement(
                        state.get("raw_lines", []),
                        fallback_lines,
                        reason="selective_ocr_fallback",
                    )
                state["raw_lines"] = fallback_lines
                with profile_step(
                    "pipeline.analyze_after_selective_fallback",
                    page_index=state["index"],
                    items=len(fallback_lines),
                ):
                    candidates, groups = analyze_image_array(
                        original,
                        fallback_lines,
                        page_index=state["index"],
                    )
                state["ocr_metadata"] = {
                    **state.get("ocr_metadata", {}),
                    "selective_fallbacks": selective_records,
                }
        grouping_fallback_reason = _grouping_fallback_reason(state, groups)
        if grouping_fallback_reason and fast_ocr_budget.enabled:
            allowed, budget_reason = fast_ocr_budget.allow(
                kind="paddle_full_page", page=state["index"]
            )
            if not allowed:
                _mark_fast_ocr_review(state, budget_reason, grouping_fallback_reason)
                print(
                    f"OCR: pagina {state['index']}/{len(image_paths)} - "
                    f"fallback pesado preservado ({budget_reason})",
                    flush=True,
                )
                grouping_fallback_reason = ""
        if grouping_fallback_reason:
            fallback_started = time.perf_counter()
            with profile_step(
                "pipeline.full_page_paddle_fallback",
                page_index=state["index"],
                metadata={"reason": grouping_fallback_reason},
            ):
                if fast_ocr_budget.enabled:
                    from fast_ocr_policy import run_ocr_with_timeout

                    fallback_lines, fallback_metadata = run_ocr_with_timeout(
                        original,
                        lang=ocr_lang,
                        engine_name="paddle",
                        page=state["index"],
                        timeout_seconds=fast_ocr_budget.page_timeout_seconds,
                    )
                    if fallback_metadata.get("timeout"):
                        _mark_fast_ocr_review(
                            state,
                            "fast_ocr_page_timeout",
                            grouping_fallback_reason,
                        )
                    state["fast_ocr_fallback_metadata"] = fallback_metadata
                else:
                    paddle = OCREngine(
                        ocr_lang,
                        engine="paddle",
                        fallback_engine="",
                    )
                    fallback_lines = paddle.detect_lines(
                        original,
                        page=state["index"],
                    )
                fallback_lines, preserved_regional_count = (
                    _preserve_selected_regional_ocr(
                        fallback_lines,
                        state.get("raw_lines", []),
                    )
                )
            fallback_elapsed = time.perf_counter() - fallback_started
            if fast_ocr_budget.enabled:
                fast_ocr_budget.record(
                    kind="paddle_full_page",
                    elapsed=fallback_elapsed,
                    page=state["index"],
                )
            state["timings"]["ocr"] = (
                float(state["timings"].get("ocr", 0.0))
                + fallback_elapsed
            )
            stage_seconds["ocr"] += fallback_elapsed
            stage_seconds["ocr_cpu"] += fallback_elapsed
            if _fallback_discards_source_text(
                state.get("raw_lines", []), fallback_lines
            ):
                # Keep the read we already have. Taking this answer would leave
                # the page with no groups at all, and an untranslated source
                # page is a worse defect than the badly-read region that asked
                # for the escalation.
                state["ocr_metadata"] = {
                    **state.get("ocr_metadata", {}),
                    "fallback_used": False,
                    "fallback_attempted_reason": grouping_fallback_reason,
                    "fallback_rejected_reason": "fallback_discards_source_text",
                    "fallback_variant": "paddle_full",
                }
            else:
                with ocr_line_provenance.page(state["index"]):
                    ocr_line_provenance.record_replacement(
                        state.get("raw_lines", []),
                        fallback_lines,
                        reason="full_page_paddle_fallback",
                    )
                state["raw_lines"] = fallback_lines
                state["ocr_metadata"] = {
                    **state.get("ocr_metadata", {}),
                    "fallback_used": True,
                    "fallback_reason": grouping_fallback_reason,
                    "original_engine": "rapidocr",
                    "final_engine": (
                        "hybrid" if preserved_regional_count else "paddle"
                    ),
                    "fallback_variant": "paddle_full",
                    "preserved_regional_line_count": preserved_regional_count,
                }
                with profile_step(
                    "pipeline.analyze_after_full_page_fallback",
                    page_index=state["index"],
                    items=len(fallback_lines),
                ):
                    candidates, groups = analyze_image_array(
                        original,
                        fallback_lines,
                        page_index=state["index"],
                    )
                if config.ENABLE_OCR_CACHE:
                    save_ocr_cache(
                        state["ocr_cache_key"],
                        state["image_hash"],
                        ocr_lang,
                        fallback_lines,
                        state["timings"]["ocr"],
                        state.get("precheck", {}),
                        ocr_metadata=state["ocr_metadata"],
                    )
        with profile_step(
            "pipeline.collect_group_text_repairs",
            page_index=state["index"],
            items=len(groups),
        ):
            state["group_text_repairs"] = _group_text_repairs(groups)
        classify_elapsed = time.perf_counter() - classify_started
        classification_profiler.record_step(
            "classification_grouping.page_total",
            classify_elapsed,
            page_index=state["index"],
            items=len(groups),
        )
        stage_seconds["classification_grouping"] += classify_elapsed
        state["timings"]["classification_grouping"] = classify_elapsed
        state["original_bgr"] = original
        state["candidates"] = candidates
        state["groups"] = groups
        # Last gate before the translation list is built: a region RapidOCR could
        # not read acceptably, even after its one selective retry, goes to review
        # instead of to the translator.
        enforce_rapidocr_quality_gate(groups, page_index=state["index"])
        with profile_step(
            "pipeline.get_translatable_groups",
            page_index=state["index"],
            items=len(groups),
        ):
            state["translatable_groups"] = get_translatable_groups(groups)
        state["structural_snapshot"] = _structural_page_snapshot(state, groups)
        classification_profiler.finish_page(
            state["index"],
            group_count=len(groups),
            translatable_groups=len(state["translatable_groups"]),
            fallback_count=sum(
                1 for record in state.get("selective_ocr_fallbacks", []) if record.get("fallback_used")
            )
            + (1 if grouping_fallback_reason else 0),
            repair_count=len(state.get("group_text_repairs", []) or []),
            classification_grouping_seconds=classify_elapsed,
        )
        analyzable_states.append(state)
        for group in state["translatable_groups"]:
            translation_targets.append(group)

    all_analyzed_groups = [
        group
        for state in analyzable_states
        for group in state.get("groups", [])
    ]
    chapter_name_repairs = normalize_recurring_compact_names(all_analyzed_groups)
    # Same chapter-level evidence, the other direction: a term the chapter
    # spells on its own that the OCR glued to the next word. Done here so the
    # canonical source the provider receives is the repaired one, while
    # ``group.original_text`` keeps the raw read for forensics.
    chapter_name_repairs += recover_protected_term_boundaries(
        all_analyzed_groups,
        extra_anchors=_session_terminology_terms(session_context),
    )
    if chapter_name_repairs:
        for state in analyzable_states:
            state["group_text_repairs"] = _group_text_repairs(
                state.get("groups", [])
            )
    detected_names = sorted(
        {
            name
            for group in all_analyzed_groups
            for name in getattr(group, "detected_proper_names", [])
        }
    )
    if hasattr(translator, "set_detected_names"):
        translator.set_detected_names(detected_names)
    translation_targets = []
    for state in analyzable_states:
        state["translatable_groups"] = get_translatable_groups(
            state.get("groups", [])
        )
        translation_targets.extend(state["translatable_groups"])

    resource_monitor.set_stage("translation")
    # Stage identity is provider-neutral: which provider actually ran is recorded
    # in provenance, not baked into a label the UI mirrors.
    print("Tradução: iniciando", flush=True)
    translation_started = time.perf_counter()
    if session_context is not None:
        session_context.prepare(all_analyzed_groups)
        if hasattr(translator, "set_session_context"):
            translator.set_session_context(session_context)
    translations = translator.translate_many(
        [group.text for group in translation_targets],
        force=args.force,
    )
    stage_seconds["translation"] = time.perf_counter() - translation_started
    translator_stats = getattr(translator, "stats", {})
    translator_stats["translation_candidates"] = len(translation_targets)
    translator_stats["translation_results_received"] = len(translations or [])
    translator_stats["translation_results_nonempty"] = sum(
        1 for item in (translations or []) if str(item or "").strip()
    )
    apply_group_translations(translation_targets, translations)
    translator_stats["translated_groups_after_apply"] = sum(
        1
        for group in translation_targets
        if getattr(group, "translation_final_state", "") == "translated"
    )
    translator_stats["renderable_translated_groups"] = sum(
        1
        for group in translation_targets
        if getattr(group, "translation_final_state", "") == "translated"
        and bool(getattr(group, "translation", ""))
        and bool(getattr(group, "translation_valid", False))
    )
    retry_started = time.perf_counter()
    if session_context is not None:
        # Learn the chapter's terminology from the first pass *before* retrying,
        # so a region near the end is judged against the decisions taken at the
        # start instead of against whatever survived the rolling window.
        session_context.record_translations(translation_targets)
        if hasattr(translator, "set_session_context"):
            translator.set_session_context(session_context)
    translation_retry_records = validate_and_retry_translations(
        translation_targets,
        translator,
        force=args.force,
        terminology_ledger=session_context,
        fidelity_verifier=getattr(translator, "fidelity_verifier", None),
        fidelity_stats=translator_stats,
        ptbr_naturalizer=getattr(translator, "ptbr_naturalizer", None),
    )
    if session_context is not None:
        session_context.record_translations(translation_targets)
    stage_seconds["translation"] += time.perf_counter() - retry_started

    resource_monitor.set_stage("rendering")
    for state in analyzable_states:
        if state.get("status") == "completed":
            continue
        try:
            render_timings = {}
            page_debug_folder = None
            if targeted_regression:
                page_debug_folder = str(
                    diagnostic_folder / f"page_{state['index']:03}"
                )
            layout_retry_attempt = 0
            while True:
                final, debug_data = render_analyzed_image(
                    state["original_bgr"],
                    state.get("raw_lines", []),
                    state["candidates"],
                    state["groups"],
                    font_path=config.FONT_PATH,
                    debug_folder=page_debug_folder,
                    page_index=state["index"],
                    image_path=state["image_path"],
                    stage_timings=render_timings,
                )
                overflow_groups = [
                    group
                    for group in state["groups"]
                    if group.sent_to_translation
                    and float(group.text_overflow_ratio or 0.0)
                    > config.MAX_TEXT_OVERFLOW_RATIO
                ]
                if (
                    not overflow_groups
                    or layout_retry_attempt >= config.TRANSLATION_MAX_RETRIES
                    or not hasattr(translator, "translate_strict")
                ):
                    break
                retry_started = time.perf_counter()
                retried = _retry_layout_overflow_translations(
                    overflow_groups,
                    translator,
                    translation_retry_records,
                    force=args.force,
                    attempt=layout_retry_attempt + 1,
                )
                stage_seconds["translation"] += time.perf_counter() - retry_started
                if not retried:
                    break
                layout_retry_attempt += 1
            for name in ("inpainting", "redraw"):
                elapsed = float(render_timings.get(name, 0.0))
                state["timings"][name] = elapsed
                stage_seconds[name] += elapsed

            save_started = time.perf_counter()
            Path(state["output_path"]).parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(state["output_path"], final):
                raise RuntimeError("cv2.imwrite retornou False")
            save_elapsed = time.perf_counter() - save_started
            state["timings"]["image_save"] = save_elapsed
            stage_seconds["image_save"] += save_elapsed

            if not valid_image(state["output_path"]):
                raise RuntimeError("imagem final invalida")

            state["status"] = "completed"
            state["cache_source"] = "fresh"
            debug_data["ocr_metadata"] = state.get("ocr_metadata", {})
            debug_data["selective_ocr_fallbacks"] = state.get(
                "selective_ocr_fallbacks",
                [],
            )
            debug_data["text_repairs"] = (
                _applied_text_repairs(state.get("ocr_metadata", {}))
                + state.get("group_text_repairs", [])
            )
            debug_data["rejected_text_repairs"] = _rejected_text_repairs(
                state.get("ocr_metadata", {})
            )
            state["debug_data"] = debug_data
            _save_page_processed_cache(state)
            resource_monitor.set_progress(
                pages_done=sum(
                    1
                    for item in page_states
                    if item.get("status") in {"completed", "completed_with_error"}
                ),
                pages_total=len(image_paths),
            )
            print(
                f"Pagina {state['index']}/{len(image_paths)}: concluida",
                flush=True,
            )
        except Exception as exc:
            # The exception object, not ``str(exc)``: flattening it here is what
            # turned five real #84F9 page failures into the word "str".
            _complete_page_with_error(
                state,
                exc,
                errors_folder,
                stage_seconds,
                stage="page_processing",
            )
            counters["pages_with_error"] += 1
            record = state["page_error"]
            print(
                f"Pagina {state['index']}/{len(image_paths)}: erro:"
                f" {record['exception_type']}: {record['message']}",
                flush=True,
            )
            # The local technical log keeps the trace; the UI never sees it.
            trace = _page_error_traceback(exc)
            if trace:
                print(trace, flush=True)
        finally:
            state.pop("original_bgr", None)
            state.pop("candidates", None)
            state.pop("groups", None)
            state.pop("translatable_groups", None)
            state.pop("raw_lines", None)
            _write_progress(
                progress_path,
                run_signature,
                args,
                len(image_paths),
                page_states,
            )

    completed_states = [
        state
        for state in sorted(page_states, key=lambda item: item["index"])
        if state.get("status") in {"completed", "completed_with_error"}
        and valid_image(state.get("output_path"))
    ]
    if not completed_states:
        raise RuntimeError("Nenhuma pagina valida foi produzida.")

    pdf_filename = (
        "regression.pdf"
        if targeted_regression
        else (
            build_pdf_filename(
                source_url=args.url,
                fallback_id=output_folder.name,
            )
            if args.full
            else f"benchmark_{args.max_images:03}.pdf"
        )
    )
    pdf_path = output_folder / pdf_filename
    pdf_temp_path = output_folder / f".{pdf_filename}.{os.getpid()}.{time.time_ns()}.tmp"
    resource_monitor.set_stage("pdf")
    pdf_started = time.perf_counter()
    try:
        generate_pdf([state["output_path"] for state in completed_states], str(pdf_temp_path))
        artifact = _finalize_immutable_file(pdf_temp_path, pdf_path)
    finally:
        try:
            if pdf_temp_path.exists():
                pdf_temp_path.unlink()
        except OSError:
            pass
    stage_seconds["pdf"] = time.perf_counter() - pdf_started
    artifact_sha256 = artifact["sha256"]
    artifact_size_bytes = artifact["size"]

    resource_monitor.set_stage("reports")
    preview_started = time.perf_counter()
    selected_states = _create_preview_contact_sheet(
        completed_states,
        contact_sheet_path,
    )
    _create_preview_compare_sheet(selected_states, compare_sheet_path)
    preview_seconds = time.perf_counter() - preview_started

    quality = _validate_quality(
        completed_states,
        pdf_path,
        expected_page_count=len(completed_states),
        full=args.full,
    )
    split_audit = smart_split_audit(smart_split_report)
    quality["smart_split_unsafe_count"] = split_audit["unsafe_count"]
    quality["smart_split_safe"] = split_audit["safe"]
    quality["smart_split_details"] = split_audit["details"]
    quality["smart_split_summary"] = {
        "safe": split_audit["safe"],
        "unsafe_count": split_audit["unsafe_count"],
        "details_count": split_audit["details_count"],
    }
    quality["passed"] = bool(
        quality.get("passed") and quality["smart_split_safe"]
    )
    summary = _aggregate_debug_data(completed_states)
    translation_accounting = _translation_quality_accounting(completed_states)
    physical_accounting = _physical_residual_accounting(completed_states)
    # Reconciled over every source page the run set out to process, not over the
    # states that survived: a page whose analysis threw produces no regions, and
    # region accounting alone cannot see it at all.
    page_accounting = _source_page_accounting(page_states, len(image_paths))
    quality["translation_accounting"] = translation_accounting
    quality["physical_quality"] = physical_accounting
    quality["source_page_accounting"] = page_accounting
    quality["source_pages_unverified"] = page_accounting["source_pages_unverified"]
    quality["source_page_findings"] = page_accounting["findings"]
    quality["source_page_gate_passed"] = page_accounting["page_gate_passed"]
    quality["final_story_output_verified"] = page_accounting["story_output_verified"]
    quality["translation_terminal_states_complete"] = translation_accounting[
        "accounting_closed"
    ]
    quality["translation_not_applied"] = translation_accounting[
        "translation_not_applied"
    ]
    quality["missing_translation_candidate"] = translation_accounting[
        "missing_candidate"
    ]
    quality["source_language_residual_groups"] = translation_accounting[
        "source_language_residual"
    ]
    quality["physical_source_residual_count"] = physical_accounting[
        "physical_source_residual_count"
    ]
    quality["physical_source_residual_group_ids"] = physical_accounting[
        "physical_source_residual_group_ids"
    ]
    quality["ordinary_story_physical_residual_count"] = physical_accounting[
        "ordinary_story_physical_residual_count"
    ]
    quality["ordinary_story_physical_residual_ids"] = physical_accounting[
        "ordinary_story_physical_residual_ids"
    ]
    quality["physical_gate_passed"] = physical_accounting["physical_gate_passed"]
    if physical_accounting.get("source_completeness"):
        quality["source_completeness"] = physical_accounting["source_completeness"]
    quality["passed"] = bool(
        quality.get("passed")
        and translation_accounting["quality_passed"]
        and physical_accounting["physical_gate_passed"]
        and page_accounting["page_gate_passed"]
    )
    counters["ocr_page_fallbacks"] = summary["ocr_page_fallbacks"]
    counters["ocr_region_fallbacks"] = summary["ocr_region_fallbacks"]
    counters["ocr_text_repairs"] = summary["ocr_text_repairs"]
    counters["translation_retries"] = summary["translation_retries"]
    counters["translation_rejections"] = summary["translation_rejections"]
    counters["visual_validation_failures"] = summary["visual_validation_failures"]
    quality["mixed_language_items"] = summary["mixed_language_items"]
    quality["no_mixed_language_items"] = summary["mixed_language_items"] == 0
    quality["passed"] = bool(
        quality.get("passed") and quality["no_mixed_language_items"]
    )
    total_seconds = time.perf_counter() - started
    translator_stats = getattr(translator, "stats", {})
    quality["translation_batches_succeeded"] = (
        translator_stats.get("failed_batches", 0) == 0
    )
    quality["passed"] = bool(
        quality.get("passed")
        and quality["translation_batches_succeeded"]
        and counters["pages_with_error"] == 0
    )
    quality["zero_processing_errors"] = counters["pages_with_error"] == 0
    quality["status"] = "passed" if quality["passed"] else "review_required"
    new_images = len(completed_states) - counters["images_skipped_by_cache"]
    cache_average = (
        stage_seconds["cache_load"] / counters["images_skipped_by_cache"]
        if counters["images_skipped_by_cache"]
        else 0.0
    )
    old_reduction = ((BASELINE_SECONDS - total_seconds) / BASELINE_SECONDS) * 100
    comparable_stages = {
        key: value
        for key, value in stage_seconds.items()
        if key not in {"ocr_cpu", "cache_load"}
    }
    slowest_stage = max(comparable_stages, key=comparable_stages.get)

    resource_summary = resource_monitor.stop()
    gpu_diagnostics = detect_gpu_basic()
    classification_profile_paths = classification_profiler.write_reports(output_folder)
    classification_profile_summary = (
        classification_profiler.summary()
        if config.CLASSIFICATION_PROFILING
        else {"enabled": False}
    )
    structural_fingerprint = _structural_fingerprint(completed_states)
    page_count_trace = _page_count_trace(
        download_report,
        all_image_paths=all_image_paths,
        source_image_paths=source_image_paths,
        image_paths=image_paths,
        completed_states=completed_states,
        smart_split_report=smart_split_report,
    )
    group_count_trace = _group_count_trace(completed_states)
    set_active_profiler(None)
    final_status = derive_final_run_status(
        technical_success=True,
        quality_validation=quality,
    )
    source_provenance = _source_manifest_provenance(download_report)

    report = {
        "url": sanitize_source_url(args.url),
        "source_type": str(download_report.get("source_type") or "url"),
        "adapter_name": str(download_report.get("adapter_name") or ""),
        "adapter_version": str(download_report.get("adapter_version") or ""),
        "transport_name": str(download_report.get("transport_name") or ""),
        # The canonical run manifest and timing report share this small, non-replayable
        # evidence block. It intentionally has no page URLs, local paths, candidate ids or
        # transport/session details.
        "source_provenance": source_provenance,
        "mode": "full" if args.full else "controlled",
        "force": bool(args.force),
        "fast": bool(args.fast),
        "job_run_id": str(getattr(args, "job_run_id", "") or ""),
        "fast_ocr_budget": fast_ocr_budget.report(),
        "run_signature": run_signature,
        "total_dom_images": download_report.get("total_dom_images", 0),
        "total_unique_urls": download_report.get("total_unique_urls", 0),
        "viewer_image_count": download_report.get("viewer_image_count", 0),
        "collection_strategy": download_report.get("collection_strategy", ""),
        "download_gate": download_report.get("download_gate", {}),
        "available_valid_images": len(all_image_paths),
        "selected_source_images": len(source_image_paths),
        "status": final_status,
        "smart_pdf_split": smart_split_report,
        "page_count_trace": page_count_trace,
        "group_count_trace": group_count_trace,
        "source_expected_slices": page_count_trace["source_expected"],
        "downloaded_source_slices": page_count_trace["downloaded"],
        "source_image_slices": page_count_trace["source_images"],
        "logical_page_count": page_count_trace["logical_pages"],
        "processed_logical_pages": page_count_trace["processed_pages"],
        "source_expected_pages": page_count_trace["source_expected"],
        "downloaded_pages": page_count_trace["downloaded"],
        "logical_pages": page_count_trace["logical_pages"],
        "smart_split_contact_sheet": (
            str(smart_split_contact_sheet) if smart_split_report.get("enabled") else ""
        ),
        "selected_page_indices": selected_page_indices,
        "missing_page_indices": missing_page_indices,
        "total_images": len(image_paths),
        "processed_images": len(completed_states),
        "images_skipped_by_cache": counters["images_skipped_by_cache"],
        "images_skipped_by_no_text_precheck": counters[
            "images_skipped_by_no_text_precheck"
        ],
        "ocr_runs": counters["ocr_runs"],
        "ocr_cache_hits": counters["ocr_cache_hits"],
        "ocr_page_fallbacks": counters["ocr_page_fallbacks"],
        "ocr_region_fallbacks": counters["ocr_region_fallbacks"],
        "ocr_region_fallback_attempts": summary["ocr_region_fallback_attempts"],
        "paddle_mobile_region_fallbacks": summary[
            "paddle_mobile_region_fallbacks"
        ],
        "paddle_full_region_fallbacks": summary["paddle_full_region_fallbacks"],
        "paddle_full_calls": summary["paddle_full_calls"],
        "paddle_full_total_seconds": round(summary["paddle_full_total_seconds"], 6),
        "paddle_full_accepted": summary["paddle_full_accepted"],
        "paddle_full_rejected": summary["paddle_full_rejected"],
        "paddle_full_no_change": summary["paddle_full_no_change"],
        "paddle_full_worse": summary["paddle_full_worse"],
        "paddle_full_duplicate": summary["paddle_full_duplicate"],
        "paddle_full_required": summary["paddle_full_required"],
        "paddle_full_useful": summary["paddle_full_useful"],
        "paddle_full_after_mobile_candidate": summary[
            "paddle_full_after_mobile_candidate"
        ],
        "paddle_full_after_mobile_sufficient": summary[
            "paddle_full_after_mobile_sufficient"
        ],
        "paddle_full_after_mobile_failure": summary[
            "paddle_full_after_mobile_failure"
        ],
        "paddle_full_for_speech": summary["paddle_full_for_speech"],
        "paddle_full_for_narration": summary["paddle_full_for_narration"],
        "paddle_full_for_sfx": summary["paddle_full_for_sfx"],
        "paddle_full_for_decorative": summary["paddle_full_for_decorative"],
        "paddle_full_for_unknown": summary["paddle_full_for_unknown"],
        "ocr_text_repairs": counters["ocr_text_repairs"],
        "ocr_text_repairs_rejected": summary["ocr_text_repairs_rejected"],
        "groups_reverted_for_visual_safety": summary[
            "groups_reverted_for_visual_safety"
        ],
        "manual_review_required_groups": summary["manual_review_required_groups"],
        "translation_retries": counters["translation_retries"],
        "translation_rejections": counters["translation_rejections"],
        "mixed_language_items": summary["mixed_language_items"],
        "text_overflow_items": summary["text_overflow_items"],
        "visual_validation_failures": counters["visual_validation_failures"],
        "translation_api_texts": translator_stats.get("api_texts", 0),
        "translation_cache_hits": translator_stats.get("cache_hits", 0),
        "translation_api_requests": translator_stats.get("api_requests", 0),
        "translation_provider": translator_stats.get("provider_name", ""),
        "provider_provenance": report_provider_provenance(translator_stats),
        "translation_model_runtime": translator_stats.get(
            "model", config.NVIDIA_TRANSLATION_MODEL
        ),
        "translation_language_pair": translator_stats.get("language_pair", ""),
        "credential_pool_size": translator_stats.get("credential_pool_size", 0),
        "eligible_credentials": translator_stats.get("eligible_credentials", 0),
        "credential_switches": translator_stats.get("credential_switches", 0),
        "credential_429_cooldowns": translator_stats.get(
            "credential_429_cooldowns", 0
        ),
        "provider_rate_limited_count": translator_stats.get(
            "provider_rate_limited_count", 0
        ),
        "provider_timeout_count": translator_stats.get("provider_timeout_count", 0),
        "provider_error_count": translator_stats.get("provider_error_count", 0),
        "translation_candidates": translator_stats.get("translation_candidates", 0),
        "translation_batches": translator_stats.get("translation_batches", 0),
        "translation_requests_succeeded": translator_stats.get("successful_batches", 0),
        "translation_requests_failed": translator_stats.get("failed_batches", 0),
        "translation_responses_received": translator_stats.get(
            "translation_responses_received", 0
        ),
        "translation_responses_nonempty": translator_stats.get(
            "translation_responses_nonempty", 0
        ),
        "translation_results_parsed": translator_stats.get(
            "translation_results_parsed", 0
        ),
        "translation_results_associated": translator_stats.get(
            "translation_results_associated", 0
        ),
        "translated_groups_after_apply": translator_stats.get(
            "translated_groups_after_apply", 0
        ),
        "renderable_translated_groups": translator_stats.get(
            "renderable_translated_groups", 0
        ),
        "translation_configuration_missing": translator_stats.get(
            "translation_configuration_missing", 0
        ),
        "translation_last_failure_reason": translator_stats.get(
            "last_transport_reason", ""
        ),
        "translation_failed_batches": translator_stats.get("failed_batches", 0),
        "translation_invalid_json_retries": translator_stats.get(
            "invalid_json_retries", 0
        ),
        "translation_invalid_json_failures": translator_stats.get(
            "invalid_json_failures", 0
        ),
        "translation_logical_batches": translator_stats.get("logical_batches", 0),
        "logical_calls_by_origin": dict(
            translator_stats.get("logical_calls_by_origin") or {}
        ),
        "provider_http_attempts": translator_stats.get("provider_http_attempts", 0),
        "provider_attempts_by_kind": dict(
            translator_stats.get("provider_attempts_by_kind") or {}
        ),
        "format_failures_total": translator_stats.get("format_failures_total", 0),
        "format_failure_truncated": translator_stats.get("format_failure_truncated", 0),
        "format_failure_malformed": translator_stats.get("format_failure_malformed", 0),
        "format_failure_wrong_schema": translator_stats.get("format_failure_wrong_schema", 0),
        "format_failure_missing_id": translator_stats.get("format_failure_missing_id", 0),
        "format_retry_requests": translator_stats.get("format_retry_requests", 0),
        "format_retry_success": translator_stats.get("format_retry_success", 0),
        "format_retry_failure": translator_stats.get("format_retry_failure", 0),
        "selective_recovery_requests": translator_stats.get("selective_recovery_requests", 0),
        "source_equal_recovery_requests": translator_stats.get("source_equal_recovery_requests", 0),
        "partial_residual_recovery_requests": translator_stats.get("partial_residual_recovery_requests", 0),
        "finish_reason_length": translator_stats.get("finish_reason_length", 0),
        "retry_budget_exhausted": translator_stats.get("retry_budget_exhausted", 0),
        "provider_request_telemetry": list(
            translator_stats.get("provider_request_telemetry") or []
        )[:500],
        "pages_with_error": counters["pages_with_error"],
        "ocr_detected_lines": summary["ocr_detected_lines"],
        "groups_formed": summary["groups_formed"],
        "groups_translated": summary["groups_translated"],
        "groups_ignored_sfx_decorative": summary[
            "groups_ignored_sfx_decorative"
        ],
        "classification_counts": summary["classification_counts"],
        "ocr_engine": config.OCR_ENGINE,
        "ocr_fallback_engine": config.OCR_FALLBACK_ENGINE,
        "download_cache_hit": download_cache_hit,
        "ocr_parallel": ocr_parallel_info,
        "adaptive_parallelism": {
            "enabled": bool(config.ADAPTIVE_PARALLELISM),
            "min_ocr_workers": config.MIN_OCR_WORKERS,
            "max_ocr_workers": config.MAX_OCR_WORKERS,
            "queue_multiplier": config.OCR_QUEUE_MULTIPLIER,
            "decisions": ocr_parallel_info.get("adaptive_decisions", []),
        },
        "translation_parallel_requested": translator_stats.get(
            "parallel_requested", config.TRANSLATION_PARALLEL
        ),
        "translation_parallel_used": translator_stats.get("parallel_used", False),
        "translation_workers": translator_stats.get(
            "workers", config.TRANSLATION_WORKERS
        ),
        "stage_seconds": {
            key: round(float(value), 6) for key, value in stage_seconds.items()
        },
        "preview_seconds": round(preview_seconds, 6),
        "total_seconds": round(total_seconds, 6),
        "average_seconds_per_image": round(total_seconds / len(completed_states), 6),
        "average_seconds_per_image_without_cache": round(
            (total_seconds - stage_seconds["cache_load"]) / new_images
            if new_images
            else 0.0,
            6,
        ),
        "average_seconds_per_image_with_cache": round(cache_average, 6),
        "slowest_stage": slowest_stage,
        "slowest_stage_seconds": round(comparable_stages[slowest_stage], 6),
        "baseline_seconds": BASELINE_SECONDS,
        "baseline_comparison_applicable": bool(args.full),
        "difference_from_baseline_seconds": round(total_seconds - BASELINE_SECONDS, 6),
        "reduction_from_baseline_percent": round(old_reduction, 3),
        "pdf_path": str(pdf_path),
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": artifact_size_bytes,
        "progress_path": str(progress_path),
        "timing_report_json": str(timing_json_path),
        "timing_report_txt": str(timing_txt_path),
        "preview_contact_sheet": str(contact_sheet_path),
        "preview_compare_sheet": str(compare_sheet_path),
        "quality_report_json": str(quality_json_path),
        "quality_report_html": str(quality_html_path),
        "quality_validation": quality,
        "resource_monitoring": resource_summary,
        "classification_profiling": {
            "enabled": bool(config.CLASSIFICATION_PROFILING),
            **classification_profile_paths,
            "top_steps": classification_profile_summary.get("top_steps", [])[:10],
            "slowest_pages": classification_profile_summary.get("slowest_pages", [])[:10],
            "slowest_groups": classification_profile_summary.get("slowest_groups", [])[:10],
        },
        "ocr_line_provenance": ocr_line_provenance.write_artifact(output_folder),
        "structural_fingerprint": structural_fingerprint,
        "gpu_diagnostics": gpu_diagnostics,
        "session_context": {
            "enabled": bool(session_context is not None),
            **(session_context.summary() if session_context is not None else {}),
        },
    }
    run_manifest_path = output_folder / "run_manifest.json"
    report["run_manifest_path"] = str(run_manifest_path)
    atomic_write_json(
        run_manifest_path,
        _output_run_manifest(output_folder, report, translator),
    )
    quality_report = _build_quality_report(
        report,
        completed_states,
        translation_retry_records,
    )
    atomic_write_json(quality_json_path, quality_report)
    quality_html_path.write_text(
        _quality_report_html(quality_report),
        encoding="utf-8",
    )
    _write_requested_artifact_aliases(
        output_folder,
        pdf_path,
        contact_sheet_path,
        compare_sheet_path,
        args,
    )
    atomic_write_json(timing_json_path, report)
    timing_txt_path.write_text(_timing_report_text(report), encoding="utf-8")
    _write_progress(
        progress_path,
        run_signature,
        args,
        len(image_paths),
        page_states,
        status=final_status,
        pdf_path=str(pdf_path),
    )

    console_report = {
        **report,
        "smart_pdf_split": {
            key: value
            for key, value in (report.get("smart_pdf_split") or {}).items()
            if key != "splits"
        },
    }
    print(dumps_json(console_report, ensure_ascii=False, indent=2), flush=True)
    return report


def _structural_fingerprint(page_states):
    records = []
    for state in page_states:
        snapshot = state.get("structural_snapshot")
        if snapshot:
            records.append(snapshot)
            continue
        records.append(_structural_page_snapshot(state, state.get("groups", []) or []))
    payload = {"pages": records}
    return {
        "hash": stable_hash(payload),
        "page_count": len(records),
        "group_count": sum(len(page["groups"]) for page in records),
    }


def _structural_page_snapshot(state, groups):
    group_records = []
    for group in groups or []:
        group_records.append(
            {
                "id": group.group_id,
                "box": [int(value) for value in group.box],
                "line_count": len(group.lines),
                "cleanup_line_count": len(group.cleanup_lines),
                "classification": group.classification,
                "ignored": bool(group.ignored),
                "ignore_reason": group.ignore_reason,
                "sent_to_translation": bool(group.sent_to_translation),
                "manual_review_required": bool(group.manual_review_required),
                "fallback_used": bool(group.fallback_used),
                "quality_reason_count": len(group.quality_reasons),
                "background_type": group.background_type,
            }
        )
    return {
        "page_index": int(state.get("index") or 0),
        "raw_lines": len(state.get("raw_lines", []) or []),
        "groups": group_records,
    }


def _retry_layout_overflow_translations(
    groups,
    translator,
    retry_records,
    force=False,
    attempt=1,
):
    retried = 0
    for group in groups:
        previous = str(group.translation or "").strip()
        try:
            candidate = translator.translate_strict(
                group.text,
                previous_translation=previous,
                validation_reason=(
                    "layout_overflow: a traducao nao cabe na regiao original; "
                    "use uma versao substancialmente mais curta sem perder o sentido"
                ),
                force=force,
            )
        except Exception as exc:
            retry_records.append(
                {
                    "group_id": group.group_id,
                    "source": group.text,
                    "previous_translation": previous,
                    "candidate_translation": "",
                    "attempt": attempt,
                    "valid": False,
                    "reason": f"layout_retry_error:{type(exc).__name__}",
                    "retry_type": "layout_overflow",
                }
            )
            continue

        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        apply_group_translations([group], [candidate])
        shorter = len(group.translation) <= max(1, len(previous))
        accepted = bool(group.translation_valid and shorter)
        retry_records.append(
            {
                "group_id": group.group_id,
                "source": group.text,
                "previous_translation": previous,
                "candidate_translation": group.translation,
                "attempt": attempt,
                "valid": accepted,
                "reason": (
                    "layout_retry_shorter"
                    if accepted
                    else "layout_retry_not_shorter_or_invalid"
                ),
                "retry_type": "layout_overflow",
            }
        )
        if accepted:
            group.translation_retry_count += 1
            group.translation_validation_reason = "layout_retry_ok"
            retried += 1
        else:
            group.translation = previous
            group.translation_valid = True
            group.translation_validation_reason = "layout_retry_rejected"
    return retried


def _download_with_cache(url, max_images, output_folder, force, source_candidate_ids=None,
                         local_manifest_path=""):
    approved_ids = list(dict.fromkeys(str(value) for value in (source_candidate_ids or []) if value))
    input_folder = output_folder / "input"
    output_report_path = output_folder / "downloaded_images.json"
    if local_manifest_path:
        # Local input is already an immutable, validated snapshot.  It is deliberately never
        # entered in the remote-download cache and it declares logical pages so Webtoon smart
        # splitting cannot alter a user-supplied page sequence.
        from local_folder_input import materialize_snapshot

        paths, manifest = materialize_snapshot(
            local_manifest_path,
            input_folder,
            max_images=max_images,
            clear_existing=bool(force),
        )
        atomic_write_json(output_report_path, manifest)
        print(f"Entrada local: {len(paths)} paginas logicas", flush=True)
        return paths, manifest, False
    key = stable_hash(
        {
            "url": url,
            "max_images": max_images,
            # A manually confirmed subset must never reuse the cache for a different reader
            # snapshot or automatic selection.
            "source_candidate_ids": approved_ids,
            "download_rules": "chapter-download-v4",
        }
    )
    download_folder = cache_folder("downloads") / key
    manifest_path = download_folder / "manifest.json"

    if _download_cache_reuse_allowed(url, force=force, approved_ids=approved_ids):
        manifest = load_json(manifest_path)
        cached_paths = _valid_download_paths(manifest)
        if _download_cache_is_complete(manifest, cached_paths):
            if input_folder.exists():
                force_remove(str(input_folder))
            input_folder.mkdir(parents=True, exist_ok=True)
            active_manifest = dict(manifest)
            active_items = []
            active_paths = []
            for index, item in enumerate(manifest.get("downloaded", []), start=1):
                cached_path = item.get("path")
                active_path = input_folder / f"{index:03}.png"
                atomic_copy(cached_path, active_path)
                active_item = dict(item)
                active_item["cache_path"] = cached_path
                active_item["path"] = str(active_path)
                active_items.append(active_item)
                active_paths.append(str(active_path))
            active_manifest["downloaded"] = active_items
            atomic_write_json(output_report_path, active_manifest)
            print(f"Download: cache ({len(active_paths)} imagens)", flush=True)
            return active_paths, active_manifest, True
        if cached_paths:
            expected = _expected_download_count(manifest)
            print(
                "Download: cache parcial ignorado "
                f"({len(cached_paths)}/{expected or 'desconhecido'} imagens)",
                flush=True,
            )

    paths = download_images(
        url,
        max_images=max_images,
        debug_folder=str(output_folder),
        target_folder=str(input_folder),
        force=True,
        approved_candidate_ids=approved_ids,
        progress_callback=lambda current, total, message: print(
            f"{message}: {current}/{total}",
            flush=True,
        ),
    )
    manifest = load_json(output_report_path)
    valid_items = _chapter_download_items(manifest)
    from chapter_source import select_adapter

    cacheable_source = select_adapter(url).is_specific and not approved_ids
    if cacheable_source:
        if download_folder.exists():
            force_remove(str(download_folder))
        download_folder.mkdir(parents=True, exist_ok=True)
    cache_items = []
    for item in valid_items:
        path = item.get("path")
        if _valid_download_item(item, path):
            file_hash = file_sha256(path)
            cache_path = download_folder / Path(path).name if cacheable_source else None
            if cache_path is not None:
                atomic_copy(path, cache_path)
            item["sha256"] = file_hash
            cache_item = dict(item)
            cache_item["path"] = str(cache_path) if cache_path is not None else ""
            cache_item["active_path"] = path
            cache_items.append(cache_item)
    manifest["downloaded"] = valid_items
    manifest["total_downloaded"] = len(valid_items)
    paths = [
        item["path"]
        for item in valid_items
        if _valid_download_item(item, item.get("path"))
    ]
    if cacheable_source:
        cache_manifest = dict(manifest)
        cache_manifest["downloaded"] = cache_items
        cache_manifest["total_downloaded"] = len(cache_items)
        atomic_write_json(manifest_path, cache_manifest)
    atomic_write_json(output_report_path, manifest)
    return paths, manifest, False


def _download_cache_reuse_allowed(url, *, force, approved_ids) -> bool:
    """Generic readers must be observed afresh; cache never substitutes source review."""
    if not config.ENABLE_DOWNLOAD_CACHE or force or approved_ids:
        return False
    from chapter_source import select_adapter

    return bool(select_adapter(url).is_specific)


def _parse_page_indices(raw):
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = re.split(r"[,;\s]+", str(raw))
    indices = []
    seen = set()
    for value in values:
        if value in (None, ""):
            continue
        try:
            index = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Indice de pagina invalido: {value!r}") from None
        if index <= 0:
            raise ValueError(f"Indice de pagina deve ser positivo: {index}")
        if index not in seen:
            indices.append(index)
            seen.add(index)
    return indices


def _resolve_download_max_images(
    max_images,
    selected_page_indices,
    local_manifest_path,
    source_candidate_ids,
):
    approved_ids = list(source_candidate_ids or [])
    if approved_ids:
        # A submitted source_selection is already a concrete reader snapshot. Keep bounded
        # runs bounded instead of expanding them to a full smart-split chapter download.
        return max_images
    if config.SMART_WEBTOON_PDF_SPLIT and not local_manifest_path:
        return None
    if selected_page_indices and (not config.SMART_WEBTOON_PDF_SPLIT or local_manifest_path):
        return max(max(selected_page_indices), max_images or 0)
    return max_images


def _select_image_entries(image_paths, selected_page_indices):
    if not selected_page_indices:
        return [
            {
                "index": index,
                "original_index": index,
                "sequence_index": index,
                "path": path,
            }
            for index, path in enumerate(image_paths, start=1)
        ], []

    entries = []
    missing = []
    for sequence_index, original_index in enumerate(selected_page_indices, start=1):
        if original_index > len(image_paths):
            missing.append(original_index)
            continue
        entries.append(
            {
                "index": original_index,
                "original_index": original_index,
                "sequence_index": sequence_index,
                "path": image_paths[original_index - 1],
            }
        )
    return entries, missing


def _write_requested_artifact_aliases(
    output_folder,
    pdf_path,
    contact_sheet_path,
    compare_sheet_path,
    args,
):
    aliases = [
        (contact_sheet_path, output_folder / "contact_sheet.jpg"),
        (compare_sheet_path, output_folder / "compare_sheet.jpg"),
    ]
    if not args.full:
        aliases.append((pdf_path, output_folder / f"pdf_{args.max_images}_pages.pdf"))
    for source, target in aliases:
        try:
            if Path(source).exists() and Path(source).resolve() != Path(target).resolve():
                shutil.copyfile(source, target)
        except OSError:
            pass


MIXED_LANGUAGE_VALIDATION_REASON_PREFIXES = (
    "mixed_language",
    "english_phrase",
    "residual_english",
    "residual_inflected_english",
    "residual_spanish",
    "multilingual_partial",
    "untranslated_english",
    "untranslated_single_english",
    "untranslated_source",
    "residual_source",
    "missing_translation",
    "invalid_translation",
)
def _is_mixed_language_validation_reason(reason):
    return str(reason or "").startswith(MIXED_LANGUAGE_VALIDATION_REASON_PREFIXES)


def _region_source_lexical_words(item):
    """Real source words that stayed visible because the item was not rendered.

    A lexical word is a run of >=2 letters containing at least one vowel. The
    vowel requirement is a generic, language-agnostic filter that drops OCR
    noise and consonant-cluster onomatopoeia (e.g. "MM", "PSST", "GRR") while
    keeping actual dialogue words, so a phantom read never forces a review.
    """
    text = str(item.get("clean_text") or item.get("raw_text") or "")
    return [
        word
        for word in re.findall(r"[A-Za-zÀ-ÿ]{2,}", text)
        if re.search(r"[aeiouyAEIOUYÀ-ÿ]", word)
    ]


_PARTIAL_RECOGNITION_MIN_CONFIDENCE = 0.8


def _item_is_partially_recognized(item):
    """A box far wider than the glyphs the engine returned still holds unread text.

    When recognition collapses a whole word to one or two glyphs, the text stays
    on the art but carries too few letters for the lexical test. The mismatch
    between the box width and the recognised glyph count is a generic, language
    agnostic signal that source text in the region was never read, so the region
    must not be reported as complete.

    Confidence separates this from a phantom read: a genuine partial recognition
    means the engine was sure of the glyphs it returned and simply did not return
    the rest, whereas noise picked up from balloon borders or art comes back with
    low confidence and must not force a review.
    """
    box = item.get("bounding_box") or []
    if len(box) != 4:
        return False
    try:
        _, _, width, height = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    try:
        confidence = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < _PARTIAL_RECOGNITION_MIN_CONFIDENCE:
        return False
    text = str(item.get("clean_text") or item.get("raw_text") or "")
    if width <= 0 or height <= 0 or not re.search(r"[A-Za-zÀ-ÿ]", text):
        return False
    glyphs = len(re.sub(r"\s", "", text))
    if glyphs <= 0:
        return False
    return width > height * 1.15 * glyphs


def _item_is_unassigned_residue_candidate(item):
    """Visible OCR residue with no terminal outcome near rendered story text.

    Some physical leftovers are no longer readable English by the time OCR sees
    them again (for example a word edge read as digits/punctuation).  They still
    cannot disappear from the quality ledger when they sit in the same story
    block as translated text.  This predicate does not make them translatable; it
    only makes the surrounding block require review.
    """
    if str(item.get("translation_final_state") or ""):
        return False
    text = str(item.get("clean_text") or item.get("raw_text") or "").strip()
    if len(re.sub(r"\s", "", text)) < 2:
        return False
    if item.get("confidence") is not None:
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < _PARTIAL_RECOGNITION_MIN_CONFIDENCE:
            return False
    if str(item.get("classification") or "") not in {"unknown", "decorative"}:
        return False
    policy = region_taxonomy.resolve_region_policy(
        original_classification=str(item.get("classification") or ""),
        source_text=text,
        preserve_as_name=bool(item.get("preserve_as_name")),
        evidence={"confidence": item.get("confidence") or 0.0},
    )
    if policy["semantic_role"] in {"credit", "promo", "logo", "sfx"}:
        return False
    return bool(re.search(r"[A-Za-z0-9!?',.]", text))


def _item_is_rendered(item):
    return (
        str(item.get("translation_final_state") or "") == "translated"
        and bool(item.get("redrawn"))
    )


def _boxes_form_one_speech_block(a, b, *, allow_small_residual=False):
    """True when two boxes read as one balloon block (stacked or same line).

    Uses only generic geometry: comparable text height, strong overlap on one
    axis and a small gap on the other. This associates split lines of the same
    balloon without any chapter-, word- or coordinate-specific rule.
    """
    try:
        ax, ay, aw, ah = (float(v) for v in a)
        bx, by, bw, bh = (float(v) for v in b)
    except (TypeError, ValueError):
        return False
    if min(aw, ah, bw, bh) <= 0:
        return False
    height_ratio = min(ah, bh) / max(ah, bh)
    if height_ratio < 0.4 and not (
        allow_small_residual and height_ratio >= 0.18
    ):
        # Very different font scale is normally not a sibling.  The exception is
        # a small OCR-partial residue directly attached to a rendered story block:
        # this is exactly how a leftover word/fragment can survive at the edge of
        # a translated balloon while the main region looks complete.
        return False
    avg_height = (ah + bh) / 2.0
    horizontal_overlap = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    vertical_overlap = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    horizontal_ratio = horizontal_overlap / min(aw, bw)
    vertical_ratio = vertical_overlap / min(ah, bh)
    vertical_gap = max(ay, by) - min(ay + ah, by + bh)
    horizontal_gap = max(ax, bx) - min(ax + aw, bx + bw)
    stacked = horizontal_ratio >= 0.35 and vertical_gap <= 0.8 * avg_height
    same_line = vertical_ratio >= 0.35 and horizontal_gap <= 0.8 * avg_height
    return stacked or same_line


def _box_covers_with_tolerance(outer, inner, *, tolerance=6):
    try:
        ox, oy, ow, oh = (float(v) for v in outer)
        ix, iy, iw, ih = (float(v) for v in inner)
    except (TypeError, ValueError):
        return False
    if min(ow, oh, iw, ih) <= 0:
        return False
    return (
        ix >= ox - tolerance
        and iy >= oy - tolerance
        and ix + iw <= ox + ow + tolerance
        and iy + ih <= oy + oh + tolerance
    )


def _residual_is_covered_by_render_cleanup(rendered_item, residual_item):
    residual_box = residual_item.get("bounding_box")
    if not residual_box:
        return False
    for cleanup_box in rendered_item.get("cleanup_line_boxes") or []:
        if _box_covers_with_tolerance(cleanup_box, residual_box):
            return True
    return False


def _incomplete_speech_region_coverage(states):
    """Rendered speech/narration lines whose balloon still shows source text.

    A region is incomplete when a translated-and-rendered speech/narration line
    shares a balloon block with another line that still carries source words but
    was never rendered (dropped, decorative, SFX, manual review, preserved).
    """
    violations = []
    for state in states:
        items = [
            item
            for item in state.get("debug_data", {}).get("items", [])
            if item.get("bounding_box")
        ]
        rendered = [
            item
            for item in items
            if _story_translation_required(item)
            and _item_is_rendered(item)
        ]
        residual = [
            item
            for item in items
            if not _item_is_rendered(item)
            and (
                _region_source_lexical_words(item)
                or _item_is_partially_recognized(item)
                or _item_is_unassigned_residue_candidate(item)
            )
        ]
        for line in rendered:
            for other in residual:
                if other is line:
                    continue
                if _boxes_form_one_speech_block(
                    line["bounding_box"],
                    other["bounding_box"],
                    allow_small_residual=(
                        _item_is_partially_recognized(other)
                        or _item_is_unassigned_residue_candidate(other)
                    ),
                ):
                    if _residual_is_covered_by_render_cleanup(line, other):
                        continue
                    violations.append(
                        {
                            "page": state.get("index"),
                            "rendered_id": line.get("id"),
                            "residual_id": other.get("id"),
                            "residual_text": str(
                                other.get("clean_text")
                                or other.get("raw_text")
                                or ""
                            ),
                        }
                    )
                    break
    return violations


def _item_source_text(item):
    return str(item.get("clean_text") or item.get("raw_text") or "").strip()


def _story_translation_required(item):
    """Whether this visible text is story-bearing and must reach a terminal outcome.

    Legacy labels are too coarse: real dialogue/system text can arrive as
    ``unknown`` or ``decorative`` when the font/container is unusual, while scan
    credits and promo pages can arrive as ``speech``/``narration``.  The
    denominator therefore follows the semantic taxonomy, not the old visual
    bucket alone.
    """
    classification = str(item.get("classification") or "")
    if classification == "sfx" and not config.TRANSLATE_SFX:
        return False
    policy = region_taxonomy.resolve_region_policy(
        original_classification=classification,
        source_text=_item_source_text(item),
        preserve_as_name=bool(item.get("preserve_as_name")),
        evidence={"confidence": item.get("confidence") or 0.0},
    )
    if not region_taxonomy.is_translatable(policy["normalized_classification"]):
        return False
    return region_taxonomy.weak_label_semantic_promotion_allowed(
        classification,
        _item_source_text(item),
    )


def _ordinary_story_residual_required(item):
    """Whether a residual is an ordinary-story product blocker.

    The physical ledger intentionally keeps every visible retained source.  This
    narrower subgate separates story content a Beta reader expects in PT-BR from
    short SFX/garbled review remnants that may keep the global physical gate in
    review without being ordinary dialogue/narration.
    """

    if not _story_translation_required(item):
        return False
    text = _item_source_text(item)
    compact = re.sub(r"[^A-Za-zÀ-ÿ]", "", text)
    final_reason = str(item.get("translation_final_reason") or "")
    if final_reason in {
        "untranslated_source_after_retries",
        "translation_not_selected",
    } and len(compact) <= 7:
        return False
    return True


# The four things a physically retained source region can actually be. Only the
# first is a product blocker: a reader who never gets "TAK" in Portuguese has
# lost nothing, a reader who never gets a line of dialogue has lost the story.
RESIDUAL_ORDINARY_STORY = "ordinary_story"
RESIDUAL_SFX_EFFECT = "sfx_effect"
RESIDUAL_PROMO = "promo"
RESIDUAL_CREDIT_LOGO = "credit_logo"
PHYSICAL_RESIDUAL_CLASSES = (
    RESIDUAL_ORDINARY_STORY,
    RESIDUAL_SFX_EFFECT,
    RESIDUAL_PROMO,
    RESIDUAL_CREDIT_LOGO,
)


def _physical_residual_class(item):
    """Which of the four classes this retained source region belongs to.

    #84F13 shipped exactly three physical residuals - ``TAK``, ``TUR`` and
    ``TRNDGE`` - and the balloon detector had labelled all three ``speech``,
    because they sit inside balloon-like containers.  They are onomatopoeia, and
    the chapter hard-failed its physical gate for them.  Classifying by the
    *shape of the text* rather than by the container label is what tells an
    effect remnant apart from a lost line of dialogue, and it is the same
    taxonomy the translation policy already uses - no new detector, and no
    per-page exception.
    """

    text = _item_source_text(item)
    if region_taxonomy.looks_like_watermark(text) or region_taxonomy.looks_like_url(text):
        return RESIDUAL_PROMO
    if region_taxonomy.looks_like_credit(text):
        return RESIDUAL_CREDIT_LOGO
    if _ordinary_story_residual_required(item):
        return RESIDUAL_ORDINARY_STORY
    return RESIDUAL_SFX_EFFECT


def _physically_rendered_without_source(item):
    """Whether the shipped page shows PT-BR and no source English for this region.

    The review verdict and the physical fact are different questions.  A region
    that shipped under ``RENDER_WITH_REVIEW`` passed the source-removal axis by
    construction - ``render_disposition`` returns ``do_not_render`` when the
    source survives - and had its Portuguese drawn, so it is still a structured
    review item everywhere else, but it is not a source residual.  #84F9 listed
    several such regions as ordinary-story residuals purely because they kept a
    review terminal state.  The two art reasons that literally mean "source
    lettering survived the cleanup" stay residual.
    """

    if not item.get("redrawn"):
        return False
    if str(item.get("render_disposition") or "") not in {
        RENDER_CLEAN,
        RENDER_WITH_REVIEW,
    }:
        return False
    if str(item.get("art_reconstruction_reason") or "") in (
        RESIDUAL_SOURCE_LETTERING_REASONS
    ):
        return False
    return bool(str(item.get("translation") or "").strip())


def _translation_quality_accounting(states):
    terminal_counts = {
        state: 0 for state in sorted(TRANSLATION_TERMINAL_STATES)
    }
    result = {
        "detected_translatable": 0,
        "sent_to_translation": 0,
        "translated": 0,
        "translated_rendered": 0,
        "rejected": 0,
        "manual_review": 0,
        "preserved_original": 0,
        "translation_failed": 0,
        "translation_unresolved": 0,
        "translation_attempts_exhausted": 0,
        "translation_review_required": 0,
        "source_fallback_prevented": 0,
        "trusted_translation_missing": 0,
        "skipped_with_reason": 0,
        "source_language_residual": 0,
        "missing_candidate": 0,
        "candidate_equals_source": 0,
        "proper_name_preserved": 0,
        "ocr_unintelligible_source": 0,
        "invalid_candidate": 0,
        # SEMANTIC-RUNTIME-001: the semantic verdict is accounted for explicitly, so a
        # region carrying a review reason can never also be counted as semantically
        # clean. Every checked region lands in exactly one of the three buckets.
        "semantic_checked": 0,
        "semantic_clean": 0,
        "semantic_review": 0,
        "semantic_rejected": 0,
        "semantic_review_ids": [],
        "translation_not_applied": 0,
        "missing_terminal_state": 0,
        "incomplete_region_coverage": 0,
        "terminal_state_counts": terminal_counts,
        "accounting_closed": False,
        "requires_review": False,
        "quality_passed": False,
    }
    coverage_violations = _incomplete_speech_region_coverage(states)
    result["incomplete_region_coverage"] = len(coverage_violations)
    result["incomplete_region_coverage_details"] = coverage_violations[:50]
    translatable_items = [
        item
        for state in states
        for item in state.get("debug_data", {}).get("items", [])
        if _story_translation_required(item)
    ]
    result["detected_translatable"] = len(translatable_items)
    for item in translatable_items:
        state = str(item.get("translation_final_state") or "")
        reason = str(
            item.get("translation_final_reason")
            or item.get("translation_validation_reason")
            or ""
        )
        source = re.sub(r"\s+", " ", str(item.get("clean_text") or "")).strip()
        candidate = re.sub(
            r"\s+", " ", str(item.get("translation_candidate") or "")
        ).strip()
        if item.get("sent_to_nvidia"):
            result["sent_to_translation"] += 1
        if state not in terminal_counts:
            result["missing_terminal_state"] += 1
            continue
        terminal_counts[state] += 1
        if state != "preserved_original":
            result[state] += 1
        if state == "translated" and item.get("redrawn"):
            result["translated_rendered"] += 1
        if item.get("translation_unresolved"):
            result["translation_unresolved"] += 1
        if item.get("translation_attempts_exhausted"):
            result["translation_attempts_exhausted"] += 1
        if item.get("manual_review_required"):
            result["translation_review_required"] += 1
        if item.get("source_fallback_prevented"):
            result["source_fallback_prevented"] += 1
        if item.get("trusted_translation_missing"):
            result["trusted_translation_missing"] += 1
        if item.get("preserved_original"):
            result["preserved_original"] += 1
        # A balloon holding only a character's name has no sentence to translate:
        # the name itself is the correct output. Counting it as an untranslated
        # source held whole chapters in review over text that was already right.
        proper_name_only = reason == PROPER_NAME_ONLY_REASON
        if proper_name_only:
            result["proper_name_preserved"] += 1
        # An unread source is held for review like any other defect, but it is a read
        # failure, not dialogue the translator skipped: counting it as untranslated
        # source overstates the translator's residual and hides the real defect.
        if reason == OCR_UNINTELLIGIBLE_SOURCE_REASON:
            result["ocr_unintelligible_source"] += 1
        if not candidate:
            result["missing_candidate"] += 1
        if (
            not proper_name_only
            and source
            and candidate
            and source.casefold() == candidate.casefold()
        ):
            result["candidate_equals_source"] += 1
        if not item.get("translation_valid", False):
            result["invalid_candidate"] += 1
        if reason.startswith(
            (
                "residual_source_language",
                "untranslated_source",
                "missing_translation_candidate",
            )
        ) or _is_mixed_language_validation_reason(reason):
            result["source_language_residual"] += 1
        # SEMANTIC-RUNTIME-001: one canonical bucket per checked region. The review
        # reason is written by the same gate that writes the rejection, so the two can
        # never disagree here, and a region with a reason can never count as clean.
        if item.get("sent_to_nvidia") and candidate:
            result["semantic_checked"] += 1
            semantic_reason = str(item.get("semantic_review_reason") or "")
            semantic_rejected = reason == "semantic_fidelity_failed_after_retries" or (
                semantic_fidelity.is_fidelity_reason(
                    item.get("translation_validation_reason")
                )
            )
            if semantic_rejected:
                result["semantic_rejected"] += 1
            elif semantic_reason:
                result["semantic_review"] += 1
                result["semantic_review_ids"].append(
                    f"p{int(item.get('page') or 0):03}:"
                    f"{item.get('region_id') or item.get('id') or ''}:{semantic_reason}"
                )
            else:
                result["semantic_clean"] += 1

    result["translation_not_applied"] = (
        result["detected_translatable"] - result["translated_rendered"]
    )
    terminal_total = sum(terminal_counts.values())
    result["accounting_closed"] = bool(
        result["missing_terminal_state"] == 0
        and terminal_total == result["detected_translatable"]
    )
    result["requires_review"] = bool(
        result["manual_review"]
        or result["rejected"]
        or result["translation_failed"]
        or result["translation_unresolved"]
        or result["trusted_translation_missing"]
        or result["missing_candidate"]
        or result["candidate_equals_source"]
        or result["source_language_residual"]
        or result["invalid_candidate"]
        or result["incomplete_region_coverage"]
        # A region the semantic gate marked for review is rendered but not clean:
        # the chapter cannot pass quality with one still outstanding.
        or result["semantic_review"]
        or result["semantic_rejected"]
        or not result["accounting_closed"]
    )
    result["quality_passed"] = bool(
        result["accounting_closed"] and not result["requires_review"]
    )
    return result


def _item_region_id(state, item):
    page = int(state.get("index") or item.get("page") or 0)
    group_id = str(item.get("id") or "")
    return f"p{page:03}:{group_id}" if group_id else f"p{page:03}"


def _render_plan_accounting(states):
    """Reconcile story regions through render-plan terminal output paths.

    This is deliberately an accounting layer, not a new renderer.  The renderer can
    keep failing closed, but every story-bearing region must now be explainable as
    exactly one of: clean render, rendered-with-residual, structured review,
    proper-name preservation, or unaccounted defect.  That prevents the split-brain
    state where translation/physical validation expect a story region while the
    render path silently never selects it.
    """

    coverage = _incomplete_speech_region_coverage(states)
    residual_by_rendered = {
        f"p{int(item.get('page') or 0):03}:{item.get('rendered_id')}"
        for item in coverage
        if item.get("page") and item.get("rendered_id")
    }
    residual_ids = {
        f"p{int(item.get('page') or 0):03}:{item.get('residual_id')}"
        for item in coverage
        if item.get("page") and item.get("residual_id")
    }
    result = {
        "story_expected_ids": [],
        "story_with_valid_candidate_ids": [],
        "render_selected_ids": [],
        "render_skipped_ids": [],
        "rendered_clean_ids": [],
        "rendered_with_residual_ids": [],
        "structured_review_ids": [],
        "semantic_review_ids": [],
        "proper_noun_preserved_ids": [],
        "unaccounted_ids": [],
        "render_skipped_reasons": {},
        "skipped_without_reason": 0,
        "coverage_residual_ids": sorted(residual_ids),
    }
    for state in states:
        for item in state.get("debug_data", {}).get("items", []):
            if not _story_translation_required(item):
                continue
            region_id = _item_region_id(state, item)
            result["story_expected_ids"].append(region_id)
            candidate = re.sub(
                r"\s+",
                " ",
                str(
                    item.get("translation_candidate")
                    or item.get("translation")
                    or item.get("raw_provider_candidate")
                    or item.get("rejected_translation")
                    or ""
                ),
            ).strip()
            final_state = str(item.get("translation_final_state") or "")
            final_reason = str(
                item.get("translation_final_reason")
                or item.get("translation_validation_reason")
                or item.get("ignore_reason")
                or ""
            )
            translated = bool(str(item.get("translation") or "").strip())
            valid = bool(item.get("translation_valid"))
            redrawn = bool(item.get("redrawn"))
            if candidate and valid:
                result["story_with_valid_candidate_ids"].append(region_id)
            render_selected = bool(
                redrawn
                or item.get("visual_attempts")
                or item.get("visual_validation")
                or item.get("mask_metrics")
            )
            if render_selected:
                result["render_selected_ids"].append(region_id)
            else:
                result["render_skipped_ids"].append(region_id)
                result["render_skipped_reasons"][region_id] = (
                    final_reason or "missing_render_skipped_reason"
                )
                if not final_reason:
                    result["skipped_without_reason"] += 1

            semantic_reason = str(item.get("semantic_review_reason") or "")
            if semantic_reason:
                # SEMANTIC-RUNTIME-001: RENDER_WITH_REVIEW. The Portuguese is drawn -
                # holding it would put the English source back on the page - but the
                # region is a structured review item, never a clean render.
                result["structured_review_ids"].append(region_id)
                result["semantic_review_ids"].append(region_id)
            elif final_state == "translated" and translated and valid and redrawn:
                if region_id in residual_by_rendered:
                    result["rendered_with_residual_ids"].append(region_id)
                else:
                    result["rendered_clean_ids"].append(region_id)
            elif final_reason == PROPER_NAME_ONLY_REASON:
                result["proper_noun_preserved_ids"].append(region_id)
            elif final_reason or item.get("manual_review_required"):
                result["structured_review_ids"].append(region_id)
            else:
                result["unaccounted_ids"].append(region_id)

    for key in (
        "story_expected_ids",
        "story_with_valid_candidate_ids",
        "render_selected_ids",
        "render_skipped_ids",
        "rendered_clean_ids",
        "rendered_with_residual_ids",
        "structured_review_ids",
        "semantic_review_ids",
        "proper_noun_preserved_ids",
        "unaccounted_ids",
    ):
        result[key] = sorted(dict.fromkeys(result[key]))
    result["counts"] = {
        "story_expected": len(result["story_expected_ids"]),
        "valid_candidate": len(result["story_with_valid_candidate_ids"]),
        "render_selected": len(result["render_selected_ids"]),
        "render_skipped": len(result["render_skipped_ids"]),
        "rendered_clean": len(result["rendered_clean_ids"]),
        "rendered_with_residual": len(result["rendered_with_residual_ids"]),
        "structured_review": len(result["structured_review_ids"]),
        "semantic_review": len(result["semantic_review_ids"]),
        "proper_noun_preserved": len(result["proper_noun_preserved_ids"]),
        "unaccounted": len(result["unaccounted_ids"]),
        "skipped_without_reason": int(result["skipped_without_reason"]),
    }
    return result


def _physical_residual_accounting(states):
    """Account final-render source retention separately from logical quality."""

    result = {
        "physical_regions_expected": 0,
        "physical_regions_translated": 0,
        "physical_regions_preserved": 0,
        "physical_regions_review_source_retained": 0,
        "physical_regions_rendered_with_review": 0,
        "physical_regions_render_failed": 0,
        "physical_regions_other_explicit": 0,
        "physical_source_residual_count": 0,
        "physical_source_residual_group_ids": [],
        "physical_gate_passed": False,
    }
    # A denominator is only evidence if the text analysis that produces it ran.
    # Counting pages by upstream outcome is what lets a zero expected-region
    # count be read as "no source text" or as "the text stage never worked",
    # instead of collapsing both into a vacuous pass.
    population = {
        "pages_total": 0,
        "pages_no_text_proven": 0,
        "pages_text_analyzed": 0,
        "pages_upstream_failed": 0,
        "pages_unresolved": 0,
    }
    # Nested and optional: a manifest written before this contract simply omits
    # the block and stays schema-valid instead of gaining fabricated zeros.
    completeness_counts = {
        "checked": 0,
        "pass": 0,
        "review": 0,
        "fail": 0,
        "unavailable": 0,
    }
    residual_ids = []
    ordinary_story_residual_ids = []
    residual_class_counts = {name: 0 for name in PHYSICAL_RESIDUAL_CLASSES}
    residual_class_ids = {name: [] for name in PHYSICAL_RESIDUAL_CLASSES}

    def record_residual(region_id, item):
        """File one retained source region under its class, once."""
        residual_class = _physical_residual_class(item)
        residual_class_counts[residual_class] += 1
        residual_class_ids[residual_class].append(region_id)
        if residual_class == RESIDUAL_ORDINARY_STORY:
            ordinary_story_residual_ids.append(region_id)

    completeness_ids = []
    missing_tokens = set()
    for state in states:
        page = int(state.get("index") or 0)
        population["pages_total"] += 1
        if state.get("ocr_error") or state.get("status") == "completed_with_error":
            population["pages_upstream_failed"] += 1
        elif (state.get("precheck") or {}).get("skip"):
            population["pages_no_text_proven"] += 1
        elif state.get("status") == "completed":
            population["pages_text_analyzed"] += 1
        else:
            population["pages_unresolved"] += 1
        for item in state.get("debug_data", {}).get("items", []):
            if not _story_translation_required(item):
                continue
            result["physical_regions_expected"] += 1
            group_id = str(item.get("id") or "")
            region_id = f"p{page:03}:{group_id}" if group_id else f"p{page:03}"
            final_state = str(item.get("translation_final_state") or "")
            final_reason = str(item.get("translation_final_reason") or "")
            translated = bool(str(item.get("translation") or "").strip())
            valid = bool(item.get("translation_valid"))
            redrawn = bool(item.get("redrawn"))
            preserved = bool(item.get("preserved_original"))
            review = bool(item.get("manual_review_required"))

            # Source completeness is a separate guard from the render outcome: a
            # group whose owned source content went missing upstream cannot be a
            # fully passing physical region even when the render itself succeeded.
            completeness = str(item.get("source_completeness_status") or "")
            if completeness in completeness_counts:
                completeness_counts["checked"] += 1
                completeness_counts[completeness] += 1
            if completeness in {"fail", "review"}:
                completeness_ids.append(region_id)
                missing_tokens.update(
                    (item.get("source_completeness") or {}).get(
                        "unexplained_missing_tokens"
                    )
                    or []
                )
                result["physical_regions_review_source_retained"] += 1
                residual_ids.append(region_id)
                record_residual(region_id, item)
                continue

            if final_state == "translated" and translated and valid and redrawn:
                result["physical_regions_translated"] += 1
                continue
            if final_reason == PROPER_NAME_ONLY_REASON:
                result["physical_regions_preserved"] += 1
                continue
            # The final rendered state is authoritative for *physical* retention:
            # review status routes the region to the review queue, it does not put
            # English pixels back on the page.
            if _physically_rendered_without_source(item):
                result["physical_regions_rendered_with_review"] += 1
                continue

            if final_state == "translated" and translated and valid and not redrawn:
                result["physical_regions_render_failed"] += 1
            elif review or preserved or final_state in {
                "manual_review",
                "skipped_with_reason",
                "translation_failed",
                "translation_unresolved",
                "translation_attempts_exhausted",
            }:
                result["physical_regions_review_source_retained"] += 1
            else:
                result["physical_regions_other_explicit"] += 1
            residual_ids.append(region_id)
            record_residual(region_id, item)

    incomplete_coverage = _incomplete_speech_region_coverage(states)
    if incomplete_coverage:
        result["physical_incomplete_region_coverage_count"] = len(incomplete_coverage)
        result["physical_incomplete_region_coverage_details"] = incomplete_coverage[:50]
        for violation in incomplete_coverage:
            page = int(violation.get("page") or 0)
            residual_id = str(violation.get("residual_id") or "")
            region_id = (
                f"p{page:03}:{residual_id}"
                if page and residual_id
                else residual_id or f"p{page:03}"
            )
            if region_id and region_id not in residual_ids:
                residual_ids.append(region_id)
            # A *rendered* PT-BR line whose own balloon still shows source words
            # is an ordinary-story defect whatever the leftover says: the reader
            # sees Portuguese and English stacked in one bubble. This invariant
            # is about the rendered line, not about the remnant's class.
            if region_id and region_id not in ordinary_story_residual_ids:
                ordinary_story_residual_ids.append(region_id)
                residual_class_counts[RESIDUAL_ORDINARY_STORY] += 1
                residual_class_ids[RESIDUAL_ORDINARY_STORY].append(region_id)

    result["physical_residual_classes"] = residual_class_counts
    result["physical_residual_class_ids"] = {
        name: ids[:200] for name, ids in residual_class_ids.items()
    }
    result["physical_source_residual_count"] = len(residual_ids)
    result["physical_source_residual_group_ids"] = residual_ids[:200]
    result["ordinary_story_physical_residual_count"] = len(
        ordinary_story_residual_ids
    )
    result["ordinary_story_physical_residual_ids"] = (
        ordinary_story_residual_ids[:200]
    )
    if completeness_counts["checked"]:
        result["source_completeness"] = {
            **completeness_counts,
            # Named, not implied: these counts are scored against the group's
            # provenance ancestry, including predecessors a retry superseded,
            # not against the lines the last pass happened to leave behind.
            "basis": source_completeness.EXPECTED_SOURCE_BASIS,
            "group_ids": completeness_ids[:200],
            "missing_tokens": sorted(missing_tokens)[:50],
        }
    # #84F14: the gate used to require *zero* physical residuals, which made an
    # onomatopoeia remnant fail the chapter exactly as hard as a lost line of
    # dialogue - #84F13 was held back by "TAK", "TUR" and "TRNDGE" alone. The
    # residual ledger is unchanged and still lists all of them; what changed is
    # who may block. A retained SFX/promo/credit region is an accounted outcome
    # that routes to review, ordinary story English is still a hard failure.
    non_story_residuals = sum(
        residual_class_counts[name]
        for name in PHYSICAL_RESIDUAL_CLASSES
        if name != RESIDUAL_ORDINARY_STORY
    )
    regions_accounted = (
        result["physical_regions_expected"]
        == result["physical_regions_translated"]
        + result["physical_regions_preserved"]
        + result["physical_regions_rendered_with_review"]
        + non_story_residuals
        and result["ordinary_story_physical_residual_count"] == 0
    )
    # A recorded upstream failure means the chapter was only partially examined:
    # whatever regions survived describe the pages that worked, never the whole
    # chapter. This is a population invariant above the per-region residual and
    # source-completeness checks, which stay exactly as they were.
    coverage_complete = population["pages_upstream_failed"] == 0
    # Zero expected regions is a positive claim about the source, so it needs
    # positive evidence: every page either proved it had no text or was actually
    # analysed. Pages with no recorded outcome prove nothing, so 0/0 stops being
    # a pass by default and becomes a question the run has to answer.
    zero_denominator_proven = bool(population["pages_total"]) and (
        population["pages_unresolved"] == 0
    )
    result["physical_population"] = population
    result["physical_population_status"] = (
        "complete" if coverage_complete else "incomplete"
    )
    if result["physical_regions_expected"] == 0:
        if not coverage_complete:
            reason = "upstream_text_analysis_incomplete"
        elif not zero_denominator_proven:
            reason = "source_text_evidence_missing"
        else:
            reason = "no_translatable_source_text"
        result["zero_denominator_reason"] = reason
        decision = "pass" if reason == "no_translatable_source_text" else "review"
    elif not coverage_complete or not regions_accounted:
        decision = "review"
    else:
        decision = "pass"
    result["physical_decision"] = decision
    result["physical_gate_passed"] = decision == "pass"
    return result


def _user_visible_output_accounting(states):
    """What a Beta reader actually sees, counted in the terms that block Setup.

    The physical ledger answers "is the English gone"; the translation ledger
    answers "did every region reach a terminal state".  Neither answers the only
    question Setup cares about: is what is printed on the page readable
    Portuguese.  #84F13 shipped three regions whose Portuguese still carried the
    corrupt source token ("UM RATO DO SLLM") and passed every existing gate,
    because ``review`` was one bucket and every member of it renders.

    So ``review`` is split.  ``review_renderable`` may remain in a Beta build -
    it is honest doubt over usable output.  ``review_unusable`` may not: the
    finding itself proves the reader is looking at garbage.
    """

    physical = _physical_residual_accounting(states)
    ordinary_residual_ids = set(physical["ordinary_story_physical_residual_ids"])
    result = {
        "ordinary_story_english_visible": 0,
        "missing_ptbr": 0,
        "semantic_clean": 0,
        "semantic_review_renderable": 0,
        "semantic_review_unusable": 0,
        "semantic_reject": 0,
        "semantic_bad_clean": 0,
        "art_bad_clean": 0,
        "semantic_review_unusable_ids": [],
        "semantic_bad_clean_ids": [],
    }
    for state in states:
        page = int(state.get("index") or 0)
        for item in state.get("debug_data", {}).get("items", []):
            if not _story_translation_required(item):
                continue
            group_id = str(item.get("id") or "")
            region_id = f"p{page:03}:{group_id}" if group_id else f"p{page:03}"
            if region_id in ordinary_residual_ids:
                result["ordinary_story_english_visible"] += 1
                continue
            if not _physically_rendered_without_source(item):
                # A non-story residual: accounted physically, nothing to read.
                continue
            if not str(item.get("translation") or "").strip():
                result["missing_ptbr"] += 1
                continue

            review_reason = str(item.get("semantic_review_reason") or "")
            final_state = str(item.get("translation_final_state") or "")
            if final_state in {"rejected", "unresolved"}:
                result["semantic_reject"] += 1
            elif review_reason or final_state in REVIEW_TERMINAL_STATES:
                usability = semantic_fidelity.review_usability(
                    review_reason or str(item.get("translation_final_reason") or "")
                )
                if usability == semantic_fidelity.REVIEW_UNUSABLE:
                    result["semantic_review_unusable"] += 1
                    result["semantic_review_unusable_ids"].append(region_id)
                else:
                    result["semantic_review_renderable"] += 1
            else:
                # Shipped as clean. It is only *proven* bad when the run itself
                # recorded a blocking finding or surviving source lettering and
                # rendered anyway - a wrong word sense nothing detected cannot be
                # counted here, and is not silently turned into a zero.
                proven_bad = semantic_fidelity.is_fidelity_reason(
                    str(item.get("translation_validation_reason") or "")
                )
                if proven_bad:
                    result["semantic_bad_clean"] += 1
                    result["semantic_bad_clean_ids"].append(region_id)
                else:
                    result["semantic_clean"] += 1
            if str(item.get("art_reconstruction_reason") or "") in (
                RESIDUAL_SOURCE_LETTERING_REASONS
            ):
                result["art_bad_clean"] += 1

    result["setup_ready"] = not any(
        result[name]
        for name in (
            "ordinary_story_english_visible",
            "missing_ptbr",
            "semantic_bad_clean",
            "semantic_review_unusable",
            "art_bad_clean",
        )
    )
    return result


def _build_quality_report(report, states, translation_retry_records):
    pages = []
    translation_accounting = _translation_quality_accounting(states)
    physical_accounting = _physical_residual_accounting(states)
    render_plan_accounting = _render_plan_accounting(states)
    totals = {
        "groups_detected": 0,
        "groups_suspicious": 0,
        "selective_fallback_attempts": 0,
        "selective_fallbacks_used": 0,
        "rapidocr_region_retry_requested": 0,
        "rapidocr_region_retry_selected": 0,
        "rapidocr_region_retry_failed": 0,
        "fallbacks_to_paddle_mobile": 0,
        "fallbacks_to_paddle_full": 0,
        "ocr_repairs": 0,
        "ocr_repairs_rejected": 0,
        "groups_reverted_for_visual_safety": 0,
        "manual_review_required_groups": 0,
        "translation_unresolved": 0,
        "translation_attempts_exhausted": 0,
        "source_fallback_prevented": 0,
        "trusted_translation_missing": 0,
        "translations_retried": len(translation_retry_records),
        "translations_rejected": 0,
        "external_narrations_translated": 0,
        "sfx_preserved": 0,
        "pages_reprocessed": 0,
        "pages_visual_validation_failed": 0,
        "mixed_language_items": 0,
        "text_overflow_items": 0,
        "white_patch_rejections": 0,
        "broad_mask_rejections": 0,
        "reconstruction_regions_expected": 0,
        "reconstruction_clean": 0,
        "reconstruction_review": 0,
        "flat_patch_suspected": 0,
        "seam_suspected": 0,
        "rectangular_line_mask_rejections": 0,
        "background_type_counts": {},
        "translation_accounting": translation_accounting,
        "render_plan_accounting": render_plan_accounting,
        "physical_quality": physical_accounting,
        "physical_regions_expected": physical_accounting["physical_regions_expected"],
        "physical_regions_translated": physical_accounting["physical_regions_translated"],
        "physical_regions_preserved": physical_accounting["physical_regions_preserved"],
        "physical_regions_review_source_retained": physical_accounting[
            "physical_regions_review_source_retained"
        ],
        "physical_regions_render_failed": physical_accounting[
            "physical_regions_render_failed"
        ],
        "physical_source_residual_count": physical_accounting[
            "physical_source_residual_count"
        ],
        "physical_source_residual_group_ids": physical_accounting[
            "physical_source_residual_group_ids"
        ],
        "ordinary_story_physical_residual_count": physical_accounting[
            "ordinary_story_physical_residual_count"
        ],
        "ordinary_story_physical_residual_ids": physical_accounting[
            "ordinary_story_physical_residual_ids"
        ],
        "physical_residual_classes": physical_accounting["physical_residual_classes"],
        "physical_residual_class_ids": physical_accounting[
            "physical_residual_class_ids"
        ],
        "user_visible_output": _user_visible_output_accounting(states),
        "physical_gate_passed": physical_accounting["physical_gate_passed"],
        "physical_decision": physical_accounting["physical_decision"],
        "physical_population_status": physical_accounting[
            "physical_population_status"
        ],
        "physical_population": physical_accounting["physical_population"],
        "zero_denominator_reason": physical_accounting.get(
            "zero_denominator_reason", ""
        ),
        "source_completeness": physical_accounting.get("source_completeness", {}),
        "speech_container_reocr": summarize_speech_container_reocr(
            [
                record
                for state in states
                for record in state.get("speech_container_reocr", []) or []
            ]
        ),
    }

    for state in sorted(states, key=lambda item: item["index"]):
        debug = state.get("debug_data", {})
        items = debug.get("items", [])
        fallback_records = debug.get("selective_ocr_fallbacks", [])
        rapidocr_records = debug.get("rapidocr_region_recovery", [])
        suspicious = [
            item
            for item in items
            if item.get("quality_reasons")
            or float(item.get("quality_score") or 1.0) < config.OCR_GROUP_MIN_QUALITY_SCORE
        ]
        mixed = [
            item
            for item in items
            if _is_mixed_language_validation_reason(
                item.get("translation_validation_reason")
            )
        ]
        visual_failures = [
            item
            for item in items
            if (item.get("visual_validation") or {})
            and not (item.get("visual_validation") or {}).get(
                "visual_validation_passed",
                True,
            )
        ]
        overflow = [
            item
            for item in items
            if float(item.get("text_overflow_ratio") or 0.0)
            > config.MAX_TEXT_OVERFLOW_RATIO
        ]
        narrations = [
            item
            for item in items
            if item.get("classification") == "narration"
            and item.get("translation_final_state") == "translated"
        ]
        sfx = [
            item
            for item in items
            if item.get("classification") == "sfx"
            and item.get("ignored")
            and not item.get("sent_to_nvidia")
        ]

        totals["groups_detected"] += debug.get("group_count", 0)
        totals["groups_suspicious"] += len(suspicious)
        totals["selective_fallback_attempts"] += len(fallback_records)
        totals["selective_fallbacks_used"] += sum(
            1 for record in fallback_records if record.get("fallback_used")
        )
        totals["rapidocr_region_retry_requested"] += len(rapidocr_records)
        totals["rapidocr_region_retry_selected"] += sum(
            1 for record in rapidocr_records if record.get("selection") == "attempt_2"
        )
        totals["rapidocr_region_retry_failed"] += sum(
            1
            for record in rapidocr_records
            if record.get("selection") != "attempt_2"
        )
        totals["fallbacks_to_paddle_mobile"] += sum(
            1
            for record in fallback_records
            if record.get("fallback_used")
            and record.get("fallback_variant") == "paddle_mobile"
        )
        totals["fallbacks_to_paddle_full"] += sum(
            1
            for record in fallback_records
            if record.get("fallback_used")
            and record.get("fallback_variant") == "paddle_full"
        )
        totals["ocr_repairs"] += len(debug.get("text_repairs", []))
        totals["ocr_repairs_rejected"] += len(
            debug.get("rejected_text_repairs", [])
        )
        totals["groups_reverted_for_visual_safety"] += sum(
            1
            for item in items
            if item.get("sent_to_nvidia") and not item.get("redrawn")
        )
        totals["manual_review_required_groups"] += sum(
            1 for item in items if item.get("manual_review_required")
        )
        totals["translation_unresolved"] += sum(
            1 for item in items if item.get("translation_unresolved")
        )
        totals["translation_attempts_exhausted"] += sum(
            1 for item in items if item.get("translation_attempts_exhausted")
        )
        totals["source_fallback_prevented"] += sum(
            1 for item in items if item.get("source_fallback_prevented")
        )
        totals["trusted_translation_missing"] += sum(
            1 for item in items if item.get("trusted_translation_missing")
        )
        totals["white_patch_rejections"] += sum(
            1
            for item in items
            if (item.get("mask_metrics") or {}).get("white_patch_rejected")
        )
        # Art reconstruction quality is accounted separately from story-text
        # coverage: a region can have every source glyph removed and still be a
        # destroyed piece of artwork, so the two verdicts never share a counter.
        totals["reconstruction_regions_expected"] += sum(
            1 for item in items if item.get("art_reconstruction_status")
        )
        totals["reconstruction_clean"] += sum(
            1 for item in items
            if item.get("art_reconstruction_status") == "clean"
        )
        totals["reconstruction_review"] += sum(
            1 for item in items
            if item.get("art_reconstruction_status") == "review"
        )
        totals["flat_patch_suspected"] += sum(
            1
            for item in items
            if (item.get("mask_metrics") or {}).get("flat_patch_rejected")
        )
        totals["seam_suspected"] += sum(
            1
            for item in items
            if (item.get("mask_metrics") or {}).get("seam_suspected")
        )
        totals["rectangular_line_mask_rejections"] += sum(
            1
            for item in items
            if (item.get("mask_metrics") or {}).get("uniform_light_line_rejected")
            or (item.get("mask_metrics") or {}).get("uniform_dark_line_rejected")
        )
        totals["broad_mask_rejections"] += sum(
            1
            for item in items
            if (item.get("mask_metrics") or {}).get("broad_rectangular_mask")
            and item.get("background_type") not in {"white_balloon", "narration_box"}
        )
        for item in items:
            background_type = item.get("background_type")
            if background_type:
                totals["background_type_counts"][background_type] = (
                    totals["background_type_counts"].get(background_type, 0) + 1
                )
        totals["translations_rejected"] += sum(
            1 for item in items if item.get("rejected_translation")
        )
        totals["external_narrations_translated"] += len(narrations)
        totals["sfx_preserved"] += len(sfx)
        totals["pages_reprocessed"] += int(any(record.get("fallback_used") for record in fallback_records))
        totals["pages_visual_validation_failed"] += int(bool(visual_failures))
        totals["mixed_language_items"] += len(mixed)
        totals["text_overflow_items"] += len(overflow)

        pages.append(
            {
                "index": state["index"],
                "sequence_index": state.get("sequence_index"),
                "original_index": state.get("original_index", state["index"]),
                "status": state.get("status"),
                "output_path": state.get("output_path"),
                "image_path": state.get("image_path"),
                "groups": debug.get("group_count", 0),
                "translated": debug.get("translated_group_count", 0),
                "suspicious_groups": [
                    _quality_item_summary(item) for item in suspicious[:12]
                ],
                "selective_ocr_fallbacks": fallback_records,
                "rapidocr_region_recovery": rapidocr_records,
                "speech_container_reocr": state.get("speech_container_reocr", []),
                "text_repairs": debug.get("text_repairs", []),
                "rejected_text_repairs": debug.get("rejected_text_repairs", []),
                "translation_retries": [
                    record
                    for record in translation_retry_records
                    if record.get("group_id")
                    in {item.get("id") for item in items}
                ],
                "translation_terminal_items": [
                    _quality_item_summary(item)
                    for item in items
                    if _story_translation_required(item)
                ],
                "mixed_language_items": [_quality_item_summary(item) for item in mixed],
                "text_overflow_items": [_quality_item_summary(item) for item in overflow],
                "narrations_translated": [_quality_item_summary(item) for item in narrations],
                "sfx_preserved": [_quality_item_summary(item) for item in sfx],
                "visual_validation_failures": [
                    _quality_item_summary(item) for item in visual_failures
                ],
                "timings": state.get("timings", {}),
            }
        )

    return {
        "summary": {
            "url": report.get("url"),
            "mode": report.get("mode"),
            "ocr_engine": report.get("ocr_engine"),
            "ocr_fallback_engine": report.get("ocr_fallback_engine"),
            "processed_images": report.get("processed_images"),
            "available_valid_images": report.get("available_valid_images"),
            "selected_page_indices": report.get("selected_page_indices"),
            "missing_page_indices": report.get("missing_page_indices"),
            "total_seconds": report.get("total_seconds"),
            "stage_seconds": report.get("stage_seconds"),
            "pdf_path": report.get("pdf_path"),
            "artifact_sha256": report.get("artifact_sha256"),
            "artifact_size_bytes": report.get("artifact_size_bytes"),
            "preview_contact_sheet": report.get("preview_contact_sheet"),
            "preview_compare_sheet": report.get("preview_compare_sheet"),
            "quality_validation": report.get("quality_validation"),
        },
        "totals": totals,
        "translation_retry_records": translation_retry_records,
        "pages": pages,
    }


def _quality_item_summary(item):
    return {
        "id": item.get("id"),
        "region_id": item.get("region_id"),
        "classification": item.get("classification"),
        "background_type": item.get("background_type"),
        "background_metrics": item.get("background_metrics"),
        "source_engine": item.get("source_engine") or item.get("engine"),
        "text": item.get("clean_text"),
        "translation": item.get("translation"),
        "confidence": item.get("confidence"),
        "quality_score": item.get("quality_score"),
        "quality_reasons": item.get("quality_reasons"),
        "fallback_used": item.get("fallback_used"),
        "translation_valid": item.get("translation_valid"),
        "translation_validation_reason": item.get("translation_validation_reason"),
        "translation_retry_count": item.get("translation_retry_count"),
        "translation_candidate": item.get("translation_candidate"),
        "translation_final_state": item.get("translation_final_state"),
        "translation_final_reason": item.get("translation_final_reason"),
        "translation_quality_impact": item.get("translation_quality_impact"),
        "preserved_original": item.get("preserved_original"),
        "text_overflow_ratio": item.get("text_overflow_ratio"),
        "visual_validation": item.get("visual_validation"),
        "visual_attempts": item.get("visual_attempts"),
        "mask_metrics": item.get("mask_metrics"),
        "manual_review_required": item.get("manual_review_required"),
        "art_reconstruction_status": item.get("art_reconstruction_status"),
        "art_reconstruction_reason": item.get("art_reconstruction_reason"),
        "safe_area": item.get("safe_area"),
        "translation_box": item.get("translation_box"),
        "bounding_box": item.get("bounding_box"),
    }


def _quality_report_html(report):
    summary = report["summary"]
    totals = report["totals"]
    rows = []
    for page in report["pages"]:
        rows.append(
            "<tr>"
            f"<td>{page['index']:03}</td>"
            f"<td>{page['groups']}</td>"
            f"<td>{page['translated']}</td>"
            f"<td>{len(page['suspicious_groups'])}</td>"
            f"<td>{sum(1 for r in page['selective_ocr_fallbacks'] if r.get('fallback_used'))}</td>"
            f"<td>{len(page['mixed_language_items'])}</td>"
            f"<td>{len(page['text_overflow_items'])}</td>"
            f"<td>{len(page['visual_validation_failures'])}</td>"
            f"<td><a href=\"pages/{Path(page['output_path']).name}\">final</a></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tradutor.Ia - Quality Report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; background:#15171c; color:#eee; margin:24px; }}
a {{ color:#ff9b6b; }}
.card {{ background:#22252d; border:1px solid #343946; border-radius:12px; padding:16px; margin:16px 0; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th,td {{ border-bottom:1px solid #343946; padding:8px; text-align:left; vertical-align:top; }}
th {{ color:#ffb088; }}
.preview {{ max-width:100%; border-radius:10px; border:1px solid #343946; }}
code {{ color:#ffd1bd; }}
</style>
</head>
<body>
<h1>Tradutor.Ia — Quality Report</h1>
<div class="card">
<p><strong>URL:</strong> {summary.get('url')}</p>
<p><strong>OCR:</strong> {summary.get('ocr_engine')} → fallback {summary.get('ocr_fallback_engine')}</p>
<p><strong>Páginas:</strong> {summary.get('processed_images')} &nbsp; <strong>Tempo:</strong> {summary.get('total_seconds')}s</p>
<p><strong>PDF:</strong> <a href="{Path(summary.get('pdf_path', '')).name}">{summary.get('pdf_path')}</a></p>
</div>
<div class="card">
<h2>Totais</h2>
<pre>{json.dumps(totals, ensure_ascii=False, indent=2)}</pre>
</div>
<div class="card">
<h2>Contact sheet</h2>
<img class="preview" src="contact_sheet.jpg" alt="contact sheet">
</div>
<div class="card">
<h2>Compare sheet</h2>
<img class="preview" src="compare_sheet.jpg" alt="compare sheet">
</div>
<div class="card">
<h2>Páginas</h2>
<table>
<thead><tr><th>Página</th><th>Grupos</th><th>Trad.</th><th>Suspeitos</th><th>Fallbacks</th><th>Misto</th><th>Overflow</th><th>Visual fail</th><th>Link</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>
</body>
</html>
"""


def _chapter_download_items(manifest):
    items = manifest.get("downloaded", [])
    chapter_items = []
    excluded = []
    for item in items:
        # New reports mark the classified reader pages explicitly.  The old hostname check
        # silently discarded every generic reader and also could not survive URL sanitising.
        # Keep it only as a compatibility fallback for legacy cached manifests.
        host = (urlparse(item.get("url", "")).hostname or "").lower()
        if item.get("is_chapter_candidate") or "webtoons.com" in host or "webtoon-phinf.pstatic.net" in host:
            chapter_items.append(item)
        else:
            excluded.append(item)
    manifest["excluded_non_chapter"] = excluded
    return chapter_items


def _valid_download_paths(manifest):
    items = _chapter_download_items(manifest)
    paths = [item.get("path") for item in items]
    if not paths:
        return []
    for item, path in zip(items, paths):
        if not _valid_download_item(item, path):
            return []
        expected_hash = item.get("sha256")
        if not expected_hash or file_sha256(path) != expected_hash:
            return []
    return paths


def _valid_download_item(item, path):
    if not path or not os.path.isfile(path):
        return False
    if not item.get("is_chapter_candidate"):
        return valid_image(path, 480, 220)
    try:
        with Image.open(path) as image:
            image.load()
            return image.width >= 480 and image.height >= 1
    except Exception:
        return False


def _reuse_completed_page(state, previous, force):
    if force:
        return False

    if (
        previous
        and previous.get("status") == "completed"
        and previous.get("process_key") == state["process_key"]
        and valid_image(previous.get("output_path"))
    ):
        if Path(previous["output_path"]).resolve() != Path(state["output_path"]).resolve():
            atomic_copy(previous["output_path"], state["output_path"])
        state.update(
            {
                "status": "completed",
                "cache_source": "resume",
                "debug_data": previous.get("debug_data", {}),
                "timings": previous.get("timings", {}),
            }
        )
        return True

    if not config.ENABLE_IMAGE_PROCESS_CACHE:
        return False
    cached = load_processed_cache(state["process_key"])
    if cached is None:
        return False
    cached_image, metadata = cached
    atomic_copy(cached_image, state["output_path"])
    state.update(
        {
            "status": "completed",
            "cache_source": "processed_cache",
            "debug_data": metadata.get("debug_data", {}),
            "timings": metadata.get("timings", {}),
        }
    )
    return True


def _save_page_processed_cache(state):
    if not config.ENABLE_IMAGE_PROCESS_CACHE:
        return
    if state.get("status") != "completed":
        return
    save_processed_cache(
        state["process_key"],
        state["output_path"],
        {
            "process_key": state["process_key"],
            "image_hash": state["image_hash"],
            "debug_data": state.get("debug_data", {}),
            "timings": state.get("timings", {}),
            "precheck": state.get("precheck", {}),
        },
    )


PAGE_ANALYSIS_ERROR_CODE = "page_analysis_error"
PAGE_ANALYSIS_UNRESOLVED_CODE = "page_analysis_unresolved"
PAGE_SOURCE_MISSING_CODE = "source_page_missing_from_accounting"


def _page_error_traceback(error):
    """The local technical trace for a page failure, or ``""`` for a plain reason."""

    if not isinstance(error, BaseException) or error.__traceback__ is None:
        return ""
    return sanitize_diagnostic_text(
        "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    )


def _page_error_record(error, *, stage, index, retryable=False):
    """PAGE-ERROR-OBSERVABILITY-001: structured, secret-free page diagnostics.

    Real run #84F9 persisted the single word ``"str"`` for five failed pages: the
    caller flattened the exception with ``str(exc)`` first, so the persistence
    layer asked a *string* for its ``code``/type and dutifully wrote down the type
    of the message.  The exception class, the stage and the reason were all gone
    before anything was written.  The record below keeps them, and keeps them
    safe: messages are run through the repository's own diagnostic sanitizer, so
    a provider URL or an API key in an exception text never reaches disk.
    """

    page = int(index or 0)
    if isinstance(error, BaseException):
        exception_type = type(error).__name__
        code = str(getattr(error, "code", "") or "") or exception_type
        message = str(error)
    else:
        exception_type = ""
        code = str(error or "").strip() or "unknown_page_error"
        message = code
    return {
        "stage": str(stage or "page_analysis"),
        "page": page,
        "page_id": f"p{page:03}",
        "code": sanitize_diagnostic_text(code)[:200],
        "exception_type": exception_type,
        "message": sanitize_diagnostic_text(message)[:400],
        "retryable": bool(retryable),
        "traceback_available": bool(_page_error_traceback(error)),
    }


def _complete_page_with_error(
    state,
    error,
    errors_folder,
    stage_seconds,
    *,
    stage="page_analysis",
):
    record = _page_error_record(error, stage=stage, index=state.get("index"))
    state["page_error"] = record
    state["error"] = record["code"]
    state["status"] = "error"
    state["cache_source"] = "error_original"
    state["debug_data"] = _empty_debug_data(
        state["image_path"],
        precheck_reason="ocr_or_processing_error",
    )
    save_started = time.perf_counter()
    if os.path.isfile(state["image_path"]):
        atomic_copy(state["image_path"], state["output_path"])
        state["status"] = "completed_with_error"
    elapsed = time.perf_counter() - save_started
    state["timings"]["image_save"] = elapsed
    stage_seconds["image_save"] += elapsed
    page_folder = errors_folder / f"page_{state['index']:03}"
    page_folder.mkdir(parents=True, exist_ok=True)
    (page_folder / "error.txt").write_text(record["code"], encoding="utf-8")
    (page_folder / "error.json").write_text(dumps_json(record), encoding="utf-8")
    # The traceback is a local diagnostic artefact only: it never reaches the UI,
    # and it is sanitized like every other persisted diagnostic.
    trace = _page_error_traceback(error)
    if trace:
        (page_folder / "traceback.txt").write_text(trace, encoding="utf-8")


def _source_page_accounting(states, expected_pages=None):
    """PAGE-ANALYSIS-FAILURE-GATE-001: reconcile source pages, not just regions.

    Region accounting is structurally blind to a page that produced no regions:
    an exception before OCR leaves nothing to count, and "nothing counted" is not
    "nothing there".  Real run #84F9 lost five pages that way, one of which
    (p032) carried ordinary narration and shipped in English.  Source pages are
    therefore reconciled on their own axis: expected == analysed + unverified,
    with every unverified page named in an explicit finding.
    """

    states = list(states)
    expected = int(expected_pages or 0) or len(states)
    result = {
        "source_pages_expected": expected,
        "source_pages_analyzed": 0,
        "source_pages_completed_with_error": 0,
        "source_pages_missing": 0,
        "source_pages_unverified": 0,
        "source_pages_unverified_ids": [],
        "findings": [],
    }
    for state in states:
        index = int(state.get("index") or 0)
        page_id = f"p{index:03}"
        status = str(state.get("status") or "")
        page_error = state.get("page_error") or {}
        failed = bool(state.get("ocr_error")) or status in {
            "error",
            "completed_with_error",
            "failed",
        }
        if not failed and status == "completed":
            result["source_pages_analyzed"] += 1
            continue
        if failed:
            result["source_pages_completed_with_error"] += 1
        result["source_pages_unverified_ids"].append(page_id)
        result["findings"].append({
            "code": (
                PAGE_ANALYSIS_ERROR_CODE if failed
                else PAGE_ANALYSIS_UNRESOLVED_CODE
            ),
            "page": index,
            "page_id": page_id,
            "status": status or "unknown",
            # Pre-#84F10 states carry no structured record; ``ocr_error`` still
            # says which stage refused the page.
            "stage": str(
                page_error.get("stage")
                or ("ocr" if state.get("ocr_error") else "page_analysis")
            ),
            "exception_type": str(page_error.get("exception_type") or ""),
            "error_code": str(
                page_error.get("code") or state.get("ocr_error") or state.get("error") or ""
            ),
            # The page never finished analysis, so nothing is known about the
            # story content it carries. Unknown is not absent.
            "story_content_verified": False,
            "quality": "review_required",
        })
    result["source_pages_missing"] = max(
        0, expected - result["source_pages_analyzed"] - len(result["source_pages_unverified_ids"])
    )
    if result["source_pages_missing"]:
        result["findings"].append({
            "code": PAGE_SOURCE_MISSING_CODE,
            "page": 0,
            "page_id": "",
            "status": "absent",
            "stage": "page_analysis",
            "exception_type": "",
            "error_code": "",
            "missing_count": result["source_pages_missing"],
            "story_content_verified": False,
            "quality": "review_required",
        })
    result["source_pages_unverified"] = (
        len(result["source_pages_unverified_ids"]) + result["source_pages_missing"]
    )
    result["accounting_closed"] = expected == (
        result["source_pages_analyzed"] + result["source_pages_unverified"]
    )
    result["page_gate_passed"] = bool(
        result["accounting_closed"] and result["source_pages_unverified"] == 0
    )
    # A chapter with an unverified page cannot claim its user-visible story output
    # was checked; the PDF may still exist, but only as a review artefact.
    result["story_output_verified"] = result["page_gate_passed"]
    result["status"] = "passed" if result["page_gate_passed"] else "review_required"
    return result


def _mark_fast_ocr_review(state, reason, trigger):
    """Preserve a difficult OCR page and make the bounded fallback visible to review."""

    safe_reason = str(reason or "fast_ocr_fallback_budget_exhausted")
    for group in state.get("groups", []) or []:
        if getattr(group, "classification", "") in {"speech", "narration", "unknown"}:
            group.manual_review_required = True
            group.fallback_reason = safe_reason
            if safe_reason not in group.quality_reasons:
                group.quality_reasons.append(safe_reason)
    state["fast_ocr_fallback"] = {
        "reason": safe_reason,
        "trigger": str(trigger or ""),
        "action": "preserve_original_and_review",
    }


def _write_progress(
    path,
    run_signature,
    args,
    total_images,
    states,
    status="running",
    pdf_path=None,
):
    serializable = [_serializable_state(state) for state in states]
    atomic_write_json(
        path,
        {
            "status": status,
            "run_signature": run_signature,
            "url": sanitize_source_url(args.url),
            "full": bool(args.full),
            "fast": bool(args.fast),
            "force": bool(args.force),
            "page_indices": _parse_page_indices(getattr(args, "page_indices", "")),
            "total_images": total_images,
            "completed_images": sum(
                state.get("status") in {"completed", "completed_with_error"}
                for state in serializable
            ),
            "ocr_completed_images": sum(
                bool(state.get("ocr_completed")) for state in serializable
            ),
            "error_images": sum(
                state.get("status") == "completed_with_error"
                for state in serializable
            ),
            "pdf_path": pdf_path,
            "pages": serializable,
        },
    )


def _serializable_state(state):
    allowed = {
        "index",
        "sequence_index",
        "original_index",
        "image_path",
        "image_hash",
        "process_key",
        "output_path",
        "ocr_lang",
        "ocr_source",
        "ocr_cache_key",
        "ocr_error",
        "ocr_metadata",
        "ocr_completed",
        "precheck",
        "status",
        "cache_source",
        "debug_data",
        "selective_ocr_fallbacks",
        "rapidocr_region_recovery",
        "speech_container_reocr",
        "fast_ocr_fallback",
        "fast_ocr_fallback_metadata",
        "timings",
        "error",
        "page_error",
    }
    return {key: value for key, value in state.items() if key in allowed}


def _grouping_fallback_reason(state, groups):
    if not (
        config.OCR_ENGINE == "rapidocr"
        and config.OCR_HYBRID_FALLBACK
        and config.OCR_FALLBACK_ENGINE == "paddle"
    ):
        return ""

    if (
        state.get("raw_lines")
        and not groups
        and not state.get("ocr_metadata", {}).get("fallback_used")
    ):
        return "zero_groups_from_ocr_lines"

    for group in groups:
        words = re.findall(r"[A-Za-zÀ-ÿ]+", group.text)
        useful_letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", group.text)
        if (
            group.classification in {"speech", "narration"}
            and group.quality_score < 0.35
            and group.cleanup_lines
        ):
            return "incomplete_group_after_selective_fallback"
        if (
            group.ignored
            and len(words) >= 4
            and len(useful_letters) >= 18
            and group.classification not in {"decorative", "sfx"}
            and group.ignore_reason not in {
                "decorative_text",
                "sfx_translation_disabled",
            }
        ):
            return "high_content_group_ignored"
        if (
            group.classification == "sfx"
            and len(words) >= 1
            and len(useful_letters) >= 8
            and group.text.upper().strip(" .!?") not in {
                "BANG",
                "BOOM",
                "BUMP",
                "CLANG",
                "CRASH",
                "GONG",
                "GRR",
                "GULP",
                "HISS",
                "KNOCK",
                "SLAM",
                "SNIFF",
                "SNIFFLE",
                "SOB",
                "THUD",
                "UGH",
                "WHAM",
                "WHOOSH",
            }
        ):
            return "sentence_like_text_classified_as_sfx"
    return ""


def _preserve_selected_regional_ocr(page_lines, selected_lines):
    """Keep regional OCR winners when a later page fallback is required.

    A full-page fallback may be needed for an unrelated group. It must not
    overwrite a regional candidate that already won the multi-engine quality
    comparison. Only page lines that substantially overlap those selected
    regional lines are replaced.
    """
    regional_lines = [
        line
        for line in selected_lines
        if (line.metadata or {}).get("selective_fallback_used")
    ]
    if not regional_lines:
        return list(page_lines), 0

    merged = [
        line
        for line in page_lines
        if not any(
            _boxes_substantially_overlap(line.box, regional.box)
            for regional in regional_lines
        )
    ]
    merged.extend(regional_lines)
    merged.sort(key=lambda line: (line.box[1], line.box[0]))
    return merged, len(regional_lines)


def _session_terminology_terms(session_context):
    """Source terms the chapter ledger already vouches for, or nothing."""
    if session_context is None or not hasattr(session_context, "term_bindings"):
        return ()
    try:
        bindings = session_context.term_bindings() or {}
    except Exception:  # noqa: BLE001 - a missing ledger must never sink a page.
        return ()
    return tuple(
        str(entry.get("source") or key)
        for key, entry in bindings.items()
        if isinstance(entry, dict)
    )


def _fallback_discards_source_text(current_lines, fallback_lines):
    """True when the page-level escalation returns less text than it replaces.

    The escalation exists to read *one* badly-read region better; it is never a
    licence to erase the page.  When the fallback engine finds nothing (or
    almost nothing) where the current engine found readable lettering, taking
    its answer drops every group, so nothing is translated, nothing is
    inpainted, and the untouched source page is what reaches the PDF - the
    worst possible outcome, produced by a step meant to improve quality.
    """

    def letters(lines):
        return sum(
            len(re.sub(r"[^A-Za-zÀ-ÿ]", "", str(getattr(line, "text", "") or "")))
            for line in lines or []
        )

    current = letters(current_lines)
    return current > 0 and letters(fallback_lines) < current * 0.6


def _boxes_substantially_overlap(left, right):
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    intersection_width = max(0, min(lx + lw, rx + rw) - max(lx, rx))
    intersection_height = max(0, min(ly + lh, ry + rh) - max(ly, ry))
    intersection = intersection_width * intersection_height
    if intersection <= 0:
        return False
    smaller_area = max(1, min(lw * lh, rw * rh))
    return intersection / smaller_area >= 0.35


def _applied_text_repairs(ocr_metadata):
    if ocr_metadata.get("final_engine") != "rapidocr":
        return []
    return [
        repair
        for repair in ocr_metadata.get("text_repairs", [])
        if repair.get("accepted", True)
    ]


def _rejected_text_repairs(ocr_metadata):
    return [
        repair
        for repair in ocr_metadata.get("text_repairs", [])
        if not repair.get("accepted", True)
    ]


def _group_text_repairs(groups):
    records = []
    for group in groups:
        if not group.repair_reason or group.repaired_text == group.original_text:
            continue
        candidate = next(
            (
                (line.metadata or {}).get("group_repair_candidate")
                for line in group.lines
                if (line.metadata or {}).get("group_repair_candidate")
            ),
            {},
        )
        records.append(
            {
                "original_text": group.original_text,
                "repaired_text": group.repaired_text,
                "repair_reason": group.repair_reason,
                "group_id": group.group_id,
                **candidate,
            }
        )
    return records


def _aggregate_debug_data(states):
    result = {
        "ocr_detected_lines": 0,
        "groups_formed": 0,
        "groups_translated": 0,
        "groups_ignored_sfx_decorative": 0,
        "ocr_page_fallbacks": 0,
        "ocr_region_fallbacks": 0,
        "ocr_region_fallback_attempts": 0,
        "rapidocr_region_retry_requested": 0,
        "rapidocr_region_retry_selected": 0,
        "rapidocr_region_retry_failed": 0,
        "paddle_mobile_region_fallbacks": 0,
        "paddle_full_region_fallbacks": 0,
        "paddle_full_calls": 0,
        "paddle_full_total_seconds": 0.0,
        "paddle_full_accepted": 0,
        "paddle_full_rejected": 0,
        "paddle_full_no_change": 0,
        "paddle_full_worse": 0,
        "paddle_full_duplicate": 0,
        "paddle_full_required": 0,
        "paddle_full_useful": 0,
        "paddle_full_after_mobile_candidate": 0,
        "paddle_full_after_mobile_sufficient": 0,
        "paddle_full_after_mobile_failure": 0,
        "paddle_full_for_speech": 0,
        "paddle_full_for_narration": 0,
        "paddle_full_for_sfx": 0,
        "paddle_full_for_decorative": 0,
        "paddle_full_for_unknown": 0,
        "ocr_text_repairs": 0,
        "ocr_text_repairs_rejected": 0,
        "groups_reverted_for_visual_safety": 0,
        "manual_review_required_groups": 0,
        "translation_retries": 0,
        "translation_rejections": 0,
        "mixed_language_items": 0,
        "text_overflow_items": 0,
        "visual_validation_failures": 0,
        "classification_counts": {
            "speech": 0,
            "narration": 0,
            "sfx": 0,
            "decorative": 0,
            "unknown": 0,
        },
    }
    for state in states:
        debug_data = state.get("debug_data", {})
        result["ocr_detected_lines"] += debug_data.get("ocr_line_count", 0)
        result["groups_formed"] += debug_data.get("group_count", 0)
        result["groups_translated"] += debug_data.get("translated_group_count", 0)
        for name in result["classification_counts"]:
            result["classification_counts"][name] += debug_data.get(
                "classification_counts", {}
            ).get(name, 0)
        result["groups_ignored_sfx_decorative"] += sum(
            1
            for item in debug_data.get("items", [])
            if item.get("ignored")
            and item.get("classification") in {"sfx", "decorative"}
        )
        ocr_metadata = debug_data.get("ocr_metadata", {})
        result["ocr_page_fallbacks"] += int(
            bool(ocr_metadata.get("fallback_used"))
        )
        fallback_records = debug_data.get("selective_ocr_fallbacks", [])
        result["ocr_region_fallback_attempts"] += len(fallback_records)
        rapidocr_records = debug_data.get("rapidocr_region_recovery", [])
        result["rapidocr_region_retry_requested"] += len(rapidocr_records)
        result["rapidocr_region_retry_selected"] += sum(
            1 for record in rapidocr_records if record.get("selection") == "attempt_2"
        )
        result["rapidocr_region_retry_failed"] += sum(
            1
            for record in rapidocr_records
            if record.get("selection") != "attempt_2"
        )
        for record in fallback_records:
            for full_call in record.get("paddle_full_calls", []) or []:
                result["paddle_full_calls"] += 1
                result["paddle_full_total_seconds"] += float(
                    full_call.get("elapsed_seconds") or 0.0
                )
                if full_call.get("full_accepted"):
                    result["paddle_full_accepted"] += 1
                else:
                    result["paddle_full_rejected"] += 1
                classification = str(full_call.get("call_classification") or "")
                if classification == "FULL_NO_CHANGE":
                    result["paddle_full_no_change"] += 1
                elif classification == "FULL_WORSE":
                    result["paddle_full_worse"] += 1
                elif classification == "FULL_DUPLICATE":
                    result["paddle_full_duplicate"] += 1
                elif classification == "FULL_REQUIRED":
                    result["paddle_full_required"] += 1
                elif classification == "FULL_USEFUL":
                    result["paddle_full_useful"] += 1
                if full_call.get("mobile_candidate_exists"):
                    result["paddle_full_after_mobile_candidate"] += 1
                if full_call.get("mobile_candidate_sufficient"):
                    result["paddle_full_after_mobile_sufficient"] += 1
                reasons = set(full_call.get("mobile_rejection_reasons") or [])
                if "mobile_no_candidate" in reasons or "mobile_error" in reasons:
                    result["paddle_full_after_mobile_failure"] += 1
                group_classification = str(
                    full_call.get("classification_before") or "unknown"
                )
                key = f"paddle_full_for_{group_classification}"
                if key in result:
                    result[key] += 1
            if not record.get("fallback_used"):
                continue
            result["ocr_region_fallbacks"] += 1
            if record.get("fallback_variant") == "paddle_mobile":
                result["paddle_mobile_region_fallbacks"] += 1
            elif record.get("fallback_variant") == "paddle_full":
                result["paddle_full_region_fallbacks"] += 1
        result["ocr_text_repairs"] += len(
            debug_data.get("text_repairs", [])
        )
        result["ocr_text_repairs_rejected"] += len(
            debug_data.get("rejected_text_repairs", [])
        )
        for item in debug_data.get("items", []):
            retry_count = int(item.get("translation_retry_count") or 0)
            result["translation_retries"] += retry_count
            if item.get("rejected_translation"):
                result["translation_rejections"] += 1
            reason = str(item.get("translation_validation_reason") or "")
            if _is_mixed_language_validation_reason(reason):
                result["mixed_language_items"] += 1
            if float(item.get("text_overflow_ratio") or 0.0) > config.MAX_TEXT_OVERFLOW_RATIO:
                result["text_overflow_items"] += 1
            visual = item.get("visual_validation") or {}
            if visual and not visual.get("visual_validation_passed", True):
                result["visual_validation_failures"] += 1
            if item.get("manual_review_required"):
                result["manual_review_required_groups"] += 1
            if item.get("sent_to_nvidia") and not item.get("redrawn"):
                result["groups_reverted_for_visual_safety"] += 1
    return result


def _empty_debug_data(image_path, precheck_reason):
    return {
        "image_path": str(image_path),
        "ocr_line_count": 0,
        "ignored_line_count": 0,
        "ignored_group_count": 0,
        "group_count": 0,
        "translated_group_count": 0,
        "redrawn_group_count": 0,
        "classification_counts": {
            "speech": 0,
            "narration": 0,
            "sfx": 0,
            "decorative": 0,
            "unknown": 0,
        },
        "items": [],
        "ocr_metadata": {},
        "text_repairs": [],
        "rejected_text_repairs": [],
        "precheck_reason": precheck_reason,
    }


def _relevant_output_config(pipeline_fingerprint):
    font_signature = config.FONT_PATH
    if config.FONT_PATH and os.path.isfile(config.FONT_PATH):
        font_signature = file_sha256(config.FONT_PATH)
    return {
        "pipeline_fingerprint": pipeline_fingerprint,
        "ocr_engine": config.OCR_ENGINE,
        "ocr_fallback": config.OCR_FALLBACK_ENGINE,
        "ocr_hybrid_fallback": config.OCR_HYBRID_FALLBACK,
        "fast_ocr_mode": bool(getattr(config, "FAST_OCR_MODE", False)),
        "fast_ocr_heavy_fallback": bool(
            getattr(config, "FAST_OCR_HEAVY_FALLBACK", False)
        ),
        "fast_ocr_page_timeout_seconds": float(
            getattr(config, "FAST_OCR_PAGE_TIMEOUT_SECONDS", 0.0)
        ),
        "fast_ocr_region_timeout_seconds": float(
            getattr(config, "FAST_OCR_REGION_TIMEOUT_SECONDS", 0.0)
        ),
        "fast_ocr_full_fallback_max_pages": int(
            getattr(config, "FAST_OCR_FULL_FALLBACK_MAX_PAGES", 0)
        ),
        "fast_ocr_full_fallback_max_regions": int(
            getattr(config, "FAST_OCR_FULL_FALLBACK_MAX_REGIONS", 0)
        ),
        "rapidocr_enabled": config.RAPIDOCR_ENABLED,
        "rapidocr_min_confidence": config.RAPIDOCR_MIN_CONFIDENCE,
        "rapidocr_page_fallback": config.RAPIDOCR_PAGE_FALLBACK,
        "rapidocr_suspicious_text_fallback": (
            config.RAPIDOCR_SUSPICIOUS_TEXT_FALLBACK
        ),
        "ocr_text_repair": config.OCR_TEXT_REPAIR,
        "ocr_text_repair_mode": config.OCR_TEXT_REPAIR_MODE,
        "ocr_quality_control": config.OCR_QUALITY_CONTROL,
        "ocr_region_selective_fallback": config.OCR_REGION_SELECTIVE_FALLBACK,
        "ocr_group_min_quality_score": config.OCR_GROUP_MIN_QUALITY_SCORE,
        "translation_validation": config.TRANSLATION_VALIDATION,
        "text_mask_padding": config.TEXT_MASK_PADDING,
        "max_mask_expansion": config.MAX_MASK_EXPANSION,
        "strict_mask_bounds": config.STRICT_MASK_BOUNDS,
        "mask_component_based": config.MASK_COMPONENT_BASED,
        "allow_large_rectangle_mask": config.ALLOW_LARGE_RECTANGLE_MASK,
        "white_balloon_flat_fill": config.WHITE_BALLOON_FLAT_FILL,
        "text_safe_padding": config.TEXT_SAFE_PADDING,
        "visual_diff_validation": config.VISUAL_DIFF_VALIDATION,
        "visual_qa_strict": config.VISUAL_QA_STRICT,
        "max_outside_change_ratio": config.MAX_OUTSIDE_CHANGE_RATIO,
        "max_outside_component_area": config.MAX_OUTSIDE_COMPONENT_AREA,
        "max_mask_to_text_area_ratio": config.MAX_MASK_TO_TEXT_AREA_RATIO,
        "reject_balloon_border_damage": config.REJECT_BALLOON_BORDER_DAMAGE,
        "reject_text_overflow": config.REJECT_TEXT_OVERFLOW,
        "white_background_min_brightness": config.WHITE_BACKGROUND_MIN_BRIGHTNESS,
        "white_background_max_std": config.WHITE_BACKGROUND_MAX_STD,
        "white_background_max_saturation": config.WHITE_BACKGROUND_MAX_SATURATION,
        "white_background_min_ratio": config.WHITE_BACKGROUND_MIN_RATIO,
        "white_background_max_texture": config.WHITE_BACKGROUND_MAX_TEXTURE,
        "white_background_max_edge_density": config.WHITE_BACKGROUND_MAX_EDGE_DENSITY,
        "white_background_max_diagonal_lines": config.WHITE_BACKGROUND_MAX_DIAGONAL_LINES,
        "white_enclosure_min_brightness": config.WHITE_ENCLOSURE_MIN_BRIGHTNESS,
        "white_enclosure_min_ratio": config.WHITE_ENCLOSURE_MIN_RATIO,
        "white_enclosure_max_dark_ratio": config.WHITE_ENCLOSURE_MAX_DARK_RATIO,
        "white_enclosure_max_saturation": config.WHITE_ENCLOSURE_MAX_SATURATION,
        "white_stylized_enclosure_min_brightness": config.WHITE_STYLIZED_ENCLOSURE_MIN_BRIGHTNESS,
        "white_stylized_enclosure_min_ratio": config.WHITE_STYLIZED_ENCLOSURE_MIN_RATIO,
        "white_stylized_enclosure_max_dark_ratio": config.WHITE_STYLIZED_ENCLOSURE_MAX_DARK_RATIO,
        "white_stylized_enclosure_max_saturation": config.WHITE_STYLIZED_ENCLOSURE_MAX_SATURATION,
        "max_textured_mask_group_ratio": config.MAX_TEXTURED_MASK_GROUP_RATIO,
        "max_textured_mask_component_ratio": config.MAX_TEXTURED_MASK_COMPONENT_RATIO,
        "reject_white_patch_outside_balloon": config.REJECT_WHITE_PATCH_OUTSIDE_BALLOON,
        "translate_sfx": config.TRANSLATE_SFX,
        "prioritize_enclosed_text": config.PRIORITIZE_ENCLOSED_TEXT,
        "translation_model": config.NVIDIA_TRANSLATION_MODEL,
        "translation_prompt_version": PROMPT_VERSION,
        "font_signature": font_signature,
        "no_text_conservative": config.NO_TEXT_SKIP_CONSERVATIVE,
        "smart_webtoon_pdf_split": config.SMART_WEBTOON_PDF_SPLIT,
        "smart_pdf_target_height": config.SMART_PDF_TARGET_HEIGHT,
        "smart_pdf_min_height": config.SMART_PDF_MIN_HEIGHT,
        "smart_pdf_max_height": config.SMART_PDF_MAX_HEIGHT,
    }


def _reset_generated_folders(*folders):
    for folder in folders:
        if folder.exists():
            force_remove(str(folder))


def _font(size, bold=False):
    candidates = (
        [r"C:\Windows\Fonts\georgiab.ttf", r"C:\Windows\Fonts\segoeuib.ttf"]
        if bold
        else [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"]
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _create_preview_contact_sheet(states, target):
    selected = _select_preview_states(states)
    canvas_width = 1000
    header_height = 150
    columns = 2
    card_width = 460
    card_height = 620
    gutter = 24
    rows = math.ceil(len(selected) / columns)
    canvas_height = header_height + rows * (card_height + gutter) + 32
    background = (20, 22, 27)
    surface = (244, 239, 231)
    accent = (225, 102, 62)
    canvas = Image.new("RGB", (canvas_width, canvas_height), background)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 18, header_height), fill=accent)
    draw.text((48, 30), "TRADUTOR.IA / QUALITY STRIP", font=_font(34, True), fill=surface)
    draw.text(
        (50, 88),
        "Amostras finais — ordem, legibilidade e consistência",
        font=_font(20),
        fill=(180, 184, 190),
    )

    for position, state in enumerate(selected):
        row, column = divmod(position, columns)
        x = 24 + column * (card_width + gutter)
        y = header_height + row * (card_height + gutter)
        draw.rounded_rectangle(
            (x, y, x + card_width, y + card_height),
            radius=12,
            fill=surface,
        )
        image = Image.open(state["output_path"]).convert("RGB")
        thumb = ImageOps.contain(image, (card_width - 32, card_height - 88))
        image.close()
        image_x = x + (card_width - thumb.width) // 2
        image_y = y + 18
        canvas.paste(thumb, (image_x, image_y))
        translated = state.get("debug_data", {}).get("translated_group_count", 0)
        label = f"PÁGINA {state['index']:03}  /  {translated} traduções"
        draw.text(
            (x + 18, y + card_height - 48),
            label,
            font=_font(17, True),
            fill=(31, 34, 40),
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "JPEG", quality=82, optimize=True, progressive=True)
    return selected


def _select_preview_states(states):
    ordered = sorted(states, key=lambda state: state["index"])
    required = [
        ordered[0],
        ordered[min(1, len(ordered) - 1)],
        ordered[len(ordered) // 2],
        ordered[-1],
    ]
    top_translated = sorted(
        ordered,
        key=lambda state: state.get("debug_data", {}).get(
            "translated_group_count", 0
        ),
        reverse=True,
    )[:3]
    randomizer = random.Random(42)
    random_states = randomizer.sample(ordered, min(3, len(ordered)))
    selected = []
    seen = set()
    for state in required + top_translated + random_states:
        if state["index"] in seen:
            continue
        seen.add(state["index"])
        selected.append(state)
    for state in ordered:
        if len(selected) >= 10:
            break
        if state["index"] not in seen:
            selected.append(state)
            seen.add(state["index"])
    return selected[:10]


def _create_preview_compare_sheet(selected_states, target):
    candidates = sorted(
        selected_states,
        key=lambda state: state.get("debug_data", {}).get(
            "translated_group_count", 0
        ),
        reverse=True,
    )[:3]
    canvas_width = 1000
    header_height = 140
    row_height = 600
    canvas = Image.new(
        "RGB",
        (canvas_width, header_height + row_height * len(candidates) + 28),
        (239, 235, 226),
    )
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, canvas_width, header_height), fill=(20, 22, 27))
    draw.text((36, 28), "ORIGINAL / FINAL", font=_font(36, True), fill=(244, 239, 231))
    draw.text(
        (38, 88),
        "Comparação leve das páginas com mais tradução",
        font=_font(19),
        fill=(183, 186, 191),
    )

    for row, state in enumerate(candidates):
        y = header_height + row * row_height
        draw.text(
            (24, y + 18),
            f"PÁGINA {state['index']:03}",
            font=_font(17, True),
            fill=(31, 34, 40),
        )
        for column, path in enumerate((state["image_path"], state["output_path"])):
            image = Image.open(path).convert("RGB")
            thumb = ImageOps.contain(image, (452, row_height - 82))
            image.close()
            x = 24 + column * 488 + (452 - thumb.width) // 2
            image_y = y + 52 + (row_height - 82 - thumb.height) // 2
            canvas.paste(thumb, (x, image_y))
            draw.text(
                (24 + column * 488, y + row_height - 26),
                "ORIGINAL" if column == 0 else "FINAL",
                font=_font(15, True),
                fill=(225, 102, 62) if column else (86, 89, 95),
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "JPEG", quality=82, optimize=True, progressive=True)


def _validate_quality(states, pdf_path, expected_page_count, full=False):
    invalid_pages = []
    for state in states:
        if not valid_image(state["output_path"]):
            invalid_pages.append(state["index"])

    pdf_pages = _count_pdf_pages(pdf_path)
    high_translation_pages = sorted(
        states,
        key=lambda state: state.get("debug_data", {}).get(
            "translated_group_count", 0
        ),
        reverse=True,
    )[:3]
    high_translation_valid = all(
        valid_image(state["output_path"]) for state in high_translation_pages
    )

    all_items = [
        item
        for state in states
        for item in state.get("debug_data", {}).get("items", [])
    ]
    preserved_sfx_items = [
        item
        for item in all_items
        if item.get("classification") == "sfx"
    ]
    sfx_policy_ok = all(
        item.get("classification") == "sfx"
        and item.get("ignored")
        and not item.get("sent_to_nvidia")
        for item in preserved_sfx_items
    )
    visual_failures = [
        item
        for item in all_items
        if (item.get("visual_validation") or {})
        and not (item.get("visual_validation") or {}).get(
            "visual_validation_passed",
            True,
        )
    ]
    manual_review_items = [
        item for item in all_items if item.get("manual_review_required")
    ]
    overflow_items = [
        item
        for item in all_items
        if float(item.get("text_overflow_ratio") or 0.0)
        > config.MAX_TEXT_OVERFLOW_RATIO
    ]
    return {
        "passed": (
            not invalid_pages
            and pdf_pages == expected_page_count
            and high_translation_valid
            and config.TRANSLATE_SFX is False
            and sfx_policy_ok
            and not visual_failures
            and not manual_review_items
            and not overflow_items
        ),
        "pdf_pages": pdf_pages,
        "expected_pdf_pages": expected_page_count,
        "invalid_or_blank_pages": invalid_pages,
        "high_translation_pages_valid": high_translation_valid,
        "translate_sfx_disabled": config.TRANSLATE_SFX is False,
        "preserved_sfx_groups": len(preserved_sfx_items),
        "sfx_policy_valid": sfx_policy_ok,
        "visual_validation_failures": len(visual_failures),
        "manual_review_required_groups": len(manual_review_items),
        "text_overflow_groups": len(overflow_items),
    }


def _count_pdf_pages(path):
    try:
        data = Path(path).read_bytes()
        return len(re.findall(rb"/Type\s*/Page(?!s)", data))
    except OSError:
        return 0


def _timing_report_text(report):
    stage = report["stage_seconds"]
    reduction = report["reduction_from_baseline_percent"]
    comparison = (
        f"{abs(reduction):.2f}% mais rapido"
        if reduction >= 0
        else f"{abs(reduction):.2f}% mais lento"
    )
    lines = [
        "Tradutor.Ia - Benchmark de performance",
        f"Modo: {report['mode']}",
        f"Force: {report['force']}",
        f"Total de imagens: {report['total_images']}",
        f"Imagens processadas: {report['processed_images']}",
        f"Imagens puladas por cache: {report['images_skipped_by_cache']}",
        (
            "Imagens puladas por no-text precheck: "
            f"{report['images_skipped_by_no_text_precheck']}"
        ),
        f"OCR executados: {report['ocr_runs']}",
        f"OCR do cache: {report['ocr_cache_hits']}",
        f"Fallbacks OCR para Paddle: {report['ocr_page_fallbacks']}",
        f"Fallbacks OCR por regiao: {report.get('ocr_region_fallbacks', 0)}",
        f"Textos OCR reparados: {report['ocr_text_repairs']}",
        f"Retries de traducao: {report.get('translation_retries', 0)}",
        f"Traducoes rejeitadas: {report.get('translation_rejections', 0)}",
        (
            "Retries por JSON invalido do provedor: "
            f"{report.get('translation_invalid_json_retries', 0)}"
        ),
        f"Itens com texto misturado: {report.get('mixed_language_items', 0)}",
        f"Falhas de validacao visual: {report.get('visual_validation_failures', 0)}",
        f"Textos enviados ao provedor: {report['translation_api_texts']}",
        f"Traducoes do cache: {report['translation_cache_hits']}",
        "",
        f"Tempo total: {report['total_seconds']:.2f}s",
        f"Download/coleta: {stage['download_collection']:.2f}s",
        f"Validacao: {stage['image_validation']:.2f}s",
        f"Reconstrucao/smart split: {stage.get('smart_pdf_split', 0.0):.2f}s",
        f"No-text precheck: {stage['no_text_precheck']:.2f}s",
        f"OCR (parede): {stage['ocr']:.2f}s",
        f"OCR (soma por pagina): {stage['ocr_cpu']:.2f}s",
        f"OCR fallback seletivo: {stage.get('ocr_selective_fallback', 0.0):.2f}s",
        (
            "Classificacao/filtro/agrupamento: "
            f"{stage['classification_grouping']:.2f}s"
        ),
        f"Traducao: {stage['translation']:.2f}s",
        f"Inpainting: {stage['inpainting']:.2f}s",
        f"Redesenho: {stage['redraw']:.2f}s",
        f"Salvamento: {stage['image_save']:.2f}s",
        f"PDF: {stage['pdf']:.2f}s",
        f"Media por imagem: {report['average_seconds_per_image']:.2f}s",
        (
            "Media por imagem sem cache: "
            f"{report['average_seconds_per_image_without_cache']:.2f}s"
        ),
        (
            "Media por imagem com cache: "
            f"{report['average_seconds_per_image_with_cache']:.4f}s"
        ),
        f"Etapa mais lenta: {report['slowest_stage']}",
        f"Comparacao com 35min29s: {comparison}",
        "",
        f"PDF: {report['pdf_path']}",
        f"Contact sheet: {report['preview_contact_sheet']}",
        f"Compare sheet: {report['preview_compare_sheet']}",
    ]
    return "\n".join(lines) + "\n"
