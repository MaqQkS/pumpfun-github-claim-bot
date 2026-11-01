"""Persistent usage ledger + budget gate (SQLite, separate from the bot's state.json).

Accounting rule (conservative): a request counts against the budget at its worst-case reservation
until real usage replaces it. If real usage never arrives (timeout after Queen started, crash),
the reservation stays counted. Requests that never reached Queen (connection refused) count 0.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, day TEXT NOT NULL,
  command TEXT, mode TEXT, model TEXT, effort TEXT, mint TEXT,
  status TEXT NOT NULL,            -- pending | ok | error | not_sent
  error TEXT,
  reserved_usd REAL NOT NULL DEFAULT 0,
  est_usd REAL, actual_usd REAL, cost_source TEXT,
  input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
  llm_calls INTEGER, models_seen TEXT,
  helius_reserved INTEGER NOT NULL DEFAULT 0, helius_est INTEGER, helius_measured INTEGER,
  latency_ms INTEGER, tools TEXT
);
"""


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime("%Y-%m-%d")


@dataclass
class BudgetDecision:
    ok: bool
    reason: str | None = None
    spent_total: float = 0.0
    spent_today: float = 0.0
    helius_today: int = 0


# Charge expression: real cost if known, else the reservation (unknown = assume worst case)
_USD = "CASE WHEN status='not_sent' THEN 0 WHEN actual_usd IS NOT NULL THEN actual_usd ELSE reserved_usd END"
_HEL = "CASE WHEN status='not_sent' THEN 0 WHEN helius_measured IS NOT NULL THEN helius_measured ELSE helius_reserved END"


