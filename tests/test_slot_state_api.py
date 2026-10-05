from deploy.vps.slot_state_api import snapshot


def test_slot_state_api_reports_two_slots_without_chat_url(tmp_path, monkeypatch):
    monkeypatch.setattr("deploy.vps.slot_state_api.urllib.request.urlopen", lambda *a, **k: _Pages())
    result = snapshot(tmp_path)
    assert result["slot_count"] == 2
    assert result["max_concurrency"] == 2
    assert result["queue_depth"] == 0
    assert all(slot["chat_url"] == "" for slot in result["slots"].values())
    assert all("chat_id" not in slot for slot in result["slots"].values())


class _Pages:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter([])

    def read(self):
        return b"[]"
