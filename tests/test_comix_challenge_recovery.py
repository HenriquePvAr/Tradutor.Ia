from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_comix_session_round_trip_is_atomic_and_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from yomu.download.comix_session import load_comix_session, save_comix_session, session_path

    save_comix_session("clearance-value", "UA/test")
    assert load_comix_session() == ("clearance-value", "UA/test")
    assert not list(tmp_path.rglob("*.tmp"))
    assert set(json.loads(session_path().read_text(encoding="utf-8"))) == {
        "cf_clearance", "user_agent"
    }


def test_comix_transport_uses_persisted_cookie_and_user_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from yomu.download.comix_session import save_comix_session
    from yomu.download.download_transport import RequestsTransport

    save_comix_session("cookie", "UA/canonical")
    adapter = SimpleNamespace(name="comix")
    transport = RequestsTransport(adapter)
    assert transport._headers("")["User-Agent"] == "UA/canonical"
    assert transport._session.cookies.get("cf_clearance") == "cookie"


def test_worker_reuse_guard_recognizes_comix_from_source_url(monkeypatch):
    from yomu.runtime import worker_service

    service = object.__new__(worker_service.Worker)
    service.store = SimpleNamespace(db_path=":memory:")
    service._append_job_log = lambda *args: None
    class Result:
        status = "source_analysis_ready"
        created_at = __import__("time").time()
        normalized_url_hash = __import__("hashlib").sha256(
            b"https://comix.to/title/1"
        ).hexdigest()
        resolved_selection = ("page-1",)
    class Store:
        def __init__(self, *_): pass
        def get_analysis(self, *_): return Result()
        def close(self): pass
    monkeypatch.setattr(worker_service, "SOURCE_ANALYSIS_REUSE_TTL_SECONDS", 900)
    monkeypatch.setattr("yomu.review.source_readiness.SourceReadinessStore", Store)
    job = {
        "id": "job",
        "source_url": "https://comix.to/title/1",
        "configuration": {"source_analysis_result_id": "analysis"},
        "source_analysis_json": json.dumps({"accepted": [], "source_type": "chapter"}),
        "source_selection_json": json.dumps({"candidate_ids": ["page-1"]}),
    }
    assert service._reuse_persisted_source_analysis(job) is None


def test_navigation_recovery_is_bounded_and_rechecks(monkeypatch):
    import yomu.download.down as down
    from yomu.download.download_transport import _preflight_result
    from yomu.download.sources.chapter_source import SourceError

    adapter = SimpleNamespace(
        name="comix", capabilities=SimpleNamespace(public=lambda: {}),
        validate_navigation_url=lambda _url: None,
        validate_path=lambda _url: None,
    )
    states = iter([
        _preflight_result(adapter, "https://comix.to/title/1", status="source_captcha_detected",
                          reason_code="challenge_required", started=0),
        _preflight_result(adapter, "https://comix.to/title/1", status="preflight_ready",
                          reason_code="preflight_ready", started=0),
    ])
    calls = []
    monkeypatch.setattr(down, "inspect_source_preflight", lambda *a, **k: next(states))
    monkeypatch.setattr(down, "_run_comix_clearance_capture", lambda *a, **k: calls.append(1) or True)
    monkeypatch.setattr(down, "invalidate_comix_session", lambda: None, raising=False)
    assert down._comix_navigation_with_recovery(adapter, "https://comix.to/title/1") == \
        "https://comix.to/title/1"
    assert calls == [1]


def test_non_comix_error_does_not_enter_recovery(monkeypatch):
    import yomu.download.down as down
    from yomu.download.sources.chapter_source import SourceError

    adapter = SimpleNamespace(
        name="other",
        validate_navigation_url=lambda _url: None,
        validate_path=lambda _url: None,
        capabilities=SimpleNamespace(public=lambda: {}),
    )
    monkeypatch.setattr(
        down, "inspect_source_preflight",
        lambda *a, **k: (_ for _ in ()).throw(SourceError("challenge_required", "x")),
    )
    monkeypatch.setattr(down, "_run_comix_clearance_capture", pytest.fail)
    with pytest.raises(SourceError):
        down._comix_navigation_with_recovery(adapter, "https://other.test/chapter")
