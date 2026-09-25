from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_passive_slot_uses_public_property_and_never_touches_yk():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    for url in ("/ad/home-728x90.html", "/ad/new-translation-728x90.html", "/ad/queue-468x60.html", "/ad/rewards-320x50.html", "/ad/translated-chapters-native.html"):
        assert url in source
    assert "__yomuPassiveAdsProviderEnabled" in source
    assert "providerEnabled" in source
    assert "credit_rewarded" not in source.lower()
    assert "DAILY_YK_CLAIM_TRIGGERED" in source


def test_passive_slots_are_limited_to_neutral_views():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    for view in ("#view-inicio", "#view-nova", "#view-queue", "#view-rewards", "#view-hist"):
        assert view in source
    assert "#view-scans" not in source
    for forbidden in ("#view-leitor", "#view-profile", "#view-cfg", "#view-updates", "#view-community"):
        assert forbidden not in source


def test_live_shell_declares_only_the_five_approved_placements():
    html = (ROOT / "ui/ui_shell.html").read_text(encoding="utf-8")
    for name in ("home", "new-translation", "queue", "rewards", "translated-chapters"):
        assert f'data-passive-ad-slot="{name}"' in html
    assert 'data-passive-ad-slot="scans"' not in html


def test_native_geometry_uses_single_flight_and_no_global_mutation_observer():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "MutationObserver" not in source
    assert "requestAnimationFrame" in source
    assert "lastSentKey" in source
    assert "inFlight" in source
    assert "pendingState" in source


def test_native_route_hook_is_explicit_and_resize_observer_is_scoped():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "yomu-passive-ad-route" in source
    assert "resizeObserver.disconnect()" in source
    assert "resizeObserver.observe(activeAdSlot)" in source
    assert "tradutor-auth-changed" in source
    assert "activeAdSlot.hidden = false" in source


def test_host_mapping_keeps_vercel_only_for_approved_placements():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "https://game-deals-alpha.vercel.app/ad/new-translation-728x90.html" in source
    assert "https://game-deals-alpha.vercel.app/ad/rewards-320x50.html" in source
    assert "https://yomusekai.com.br/ad/home-728x90.html" in source
    assert "https://yomusekai.com.br/ad/queue-468x60.html" in source
    assert "https://yomusekai.com.br/ad/translated-chapters-native.html" in source


def test_native_surface_is_lazy_and_ads_off_hides_without_bounds_request():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    js = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "_request_native_ad_surface" in source
    assert "_NATIVE_AD_PENDING_BOUNDS" in source
    assert "invalidateAdSession('ads_disabled')" in js
    assert "_NATIVE_AD_SURFACE = NativeAdSurface(window)" not in source.split("def run", 1)[1].split("webview.start", 1)[0]


