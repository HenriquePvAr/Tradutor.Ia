from pathlib import Path


UI_SOURCE = Path(__file__).resolve().parents[1] / "static" / "tradutor_ui.js"


def _source() -> str:
    return UI_SOURCE.read_text(encoding="utf-8")


def test_new_translation_suppresses_historical_result_during_source_analysis():
    source = _source()
    assert "suppressHistoricalResult: false" in source
    assert "appState.suppressHistoricalResult = true;" in source
    render_start = source.index("function renderRuntime(runtime)")
    render_end = source.index("function renderRunStatus(runtime)", render_start)
    render_runtime = source[render_start:render_end]
    assert "if (appState.suppressHistoricalResult)" in render_runtime
    assert "$('#jobCompletionPanel') && ($('#jobCompletionPanel').hidden = true);" in render_runtime
    assert "$('#artifactActions') && ($('#artifactActions').innerHTML = '');" in render_runtime


def test_source_analysis_failure_releases_retry_gate_without_showing_old_result():
    source = _source()
    error_start = source.index("function showSourceValidationError(error)")
    error_end = source.index("function ", error_start + len("function "))
    error_handler = source[error_start:error_end]
    assert "status: 'blocked'" in error_handler
    assert "appState.sourceReady = null;" in error_handler
    assert "reasonCode:" in error_handler


def test_historical_result_suppression_ends_only_after_job_ownership_exists():
    source = _source()
    assert source.count("appState.suppressHistoricalResult = false;") >= 2
    pending = source.index("appState.currentJobId = String(pending.job_id || '')")
    assert source.index("appState.suppressHistoricalResult = false;", pending) < source.index("ensurePolling();", pending)
    created = source.index("appState.currentJobId = String(result.job_id || '')")
    assert source.index("appState.suppressHistoricalResult = false;", created) < source.index("persistPipelineIdentity", created)
