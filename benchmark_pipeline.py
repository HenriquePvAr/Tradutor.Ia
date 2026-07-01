import json
import math
import os
import random
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import cv2
from PIL import Image, ImageDraw, ImageFont, ImageOps

import config
from down import download_images, force_remove
from json_utils import dumps_json
from ocr_balloon import (
    analyze_image_array,
    apply_selective_ocr_fallbacks,
    apply_group_translations,
    get_translatable_groups,
    render_analyzed_image,
    validate_and_retry_translations,
)
from ocr_parallel import detect_ocr_jobs
from ocr_engine import OCREngine
from pdf import generate_pdf
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
from translator_nllb import get_translator
from translator_nvidia import PROMPT_VERSION


BASELINE_SECONDS = 2129.41
OUTPUT_FOLDER = Path("output/full_chapter")
PIPELINE_FILES = (
    "ocr_balloon.py",
    "ocr_engine.py",
    "translator_nvidia.py",
    "pdf.py",
)


def run_benchmark(args):
    started = time.perf_counter()
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

    output_folder.mkdir(parents=True, exist_ok=True)
    if args.force:
        _reset_generated_folders(pages_folder, errors_folder)
        if targeted_regression:
            _reset_generated_folders(diagnostic_folder)
    pages_folder.mkdir(parents=True, exist_ok=True)
    errors_folder.mkdir(parents=True, exist_ok=True)

    effective_debug = bool(config.DEBUG_VISUAL and not args.fast)
    max_images = None if args.full else args.max_images
    download_max_images = max_images
    if selected_page_indices and not args.full:
        download_max_images = max(max(selected_page_indices), max_images or 0)
    run_signature = stable_hash(
        {
            "url": args.url,
            "max_images": max_images,
            "full": args.full,
            "fast": args.fast,
            "page_indices": selected_page_indices,
            "pipeline": "benchmark-v1",
            "ocr_engine": config.OCR_ENGINE,
            "ocr_fallback_engine": config.OCR_FALLBACK_ENGINE,
            "ocr_hybrid_fallback": config.OCR_HYBRID_FALLBACK,
        }
    )
    pipeline_fingerprint = fingerprint_files(PIPELINE_FILES)
    relevant_config = _relevant_output_config(pipeline_fingerprint)

    print(f"Benchmark: {args.url}", flush=True)
    print(f"Imagens: {'capitulo completo' if args.full else max_images}", flush=True)
    if selected_page_indices:
        print(f"Paginas selecionadas: {selected_page_indices}", flush=True)
    print(f"Fast: {'sim' if args.fast else 'nao'}", flush=True)
    print(f"Force: {'sim' if args.force else 'nao'}", flush=True)
    print(f"Saida: {output_folder}", flush=True)

    download_started = time.perf_counter()
    all_image_paths, download_report, download_cache_hit = _download_with_cache(
        args.url,
        download_max_images,
        output_folder,
        force=bool(getattr(args, "force_download", False)),
    )
    download_wall_seconds = time.perf_counter() - download_started
    if not all_image_paths:
        raise RuntimeError("Nenhuma imagem valida encontrada para o benchmark.")
    image_entries, missing_page_indices = _select_image_entries(
        all_image_paths,
        selected_page_indices,
    )
    if missing_page_indices:
        print(
            "Aviso: paginas selecionadas indisponiveis apos download: "
            + ",".join(str(item) for item in missing_page_indices),
            flush=True,
        )
    if not image_entries:
        raise RuntimeError("Nenhuma pagina selecionada estava disponivel para processar.")
    image_paths = [entry["path"] for entry in image_entries]

    previous_progress = load_json(progress_path, default={})
    previous_records = {}
    if not args.force and previous_progress.get("run_signature") == run_signature:
        previous_records = {
            int(record["index"]): record
            for record in previous_progress.get("pages", [])
            if str(record.get("index", "")).isdigit()
        }

    translator, ocr_lang = get_translator("3")
    if hasattr(translator, "force_cache"):
        translator.force_cache = bool(args.force)

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

    ocr_wall_started = time.perf_counter()
    ocr_results, ocr_parallel_info = detect_ocr_jobs(
        ocr_jobs,
        ocr_lang,
        parallel=config.OCR_PARALLEL,
        workers=config.OCR_WORKERS,
    )
    stage_seconds["ocr"] = time.perf_counter() - ocr_wall_started
    counters["ocr_runs"] = len(ocr_jobs)

    state_by_index = {state["index"]: state for state in page_states}
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
        if config.ENABLE_OCR_CACHE:
            save_ocr_cache(
                state["ocr_cache_key"],
                state["image_hash"],
                ocr_lang,
                state["raw_lines"],
                elapsed,
                state.get("precheck", {}),
                ocr_metadata=state.get("ocr_metadata", {}),
            )

    translation_targets = []
    analyzable_states = []
    for state in page_states:
        if state.get("status") == "completed":
            continue

        if state.get("ocr_error"):
            _complete_page_with_error(
                state,
                state["ocr_error"],
                errors_folder,
                stage_seconds,
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
            )
            counters["pages_with_error"] += 1
            continue

        classify_started = time.perf_counter()
        candidates, groups = analyze_image_array(original, state.get("raw_lines", []))
        selective_started = time.perf_counter()
        fallback_lines, selective_records = apply_selective_ocr_fallbacks(
            original,
            state.get("raw_lines", []),
            groups,
            ocr_lang,
            state["index"],
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
                state["raw_lines"] = fallback_lines
                candidates, groups = analyze_image_array(original, fallback_lines)
                state["ocr_metadata"] = {
                    **state.get("ocr_metadata", {}),
                    "selective_fallbacks": selective_records,
                }
        grouping_fallback_reason = _grouping_fallback_reason(state, groups)
        if grouping_fallback_reason:
            fallback_started = time.perf_counter()
            paddle = OCREngine(
                ocr_lang,
                engine="paddle",
                fallback_engine="",
            )
            fallback_lines = paddle.detect_lines(
                original,
                page=state["index"],
            )
            fallback_elapsed = time.perf_counter() - fallback_started
            state["timings"]["ocr"] = (
                float(state["timings"].get("ocr", 0.0))
                + fallback_elapsed
            )
            stage_seconds["ocr"] += fallback_elapsed
            stage_seconds["ocr_cpu"] += fallback_elapsed
            state["raw_lines"] = fallback_lines
            state["ocr_metadata"] = {
                **state.get("ocr_metadata", {}),
                "fallback_used": True,
                "fallback_reason": grouping_fallback_reason,
                "original_engine": "rapidocr",
                "final_engine": "paddle",
                "fallback_variant": "paddle_full",
            }
            candidates, groups = analyze_image_array(original, fallback_lines)
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
        state["group_text_repairs"] = _group_text_repairs(groups)
        classify_elapsed = time.perf_counter() - classify_started
        stage_seconds["classification_grouping"] += classify_elapsed
        state["timings"]["classification_grouping"] = classify_elapsed
        state["original_bgr"] = original
        state["candidates"] = candidates
        state["groups"] = groups
        state["translatable_groups"] = get_translatable_groups(groups)
        analyzable_states.append(state)
        for group in state["translatable_groups"]:
            translation_targets.append(group)

    translation_started = time.perf_counter()
    translations = translator.translate_many(
        [group.text for group in translation_targets],
        force=args.force,
    )
    stage_seconds["translation"] = time.perf_counter() - translation_started
    apply_group_translations(translation_targets, translations)
    retry_started = time.perf_counter()
    translation_retry_records = validate_and_retry_translations(
        translation_targets,
        translator,
        force=args.force,
    )
    stage_seconds["translation"] += time.perf_counter() - retry_started

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
            print(
                f"Pagina {state['index']}/{len(image_paths)}: concluida",
                flush=True,
            )
        except Exception as exc:
            _complete_page_with_error(
                state,
                str(exc),
                errors_folder,
                stage_seconds,
            )
            counters["pages_with_error"] += 1
            print(
                f"Pagina {state['index']}/{len(image_paths)}: erro: {exc}",
                flush=True,
            )
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

    pdf_path = (
        output_folder / "regression.pdf"
        if targeted_regression
        else (
            output_folder / "capitulo_completo_traduzido.pdf"
            if args.full
            else output_folder / f"benchmark_{args.max_images:03}.pdf"
        )
    )
    pdf_started = time.perf_counter()
    generate_pdf([state["output_path"] for state in completed_states], str(pdf_path))
    stage_seconds["pdf"] = time.perf_counter() - pdf_started

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
    summary = _aggregate_debug_data(completed_states)
    counters["ocr_page_fallbacks"] = summary["ocr_page_fallbacks"]
    counters["ocr_region_fallbacks"] = summary["ocr_region_fallbacks"]
    counters["ocr_text_repairs"] = summary["ocr_text_repairs"]
    counters["translation_retries"] = summary["translation_retries"]
    counters["translation_rejections"] = summary["translation_rejections"]
    counters["visual_validation_failures"] = summary["visual_validation_failures"]
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

    report = {
        "url": args.url,
        "mode": "full" if args.full else "controlled",
        "force": bool(args.force),
        "fast": bool(args.fast),
        "run_signature": run_signature,
        "total_dom_images": download_report.get("total_dom_images", 0),
        "total_unique_urls": download_report.get("total_unique_urls", 0),
        "available_valid_images": len(all_image_paths),
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
        "translation_failed_batches": translator_stats.get("failed_batches", 0),
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
        "progress_path": str(progress_path),
        "timing_report_json": str(timing_json_path),
        "timing_report_txt": str(timing_txt_path),
        "preview_contact_sheet": str(contact_sheet_path),
        "preview_compare_sheet": str(compare_sheet_path),
        "quality_report_json": str(quality_json_path),
        "quality_report_html": str(quality_html_path),
        "quality_validation": quality,
    }
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
        status="completed",
        pdf_path=str(pdf_path),
    )

    print(dumps_json(report, ensure_ascii=False, indent=2), flush=True)
    return report


