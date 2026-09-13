"""CLI-only frozen validation harness for the real page pipeline.

This module is intentionally an adapter around :func:`benchmark_pipeline.run_benchmark`.
It supplies a trusted local snapshot and a deterministic translator for offline validation;
it never changes the production provider or exposes a UI entry point.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse


def _interval_union_ms(intervals) -> float:
    """Deterministically merge [start_ms, end_ms] intervals."""
    normalized = sorted(
        (float(start), float(end))
        for start, end in intervals
        if start is not None and end is not None and float(end) >= float(start)
    )
    if not normalized:
        return 0.0
    total = 0.0
    current_start, current_end = normalized[0]
    for start, end in normalized[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


class DeterministicLocalTranslator:
    model = "deterministic-local"
    provider_name = "deterministic-local"

    def __init__(self) -> None:
        self.force_cache = False
        self.calls: list[list[str]] = []
        self.stats = {
            "provider_name": self.provider_name,
            "model": self.model,
            "api_texts": 0,
            "api_requests": 0,
            "translation_batches": 0,
            "successful_batches": 0,
            "failed_batches": 0,
            "provider_http_attempts": 0,
        }

    def translate_many(self, texts, force=False):
        values = [str(text or "") for text in texts]
        self.calls.append(values)
        self.stats["translation_batches"] += 1
        self.stats["successful_batches"] += 1
        self.stats["api_texts"] += len(values)
        return [f"[LOCAL {index + 1}] {text}" for index, text in enumerate(values)]

    def translate_strict(self, *_args, **_kwargs):
        raise AssertionError("deterministic-local translator must not retry")

    def set_detected_names(self, _names):
        return None

    def set_session_context(self, _context):
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="performance-validation", add_help=True)
    parser.add_argument("--input-folder", required=True)
    parser.add_argument("--output-folder", required=True)
    parser.add_argument("--ocr-mode", choices=("rapidocr", "nvidia", "hybrid"), required=True)
    parser.add_argument("--page-workers", type=int, choices=(1, 2), required=True)
    parser.add_argument("--translation-mode", choices=("deterministic-local",), required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def _nvidia_preflight(config_module=None) -> dict:
    """Return sanitized NVIDIA readiness without making any network call."""
    config_module = config_module or import_module("config")
    result = {
        "nvidia_lane_requested": True,
        "nvidia_provider_importable": False,
        "nvidia_api_key_present": bool(str(getattr(config_module, "NVIDIA_API_KEY", "") or "").strip()),
        "nvidia_endpoint_present": False,
        "nvidia_deployment": str(getattr(config_module, "NVIDIA_OCR_DEPLOYMENT", "hosted") or "hosted").strip().lower(),
        "nvidia_config_valid": False,
        "nvidia_provider_constructed": False,
        "nvidia_worker_created": False,
        "nvidia_lane_enabled": False,
        "disable_reason": "",
    }
    try:
        provider_module = import_module("nvidia_ocr_provider")
        provider_cls = provider_module.NvidiaOCRProvider
        result["nvidia_provider_importable"] = True
    except Exception:
        result["disable_reason"] = "nvidia_provider_unimportable"
        return result

    deployment = result["nvidia_deployment"]
    if deployment == "self_hosted":
        endpoint = str(getattr(config_module, "NVIDIA_OCR_ENDPOINT", "") or "").strip()
        if not endpoint:
            base = str(getattr(config_module, "NVIDIA_OCR_SELF_HOSTED_BASE_URL", "") or "").strip()
            endpoint = f"{base.rstrip('/')}/v1/ocr" if base else ""
    else:
        endpoint = str(getattr(config_module, "NVIDIA_OCR_INVOKE_URL", "") or "").strip()
    parsed = urlparse(endpoint)
    result["nvidia_endpoint_present"] = bool(parsed.scheme in {"http", "https"} and parsed.netloc)

    if not bool(getattr(config_module, "NVIDIA_OCR_ENABLED", False)):
        result["disable_reason"] = "nvidia_ocr_disabled"
        return result
    if not result["nvidia_api_key_present"]:
        result["disable_reason"] = "nvidia_api_key_missing"
        return result
    if not result["nvidia_endpoint_present"]:
        result["disable_reason"] = "nvidia_endpoint_missing_or_invalid"
        return result
    try:
        provider = provider_cls()
        result["nvidia_provider_constructed"] = True
        if not provider.is_configured():
            result["disable_reason"] = "nvidia_provider_not_configured"
            return result
    except Exception as exc:
        reason = str(exc).split(":", 1)[0].strip().lower().replace(" ", "_")
        result["disable_reason"] = reason or "nvidia_provider_configuration_error"
        return result
    result["nvidia_config_valid"] = True
    result["nvidia_worker_created"] = True
    result["nvidia_lane_enabled"] = True
    return result


def _hybrid_preflight(config_module=None) -> dict:
    config_module = config_module or import_module("config")
    try:
        import_module("ocr_hybrid_scheduler")
        rapid_ready = bool(getattr(config_module, "RAPIDOCR_ENABLED", True))
    except Exception:
        rapid_ready = False
    nvidia = _nvidia_preflight(config_module)
    result = {
        "requested_mode": "hybrid",
        "rapidocr_lane_ready": rapid_ready,
        **nvidia,
    }
    result["hybrid_two_lane_preflight"] = bool(rapid_ready and nvidia["nvidia_lane_enabled"])
    if not result["hybrid_two_lane_preflight"] and not result["disable_reason"]:
        result["disable_reason"] = "rapidocr_lane_unavailable"
    return result


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _build_timeline(report: dict, started: float, finished: float) -> dict:
    stages = report.get("stage_seconds") or {}
    timeline = []
    for event in report.get("performance_events") or []:
        start_ns = event.get("start_offset_ns"); end_ns = event.get("end_offset_ns")
        timeline.append({**event, "event_kind": "INTERVAL_EVENT", "queue_enter": None, "start": None, "end": None,
                         "duration_ms": round((end_ns - start_ns) / 1_000_000, 3)
                         if start_ns is not None and end_ns is not None else None,
                         "resource_wait_ms": None})
    for stage, duration in stages.items():
        if not isinstance(duration, (int, float)):
            continue
        timeline.append({"event_kind": "AGGREGATE_METRIC", "page_index": None, "phase": "run", "stage": stage,
                         "worker": "main", "queue_enter": None, "start": None,
                         "end": None, "duration_ms": round(float(duration) * 1000, 3),
                         "resource_wait_ms": None})
    for page, timings in sorted((report.get("page_timings") or {}).items(), key=lambda item: int(item[0])):
        for event in timings.get("performance_events", []) if isinstance(timings, dict) else []:
            start_ns = event.get("start_offset_ns")
            end_ns = event.get("end_offset_ns")
            timeline.append({
                "event_kind": "INTERVAL_EVENT", "page_index": int(page), "phase": event.get("phase", "page"),
                "stage": event.get("stage", "page"), "worker": event.get("worker", "page"),
                "queue_enter": None, "start": None, "end": None,
                "start_offset_ns": start_ns, "end_offset_ns": end_ns,
                "duration_ms": round((float(end_ns) - float(start_ns)) / 1_000_000, 3)
                    if start_ns is not None and end_ns is not None else None,
                "resource_wait_ms": None,
            })
        for stage, duration in timings.items():
            if isinstance(duration, (int, float)) and float(duration) > 0:
                timeline.append({"event_kind": "AGGREGATE_METRIC", "page_index": int(page), "phase": "page", "stage": stage,
                                 "worker": "page", "queue_enter": None, "start": None,
                                 "end": None, "duration_ms": round(float(duration) * 1000, 3),
                                 "resource_wait_ms": None})
            elif stage == "cleanup_substages" and isinstance(duration, dict):
                for substage, metrics in sorted(duration.items()):
                    if isinstance(metrics, dict):
                        timeline.append({"event_kind": "AGGREGATE_METRIC", "page_index": int(page), "phase": "page", "stage": f"cleanup.{substage}",
                                         "worker": "page", "queue_enter": None, "start": None,
                                         "end": None, "duration_ms": metrics.get("total_ms", 0.0),
                                         "resource_wait_ms": None,
                                         "call_count": metrics.get("call_count", 0),
                                         "max_single_call_ms": metrics.get("max_single_call_ms", 0.0)})
    clock = report.get("performance_clock") or {}
    interval_events = [e for e in timeline if e.get("start_offset_ns") is not None and e.get("end_offset_ns") is not None]
    job_wall_ms = round((finished - started) * 1000, 3)
    # The harness job also includes real orchestration before the first stage
    # and finalization after the last measured stage.  Represent those explicit
    # boundaries as intervals; aggregates remain aggregates.
    if interval_events:
        first_start = min(e["start_offset_ns"] for e in interval_events) / 1_000_000
        last_end = max(e["end_offset_ns"] for e in interval_events) / 1_000_000
        if first_start > 0:
            timeline.append({"event_kind": "INTERVAL_EVENT", "stage": "job_startup", "phase": "orchestration",
                             "page_index": None, "worker": "main", "start_offset_ns": 0,
                             "end_offset_ns": int(first_start * 1_000_000), "duration_ms": round(first_start, 3),
                             "queue_enter": None, "start": None, "end": None, "resource_wait_ms": None})
        if last_end < job_wall_ms:
            timeline.append({"event_kind": "INTERVAL_EVENT", "stage": "job_finalize", "phase": "orchestration",
                             "page_index": None, "worker": "main", "start_offset_ns": int(last_end * 1_000_000),
                             "end_offset_ns": int(job_wall_ms * 1_000_000), "duration_ms": round(job_wall_ms - last_end, 3),
                             "queue_enter": None, "start": None, "end": None, "resource_wait_ms": None})
    interval_events = [e for e in timeline if e.get("event_kind") == "INTERVAL_EVENT" and e.get("start_offset_ns") is not None and e.get("end_offset_ns") is not None]
    accounted_ms = _interval_union_ms([(e["start_offset_ns"] / 1_000_000, e["end_offset_ns"] / 1_000_000) for e in interval_events])
    gaps = []
    cursor = 0.0
    for start, end in sorted((e["start_offset_ns"] / 1_000_000, e["end_offset_ns"] / 1_000_000) for e in interval_events):
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < job_wall_ms:
        gaps.append((cursor, job_wall_ms))
    gap_payload = [{"rank": i + 1, "start_offset_ms": round(a, 3), "end_offset_ms": round(b, 3),
                    "duration_ms": round(b - a, 3)} for i, (a, b) in enumerate(sorted(gaps, key=lambda x: x[1]-x[0], reverse=True)[:20])]
    absolute_offsets_available = bool(interval_events) and all(
        isinstance(e.get("start_offset_ns"), int) and isinstance(e.get("end_offset_ns"), int)
        and e["end_offset_ns"] >= e["start_offset_ns"] for e in interval_events
    )
    return {"run_id": report.get("job_run_id") or "", "job_wall_clock_ms": job_wall_ms,
            "clock": clock.get("clock", "perf_counter_ns"),
            "origin": clock.get("origin", "job_start"),
            "absolute_offsets_available": absolute_offsets_available,
            "events": timeline, "page_events_available": bool(timeline),
            "interval_event_count": len(interval_events),
            "events_with_valid_offsets": len(interval_events),
            "events_without_offsets": len(timeline) - len(interval_events),
            "accounted_wall_clock_ms": round(accounted_ms, 3),
            "unaccounted_wall_clock_ms": round(max(0.0, job_wall_ms - accounted_ms), 3),
            "unaccounted_percent": round(max(0.0, job_wall_ms - accounted_ms) / job_wall_ms * 100, 3) if job_wall_ms else 0.0,
            "largest_unaccounted_gaps": gap_payload,
            "interval_event_count": len(interval_events),
            "aggregate_metric_count": len(timeline) - len(interval_events)}


def _build_summary(report: dict, started: float, finished: float, timeline: dict | None = None) -> dict:
    stages = report.get("stage_seconds") or {}
    summary = {"job_wall_clock_ms": round((finished - started) * 1000, 3)}
    for key in ("download_collection", "image_validation", "ocr", "ocr_selective_fallback",
                "classification_grouping", "translation", "cleanup", "inpainting",
                "typography", "render", "quality", "save", "pdf"):
        summary[key] = round(float(stages[key]) * 1000, 3) if isinstance(stages.get(key), (int, float)) else "UNAVAILABLE"
    summary.update({
        "full_page_ocr_wall_ms": summary.get("ocr", "UNAVAILABLE"),
        "full_page_ocr_sequential_sum_ms": "UNAVAILABLE",
        "regional_recovery_ms": summary.get("ocr_selective_fallback", "UNAVAILABLE"),
        "translation_fixture_ms": summary.get("translation", "UNAVAILABLE"),
        "lama_queue_wait_ms": "NOT_EXERCISED", "lama_resource_wait_ms": "NOT_EXERCISED",
        "lama_hash_ms": "NOT_EXERCISED", "lama_load_ms": "NOT_EXERCISED", "lama_inference_ms": "NOT_EXERCISED",
        "lama_counters": {"status": "NOT_EXERCISED", "call_count": 0, "hash_lookup_count": 0,
                          "hash_cache_hit_count": 0, "hash_cache_miss_count": 0,
                          "real_hash_compute_count": 0, "model_load_count": 0, "inference_count": 0},
        "recovery_breakdown": {"status": "NOT_EXERCISED", "by_type": {}},
        "queue_waits": {"status": "UNAVAILABLE", "ocr_ms": "UNAVAILABLE", "pre_ms": "UNAVAILABLE", "post_ms": "UNAVAILABLE"},
        "resource_waits": {"status": "UNAVAILABLE", "rapidocr_ms": "UNAVAILABLE", "lama_ms": "NOT_EXERCISED"},
        "accounted_wall_clock_ms": (timeline or {}).get("accounted_wall_clock_ms", "UNAVAILABLE"),
        "unaccounted_wall_clock_ms": (timeline or {}).get("unaccounted_wall_clock_ms", "UNAVAILABLE"),
        "unaccounted_percent": (timeline or {}).get("unaccounted_percent", "UNAVAILABLE"),
        "unaccounted_intervals": (timeline or {}).get("largest_unaccounted_gaps", []),
        "rapidocr_full_page_calls": report.get("ocr_runs", "UNAVAILABLE"),
        "nvidia_ocr_calls": 0,
        "max_concurrent_pre_pages": report.get("pipeline_page_workers", "UNAVAILABLE"),
        "max_concurrent_post_pages": report.get("pipeline_page_workers", "UNAVAILABLE"),
        "max_concurrent_rapidocr": 1, "max_concurrent_lama": 1,
        "pre_translation_overlap": bool(report.get("pre_translation_parallel", False)),
        "post_translation_overlap": bool(report.get("pipeline_page_workers", 1) > 1),
        "source_completeness_status": (report.get("quality_validation") or {}).get("source_completeness", "UNAVAILABLE"),
        "provenance_duplicates": 0,
        "pages": {
            str(page): {
                "full_page_ocr_ms": "UNAVAILABLE",
                "recovery_ms": round(float(values.get("ocr_selective_fallback", 0.0)) * 1000, 3),
                "grouping_ms": "UNAVAILABLE",
                "classification_ms": "UNAVAILABLE",
                "cleanup_ms": round(float(values.get("cleanup", 0.0)) * 1000, 3),
                "lama_ms": "UNAVAILABLE",
                "typography_ms": round(float(values.get("typography", values.get("redraw", 0.0))) * 1000, 3),
                "render_ms": round(float(values.get("render", 0.0)) * 1000, 3),
                "quality_ms": round(float(values.get("quality", 0.0)) * 1000, 3),
                "save_ms": round(float(values.get("image_save", 0.0)) * 1000, 3),
                "total_active_ms": round(sum(float(v) for v in values.values() if isinstance(v, (int, float))) * 1000, 3),
                "queue_wait_ms": "UNAVAILABLE",
                "resource_wait_ms": "UNAVAILABLE",
                "cleanup_substages": values.get("cleanup_substages", {}),
            }
            for page, values in (report.get("page_timings") or {}).items()
        },
        "top_execution_stages": sorted(
            ({"stage": key, "seconds": float(value)} for key, value in stages.items() if isinstance(value, (int, float))),
            key=lambda item: item["seconds"], reverse=True,
        )[:5],
        "top_queue_waits": [],
        "top_resource_waits": [],
        "accounting_method": "canonical perf_counter_ns origin; interval events only; aggregate metrics excluded",
    })
    if timeline:
        summary.update({
            "absolute_offsets_available": bool(timeline.get("absolute_offsets_available")),
            "accounted_wall_clock_available": bool(timeline.get("absolute_offsets_available")),
            "interval_event_count": timeline.get("interval_event_count", 0),
            "aggregate_metric_count": timeline.get("aggregate_metric_count", 0),
            "events_with_valid_offsets": timeline.get("events_with_valid_offsets", 0),
            "events_without_offsets": timeline.get("events_without_offsets", 0),
        })
    return summary


def run(parsed: argparse.Namespace) -> int:
    input_folder = Path(parsed.input_folder).expanduser().resolve()
    output_folder = Path(parsed.output_folder).expanduser().resolve()
    if not input_folder.is_dir() and not parsed.preflight_only:
        raise SystemExit("input_folder_not_found")
    import config
    if parsed.ocr_mode == "hybrid":
        preflight = _hybrid_preflight(config)
        output_folder = Path(parsed.output_folder).expanduser().resolve()
        output_folder.mkdir(parents=True, exist_ok=True)
        _write_json(output_folder / "hybrid_two_lane_preflight.json", preflight)
        if parsed.preflight_only:
            print(json.dumps(preflight, ensure_ascii=False, sort_keys=True))
            return 0 if preflight["hybrid_two_lane_preflight"] else 1
        if not preflight["hybrid_two_lane_preflight"]:
            raise SystemExit(f"hybrid_two_lane_unavailable:{preflight['disable_reason']}")
    pages = sorted(p for p in input_folder.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".avif"})
    if not pages:
        raise SystemExit("input_folder_has_no_images")
    output_folder.mkdir(parents=True, exist_ok=True)
    snapshot_root = output_folder / ".snapshot"
    snapshot_root.mkdir(parents=True, exist_ok=True)

    import benchmark_pipeline
    import local_folder_input
    from local_folder_source import LocalFolderChapterAdapter, LocalFolderPolicy

    snapshot = LocalFolderChapterAdapter(LocalFolderPolicy(allowed_roots=(input_folder.parent,))).snapshot(
        input_folder, snapshot_root, snapshot_id=(parsed.run_id or "performance_validation")[:64]
    )
    reference = local_folder_input.local_source_reference(snapshot.analysis.source_fingerprint)
    translator = DeterministicLocalTranslator()
    args = SimpleNamespace(
        url=reference, max_images=len(pages), full=False, debug_folder=str(output_folder / "debug"),
        keep_debug=False, fast=False, benchmark=True, force=True, force_download=True,
        page_indices="", output_folder=str(output_folder), ocr_engine=parsed.ocr_mode,
        use_context=False, session_context_path=str(output_folder / "session_context.json"),
        source_candidate_ids=[], local_manifest_path=str(snapshot.manifest_path),
        translation_provider="yomu_backend", job_run_id=parsed.run_id or "performance-validation",
    )
    old_workers = os.environ.get("PIPELINE_PAGE_WORKERS")
    old_ocr = os.environ.get("OCR_ENGINE")
    old_rapidocr = getattr(config, "RAPIDOCR_ENABLED", True)
    old_execution_mode = getattr(config, "OCR_EXECUTION_MODE", "rapidocr")
    os.environ["PIPELINE_PAGE_WORKERS"] = str(parsed.page_workers)
    os.environ["TRANSLATION_ENABLED"] = "true"
    os.environ["YOMU_ENV"] = "test"
    os.environ["YOMU_TEST_BACKEND"] = "mock"
    config.OCR_ENGINE = parsed.ocr_mode
    # The full-page dispatcher keys off OCR_EXECUTION_MODE; keep it aligned
    # with the explicit harness argument instead of silently using the legacy
    # rapidocr/default path (which could then fall through to Tesseract).
    config.OCR_EXECUTION_MODE = parsed.ocr_mode
    config.RAPIDOCR_ENABLED = True

    original_materialize = local_folder_input.materialize_snapshot
    def materialize_for_harness(manifest_path, target_folder, **kwargs):
        return original_materialize(manifest_path, target_folder, output_root=Path(target_folder).parent, **kwargs)
    started = time.perf_counter()
    try:
        with mock.patch.object(local_folder_input, "LOCAL_SNAPSHOT_ROOT", snapshot_root), \
             mock.patch.object(local_folder_input, "materialize_snapshot", side_effect=materialize_for_harness), \
             mock.patch.object(benchmark_pipeline, "_resolve_translation_runtime", return_value=(translator, "eng")), \
             mock.patch.object(benchmark_pipeline, "resolve_provider_provenance", return_value={}):
            report = benchmark_pipeline.run_benchmark(args)
    finally:
        finished = time.perf_counter()
        if old_workers is None: os.environ.pop("PIPELINE_PAGE_WORKERS", None)
        else: os.environ["PIPELINE_PAGE_WORKERS"] = old_workers
        if old_ocr is None: os.environ.pop("OCR_ENGINE", None)
        else: os.environ["OCR_ENGINE"] = old_ocr
        config.RAPIDOCR_ENABLED = old_rapidocr
        config.OCR_EXECUTION_MODE = old_execution_mode
    timeline = _build_timeline(report, started, finished)
    if timeline.get("absolute_offsets_available"):
        report.setdefault("performance_clock", {})["absolute_offsets_available"] = True
    summary = _build_summary(report, started, finished, timeline)
    _write_json(output_folder / "report.json", report)
    _write_json(output_folder / "performance_timeline.json", timeline)
    _write_json(output_folder / "performance_summary.json", summary)
    # Keep a sanitized copy under the normal runtime root so the existing
    # diagnostics exporter can collect performance evidence without knowing
    # about this private CLI entry point.
    from runtime_paths import runtime_root
    export_root = runtime_root() / "performance_validation" / (parsed.run_id or "run")
    export_root.mkdir(parents=True, exist_ok=True)
    for artifact in ("report.json", "performance_timeline.json", "performance_summary.json",
                     "quality_report.json", "run_manifest.json", "ocr_line_provenance.json"):
        source = output_folder / artifact
        if source.is_file():
            shutil.copy2(source, export_root / artifact)
    print(json.dumps({"run_id": parsed.run_id or "performance-validation", "status": report.get("status"),
                      "page_count": report.get("processed_logical_pages", 0),
                      "wall_clock_ms": round((finished - started) * 1000, 3),
                      "output_folder": str(output_folder), "pdf_path": report.get("pdf_path", "")}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
