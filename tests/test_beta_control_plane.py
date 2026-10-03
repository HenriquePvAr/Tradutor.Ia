import sqlite3, time
from beta_control_plane import ControlPlane

def test_flags_invites_and_sanitized_error():
    cp = ControlPlane(sqlite3.connect(":memory:", check_same_thread=False))
    cp.set_flag("translation_enabled", False); assert not cp.flag("translation_enabled")
    code = cp.create_invite(created_by="dev", max_uses=1, days=30)
    assert cp.redeem_invite(user_id="u", code=code, request_id="r1") == 30
    try: cp.redeem_invite(user_id="u2", code=code, request_id="r2")
    except ValueError: pass
    else: assert False
    cp.record_error(report_id="YS-ERR-1", user_id="u", code="provider_timeout", stage="download")
