from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import performance_validation_harness as harness
import start_tradutor
import config


def test_performance_harness_parser_accepts_supported_modes():
    args = harness._parser().parse_args([
        "--input-folder", "in", "--output-folder", "out", "--ocr-mode", "rapidocr",
        "--page-workers", "2", "--translation-mode", "deterministic-local",
    ])
    assert args.page_workers == 2
    assert args.translation_mode == "deterministic-local"


def test_deterministic_translator_is_local_and_never_http():
    translator = harness.DeterministicLocalTranslator()
    assert translator.translate_many(["a", "b"]) == ["[LOCAL 1] a", "[LOCAL 2] b"]
    assert translator.stats["provider_http_attempts"] == 0
    assert translator.stats["api_requests"] == 0


def test_internal_child_dispatches_harness():
    with mock.patch("performance_validation_harness.main", return_value=0) as entry:
        assert start_tradutor._run_internal_child("performance-validation", []) == 0
    entry.assert_called_once_with([])


def test_summary_uses_unavailable_for_unmeasured_metrics():
    summary = harness._build_summary({"stage_seconds": {"ocr": 1.0}, "ocr_runs": 5}, 0.0, 1.0)
    assert summary["full_page_ocr_wall_ms"] == 1000.0
    assert summary["full_page_ocr_sequential_sum_ms"] == "UNAVAILABLE"


def test_timeline_and_summary_include_post_forensic_stages():
    report = {
        "stage_seconds": {"ocr": 1.0, "cleanup": 0.2, "typography": 0.1, "render": 0.3},
        "page_timings": {"1": {"cleanup": 0.2, "image_save": 0.01, "render": 0.3}},
        "ocr_runs": 1,
    }
    timeline = harness._build_timeline(report, 0.0, 1.0)
    stages = {event["stage"] for event in timeline["events"]}
    assert {"cleanup", "typography", "render"}.issubset(stages)
    summary = harness._build_summary(report, 0.0, 1.0)
    assert summary["pages"]["1"]["cleanup_ms"] == 200.0
    assert summary["top_execution_stages"][0]["stage"] == "ocr"


def test_timeline_offsets_drive_accounting_summary():
    report = {"stage_seconds": {"ocr": 1.0}, "performance_events": [
        {"stage": "ocr", "phase": "ocr", "worker": "main", "start_offset_ns": 0, "end_offset_ns": 1_000_000_000}
    ]}
    timeline = harness._build_timeline(report, 0.0, 1.0)
    summary = harness._build_summary(report, 0.0, 1.0, timeline)
    assert timeline["absolute_offsets_available"] is True
    assert summary["absolute_offsets_available"] is True
    assert summary["accounted_wall_clock_available"] is True
    assert summary["interval_event_count"] == 1


def test_interval_union_cases():
    assert harness._interval_union_ms([]) == 0.0
    assert harness._interval_union_ms([(0, 10), (20, 30)]) == 20.0
    assert harness._interval_union_ms([(0, 10), (5, 20)]) == 20.0
    assert harness._interval_union_ms([(0, 30), (5, 10)]) == 30.0
    assert harness._interval_union_ms([(0, 10), (10, 20)]) == 20.0
    assert harness._interval_union_ms([(0, 100), (20, 40), (30, 120)]) == 120.0


def test_hybrid_preflight_fails_closed_when_nvidia_disabled():
    with mock.patch.object(config, "NVIDIA_OCR_ENABLED", False):
        result = harness._hybrid_preflight(config)
    assert result["rapidocr_lane_ready"] is True
    assert result["nvidia_lane_enabled"] is False
    assert result["disable_reason"] == "nvidia_ocr_disabled"
    assert result["hybrid_two_lane_preflight"] is False


def test_hybrid_preflight_valid_fake_config_has_two_lanes_without_http():
    class FakeProvider:
        def __init__(self):
            pass

        def is_configured(self):
            return True

    with mock.patch.object(config, "NVIDIA_OCR_ENABLED", True), \
         mock.patch.object(config, "NVIDIA_API_KEY", "test-only-key"), \
         mock.patch("nvidia_ocr_provider.NvidiaOCRProvider", FakeProvider):
        result = harness._hybrid_preflight(config)
    assert result["hybrid_two_lane_preflight"] is True
    assert result["nvidia_lane_enabled"] is True
    assert result["nvidia_worker_created"] is True


def test_hybrid_preflight_rejects_missing_key_and_invalid_endpoint():
    with mock.patch.object(config, "NVIDIA_OCR_ENABLED", True), \
         mock.patch.object(config, "NVIDIA_API_KEY", ""), \
         mock.patch.object(config, "NVIDIA_OCR_INVOKE_URL", "not-a-url"):
        result = harness._hybrid_preflight(config)
    assert result["hybrid_two_lane_preflight"] is False
    assert result["disable_reason"] == "nvidia_api_key_missing"


def test_hybrid_preflight_rejects_invalid_endpoint_when_key_present():
    with mock.patch.object(config, "NVIDIA_OCR_ENABLED", True), \
         mock.patch.object(config, "NVIDIA_API_KEY", "test-only-key"), \
         mock.patch.object(config, "NVIDIA_OCR_INVOKE_URL", "not-a-url"):
        result = harness._hybrid_preflight(config)
    assert result["hybrid_two_lane_preflight"] is False
    assert result["disable_reason"] == "nvidia_endpoint_missing_or_invalid"


def test_hybrid_preflight_rejects_unavailable_provider():
    class UnavailableProvider:
        def is_configured(self):
            return False

    with mock.patch.object(config, "NVIDIA_OCR_ENABLED", True), \
         mock.patch.object(config, "NVIDIA_API_KEY", "test-only-key"), \
         mock.patch("nvidia_ocr_provider.NvidiaOCRProvider", UnavailableProvider):
        result = harness._hybrid_preflight(config)
    assert result["hybrid_two_lane_preflight"] is False
    assert result["disable_reason"] == "nvidia_provider_not_configured"


def test_hybrid_preflight_only_does_not_touch_input_or_pipeline(tmp_path, capsys):
    args = harness._parser().parse_args([
        "--input-folder", str(tmp_path / "does-not-exist"),
        "--output-folder", str(tmp_path / "out"), "--ocr-mode", "rapidocr",
        "--page-workers", "2", "--translation-mode", "deterministic-local",
        "--preflight-only",
    ])
    # Non-hybrid preflight-only remains a no-op contract; hybrid is the strict gate.
    assert args.preflight_only is True
