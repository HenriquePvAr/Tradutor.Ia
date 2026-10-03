import json

import pytest

from chapter_source import (
    COMIX,
    ComixAdapter,
    SourceError,
    SUPPORTED_SPECIFIC_ADAPTER,
    select_adapter,
)


def _html(payload):
    data = {"read": {"chapterId": 42}, "chapterPayload": payload}
    return '<script id="initial-data" type="application/json">' + json.dumps(data) + '</script>'


def _payload():
    return {"pages": {"baseUrl": "https://static.comix.to/c/", "items": [
        {"url": "page-001", "index": 1, "width": 1000, "height": 1400},
        {"url": "page-002", "index": 2, "width": 1000, "height": 1400},
        {"url": "page-010", "index": 10, "width": 1000, "height": 1400},
    ]}}


def test_comix_hostname_and_spoof_rejection():
    adapter = ComixAdapter()
    assert adapter.supports("https://comix.to/title/home/123-chapter-1")
    assert adapter.supports("https://www.comix.to/title/home/123-chapter-1")
    assert not adapter.supports("https://comix.to.evil.example/title/home/123-chapter-1")
    assert not adapter.supports("https://evilcomix.to/title/home/123-chapter-1")
    assert not adapter.supports("https://example.com/?target=https://comix.to")


def test_comix_url_classification_and_validation():
    adapter = ComixAdapter()
    assert adapter.classify_url("https://comix.to/title/home/1536897-chapter-1") == "CHAPTER"
    assert adapter.classify_url("https://comix.to/title/home") == "SERIES"
    assert adapter.classify_url("https://comix.to/browser") == "UNKNOWN"
    with pytest.raises(SourceError, match="series_url_requires_chapter"):
        adapter.validate_path("https://comix.to/title/home")


def test_comix_fixture_extracts_metadata_shape_and_explicit_order():
    adapter = ComixAdapter()
    candidates = adapter.collect_dom_candidates_from_html(_html(_payload()), "https://comix.to/title/home/42-chapter-1")
    assert candidates and len(candidates) == 3
    assert [item["url"].rsplit("/", 1)[-1] for item in candidates] == ["page-001", "page-002", "page-010"]
    assert [item["order"] for item in candidates] == [0, 1, 2]


def test_comix_empty_fixture_is_not_a_valid_chapter():
    adapter = ComixAdapter()
    assert adapter.collect_dom_candidates_from_html(_html({"pages": {"items": []}}), "https://comix.to/title/x/1-chapter-1") is None


def test_comix_routes_only_to_comix_adapter():
    selected = select_adapter("https://comix.to/title/home/1536897-chapter-1")
    assert selected.name == "comix"
    assert select_adapter("https://webtoons.com/en/episode/1") .name == "webtoons"