class Ledger:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(requests)")}
        if "source" not in cols:     # Phase 6 migration: manual | auto | direct (old rows = manual)
            self._db.execute("ALTER TABLE requests ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        self._lock = threading.Lock()

    def totals(self, day: str | None = None) -> dict:
        day = day or utc_day()
        q = f"SELECT COALESCE(SUM({_USD}),0), COALESCE(SUM(CASE WHEN day=? THEN {_USD} ELSE 0 END),0), " \
            f"COALESCE(SUM(CASE WHEN day=? THEN {_HEL} ELSE 0 END),0), COALESCE(SUM({_HEL}),0), COUNT(*) FROM requests"
        t, d, hd, ht, n = self._db.execute(q, (day, day)).fetchone()
        return {"usd_total": t, "usd_today": d, "helius_today": hd, "helius_total": ht, "requests": n}

    def auto_totals(self, day: str | None = None) -> dict:
        day = day or utc_day()
        q = f"SELECT COALESCE(SUM({_USD}),0), COALESCE(SUM({_HEL}),0), COUNT(*) FROM requests WHERE source='auto' AND day=?"
        u, h, n = self._db.execute(q, (day,)).fetchone()
        return {"usd_today": u, "helius_today": h, "runs_today": n}

    def record_direct_helius(self, method: str, credits: int = 1, command: str = "direct", mint: str | None = None) -> None:
        """Durable record of a direct Helius RPC made by GitClaim's research path on QUEEN's key (/ho, /re context).
        Queen's own RPCs are counted separately via its meter, so nothing is counted twice."""
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO requests(ts,day,command,mode,model,mint,status,reserved_usd,helius_reserved,"
                             "helius_measured,cost_source,source) VALUES(?,?,?,?,?,?,'ok',0,0,?,'local_estimate','direct')",
                             (now, utc_day(now), command, "direct", "helius:" + method, mint, int(credits)))

    def helius_allowed(self, settings, need: int) -> BudgetDecision:
        t = self.totals()
        if t["helius_today"] + need > settings.helius_daily_credits:
            return BudgetDecision(False, f"helius_daily: ~{t['helius_today']} credits today of {settings.helius_daily_credits}")
        if t["helius_total"] + need > settings.helius_total_credits:
            return BudgetDecision(False, f"helius_total: ~{t['helius_total']} credits of {settings.helius_total_credits}")
        return BudgetDecision(True)

    def reserve(self, *, settings, command: str, mode: str, model: str, effort: str, mint: str,
                source: str = "manual", auto: dict | None = None) -> tuple[int | None, BudgetDecision]:
        """Atomic pre-request check + reservation. Returns (row id, decision).
        source='auto' must ALSO fit `auto` = {"daily_usd", "max_per_day", "daily_helius"}; missing/zero => refused."""
        wc = float(settings.worst_case_usd.get(model, max(settings.worst_case_usd.values())))
        with self._lock:
            t = self.totals()
            dec = BudgetDecision(True, None, t["usd_total"], t["usd_today"], t["helius_today"])
            if source == "auto":
                a, lim = self.auto_totals(), auto or {}
                if a["runs_today"] + 1 > int(lim.get("max_per_day") or 0):
                    dec.ok, dec.reason = False, f"auto_runs: {a['runs_today']} of {int(lim.get('max_per_day') or 0)} today"
                elif a["usd_today"] + wc > float(lim.get("daily_usd") or 0):
                    dec.ok, dec.reason = False, f"auto_daily_usd: ${a['usd_today']:.4f} of ${float(lim.get('daily_usd') or 0):.2f}, next may cost ${wc:.2f}"
                elif a["helius_today"] + settings.helius_worst_case > int(lim.get("daily_helius") or 0):
                    dec.ok, dec.reason = False, f"auto_helius: ~{a['helius_today']} of {int(lim.get('daily_helius') or 0)} credits today"
                if not dec.ok:
                    return None, dec
            if t["usd_total"] + wc > settings.budget_total_usd:
                dec.ok, dec.reason = False, f"total_budget: ${t['usd_total']:.4f} spent of ${settings.budget_total_usd:.2f}, next request may cost up to ${wc:.2f}"
            elif t["usd_today"] + wc > settings.budget_daily_usd:
                dec.ok, dec.reason = False, f"daily_budget: ${t['usd_today']:.4f} spent today of ${settings.budget_daily_usd:.2f}, next request may cost up to ${wc:.2f}"
            elif t["helius_today"] + settings.helius_worst_case > settings.helius_daily_credits:
                dec.ok, dec.reason = False, f"helius_daily: ~{t['helius_today']} credits today of {settings.helius_daily_credits}"
            elif t["helius_total"] + settings.helius_worst_case > settings.helius_total_credits:
                dec.ok, dec.reason = False, f"helius_total: ~{t['helius_total']} credits of {settings.helius_total_credits}"
            if not dec.ok:
                return None, dec
            now = time.time()
            src = source if source in ("manual", "auto") else "manual"
            cur = self._db.execute(
                "INSERT INTO requests(ts,day,command,mode,model,effort,mint,status,reserved_usd,helius_reserved,helius_est,source) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (now, utc_day(now), command, mode, model, effort, mint, wc, settings.helius_worst_case, settings.helius_est_per_research, src))
            return cur.lastrowid, dec

    def finish(self, row_id: int, *, status: str, error: str | None = None, latency_ms: int | None = None,
               tools: list | None = None, usage: dict | None = None, est_usd: float | None = None) -> None:
        u = usage or {}
        with self._lock:
            self._db.execute(
                "UPDATE requests SET status=?, error=?, latency_ms=?, tools=?, est_usd=?, actual_usd=?, cost_source=?, "
                "input_tokens=?, cached_tokens=?, output_tokens=?, reasoning_tokens=?, llm_calls=?, models_seen=?, helius_measured=? WHERE id=?",
                (status, (error or None) and str(error)[:300], latency_ms, json.dumps(tools) if tools is not None else None, est_usd,
                 u.get("actual_usd"), u.get("cost_source"), u.get("input_tokens"), u.get("cached_tokens"), u.get("output_tokens"),
                 u.get("reasoning_tokens"), u.get("llm_calls"), json.dumps(u.get("models")) if u.get("models") else None,
                 u.get("helius_credits"), row_id))

    def rows(self, limit: int = 50) -> list[dict]:
        cur = self._db.execute("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        self._db.close()
