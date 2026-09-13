import sqlite3
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from device_control import DeviceChallenges

def test_challenge_signature_and_replay():
    key=Ed25519PrivateKey.generate(); pub=key.public_key().public_bytes_raw(); c=DeviceChallenges(sqlite3.connect(":memory:")); n=c.issue(user_id="u",device_id="d"); sig=key.sign(n.encode())
    assert c.verify(nonce=n,user_id="u",device_id="d",public_key=pub,signature=sig)
    assert not c.verify(nonce=n,user_id="u",device_id="d",public_key=pub,signature=sig)
