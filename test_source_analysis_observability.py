from app_ui import _safe_source_error_detail, _source_analysis_observability


def test_source_analysis_observability_is_bounded_and_path_free() -> None:
    payload = {
        "source_type": "url",
        "url": "https://www.webtoons.com/en/example/chapter?token=secret",
        "ordered_media_ids": ["opaque-1", "opaque-2"],
        "local_folder": r"C:\Users\private\chapter",
        "download_only": True,
        "full": False,
        "max_images": 10,
        "pipeline_intent": {"scope": "10"},
    }
    observed = _source_analysis_observability(payload)
    assert observed == {
        "source_type": "url",
        "has_url": True,
        "url_host": "www.webtoons.com",
        "local_media_count": 2,
        "has_local_folder": True,
        "page_manager_count": 2,
        "download_only": True,
        "scope_kind": "10",
        "requested_page_count": 10,
    }
    rendered = repr(observed)
    assert "C:\\Users" not in rendered
    assert "token=secret" not in rendered


def test_source_analysis_observability_full_scope_has_no_page_count() -> None:
    observed = _source_analysis_observability({
        "source_type": "url", "url": "https://webtoons.com/chapter", "full": True,
        "pipeline_intent": {"scope": "full"},
    })
    assert observed["scope_kind"] == "full"
    assert observed["requested_page_count"] is None


def test_source_error_detail_accepts_only_branch_token() -> None:
    assert _safe_source_error_detail("url_and_folder") == "url_and_folder"
    assert _safe_source_error_detail("C:\\secret\\token") == "redacted"
    assert _safe_source_error_detail("invalid_request: secret") == "redacted"
