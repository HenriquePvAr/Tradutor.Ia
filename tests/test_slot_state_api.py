from deploy.vps.slot_state_api import snapshot


def test_slot_state_api_reports_one_slot_without_chat_url(tmp_path, monkeypatch):
    monkeypatch.setattr("deploy.vps.slot_state_api.urllib.request.urlopen", lambda *a, **k: _Pages())
    result = snapshot(tmp_path)
    assert result["slot_count"] == 1
    assert result["max_concurrency"] == 1
    assert result["queue_depth"] == 0
    assert "chat_url" not in result


class _Pages:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter([])

    def read(self):
        return b"[]"
