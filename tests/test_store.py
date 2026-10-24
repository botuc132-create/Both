import os
import tempfile
import unittest
from database import Store, KEY_COST, REWARD_TOKENS

class FakeUser:
    def __init__(self, uid, username=None, first_name="Test"):
        self.id=uid; self.username=username; self.first_name=first_name

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=os.path.join(self.tmp.name,"test.db")
        self.s=Store(self.db)
        self.s.upsert_user(FakeUser(1))
        self.s.upsert_user(FakeUser(2))
        self.s.upsert_user(FakeUser(3))

    def tearDown(self):
        self.tmp.cleanup()

    def test_referral_reward_once(self):
        self.assertTrue(self.s.reward_referral(1,2))
        self.assertFalse(self.s.reward_referral(1,2))
        self.assertEqual(self.s.balance(1),REWARD_TOKENS)
        with self.s.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0],1)

    def test_referral_reward_only_after_verification(self):
        self.s.set_pending_inviter(2, 1)
        self.assertEqual(self.s.balance(1), 0)
        with self.s.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0], 0)
        verified, inviter = self.s.verify_user_and_reward(2)
        self.assertTrue(verified)
        self.assertEqual(inviter, 1)
        self.assertEqual(self.s.balance(1), REWARD_TOKENS)
        verified_again, inviter_again = self.s.verify_user_and_reward(2)
        self.assertFalse(verified_again)
        self.assertIsNone(inviter_again)
        self.assertEqual(self.s.balance(1), REWARD_TOKENS)

    def test_no_self_referral(self):
        self.assertFalse(self.s.reward_referral(1,1))
        self.assertEqual(self.s.balance(1),0)

    def test_insufficient_tokens_cannot_claim(self):
        self.s.add_keys(["KEY-A"],99)
        ok,status,key=self.s.claim_key(1)
        self.assertFalse(ok)
        self.assertEqual(status,"INSUFFICIENT")
        self.assertEqual(self.s.balance(1),0)

    def test_duplicate_key_lines_are_separate_records(self):
        self.assertEqual(self.s.add_keys(["SAME","SAME","SAME"],99),3)
        with self.s.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM keys WHERE key_text='SAME'").fetchone()[0],3)

    def test_key_record_only_claimed_once(self):
        self.s.add_tokens(1,30,"test")
        self.s.add_tokens(2,30,"test")
        self.s.add_keys(["ONE"],99)
        self.assertTrue(self.s.claim_key(1)[0])
        result=self.s.claim_key(2)
        self.assertFalse(result[0])
        self.assertEqual(result[1],"EMPTY")
        with self.s.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM key_claims").fetchone()[0],1)

    def test_balance_never_negative(self):
        with self.assertRaises(ValueError):
            self.s.add_tokens(1,-1,"test")
        self.assertEqual(self.s.balance(1),0)

    def test_data_persists_after_restart(self):
        self.s.add_tokens(1,40,"test")
        self.s.add_keys(["PERSIST"],99)
        s2=Store(self.db)
        self.assertEqual(s2.balance(1),40)
        self.assertEqual(s2.key_stats()["available"],1)

if __name__ == "__main__":
    unittest.main()
