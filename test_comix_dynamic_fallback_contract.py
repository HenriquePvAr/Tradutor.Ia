from types import SimpleNamespace
from unittest import mock

import pytest

import down
from chapter_source import SourceError, select_adapter
from scrapling_reader_resolver import DynamicReaderError


URL = "https://comix.to/title/k72ge-home/1536897-chapter-1"


@pytest.mark.parametrize(
    "provider,reason,status,expected",
    [
        ("comix", "challenge_required", 200, True),
        ("comix", "source_access_denied", 403, True),
        ("comix", "source_unavailable", 522, True),
        ("webtoons", "challenge_required", 200, False),
        ("comix", "source_unavailable", 500, False),
    ],
)
def test_dynamic_fallback_policy_is_centralized(provider, reason, status, expected):
    assert down.should_use_dynamic_resolver(provider, reason, status) is expected


def test_challenge_telemetry_records_decision_start_and_end_once():
    direct = SourceError("challenge_required", "navigation_preflight")
    direct.preflight_result = {"adapter": "comix", "http_status": 200,
                               "reason_code": "challenge_required"}
    events = []
    with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
         mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", return_value=_analysis()) as resolve:
        down.discover_chapter_source(
            URL, diagnostic_callback=lambda event, **fields: events.append((event, fields)))
    resolve.assert_called_once()
    assert [event for event, _ in events] == [
        "SOURCE_FALLBACK_DECISION", "DYNAMIC_RESOLVER_START", "DYNAMIC_RESOLVER_END"]
    assert events[0][1]["fallback_allowed"] is True
    assert events[1][1]["result"] == "started"
    assert events[2][1]["result"] == "pass"


def test_budget_failure_is_only_reported_after_resolver_started():
    direct = SourceError("challenge_required", "navigation_preflight")
    direct.preflight_result = {"adapter": "comix", "reason_code": "challenge_required"}
    events = []
    budget = DynamicReaderError("canonical_materialization_retry_budget_insufficient")
    with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
         mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", side_effect=budget) as resolve:
        with pytest.raises(SourceError) as caught:
            down.discover_chapter_source(
                URL, diagnostic_callback=lambda event, **fields: events.append((event, fields)))
    resolve.assert_called_once()
    assert caught.value.code == "dynamic_source_unavailable"
    assert caught.value.detail == "canonical_materialization_retry_budget_insufficient"
    assert events[-1][1]["result"] == "budget_failure"


def _analysis():
    return SimpleNamespace(outcome="supported_specific_adapter", accepted=[
        {"id": "p1", "url": "https://cdn.example/1.webp", "order": 0},
        {"id": "p2", "url": "https://cdn.example/2.webp", "order": 1},
    ])


@pytest.mark.parametrize("status", [401, 403])
def test_comix_reader_api_status_retries_existing_dynamic_fallback(status):
    adapter = select_adapter(URL)
    direct = SourceError("source_access_denied", f"reader_api_status_{status}")
    with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
         mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", return_value=_analysis()) as resolve:
        result = down.discover_chapter_source(URL)
    assert result is not None
    assert len(result.accepted) == 2
    resolve.assert_called_once()
    assert adapter.name == "comix"


def test_comix_real_dynamic_access_denial_remains_controlled():
    direct = SourceError("source_access_denied", "reader_api_status_403")
    denied = DynamicReaderError("challenge_required")
    with mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", side_effect=denied):
        with pytest.raises(SourceError, match="source_access_denied"):
            down.discover_chapter_source(URL)


def test_comix_522_continues_to_bounded_specialized_fallback():
    direct = SourceError("source_unavailable", "navigation_preflight_http")
    direct.preflight_result = {
        "adapter": "comix",
        "http_status": 522,
        "reason_code": "source_unavailable",
        "retry_count": 1,
        "classification": "transient_browser_fallback",
    }
    with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
         mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", return_value=_analysis()) as resolve:
        result = down.discover_chapter_source(URL)
    assert len(result.accepted) == 2
    resolve.assert_called_once()


def test_comix_522_specialized_failure_is_controlled_without_retry_loop():
    direct = SourceError("source_unavailable", "navigation_preflight_http")
    direct.preflight_result = {"adapter": "comix", "http_status": 522}
    unavailable = DynamicReaderError("browser_unavailable")
    with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
         mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", side_effect=unavailable) as resolve:
        with pytest.raises(SourceError) as caught:
            down.discover_chapter_source(URL)
    assert caught.value.code == "dynamic_source_unavailable"
    resolve.assert_called_once()


def test_comix_preflight_challenge_uses_dynamic_browser_fallback():
    direct = SourceError("challenge_required", "navigation_preflight")
    direct.preflight_result = {
        "adapter": "comix", "reason_code": "challenge_required",
        "captcha_detected": True, "security_blocked": True,
    }
    with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
         mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", return_value=_analysis()) as resolve:
        result = down.discover_chapter_source(URL)
    assert result is not None
    assert len(result.accepted) == 2
    resolve.assert_called_once()


def test_comix_preflight_challenge_resolver_failure_is_terminal_and_no_bypass():
    direct = SourceError("challenge_required", "navigation_preflight")
    direct.preflight_result = {"adapter": "comix", "reason_code": "challenge_required"}
    challenge = DynamicReaderError("challenge_required")
    with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
         mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", side_effect=challenge) as resolve:
        with pytest.raises(SourceError) as caught:
            down.discover_chapter_source(URL)
    assert caught.value.code == "source_access_denied"
    assert caught.value.detail == "dynamic_reader_challenge_required"
    resolve.assert_called_once()


def test_comix_dynamic_browser_unavailable_is_not_false_access_denied():
    direct = SourceError("source_access_denied", "reader_api_status_403")
    unavailable = DynamicReaderError("browser_unavailable")
    with mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve", side_effect=unavailable):
        with pytest.raises(SourceError) as caught:
            down.discover_chapter_source(URL)
    assert caught.value.code == "dynamic_source_unavailable"


def test_comix_non_reader_access_denial_does_not_trigger_dynamic_fallback():
    direct = SourceError("source_access_denied", "account_permission_denied")
    with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
         mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve") as resolve:
        with pytest.raises(SourceError) as caught:
            down.discover_chapter_source(URL)
    assert caught.value is direct
    resolve.assert_not_called()


def test_optional_resolver_import_failure_is_controlled_not_name_error():
    direct = SourceError("source_access_denied", "reader_api_status_403")
    import_failure = ImportError("scrapling capability unavailable")
    with mock.patch.object(down, "analyze_chapter_source", side_effect=direct), \
         mock.patch("scrapling_reader_resolver.resolve",
                    side_effect=DynamicReaderError("capability_unavailable")), \
         mock.patch.object(down, "_load_dynamic_reader_resolver",
                           return_value=(None, None, import_failure)):
        with pytest.raises(SourceError) as caught:
            down.discover_chapter_source(URL)
    assert caught.value.code == "dynamic_source_unavailable"
    assert caught.value.detail == "ImportError"


def test_dynamic_reader_error_contract_is_defined_when_resolver_available():
    error_type, resolver, import_failure = down._load_dynamic_reader_resolver()
    assert import_failure is None
    assert error_type is not None
    assert error_type.__name__ == "DynamicReaderError"
    assert callable(resolver)