def test_native_bounds_are_coalesced_and_have_a_circuit_breaker():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "_begininvoke_pending" in source
    assert "_bounds_coalesced" in source
    assert "AD_STORM_CIRCUIT_BREAKER" in source
    assert "len(self._native_bounds_events) > 500" in source
    assert "nativeDispatchTimer" in (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "now - lastNativeDispatchAt < 200" in (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")


def test_native_route_navigation_is_deduplicated_and_stateful_visibility_is_logged():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "_current_navigation_url" in source
    assert "PLACEMENT_NAV_START" in source
    assert "self._surface_visible" in source
    assert "if desired_visible and not self._surface_visible" in source


def test_native_route_commit_reapplies_visibility_after_hidden_transition():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    commit = source.split("def _on_navigation_completed", 1)[1].split("def _hide_surface_for_navigation_failure", 1)[0]
    assert "self._force_visibility_reapply = True" in commit
    assert "force_visibility_reapply=True" in commit
    assert "self.set_bounds(x, y, width, height, visible=True, force_visibility_reapply=True)" in commit


def test_native_bounds_dedup_does_not_skip_forced_visibility_restore():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "def set_bounds(self, x, y, width, height, visible=True, force_visibility_reapply=False)" in source
    assert "force_reapply = bool(force_visibility_reapply or self._force_visibility_reapply)" in source
    assert "if current == self._applied_bounds and not force_apply" in source
    assert "self._force_visibility_reapply = False" in source


def test_native_navigation_serializes_overlapping_requests_latest_wins():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    navigate = source.split("def _navigate_requested_on_ui_thread", 1)[1].split("def set_bounds", 1)[0]
    assert 'if self._navigation_in_progress:' in navigate
    assert 'NAV_LATEST_WINS' in navigate
    stale = source.split("if generation != self._navigation_generation:", 1)[1].split("expected = placement[1]", 1)[0]
    assert "self._navigation_in_progress = False" in stale
    assert "self._navigate_requested_on_ui_thread()" in stale


def test_content_diagnostic_logging_is_independent_from_native_poc_flag():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "def _content_diagnostic_enabled" in source
    assert "_env_flag(\"YOMU_ADS_NATIVE_POC\") or _content_diagnostic_enabled()" in source
    assert "AD_CONTENT_DIAGNOSTIC_PROBE" in source


def test_dom_snapshot_scheduler_uses_ui_timer_and_is_generation_safe():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "from System.Windows.Forms import Timer" in source
    assert "AD_SNAPSHOT_SEQUENCE_CREATED" in source
    assert "AD_SNAPSHOT_SEQUENCE_COMPLETE" in source
    assert "generation != self._navigation_generation" in source
    assert "from System.Threading import Timer" not in source
    assert ".Wait()" not in source
    assert ".Join()" not in source


def test_dom_snapshot_task_binding_is_nonblocking_and_normalizes_json():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "task.IsCompleted" in source
    assert "task.GetAwaiter().GetResult()" in source
    assert "AD_DOM_EXECUTE_SCRIPT_ERROR" in source
    assert "EXECUTE_SCRIPT_TIMEOUT" in source
    assert "PYTHONNET_RESULT_BINDING_ERROR" in source
    assert "json.loads(str(raw))" in source
    assert "task.ContinueWith" not in source
    assert "task.Result" not in source


def test_native_route_navigation_separates_requested_and_committed_state():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "self._requested_placement" in source
    assert "self._committed_placement" in source
    assert "self._active_navigation" in source
    assert "PLACEMENT_RESOLVED" in source
    assert "NAV_COMMIT_CURRENT" in source


def test_native_route_failure_invalidates_surface_and_hides_stale_creative():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "NAV_FAILURE_HIDE" in source
    assert "_hide_surface_for_navigation_failure" in source
    assert "self._committed_placement = None" in source
    assert "self._current_navigation_url = None" in source


def test_native_route_callbacks_are_generation_and_source_guarded():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "NAV_CALLBACK_STALE_IGNORED" in source
    assert "generation != self._navigation_generation" in source
    assert "is_allowed_ad_navigation(expected, source)" in source
    assert "NavigationStarting += self._on_navigation_starting" in source


def test_passive_ads_claim_requires_server_preference_and_visible_successful_surface():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    desktop = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "native_ad_session_state" in source
    assert "navigation_success" in source
    assert "surface_visible" in source
    assert "AD_SESSION_STATE" in source
    assert "DAILY_YK_CLAIM_TRIGGERED" in source
    assert "def native_ad_session_state" in desktop


def test_passive_ads_claim_is_deduplicated_and_off_invalidates_session():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "dailyClaimAttempted" in source
    assert "invalidateAdSession('ads_disabled')" in source
    assert "invalidateAdSession('route_changed')" in source


def test_daily_claim_ui_blocks_ads_off_and_exposes_cooldown_text():
    source = (ROOT / "static/control_plane_client.js").read_text(encoding="utf-8")
    assert "state.passiveAdsEnabled !== true" in source
    assert "passive_ads_disabled" in source
    assert "already_claimed" in source
    assert "nextDailyClaimText" in source
    assert "Disponível novamente em" in source


def test_yk_live_observability_is_opt_in_and_sanitized():
    source = (ROOT / "static/control_plane_client.js").read_text(encoding="utf-8")
    app = (ROOT / "app_ui.py").read_text(encoding="utf-8")
    assert "YK_DIAGNOSTIC_ENABLED" in source
    assert "YK_WALLET_SNAPSHOT" in source
    assert "YK_CLAIM_START" in source
    assert "YK_CLAIM_RESULT" in source
    assert "YK_HEADER_STATE" in source
    assert "claim_attempt_count" in source
    assert "__yomuYkDiagnostic" in app
    assert "access_token" not in source[source.index("function ykTrace"):source.index("function walletDiagnosticFields")]
    assert "refresh_token" not in source[source.index("function ykTrace"):source.index("function walletDiagnosticFields")]


def test_yk_observability_allowlist_contains_only_sanitized_state_fields():
    source = (ROOT / "static/control_plane_client.js").read_text(encoding="utf-8")
    app = (ROOT / "app_ui.py").read_text(encoding="utf-8")
    for field in ("amount_granted", "already_claimed", "daily_target", "daily_stored", "usable_now", "header_match",
                  "next_claim_at", "backend_next_available", "cooldown_match", "claim_attempt_count", "route", "session_generation"):
        assert field in app
        assert field in source
    yk_block = source[source.index("function ykTrace"):source.index("function walletDiagnosticFields")]
    for forbidden in ("access_token", "refresh_token", "jwt", "authorization", "cookie", "service_role", "api_key"):
        assert forbidden not in yk_block.lower()


def test_yk_plan_snapshot_fields_are_sanitized_and_backward_compatible():
    source = (ROOT / "static/control_plane_client.js").read_text(encoding="utf-8")
    migration = (ROOT / ".local-audit/yk-plan-diagnostic/20260916120000_wallet_summary_plan_diagnostic.sql").read_text(encoding="utf-8")
    for field in ("has_active_plan", "resolved_plan", "daily_yk_target"):
        assert field in source
        assert field in migration
    assert "active_plan_row" in migration
    assert "grant execute on function public.active_plan_row" not in migration
    assert "user_id" not in migration.split("return jsonb_build_object", 1)[1]


def test_native_navigation_is_marshaled_to_winforms_ui_thread():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "_navigate_requested_on_ui_thread" in source
    assert "BeginInvoke" in source
    assert "CoreWebView2.Navigate(placement[1])" in source


def test_native_route_map_preserves_all_five_canonical_urls():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    expected = (
        "https://yomusekai.com.br/ad/home-728x90.html",
        "https://game-deals-alpha.vercel.app/ad/new-translation-728x90.html",
        "https://yomusekai.com.br/ad/queue-468x60.html",
        "https://game-deals-alpha.vercel.app/ad/rewards-320x50.html",
        "https://yomusekai.com.br/ad/translated-chapters-native.html",
    )
    for url in expected:
        assert url in source
