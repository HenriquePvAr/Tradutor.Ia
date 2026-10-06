from types import SimpleNamespace

from deploy.vps import discord_observability as dispatcher


def test_formatter_has_no_secret_fields():
    text = dispatcher._content({"event_name": "JOB_COMPLETED", "job_id": "job", "webhook": "hidden", "pages": 1})
    assert "hidden" not in text
    assert "webhook" not in text.lower()


def test_job_started_and_progress_include_only_operational_summary():
    started = dispatcher._content({
        "event_name": "JOB_STARTED", "job_id": "job", "series": "series",
        "chapter": "chapter-1", "preset": "apenas_clean", "pages": 43,
        "translation_enabled": False, "started_at": 1791288000,
    })
    assert "apenas_clean" in started
    assert "Tradução\nFalse" in started
    assert "Início (UTC)" in started
    progress = dispatcher._content({
        "event_name": "REMOTE_PROGRESS", "job_id": "job", "percent": 50,
        "processed_pages": 21, "total_pages": 43,
    })
    assert "50%" in progress
    assert "21/43" in progress


def test_app_mounts_observability_routes():
    from research.gemini_vps_web_worker.app import app

    assert "/v1/observability/health" in app.openapi()["paths"]
    assert "/v1/observability/events" in app.openapi()["paths"]


def test_delivery_retries_429_with_bounded_retry_after(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return SimpleNamespace(status_code=429, headers={"Retry-After": "0"}, raise_for_status=lambda: None)
        return SimpleNamespace(status_code=204, headers={}, raise_for_status=lambda: None)

    monkeypatch.setattr(dispatcher.requests, "post", fake_post)
    monkeypatch.setattr(dispatcher.time, "sleep", lambda _: None)
    assert dispatcher._deliver("https://discord.example/webhook", "TEST") == 204
    assert len(calls) == 2


def test_ingest_reports_discord_http_status_without_secret(monkeypatch):
    import uuid
    from research.gemini_vps_web_worker import discord_observability as implementation

    monkeypatch.setattr(implementation, "_webhook", lambda: "https://example.test/webhook/secret")
    monkeypatch.setattr(implementation, "_deliver", lambda webhook, content: 204)
    response = implementation.ingest({"events": [{
        "schema_version": implementation.SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "event_name": "JOB_STARTED",
    }]})
    assert response == {
        "accepted": 1, "delivered": 1, "webhook_configured": True,
        "discord_http_status": 204,
    }
    assert "secret" not in str(response)