def _download_with_cache(url, max_images, output_folder, force):
    key = stable_hash(
        {
            "url": url,
            "max_images": max_images,
            "download_rules": "webtoon-download-v2",
        }
    )
    download_folder = cache_folder("downloads") / key
    manifest_path = download_folder / "manifest.json"
    input_folder = output_folder / "input"
    output_report_path = output_folder / "downloaded_images.json"

    if config.ENABLE_DOWNLOAD_CACHE and not force:
        manifest = load_json(manifest_path)
        cached_paths = _valid_download_paths(manifest)
        if cached_paths:
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

    paths = download_images(
        url,
        max_images=max_images,
        debug_folder=str(output_folder),
        target_folder=str(input_folder),
        force=True,
        progress_callback=lambda current, total, message: print(
            f"{message}: {current}/{total}",
            flush=True,
        ),
    )
    manifest = load_json(output_report_path)
    valid_items = _chapter_download_items(manifest)
    if download_folder.exists():
        force_remove(str(download_folder))
    download_folder.mkdir(parents=True, exist_ok=True)
    cache_items = []
    for item in valid_items:
        path = item.get("path")
        if valid_image(path, 480, 220):
            file_hash = file_sha256(path)
            cache_path = download_folder / Path(path).name
            atomic_copy(path, cache_path)
            item["sha256"] = file_hash
            cache_item = dict(item)
            cache_item["path"] = str(cache_path)
            cache_item["active_path"] = path
            cache_items.append(cache_item)
    manifest["downloaded"] = valid_items
    manifest["total_downloaded"] = len(valid_items)
    paths = [item["path"] for item in valid_items if valid_image(item.get("path"), 480, 220)]
    cache_manifest = dict(manifest)
    cache_manifest["downloaded"] = cache_items
    cache_manifest["total_downloaded"] = len(cache_items)
    atomic_write_json(manifest_path, cache_manifest)
    atomic_write_json(output_report_path, manifest)
    return paths, manifest, False


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


