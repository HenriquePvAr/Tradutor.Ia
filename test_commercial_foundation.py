import sqlite3
import unittest
from datetime import datetime, timezone, timedelta

from commercial_foundation import DisabledBillingProvider, DisabledRewardedAdProvider, YKWallet


class CommercialFoundationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.wallet = YKWallet(self.db)
        self.now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
        self.user = "user-1"

    def tearDown(self):
        self.db.close()

    def credit(self, amount, bucket="permanent"):
        self.db.execute("INSERT INTO yk_ledger VALUES (?,?,?,?,?,?,?,?,?)", (str(amount) + bucket, self.user, amount, bucket, "test", None, None, self.now.isoformat(), "{}"))
        self.db.commit()

    def test_daily_completes_to_five_without_accumulating(self):
        for permanent, expected in ((0, 5), (1, 4), (3, 2), (4, 1), (5, 0), (100, 0)):
            db = sqlite3.connect(":memory:")
            wallet = YKWallet(db)
            if permanent:
                db.execute("INSERT INTO yk_ledger VALUES (?,?,?,?,?,?,?,?,?)", ("p", self.user, permanent, "permanent", "test", None, None, self.now.isoformat(), "{}"))
                db.commit()
            self.assertEqual(wallet.claim_daily(self.user, now=self.now), expected)
            self.assertEqual(wallet.claim_daily(self.user, now=self.now), 0)
            db.close()

    def test_spend_is_idempotent_and_uses_expiring_buckets_first(self):
        self.credit(3, "permanent")
        self.credit(2, "daily")
        reservation = self.wallet.reserve_translation(self.user, "job-1", now=self.now)
        self.wallet.consume_reservation(reservation["id"], now=self.now)
        self.assertEqual(self.wallet.balances(self.user, now=self.now).available, 4)
        self.assertEqual(self.wallet.reserve_translation(self.user, "job-1", now=self.now)["id"], reservation["id"])

    def test_daily_expires_at_utc_reset(self):
        self.wallet.claim_daily(self.user, now=self.now)
        tomorrow = self.now + timedelta(days=1, minutes=1)
        self.assertEqual(self.wallet.balances(self.user, now=tomorrow).daily, 0)

    def test_real_providers_are_disabled_until_configured(self):
        with self.assertRaisesRegex(RuntimeError, "not_configured"):
            DisabledBillingProvider().create_checkout(user_id=self.user, plan_slug="pro_monthly")
        with self.assertRaisesRegex(RuntimeError, "not_configured"):
            DisabledRewardedAdProvider().verify_reward({})

