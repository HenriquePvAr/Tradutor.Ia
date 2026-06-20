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
    apply_group_translations,
    get_translatable_groups,
    render_analyzed_image,
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
from translator_nvidia import EXPECTED_TRANSLATIONS, PROMPT_VERSION


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
    progress_path = output_folder / "progress.json"
    timing_json_path = output_folder / "timing_report.json"
    timing_txt_path = output_folder / "timing_report.txt"
    contact_sheet_path = output_folder / "preview_contact_sheet.jpg"
    compare_sheet_path = output_folder / "preview_compare_sheet.jpg"

    output_folder.mkdir(parents=True, exist_ok=True)
    if args.force:
        _reset_generated_folders(pages_folder, errors_folder)
    pages_folder.mkdir(parents=True, exist_ok=True)
    errors_folder.mkdir(parents=True, exist_ok=True)

    effective_debug = bool(config.DEBUG_VISUAL and not args.fast)
    max_images = None if args.full else args.max_images
    run_signature = stable_hash(
        {
            "url": args.url,
            "max_images": max_images,
            "full": args.full,
            "fast": args.fast,
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
    print(f"Fast: {'sim' if args.fast else 'nao'}", flush=True)
    print(f"Force: {'sim' if args.force else 'nao'}", flush=True)
    print(f"Saida: {output_folder}", flush=True)

    download_started = time.perf_counter()
    image_paths, download_report, download_cache_hit = _download_with_cache(
        args.url,
        max_images,
        output_folder,
        force=args.force,
    )
    download_wall_seconds = time.perf_counter() - download_started
    if not image_paths:
        raise RuntimeError("Nenhuma imagem valida encontrada para o benchmark.")

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
        "ocr_text_repairs": 0,
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
    for index, image_path in enumerate(image_paths, start=1):
        image_hash = file_sha256(image_path)
        process_key = processed_cache_key(
            image_hash,
            pipeline_fingerprint,
            relevant_config,
        )
        output_path = pages_folder / f"page_{index:03}.png"
        state = {
            "index": index,
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

    for state in analyzable_states:
        if state.get("status") == "completed":
            continue
        try:
            render_timings = {}
            final, debug_data = render_analyzed_image(
                state["original_bgr"],
                state.get("raw_lines", []),
                state["candidates"],
                state["groups"],
                font_path=config.FONT_PATH,
                debug_folder=None,
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
            debug_data["text_repairs"] = (
                _applied_text_repairs(state.get("ocr_metadata", {}))
                + state.get("group_text_repairs", [])
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
        output_folder / "capitulo_completo_traduzido.pdf"
        if args.full
        else output_folder / f"benchmark_{args.max_images:03}.pdf"
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
    counters["ocr_text_repairs"] = summary["ocr_text_repairs"]
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
        "total_images": len(image_paths),
        "processed_images": len(completed_states),
        "images_skipped_by_cache": counters["images_skipped_by_cache"],
        "images_skipped_by_no_text_precheck": counters[
            "images_skipped_by_no_text_precheck"
        ],
        "ocr_runs": counters["ocr_runs"],
        "ocr_cache_hits": counters["ocr_cache_hits"],
        "ocr_page_fallbacks": counters["ocr_page_fallbacks"],
        "ocr_text_repairs": counters["ocr_text_repairs"],
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
        "quality_validation": quality,
    }
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
    return list(ocr_metadata.get("text_repairs", []))


def _group_text_repairs(groups):
    return [
        {
            "original_text": group.original_text,
            "repaired_text": group.repaired_text,
            "repair_reason": group.repair_reason,
            "group_id": group.group_id,
        }
        for group in groups
        if group.repair_reason and group.repaired_text != group.original_text
    ]


def _aggregate_debug_data(states):
    result = {
        "ocr_detected_lines": 0,
        "groups_formed": 0,
        "groups_translated": 0,
        "groups_ignored_sfx_decorative": 0,
        "ocr_page_fallbacks": 0,
        "ocr_text_repairs": 0,
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
        result["ocr_text_repairs"] += len(
            debug_data.get("text_repairs", [])
        )
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

    expected_checks = {}
    for source, expected in EXPECTED_TRANSLATIONS.items():
        matches = [
            item
            for item in all_items
            if str(item.get("clean_text", "")).strip().upper() == source.upper()
        ]
        expected_checks[source] = {
            "present": bool(matches),
            "passed": all(
                str(item.get("translation", "")).strip().upper()
                == expected.strip().upper()
                for item in matches
            )
            if matches
            else None,
        }

    reference_presence_ok = (
        not full
        or (
            bool(sniffle_items)
            and all(check["present"] for check in expected_checks.values())
        )
    )
    return {
        "passed": (
            not invalid_pages
            and pdf_pages == expected_page_count
            and high_translation_valid
            and config.TRANSLATE_SFX is False
            and sniffle_ok
            and reference_presence_ok
            and all(
                check["passed"] is not False for check in expected_checks.values()
            )
        ),
        "pdf_pages": pdf_pages,
        "expected_pdf_pages": expected_page_count,
        "invalid_or_blank_pages": invalid_pages,
        "high_translation_pages_valid": high_translation_valid,
        "translate_sfx_disabled": config.TRANSLATE_SFX is False,
        "sniffle_present": bool(sniffle_items),
        "sniffle_sfx_not_translated": sniffle_ok,
        "reference_texts_present_when_required": reference_presence_ok,
        "expected_translation_checks": expected_checks,
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
        f"Textos OCR reparados: {report['ocr_text_repairs']}",
        f"Textos enviados a NVIDIA: {report['translation_api_texts']}",
        f"Traducoes do cache: {report['translation_cache_hits']}",
        "",
        f"Tempo total: {report['total_seconds']:.2f}s",
        f"Download/coleta: {stage['download_collection']:.2f}s",
        f"Validacao: {stage['image_validation']:.2f}s",
        f"No-text precheck: {stage['no_text_precheck']:.2f}s",
        f"OCR (parede): {stage['ocr']:.2f}s",
        f"OCR (soma por pagina): {stage['ocr_cpu']:.2f}s",
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
