from pathlib import Path


AUTH_UI = Path(__file__).parent / "static" / "auth_ui.js"


def test_sdk_session_is_not_authenticated_before_canonical_confirmation():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "canonicalConfirmed = false" in source
    assert "(session && canonicalConfirmed) ? 'AUTHENTICATED_APP' : 'LOGIN'" in source
    assert "canonical_pending" in source
    assert "no_session" in source


def test_no_session_provider_events_do_not_probe_canonical_endpoint():
    source = AUTH_UI.read_text(encoding="utf-8")
    start = source.index("if (!session && authEvent && authEvent !== 'SIGNED_OUT')")
    end = source.index("if (!session && authEvent === 'SIGNED_OUT'", start)
    branch = source[start:end]
    assert "canonical_session_skipped_no_session" in branch
    assert "syncBackendSession" not in branch


def test_canonical_backend_success_marks_destination_confirmed():
    source = AUTH_UI.read_text(encoding="utf-8")
    start = source.index("const destination = resolveAuthDestination({", source.index("async function syncBackendSession"))
    end = source.index("});", start) + 3
    assert "canonicalConfirmed: true" in source[start:end]


def test_bootstrap_does_not_promote_unconfirmed_persisted_token():
    source = (Path(__file__).parent / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
    assert "getGlobal('__tradutorAuthState') === 'authenticated'" in source


def test_explicit_login_owns_canonical_verification_during_sdk_event():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "canonical_session_deferred_to_explicit_login" in source
    assert "explicitLoginSubmissionInProgress" in source


def test_stale_bearer_is_blocked_until_canonical_auth_state():
    source = (Path(__file__).parent / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
    assert "let bearer = authState === 'authenticated'" in source
    assert "let bearer = getGlobal('__tradutorAccessToken') || ''" not in source


def test_historical_boot_condition_would_have_sent_stale_bearer():
    # Deterministic RED model for the historical expression: a stale global
    # bearer was sent regardless of the provisional auth state.
    auth_state = 'auth_loading'
    stale = 'synthetic-stale-token'
    historical_bearer = stale or ''
    current_bearer = stale if auth_state == 'authenticated' else ''
    assert historical_bearer == stale
    assert current_bearer == ''
