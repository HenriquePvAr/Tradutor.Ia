import sqlite3
from progression_service import ProgressionLedger, rank_for_xp

def test_rank_thresholds_and_idempotency():
    assert rank_for_xp(0) == "Leitor Iniciante"
    assert rank_for_xp(5000) == "Lenda do Sekai"
    ledger = ProgressionLedger(sqlite3.connect(":memory:"))
    assert ledger.award_once(user_id="u", event_id="job:1", xp=50, source="page")
    assert not ledger.award_once(user_id="u", event_id="job:1", xp=50, source="page")
    assert ledger.get("u").xp == 50