def _build_quality_report(report, states, translation_retry_records):
    pages = []
    totals = {
        "groups_detected": 0,
        "groups_suspicious": 0,
        "selective_fallback_attempts": 0,
        "selective_fallbacks_used": 0,
        "fallbacks_to_paddle_mobile": 0,
        "fallbacks_to_paddle_full": 0,
        "ocr_repairs": 0,
        "ocr_repairs_rejected": 0,
        "groups_reverted_for_visual_safety": 0,
        "manual_review_required_groups": 0,
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
        "background_type_counts": {},
    }

    for state in sorted(states, key=lambda item: item["index"]):
        debug = state.get("debug_data", {})
        items = debug.get("items", [])
        fallback_records = debug.get("selective_ocr_fallbacks", [])
        suspicious = [
            item
            for item in items
            if item.get("quality_reasons")
            or float(item.get("quality_score") or 1.0) < config.OCR_GROUP_MIN_QUALITY_SCORE
        ]
        mixed = [
            item
            for item in items
            if str(item.get("translation_validation_reason") or "").startswith(
                ("mixed_language", "english_phrase")
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
            and item.get("sent_to_nvidia")
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
        totals["white_patch_rejections"] += sum(
            1
            for item in items
            if (item.get("mask_metrics") or {}).get("white_patch_rejected")
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
                "text_repairs": debug.get("text_repairs", []),
                "rejected_text_repairs": debug.get("rejected_text_repairs", []),
                "translation_retries": [
                    record
                    for record in translation_retry_records
                    if record.get("group_id")
                    in {item.get("id") for item in items}
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
        "text_overflow_ratio": item.get("text_overflow_ratio"),
        "visual_validation": item.get("visual_validation"),
        "visual_attempts": item.get("visual_attempts"),
        "mask_metrics": item.get("mask_metrics"),
        "manual_review_required": item.get("manual_review_required"),
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
        host = (urlparse(item.get("url", "")).hostname or "").lower()
        if "webtoons.com" in host or "webtoon-phinf.pstatic.net" in host:
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
        if not valid_image(path, 480, 220):
            return []
        expected_hash = item.get("sha256")
        if not expected_hash or file_sha256(path) != expected_hash:
            return []
    return paths


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


def _complete_page_with_error(state, error, errors_folder, stage_seconds):
    state["error"] = error
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
    (page_folder / "error.txt").write_text(str(error), encoding="utf-8")


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
            "url": args.url,
            "full": bool(args.full),
            "fast": bool(args.fast),
            "force": bool(args.force),
            "page_indices": _parse_page_indices(getattr(args, "page_indices", "")),
            "total_images": total_images,
            "completed_images": sum(
                state.get("status") in {"completed", "completed_with_error"}
                for state in serializable
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
        "precheck",
        "status",
        "cache_source",
        "debug_data",
        "selective_ocr_fallbacks",
        "timings",
        "error",
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
        "paddle_mobile_region_fallbacks": 0,
        "paddle_full_region_fallbacks": 0,
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
        for record in fallback_records:
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
            if reason.startswith("mixed_language") or reason.startswith("english_phrase"):
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
    sniffle_items = [
        item
        for item in all_items
        if str(item.get("clean_text", "")).strip().upper() == "SNIFFLE"
    ]
    sniffle_ok = all(
        item.get("classification") == "sfx"
        and item.get("ignored")
        and not item.get("sent_to_nvidia")
        for item in sniffle_items
    )

    reference_presence_ok = (
        not full
        or bool(sniffle_items)
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
            and sniffle_ok
            and reference_presence_ok
            and not visual_failures
            and not manual_review_items
            and not overflow_items
        ),
        "pdf_pages": pdf_pages,
        "expected_pdf_pages": expected_page_count,
        "invalid_or_blank_pages": invalid_pages,
        "high_translation_pages_valid": high_translation_valid,
        "translate_sfx_disabled": config.TRANSLATE_SFX is False,
        "sniffle_present": bool(sniffle_items),
        "sniffle_sfx_not_translated": sniffle_ok,
        "reference_texts_present_when_required": reference_presence_ok,
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
        f"Itens com texto misturado: {report.get('mixed_language_items', 0)}",
        f"Falhas de validacao visual: {report.get('visual_validation_failures', 0)}",
        f"Textos enviados a NVIDIA: {report['translation_api_texts']}",
        f"Traducoes do cache: {report['translation_cache_hits']}",
        "",
        f"Tempo total: {report['total_seconds']:.2f}s",
        f"Download/coleta: {stage['download_collection']:.2f}s",
        f"Validacao: {stage['image_validation']:.2f}s",
        f"No-text precheck: {stage['no_text_precheck']:.2f}s",
        f"OCR (parede): {stage['ocr']:.2f}s",
        f"OCR (soma por pagina): {stage['ocr_cpu']:.2f}s",
        f"OCR fallback seletivo: {stage.get('ocr_selective_fallback', 0.0):.2f}s",
        (
            "Classificacao/filtro/agrupamento: "
            f"{stage['classification_grouping']:.2f}s"
        ),
        f"Traducao NVIDIA: {stage['translation']:.2f}s",
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
