from __future__ import annotations

import json
from pathlib import Path


def test_public_runtime_config_contains_only_public_contract():
    payload = json.loads(Path("config/public-runtime.json").read_text(encoding="utf-8"))
    assert set(payload) == {"supabase_url", "publishable_key"}
    assert "secret" not in json.dumps(payload).casefold()
    assert payload["supabase_url"] == "https://ybxwjiycujminbwbdtlb.supabase.co"
    assert payload["publishable_key"] == "sb_publishable_eNFB__SJGMbnaQxjgD1FYw_hkaEb2Ts"
    assert "mimrsxnhqbqkffsekxuw" not in json.dumps(payload)


def test_release_checker_uses_public_publishable_key_contract():
    source = Path("tools/release/check_beta_configuration.py").read_text(encoding="utf-8")
    assert "SUPABASE_PUBLISHABLE_KEY" in source
    assert "SUPABASE_ANON_KEY" not in source
    assert "TRADUTOR_IA_UPDATE_MANIFEST_URL" in source


def test_jwks_diagnostic_uses_active_public_runtime_not_a_hardcoded_project():
    source = Path("yomu/auth/jwks_network_diagnostics.py").read_text(encoding="utf-8")
    assert "public-runtime.json" in source
    assert "getaddrinfo(host" in source
    assert "mimrsxnhqbqkffsekxuw" not in source
