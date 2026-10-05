from unittest import mock

import pytest

from yomu.download import down
from yomu.download.sources.chapter_source import SourceError


COMIX = "https://comix.to/title/example/1-chapter-1"


def test_capture_retry_fires_once_for_comix_when_ui_enabled():
    calls = {"count": 0}

    def once(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise SourceError("dynamic_source_unavailable", "no_reader_images")
        return "ANALYSIS"

    with mock.patch.object(down, "_discover_chapter_source_once", side_effect=once), \
            mock.patch.object(down, "_comix_cf_autocapture_enabled", return_value=True), \
            mock.patch.object(down, "_run_comix_clearance_capture", return_value=True) as capture:
        assert down.discover_chapter_source(COMIX) == "ANALYSIS"
    assert calls["count"] == 2
    capture.assert_called_once()


def test_capture_is_disabled_for_headless_worker():
    with mock.patch.object(down, "_discover_chapter_source_once",
                           side_effect=SourceError("dynamic_source_unavailable", "reader")), \
            mock.patch.object(down, "_comix_cf_autocapture_enabled", return_value=False), \
            mock.patch.object(down, "_run_comix_clearance_capture") as capture:
        with pytest.raises(SourceError):
            down.discover_chapter_source(COMIX)
    capture.assert_not_called()


def test_non_comix_source_never_opens_clearance_capture():
    with mock.patch.object(down, "_discover_chapter_source_once",
                           side_effect=SourceError("dynamic_source_unavailable", "reader")), \
            mock.patch.object(down, "_comix_cf_autocapture_enabled", return_value=True), \
            mock.patch.object(down, "_run_comix_clearance_capture") as capture:
        with pytest.raises(SourceError):
            down.discover_chapter_source("https://example.test/chapter/1")
    capture.assert_not_called()


def test_clearance_capture_url_host_is_strict():
    assert down._is_comix_url(COMIX)
    assert down._is_comix_url("https://www.comix.to/title/example/1-chapter-1")
    assert not down._is_comix_url("https://comix.to.evil.example/chapter/1")
