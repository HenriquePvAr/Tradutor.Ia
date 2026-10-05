import inspect
from types import SimpleNamespace
from unittest import mock

from yomu.download.readers import scrapling_reader_resolver as resolver


class _Process:
    def __init__(self, *, exited=False):
        self.exited = exited
        self.terminated = False
        self.waited = False

    def poll(self):
        return 1 if self.exited else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True

    def kill(self):
        self.terminated = True


class _Page:
    def title(self):
        return "Comix"

    def evaluate(self, _script):
        return "owner-browser-ua"


class _Context:
    def __init__(self, cookies):
        self._cookies = cookies
        self.pages = [_Page()]

    def cookies(self, _url):
        return self._cookies


class _Cdp:
    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    def close(self):
        self.closed = True


class _PlaywrightContext:
    def __init__(self, cdp):
        self.chromium = SimpleNamespace(
            connect_over_cdp=lambda _url: cdp,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _run_capture(monkeypatch, tmp_path, *, cookies, process=None):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    process = process or _Process()
    cdp = _Cdp(_Context(cookies))
    monkeypatch.setattr(resolver, "_discover_cloak_binary", lambda: "cloak.exe")
    monkeypatch.setattr(resolver, "discover_system_browser", lambda: ("Chrome", "chrome.exe"))
    monkeypatch.setattr(resolver.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        resolver,
        "_comix_cf_cookie_path",
        lambda: tmp_path / "YomuSekai" / "comix_cf_cookie.json",
    )
    with mock.patch(
        "patchright.sync_api.sync_playwright",
        return_value=_PlaywrightContext(cdp),
    ):
        result = resolver.capture_cloudflare_clearance(["https://comix.to/title/1"])
    return result, process, cdp


def test_human_capture_uses_official_visible_browser_and_persistent_profile(monkeypatch, tmp_path):
    result, process, _cdp = _run_capture(
        monkeypatch,
        tmp_path,
        cookies=[{"name": "cf_clearance", "value": "secret-cookie"}],
    )

    assert result == 0
    assert process.terminated is True
    cache = tmp_path / "YomuSekai" / "comix_cf_cookie.json"
    assert cache.exists()
    assert "secret-cookie" in cache.read_text(encoding="utf-8")


def test_human_capture_does_not_use_cloak_or_hidden_window_flags(monkeypatch, tmp_path):
    process = _Process()
    cdp = _Cdp(_Context([{"name": "cf_clearance", "value": "secret-cookie"}]))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(resolver, "_discover_cloak_binary", lambda: "cloak.exe")
    monkeypatch.setattr(resolver, "discover_system_browser", lambda: ("Chrome", "chrome.exe"))
    monkeypatch.setattr(resolver, "_comix_cf_cookie_path", lambda: tmp_path / "cookie.json")
    with mock.patch.object(resolver.subprocess, "Popen", return_value=process) as popen, \
            mock.patch("patchright.sync_api.sync_playwright", return_value=_PlaywrightContext(cdp)):
        assert resolver.capture_cloudflare_clearance(["https://comix.to/"]) == 0
    command = popen.call_args.args[0]
    assert command[0] == "chrome.exe"
    assert "cloak.exe" not in command
    assert not any("window-position" in item or "start-minimized" in item for item in command)
    assert any(item.startswith("--user-data-dir=") and "comix_cdp_profile" in item for item in command)


def test_human_capture_reports_timeout_and_cleans_up(monkeypatch, tmp_path, capsys):
    process = _Process()
    monkeypatch.setattr(resolver.time, "monotonic", mock.Mock(side_effect=[0.0, 241.0]))
    result, process, _cdp = _run_capture(monkeypatch, tmp_path, cookies=[], process=process)

    assert result == 1
    assert process.terminated is True
    assert process.waited is True
    output = capsys.readouterr().out
    assert "reason=capture_timeout" in output
    assert "secret-cookie" not in output


def test_human_capture_reports_browser_exit_without_waiting(monkeypatch, tmp_path, capsys):
    process = _Process(exited=True)
    result, process, _cdp = _run_capture(monkeypatch, tmp_path, cookies=[], process=process)

    assert result == 1
    assert process.terminated is True
    output = capsys.readouterr().out
    assert "reason=browser_exited" in output


def test_human_capture_reports_launch_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(resolver, "_discover_cloak_binary", lambda: "cloak.exe")
    monkeypatch.setattr(resolver, "discover_system_browser", lambda: ("Chrome", "chrome.exe"))
    monkeypatch.setattr(resolver.subprocess, "Popen", mock.Mock(side_effect=OSError("launch")))

    assert resolver.capture_cloudflare_clearance(["https://comix.to/"]) == 1
    assert "reason=launch_failed" in capsys.readouterr().out


def test_automatic_stealth_path_retains_hidden_cloak_behavior():
    source = inspect.getsource(resolver._resolve_inline)
    assert "using_cloak" in source
    assert "--window-position=-32000,-32000" in source
    assert "--start-minimized" in source
