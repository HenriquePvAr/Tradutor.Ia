"""Regression: a start failure that creates NO job must not strand the button.

Physically observed (runtime worker-contract-v2, 2026-09-25):

    source ready -> owner clicks "Iniciar Tradução" -> POST /api/ui/run reaches
    the backend -> backend fails BEFORE creating a job with
    ``control_plane_temporarily_unavailable`` (retryable) -> JobStore keeps 0
    active jobs -> ``startInFlight``/``activeStartFingerprint`` are cleared ->
    BUT ``appState.status`` stayed ``'staging'`` (set optimistically by
    ``renderLocalPipelineState`` before the POST) -> ``pipelineBusy`` stayed true
    -> the start button stayed ``disabled`` -> every further physical gesture
    logged ``result:"TIMEOUT" click_seen:false`` and no new /api/ui/run could be
    sent.  Only a UI reload recovered.

``startInFlight`` alone was NOT the culprit: the single-flight promise resolves
even on failure and both guards are cleared in its ``.then``/``.catch``.  The
stuck field was ``appState.status`` holding an active-operation value.

The fix adds ``clearOptimisticExecutionBusy()`` and calls it on the no-job
settle path, so a pre-job failure re-enables retry without a reload -- while a
genuinely active job (``appState.activeJobId``) keeps gating the button.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")

# Mirror of the JS Set literal so the model can never silently drift from source.
ACTIVE_OPERATION_STATUSES = {
    "staging", "claiming", "starting", "running", "cancelling", "awaiting_source_review",
}


# --------------------------------------------------------------------------- #
# Structural contract: the fix exists and is wired at the single settle point.
# --------------------------------------------------------------------------- #
def test_source_declares_the_active_operation_statuses_used_by_the_model():
    # If the JS Set changes, this test's model must be updated deliberately.
    assert ("const activeOperationStatuses = new Set(['staging', 'claiming', "
            "'starting', 'running', 'cancelling', 'awaiting_source_review']);") in SOURCE


def test_clear_helper_is_defined_and_guarded_by_active_job_identity():
    assert "function clearOptimisticExecutionBusy()" in SOURCE
    # It must only clear when no real active job exists (never mask a running job)
    # and only when the status is actually an optimistic active-operation value.
    assert "if (!appState.activeJobId && activeOperationStatuses.has(appState.status))" in SOURCE
    # It restores a non-active status, preserving source readiness when ready.
    assert "appState.status = appState.sourceValidation?.status === 'ready'" in SOURCE
    assert "'source_analysis_ready'" in SOURCE


def test_clear_helper_is_called_on_the_no_job_settle_path():
    # The no-job branch of the single-flight promise (same branch that clears the
    # fingerprint) must also clear the optimistic execution-busy status.
    assert ("if (!result || !result.job_id) { activeStartFingerprint = ''; "
            "clearOptimisticExecutionBusy(); }") in SOURCE
    # And the unhandled-error path of the SAME single-flight promise too.
    assert ("activeStartFingerprint = '';\n      clearOptimisticExecutionBusy();") in SOURCE


def test_the_run_failure_catch_does_not_itself_reset_status_but_settle_path_does():
    # Documents WHY the fix lives at the settle point: the run-failure catch only
    # restores the button label + error card, never appState.status.
    assert "showStartError(error);" in SOURCE
    # Preserve the prior active-job gate (must not regress).
    assert "activeOperationStatuses.has(appState.status) || Boolean(appState.activeJobId)" in SOURCE


# --------------------------------------------------------------------------- #
# Behavioural model of the start gate (mirrors updateTranslationStartControls).
# --------------------------------------------------------------------------- #
class StartGateModel:
    def __init__(self, *, source_ready=True, policy_allows=True):
        self.status = "source_analysis_ready" if source_ready else "ready"
        self.active_job_id = ""
        self.start_in_flight = False
        self.active_start_fingerprint = ""
        self.new_translation_draft = False
        self.source_validation_status = "ready" if source_ready else "blocked"
        self.minimum_valid = True
        self.policy_allows = policy_allows
        self.run_request_count = 0

    # --- gate (updateTranslationStartControls / translationStartDisabledReasons) ---
    @property
    def pipeline_busy(self):
        return self.status in ACTIVE_OPERATION_STATUSES or bool(self.active_job_id)

    @property
    def button_enabled(self):
        busy_blocks_draft = self.pipeline_busy and not self.new_translation_draft
        start_busy = bool(self.start_in_flight or self.active_start_fingerprint)
        return (self.minimum_valid and self.policy_allows
                and not busy_blocks_draft and not start_busy)

    # --- fix (clearOptimisticExecutionBusy) ---
    def _clear_optimistic_execution_busy(self):
        if not self.active_job_id and self.status in ACTIVE_OPERATION_STATUSES:
            self.status = ("source_analysis_ready"
                           if self.source_validation_status == "ready" else "ready")

    # --- one start attempt; returns the job_id created (or "" on pre-job failure) ---
    def attempt_start(self, *, run_result_job_id="", backend_has_active_job=False):
        # optimistic lock (runStartTranslation head + renderLocalPipelineState)
        self.start_in_flight = True
        self.active_start_fingerprint = "fp"
        self.status = "staging"
        # POST /api/ui/run
        self.run_request_count += 1
        # settle (.then/.catch/.finally of the single-flight promise)
        job_created = bool(run_result_job_id)
        if backend_has_active_job:
            self.active_job_id = run_result_job_id or "running-elsewhere"
        if not job_created:
            self.active_start_fingerprint = ""
            self._clear_optimistic_execution_busy()
        else:
            self.active_job_id = run_result_job_id
        self.start_in_flight = None if False else False
        return run_result_job_id


def test_prejob_failure_reenables_retry_without_reload():
    m = StartGateModel(source_ready=True)
    assert m.button_enabled  # idle, source ready
    m.attempt_start(run_result_job_id="")  # /api/ui/run fails before creating a job
    assert m.active_job_id == ""            # no job created
    assert m.pipeline_busy is False         # optimistic 'staging' was cleared
    assert m.start_in_flight is False
    assert m.active_start_fingerprint == ""
    assert m.button_enabled is True         # retry is possible without a reload


def test_second_click_sends_a_new_run_request():
    m = StartGateModel(source_ready=True)
    m.attempt_start(run_result_job_id="")   # first failure
    assert m.run_request_count == 1
    assert m.button_enabled                 # not stranded
    m.attempt_start(run_result_job_id="")   # owner clicks again -> new POST
    assert m.run_request_count == 2         # RUN_REQUEST_COUNT=2, no reload


def test_a_real_active_job_still_blocks_a_second_start():
    m = StartGateModel(source_ready=True)
    # A genuinely active job is reported (e.g. a running job discovered by poll).
    m.attempt_start(run_result_job_id="job-123", backend_has_active_job=True)
    assert m.active_job_id == "job-123"
    assert m.pipeline_busy is True
    assert m.button_enabled is False        # concurrency is refused


def test_old_failed_job_with_no_active_job_allows_retry():
    m = StartGateModel(source_ready=True)
    # Simulate a stale terminal status left by a previously failed job, but no
    # active job in the runtime.  The active-operation status is what strands it;
    # a terminal status must not.
    m.status = "failed"          # terminal, not in ACTIVE_OPERATION_STATUSES
    m.active_job_id = ""
    assert m.pipeline_busy is False
    assert m.button_enabled is True         # error card may show; retry allowed


def test_invalid_conditions_still_block_after_clearing_busy():
    # Clearing residual busy must NOT force-enable when other gates fail.
    blocked_policy = StartGateModel(source_ready=True, policy_allows=False)
    blocked_policy.attempt_start(run_result_job_id="")
    assert blocked_policy.pipeline_busy is False
    assert blocked_policy.button_enabled is False   # workspace policy still blocks

    invalid_input = StartGateModel(source_ready=True)
    invalid_input.minimum_valid = False
    invalid_input.attempt_start(run_result_job_id="")
    assert invalid_input.button_enabled is False     # invalid source still blocks


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} checks passed")
    sys.exit(0)
