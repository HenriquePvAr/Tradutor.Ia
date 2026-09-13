from pathlib import Path


UI = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
CP = Path("static/control_plane_client.js").read_text(encoding="utf-8")
APP = Path("app_ui.py").read_text(encoding="utf-8")


def test_control_plane_flag_is_read_from_authoritative_bootstrap():
    assert "feature_flags" in UI
    assert "translation_enabled" in UI
    assert "syncAuthoritativeTranslationFlag" in UI
    assert "normalizeBootstrapSnapshot" in CP
    assert "bootstrap_policy_missing" in CP


def test_control_plane_policy_event_uses_matching_window_contract():
    assert "window.dispatchEvent(new CustomEvent('tradutor-control-plane-updated'" in CP
    assert "window.addEventListener('tradutor-control-plane-updated'" in UI
    assert "event_target: 'window'" in CP


def test_valid_snapshot_dispatches_after_apply_and_ui_has_startup_sync():
    applied = CP.find("trace('CONTROL_PLANE_SNAPSHOT_APPLIED'")
    dispatched = CP.find("dispatchControlPlaneUpdated({bootstrap: state.bootstrap", applied)
    assert applied >= 0 and dispatched > applied
    assert "window.addEventListener('tradutor-control-plane-ready', syncAuthoritativeTranslationFlag)" in UI
    assert "syncAuthoritativeTranslationFlag();" in UI


def test_ui_policy_sync_fails_closed_without_valid_snapshot():
    assert "if (typeof flags?.translation_enabled !== 'boolean') return null;" in UI


def test_run_payload_snapshots_translation_policy():
    assert "translation_enabled: appState.translationEnabled === true" in UI
    assert "await controlPlane.bootstrap()" in UI
    assert '"translation_enabled": normalized["translation_enabled"]' in Path("ui_bridge.py").read_text(encoding="utf-8")


def test_failed_policy_refresh_cannot_create_silent_disabled_job():
    # A transient bootstrap failure must abort before formPayload()/run; it must
    # never rewrite a previously authoritative true snapshot to false.
    assert "TRANSLATION_POLICY_REFRESH_FAILED" in UI
    assert "control_plane_refresh_failed" in UI
    assert "if (!controlPlaneRefreshOk) appState.translationEnabled = false" not in UI


def test_http_200_without_policy_cannot_replace_snapshot():
    assert "const normalizedBootstrap = normalizeBootstrapSnapshot(bootstrapPayload)" in CP
    assert "state.bootstrap = normalizedBootstrap" in CP
    assert "BOOTSTRAP_FEATURE_FLAGS" in CP


def test_control_plane_trace_allowlist_preserves_bootstrap_policy_fields():
    app = Path("app_ui.py").read_text(encoding="utf-8")
    for field in ("project_ref", "function_slug", "translation_enabled", "maintenance_mode",
                  "keys_present", "feature_flags_present", "feature_flags_type"):
        assert f'"{field}"' in app


def test_ui_policy_trace_allowlist_preserves_propagation_fields():
    for field in ("translation_enabled", "source", "resolved_at",
                  "policy_source", "policy_resolved_at"):
        assert f"'{field}'" in UI


def test_beta_bootstrap_client_returns_direct_json_payload_to_normalizer():
    # The runtime fetch helper parses response.json() and returns that object;
    # no {data,payload,body} wrapper is introduced before normalization.
    assert "const payload = await r.json().catch(() => ({}));" in CP
    assert "return payload;" in CP
    assert "normalizeBootstrapSnapshot(bootstrapPayload)" in CP


def test_bridge_normalization_preserves_explicit_true_snapshot(monkeypatch):
    import ui_bridge

    bridge = object.__new__(ui_bridge.UiBridge)
    monkeypatch.setattr(ui_bridge, "env_status", lambda: {"env_exists": True, "nvidia_configured": True})
    monkeypatch.setattr(ui_bridge, "build_run_command", lambda **_: [])
    payload = {"url": "https://example.com/chapter-1", "translation_enabled": True,
               "translation_provider": "yomu_backend", "force": True}
    normalized = bridge._normalize_payload(payload)
    assert normalized["translation_enabled"] is True


def test_job_configuration_records_translation_policy_provenance():
    bridge_source = Path("ui_bridge.py").read_text(encoding="utf-8")
    assert '"translation_policy_source"' in bridge_source
    assert '"translation_policy_resolved_at"' in bridge_source


def test_enabled_translation_cannot_finish_as_silent_untranslated_pdf():
    source = Path("benchmark_pipeline.py").read_text(encoding="utf-8")
    assert 'raise RuntimeError("translation_not_executed")' in source
    assert "translation_execution_invariant" in source


def test_bridge_normalization_is_fail_closed_when_flag_absent(monkeypatch):
    import ui_bridge

    bridge = object.__new__(ui_bridge.UiBridge)
    monkeypatch.setattr(ui_bridge, "env_status", lambda: {"env_exists": True, "nvidia_configured": True})
    monkeypatch.setattr(ui_bridge, "build_run_command", lambda **_: [])
    normalized = bridge._normalize_payload({"url": "https://example.com/chapter-1"})
    assert normalized["translation_enabled"] is False


def test_backend_run_diagnostics_reflect_payload_not_hardcoded_disabled():
    assert "TRANSLATION_POLICY_RESOLVED" in APP
    assert 'payload.get("translation_enabled") is True' in APP
    assert 'result="DISABLED"' not in APP[APP.find("async def api_run"):APP.find("async def api_run") + 4000]


def test_control_plane_wallet_avoids_false_zero_while_loading():
    assert "walletLoaded" in CP
    assert "Sincronizando…" in CP
    assert "state.walletLoaded = false" in CP


def test_wallet_refreshes_on_rewards_entry_and_daily_claim():
    assert "event.detail?.tab === 'rewards'" in CP
    assert "state.wallet = await call('wallet-summary')" in CP
    assert "state.walletLoaded = true" in CP
