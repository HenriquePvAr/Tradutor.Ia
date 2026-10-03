import hashlib
import hmac
import unittest

from ayet_rewarded import canonical_callback_payload, verify_callback


class AyetRewardedTests(unittest.TestCase):
    def setUp(self):
        self.params = {
            "transaction_id": "tx-1",
            "external_identifier": "ads-user-1",
            "amount": "1",
            "adslot_id": "16",
        }
        self.secret = "test-only-provider-secret"

    def signature(self):
        return hmac.new(self.secret.encode(), canonical_callback_payload(self.params).encode(), hashlib.sha256).hexdigest()

    def test_valid_callback(self):
        event = verify_callback(self.params, self.signature(), self.secret)
        self.assertEqual(event.transaction_id, "tx-1")
        self.assertEqual(event.amount, 1)

    def test_invalid_signature_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_reward_signature"):
            verify_callback(self.params, "bad", self.secret)

    def test_missing_transaction_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_reward_callback"):
            verify_callback({**self.params, "transaction_id": ""}, self.signature(), self.secret)

    def test_unconfigured_provider_rejected(self):
        with self.assertRaisesRegex(ValueError, "provider_not_configured"):
            verify_callback(self.params, self.signature(), "")

