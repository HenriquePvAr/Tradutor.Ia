from deploy.vps.perf_timing import PhaseTimer


def test_phase_timer_uses_monotonic_marks_and_nonnegative_durations():
    timer = PhaseTimer()
    timer.mark("upload_start_at")
    timer.mark("upload_end_at")
    result = timer.export()
    assert result["clock"] == "monotonic"
    assert result["marks_monotonic"]["upload_end_at"] >= result["marks_monotonic"]["upload_start_at"]
    assert result["durations_ms"]["upload_end_ms"] >= 0
