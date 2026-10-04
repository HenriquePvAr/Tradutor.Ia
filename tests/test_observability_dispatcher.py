from types import SimpleNamespace

from deploy.vps import discord_observability as dispatcher


def test_formatter_has_no_secret_fields():
    text = dispatcher._content({"event_name": "JOB_COMPLETED", "job_id": "job", "webhook": "hidden", "pages": 1})
    assert "hidden" not in text
    assert "webhook" not in text.lower()


def test_delivery_retries_429_with_bounded_retry_after(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return SimpleNamespace(status_code=429, headers={"Retry-After": "0"}, raise_for_status=lambda: None)
        return SimpleNamespace(status_code=204, headers={}, raise_for_status=lambda: None)

    monkeypatch.setattr(dispatcher.requests, "post", fake_post)
    monkeypatch.setattr(dispatcher.time, "sleep", lambda _: None)
    dispatcher._deliver("https://discord.example/webhook", "TEST")
    assert len(calls) == 2
