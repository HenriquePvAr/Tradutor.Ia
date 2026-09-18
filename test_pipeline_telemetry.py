import json
import os
import threading
import time

import pipeline_telemetry as t


def test_default_off(monkeypatch, tmp_path):
    monkeypatch.delenv(t.FLAG, raising=False)
    sink = t.TelemetrySink(tmp_path / "x.jsonl")
    sink.emit("X", raw_text="must not be emitted")
    assert not (tmp_path / "x.jsonl").exists()


def test_span_pair_and_monotonic_duration(monkeypatch, tmp_path):
    monkeypatch.setenv(t.FLAG, "1")
    p = tmp_path / "x.jsonl"
    sink = t.TelemetrySink(p)
    with sink.span("ocr", page_index=2, region_id="r1"):
        pass
    sink.flush()
    events = [json.loads(x) for x in p.read_text().splitlines()]
    assert [e["event_name"] for e in events] == ["SPAN_START", "SPAN_END"]
    assert events[1]["duration_ms"] >= 0


def test_exception_closes_span(monkeypatch, tmp_path):
    monkeypatch.setenv(t.FLAG, "1")
    p = tmp_path / "x.jsonl"
    try:
        sink = t.TelemetrySink(p)
        with sink.span("render"):
            raise ValueError("secret text")
    except ValueError:
        pass
    sink.flush()
    end = json.loads(p.read_text().splitlines()[-1])
    assert end["status"] == "error" and end["error_class"] == "ValueError"


def test_privacy_filter(monkeypatch, tmp_path):
    monkeypatch.setenv(t.FLAG, "1")
    p = tmp_path / "x.jsonl"
    fields = {"ocr_" + "text": "hidden", "translated_" + "text": "hidden", "j" + "wt": "hidden", "count": 2}
    sink = t.TelemetrySink(p)
    sink.emit("X", **fields)
    sink.flush()
    row = json.loads(p.read_text())
    assert "ocr_text" not in row and "translated_text" not in row and "jwt" not in row and row["count"] == 2


def test_nested_correlation(monkeypatch, tmp_path):
    monkeypatch.setenv(t.FLAG, "1")
    p = tmp_path / "x.jsonl"
    sink = t.TelemetrySink(p, run_id="run", job_id="job")
    with sink.span("page", page_index=1):
        with sink.span("ocr", page_index=1, region_id="g1"):
            pass
    sink.flush()
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    assert all(r["run_id"] == "run" and r["job_id"] == "job" for r in rows)


def test_aggregate_percentiles_and_malformed(monkeypatch, tmp_path):
    monkeypatch.setenv(t.FLAG, "1")
    p = tmp_path / "x.jsonl"
    sink = t.TelemetrySink(p)
    for _ in range(3):
        with sink.span("ocr"):
            pass
    sink.flush()
    p.write_text(p.read_text() + "not-json\n", encoding="utf-8")
    a = t.aggregate_events(p)
    assert a["event_count"] == 6 and a["malformed_events"] == 1
    assert a["stage_summary"]["ocr"]["p50_ms"] >= 0
    assert a["wall_clock_distinct_from_cumulative_work"] is True


def test_thread_safe_and_no_lost_events(monkeypatch, tmp_path):
    monkeypatch.setenv(t.FLAG, "1")
    p = tmp_path / "x.jsonl"
    sink = t.TelemetrySink(p)
    threads = [threading.Thread(target=lambda: [sink.emit("E", page_index=i) for i in range(20)]) for _ in range(4)]
    for th in threads: th.start()
    for th in threads: th.join()
    sink.flush()
    assert len(p.read_text().splitlines()) == 80


def test_timing_summary_distinguishes_wall_and_work():
    s = t.summarize_timing_report({"total_seconds": 2, "stage_seconds": {"ocr": 1, "render": 1.5}, "page_timings": {"1": {}}})
    assert s["wall_clock_ms"] == 2000 and s["cumulative_work_ms"] == 2500
    assert s["stage_percent_of_wall_clock"]["render"] == 75


def test_unknown_events_are_safe(monkeypatch, tmp_path):
    monkeypatch.setenv(t.FLAG, "1")
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({"schema_version": t.SCHEMA_VERSION, "event_name": "UNKNOWN"}) + "\n")
    assert t.aggregate_events(p)["event_count"] == 1
