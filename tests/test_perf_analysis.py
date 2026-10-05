from deploy.vps.perf_analysis import compare, summarize_runs


def test_summarize_runs_has_variability_and_phase_means():
    result = summarize_runs([
        {"total_seconds": 10, "completed": 2, "phase_means_ms": {"generation_ms": 7000}},
        {"total_seconds": 14, "completed": 2, "phase_means_ms": {"generation_ms": 9000}},
    ])
    assert result["total_seconds"]["mean"] == 12
    assert result["total_seconds"]["stddev"] > 0
    assert result["phase_means_ms"]["generation_ms"]["mean"] == 8000


def test_compare_classifies_slow_c2_and_phase_ratio():
    c1 = summarize_runs([{"total_seconds": 10, "phase_means_ms": {"generation_ms": 7000}}])
    c2 = summarize_runs([{"total_seconds": 12, "phase_means_ms": {"generation_ms": 9000}}])
    result = compare(c1, c2)
    assert result["speedup"] < 1
    assert result["classification"] == "C2_SAFE_BUT_NOT_FASTER"
    assert result["phase_contention_ratio_c2_over_c1"]["generation_ms"] > 1
