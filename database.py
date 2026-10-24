import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

REWARD_TOKENS = 10
KEY_COST = 30
DB_PATH = os.getenv("DB_PATH", "bot.db")

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class Store:
    def __init__(self, path=DB_PATH):
        self.path = path
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
        finally:
            con.close()

    def init_db(self):
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users(
              user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
              token_balance INTEGER NOT NULL DEFAULT 0 CHECK(token_balance >= 0),
              started_at TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS referrals(
              id INTEGER PRIMARY KEY AUTOINCREMENT, inviter_id INTEGER NOT NULL,
              invitee_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL,
              CHECK(inviter_id != invitee_id)
            );
            CREATE TABLE IF NOT EXISTS referral_rewards(
              id INTEGER PRIMARY KEY AUTOINCREMENT, inviter_id INTEGER NOT NULL,
              invitee_id INTEGER NOT NULL UNIQUE, amount INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS token_transactions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
              amount INTEGER NOT NULL, balance_after INTEGER NOT NULL,
              reason TEXT NOT NULL, actor_id INTEGER, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS keys(
              id INTEGER PRIMARY KEY AUTOINCREMENT, key_text TEXT NOT NULL,
              added_by INTEGER, added_at TEXT NOT NULL, claimed_by INTEGER,
              claimed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_keys_available ON keys(claimed_at, id);
            CREATE TABLE IF NOT EXISTS key_claims(
              id INTEGER PRIMARY KEY AUTOINCREMENT, key_id INTEGER NOT NULL UNIQUE,
              user_id INTEGER NOT NULL, cost INTEGER NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(key_id) REFERENCES keys(id)
            );
            CREATE TABLE IF NOT EXISTS required_chats(
              chat_id TEXT PRIMARY KEY, title TEXT NOT NULL, invite_url TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, added_by INTEGER, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broadcast_logs(
              id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
              target TEXT NOT NULL, message TEXT NOT NULL, success_count INTEGER NOT NULL,
              failure_count INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_audit_logs(
              id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
              action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """)

    def upsert_user(self, user):
        ts = now_iso()
        with self.connect() as c:
            c.execute("""INSERT INTO users(user_id,username,first_name,started_at,last_seen)
                VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username, first_name=excluded.first_name,last_seen=excluded.last_seen""",
                (user.id, user.username, user.first_name or "", ts, ts))

    def get_user(self, user_id):
        with self.connect() as c:
            return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def users_count(self):
        with self.connect() as c:
            return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]

    def balance(self, user_id):
        row = self.get_user(user_id)
        return int(row["token_balance"]) if row else 0

    def add_tokens(self, user_id, amount, reason, actor_id=None):
        if amount == 0:
            raise ValueError("Amount must not be zero")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT token_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                c.execute("ROLLBACK")
                raise ValueError("User has not started the bot")
            new_balance = row["token_balance"] + amount
            if new_balance < 0:
                c.execute("ROLLBACK")
                raise ValueError("Insufficient Token; balance cannot be negative")
            c.execute("UPDATE users SET token_balance=? WHERE user_id=?", (new_balance, user_id))
            c.execute("INSERT INTO token_transactions(user_id,amount,balance_after,reason,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                      (user_id, amount, new_balance, reason, actor_id, now_iso()))
            c.execute("COMMIT")
            return new_balance

    def reward_referral(self, inviter_id, invitee_id):
        if inviter_id == invitee_id:
            return False
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM users WHERE user_id=?", (inviter_id,)).fetchone():
                c.execute("ROLLBACK"); return False
            if not c.execute("SELECT 1 FROM users WHERE user_id=?", (invitee_id,)).fetchone():
                c.execute("ROLLBACK"); return False
            try:
                c.execute("INSERT INTO referrals(inviter_id,invitee_id,created_at) VALUES(?,?,?)",
                          (inviter_id, invitee_id, now_iso()))
                row = c.execute("SELECT token_balance FROM users WHERE user_id=?", (inviter_id,)).fetchone()
                balance = row["token_balance"] + REWARD_TOKENS
                c.execute("UPDATE users SET token_balance=? WHERE user_id=?", (balance, inviter_id))
                c.execute("INSERT INTO referral_rewards(inviter_id,invitee_id,amount,created_at) VALUES(?,?,?,?)",
                          (inviter_id, invitee_id, REWARD_TOKENS, now_iso()))
                c.execute("INSERT INTO token_transactions(user_id,amount,balance_after,reason,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                          (inviter_id, REWARD_TOKENS, balance, f"Referral reward for {invitee_id}", invitee_id, now_iso()))
                c.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                c.execute("ROLLBACK")
                return False

    def add_keys(self, lines, admin_id):
        clean = [line.strip() for line in lines if line.strip()]
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.executemany("INSERT INTO keys(key_text,added_by,added_at) VALUES(?,?,?)",
                          [(line, admin_id, now_iso()) for line in clean])
            c.execute("COMMIT")
        return len(clean)

    def key_stats(self):
        with self.connect() as c:
            row = c.execute("""SELECT COUNT(*) total,
              SUM(CASE WHEN claimed_at IS NULL THEN 1 ELSE 0 END) available,
              SUM(CASE WHEN claimed_at IS NOT NULL THEN 1 ELSE 0 END) claimed FROM keys""").fetchone()
            return {k: int(row[k] or 0) for k in ("total", "available", "claimed")}

    def claim_key(self, user_id):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            user = c.execute("SELECT token_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not user:
                c.execute("ROLLBACK"); return (False, "USER_NOT_FOUND", None)
            if user["token_balance"] < KEY_COST:
                c.execute("ROLLBACK"); return (False, "INSUFFICIENT", None)
            key = c.execute("SELECT id,key_text FROM keys WHERE claimed_at IS NULL ORDER BY id LIMIT 1").fetchone()
            if not key:
                c.execute("ROLLBACK"); return (False, "EMPTY", None)
            ts = now_iso()
            # BEGIN IMMEDIATE serializes claimers; update, debit, and claim record are atomic.
            c.execute("UPDATE keys SET claimed_by=?,claimed_at=? WHERE id=? AND claimed_at IS NULL",
                      (user_id, ts, key["id"]))
            if c.execute("SELECT changes()").fetchone()[0] != 1:
                c.execute("ROLLBACK"); return (False, "RACE", None)
            balance = user["token_balance"] - KEY_COST
            c.execute("UPDATE users SET token_balance=? WHERE user_id=?", (balance, user_id))
            c.execute("INSERT INTO key_claims(key_id,user_id,cost,created_at) VALUES(?,?,?,?)",
                      (key["id"], user_id, KEY_COST, ts))
            c.execute("INSERT INTO token_transactions(user_id,amount,balance_after,reason,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                      (user_id, -KEY_COST, balance, "Claim key", None, ts))
            c.execute("COMMIT")
            return (True, "OK", key["key_text"])

    def user_details(self, user_id):
        with self.connect() as c:
            u = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not u: return None
            referrals = c.execute("SELECT COUNT(*) n FROM referrals WHERE inviter_id=?", (user_id,)).fetchone()["n"]
            claims = c.execute("SELECT k.key_text,k.claimed_at FROM key_claims kc JOIN keys k ON k.id=kc.key_id WHERE kc.user_id=? ORDER BY kc.id DESC LIMIT 10", (user_id,)).fetchall()
            txs = c.execute("SELECT amount,reason,balance_after,created_at FROM token_transactions WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)).fetchall()
            return u, referrals, claims, txs

    def add_required_chat(self, chat_id, title, invite_url, admin_id):
        with self.connect() as c:
            c.execute("""INSERT INTO required_chats(chat_id,title,invite_url,enabled,added_by,created_at)
              VALUES(?,?,?,1,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
              invite_url=excluded.invite_url,enabled=1,added_by=excluded.added_by""",
              (str(chat_id), title, invite_url, admin_id, now_iso()))

    def required_chats(self, enabled_only=True):
        with self.connect() as c:
            q = "SELECT * FROM required_chats" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY title"
            return c.execute(q).fetchall()

    def chat_setting(self, chat_id, enabled=None, delete=False):
        with self.connect() as c:
            if delete: c.execute("DELETE FROM required_chats WHERE chat_id=?", (str(chat_id),))
            elif enabled is not None: c.execute("UPDATE required_chats SET enabled=? WHERE chat_id=?", (int(enabled), str(chat_id)))

    def audit(self, admin_id, action, details):
        with self.connect() as c:
            c.execute("INSERT INTO admin_audit_logs(admin_id,action,details,created_at) VALUES(?,?,?,?)",
                      (admin_id, action, details[:2000], now_iso()))

    def broadcast_log(self, admin_id, target, message, success, failure):
        with self.connect() as c:
            c.execute("INSERT INTO broadcast_logs(admin_id,target,message,success_count,failure_count,created_at) VALUES(?,?,?,?,?,?)",
                      (admin_id, target, message[:4000], success, failure, now_iso()))

