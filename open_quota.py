"""Persistent per-user / per-group quotas for paid manual /re in OPEN groups (RESEARCH_OPEN_GROUP_CHAT_IDS).

UTC-day windows. A slot is reserved (worst-case USD) when the /re is accepted, then reconciled after the adapter returns:
paid LLM call -> actual cost (worst case if unknown); cache hit / coalesced duplicate / refused / failed-before-call -> released.
Order: per-user -> group count -> group USD. Global xAI/Helius budgets are still enforced by the ledger afterwards.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time


def utc_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


class OpenGroupQuota:
    def __init__(self, path: str, *, user_max: int, group_max: int, group_usd: float, clock=time.time):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.user_max, self.group_max, self.group_usd, self.clock = user_max, group_max, group_usd, clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL,
            chat INTEGER NOT NULL, user INTEGER NOT NULL, mint TEXT, status TEXT NOT NULL, reserved_usd REAL NOT NULL,
            actual_usd REAL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS usage_day ON usage(day, chat, user)")
        self._db.execute("UPDATE usage SET status='released' WHERE status='reserved' AND day<?", (utc_day(clock()),))

    def _totals(self, day, chat, user):
        q = ("SELECT COALESCE(SUM(CASE WHEN user=? THEN 1 ELSE 0 END),0), COUNT(*), "
             "COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_usd ELSE COALESCE(actual_usd, reserved_usd) END),0) "
             "FROM usage WHERE day=? AND chat=? AND status IN ('reserved','done')")
        return self._db.execute(q, (user, day, chat)).fetchone()

    def reserve(self, chat: int, user: int, mint: str, worst_usd: float) -> tuple[int | None, str | None]:
        """(row id, None) when allowed; (None, user-facing reason) when a limit is hit."""
        day = utc_day(self.clock())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                u, g, usd = self._totals(day, chat, user)
                why = None
                if u >= self.user_max:
                    why = f"you have used {u} of {self.user_max} research requests today"
                elif g >= self.group_max:
                    why = f"this group has used {g} of {self.group_max} research requests today"
                elif usd + worst_usd > self.group_usd + 1e-9:
                    why = (f"this group's research budget is ${usd:.2f} of ${self.group_usd:.2f} today "
                           f"(each request reserves up to ${worst_usd:.2f} until it finishes)")
                if why:
                    self._db.execute("COMMIT"); return None, why
                cur = self._db.execute("INSERT INTO usage(ts,day,chat,user,mint,status,reserved_usd) VALUES(?,?,?,?,?,'reserved',?)",
                                       (self.clock(), day, chat, user, mint, float(worst_usd)))
                self._db.execute("COMMIT"); return cur.lastrowid, None
            except Exception:
                self._db.execute("ROLLBACK"); raise

    def release(self, row: int | None):
        if row is None: return
        with self._lock:
            self._db.execute("UPDATE usage SET status='released' WHERE id=? AND status='reserved'", (row,))

    def finish(self, row: int | None, actual_usd: float | None):
        """Paid call happened: count it at actual cost (worst-case reservation kept if the cost is unknown)."""
        if row is None: return
        with self._lock:
            self._db.execute("UPDATE usage SET status='done', actual_usd=? WHERE id=? AND status='reserved'",
                             (actual_usd if isinstance(actual_usd, (int, float)) else None, row))

    def today(self, chat: int, user: int | None = None) -> dict:
        u, g, usd = self._totals(utc_day(self.clock()), chat, user if user is not None else -1)
        return {"user": u, "group": g, "usd": usd}
