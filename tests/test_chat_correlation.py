import pytest

from deploy.vps.chat_correlation import fresh_sources, require_fresh_source


def test_stale_blob_is_rejected():
    assert fresh_sources(("old-blob",), ["old-blob"]) == []
    with pytest.raises(RuntimeError, match="stale_output_rejected"):
        require_fresh_source(("old-blob",), ["old-blob"])


def test_new_blob_is_selected_without_reusing_old():
    assert require_fresh_source(("old-blob",), ["old-blob", "new-blob"]) == "new-blob"
