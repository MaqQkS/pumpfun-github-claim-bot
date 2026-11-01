"""Phase 6: optional automatic Queen research after qualifying GitClaim alerts. OFF by default.

Order: scanner detects -> original alert sent unchanged -> alert_map records message_id -> on_alert() decides
(explainable log line) -> persistent job (sqlite, unique per claim tx + mint + chat) -> CommandService auto queue
(bounded, manual /re first) -> Queen grok-4.3 low via the normal adapter (source='auto' budget) -> report sent as a
reply to the original alert in an opted-in chat. on_alert() never blocks and never raises into the scanner.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from alert_map import b58_32

log = logging.getLogger("gitclaim.auto")
STATES = ("queued", "running", "completed", "skipped", "failed", "failed_unknown", "expired", "undelivered")


def _env(env_file: Path | None = None) -> dict:
    out = {}
    try:
        from research.config import HERE, _parse_env_file
        out.update(_parse_env_file(env_file or HERE / "research.env"))
    except Exception:
        pass
    out.update({k: v for k, v in os.environ.items() if k.startswith(("AUTO_RESEARCH_", "CATCHUP_"))})
    return out


def _ids(v: str) -> frozenset:
    out = set()
    for x in (v or "").replace(";", ",").split(","):
        x = x.strip()
        if x.lstrip("-").isdigit():
            out.add(int(x))
    return frozenset(out)


@dataclass(frozen=True)
class AutoConfig:
    enabled: bool = False
    chats: frozenset = frozenset()
    max_per_day: int = 0
    daily_usd: float = 0.0
    daily_helius: int = 0
    max_queue: int = 2
    stale_s: int = 600
    min_sol: float = 0.3
    catchup_enabled: bool = False
    catchup_window_s: int = 900
    catchup_max_sigs: int = 25

    @classmethod
    def from_env(cls, env: dict | None = None, min_sol: float = 0.3):
        e = _env() if env is None else env
        b = lambda k: str(e.get(k, "false")).strip().lower() in ("1", "true", "yes", "on")
        num = lambda k, d, t: t(e.get(k) or d)
        return cls(enabled=b("AUTO_RESEARCH_ENABLED"), chats=_ids(e.get("AUTO_RESEARCH_CHAT_IDS", "")),
                   max_per_day=num("AUTO_RESEARCH_MAX_PER_DAY", 0, int), daily_usd=num("AUTO_RESEARCH_DAILY_USD", 0, float),
                   daily_helius=num("AUTO_RESEARCH_DAILY_HELIUS_CREDITS", 0, int),
                   max_queue=max(0, min(5, num("AUTO_RESEARCH_MAX_QUEUE", 2, int))), stale_s=num("AUTO_RESEARCH_STALE_S", 600, int),
                   min_sol=min_sol, catchup_enabled=b("CATCHUP_ENABLED"),
                   catchup_window_s=max(0, min(3600, num("CATCHUP_WINDOW_S", 900, int))),
                   catchup_max_sigs=max(0, min(100, num("CATCHUP_MAX_SIGS", 25, int))))

    @property
    def limits(self) -> dict:
        return {"daily_usd": self.daily_usd, "max_per_day": self.max_per_day, "daily_helius": self.daily_helius}


class JobStore:
    def __init__(self, path: str | Path, now=time.time):
        self.path, self.now = str(path), now
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._c() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, sig TEXT NOT NULL, mint TEXT NOT NULL,"
                      " chat INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT, created REAL, updated REAL, alert_msg INTEGER,"
                      " meta TEXT, UNIQUE(sig, mint, chat))")

    def _c(self):
        return sqlite3.connect(self.path, timeout=5)

    def claim(self, sig, mint, chat, alert_msg, meta) -> int | None:
        """Atomically create the job; None if this (claim tx, mint, chat) was ever seen before (any state)."""
        try:
            with self._lock, self._c() as c:
                cur = c.execute("INSERT INTO jobs(sig,mint,chat,state,created,updated,alert_msg,meta) VALUES(?,?,?,'queued',?,?,?,?)",
                                (sig, mint, int(chat), self.now(), self.now(), alert_msg, json.dumps(meta or {}, default=str)))
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def skip(self, sig, mint, chat, reason):
        """Record a skip so a later duplicate event is still recognised (idempotent)."""
        try:
            with self._lock, self._c() as c:
                c.execute("INSERT OR IGNORE INTO jobs(sig,mint,chat,state,reason,created,updated) VALUES(?,?,?,'skipped',?,?,?)",
                          (sig, mint, int(chat), reason, self.now(), self.now()))
        except Exception:
            pass

    def set(self, job_id, state, reason=None):
        assert state in STATES
        with self._lock, self._c() as c:
            c.execute("UPDATE jobs SET state=?, reason=COALESCE(?, reason), updated=? WHERE id=?", (state, reason, self.now(), job_id))

    def get(self, job_id):
        with self._lock, self._c() as c:
            r = c.execute("SELECT id,sig,mint,chat,state,reason,created,alert_msg,meta FROM jobs WHERE id=?", (job_id,)).fetchone()
        return None if r is None else dict(zip(("id", "sig", "mint", "chat", "state", "reason", "created", "alert_msg", "meta"), r))

    def states(self) -> dict:
        with self._lock, self._c() as c:
            return dict(c.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall())

    def recover(self, stale_s: int) -> list[dict]:
        """At startup: 'running' (outcome unknown) -> failed_unknown, never retried (the ledger row stays charged at its
        worst-case reservation). 'queued' older than stale_s -> expired. Returns fresh queued jobs to re-enqueue."""
        now = self.now()
        with self._lock, self._c() as c:
            c.execute("UPDATE jobs SET state='failed_unknown', reason='bot restarted while running; not retried', updated=? "
                      "WHERE state='running'", (now,))
            c.execute("UPDATE jobs SET state='expired', reason='stale after restart', updated=? WHERE state='queued' AND created<?",
                      (now, now - stale_s))
            rows = c.execute("SELECT id,sig,mint,chat,alert_msg,meta,created FROM jobs WHERE state='queued'").fetchall()
        return [dict(zip(("id", "sig", "mint", "chat", "alert_msg", "meta", "created"), r)) for r in rows]


@dataclass
class AutoJob:
    """Shape CommandService's research worker expects (chat, mint, key, facts, auto, stale_at) + auto extras."""
    chat: int
    mint: str
    key: tuple
    facts: object = None
    auto: bool = True
    stale_at: float | None = None
    limits: dict = field(default_factory=dict)
    alert_msg_id: int | None = None
    job_id: int | None = None
    store: object = None
    enqueued: float = field(default_factory=time.time)

    def mark(self, state):
        if self.store and self.job_id:
            self.store.set(self.job_id, state)

    def on_done(self, status, detail=""):
        if self.store and self.job_id:
            self.store.set(self.job_id, status, detail[:300])
        log.info("AUTO job %s %s mint=%s chat=%s %s", self.job_id, status, self.mint, self.chat, detail[:160])


@dataclass
class AlertEvent:
    sig: str
    kind: str | None
    mint: str | None                 # primary (top) verified mint
    mints: list                      # all qualifying mints in the alert
    share_sol: float | None
    origin: str = "live"             # live | catchup | replay
    tags: tuple = ()
    facts: dict | None = None        # verified ClaimFacts.as_data() built from the alert GitClaim generated


def decide(ev: AlertEvent, cfg: AutoConfig, chat: int, delivered: dict) -> str | None:
    """None = eligible; otherwise the skip reason (logged)."""
    if not cfg.enabled:
        return "disabled"
    if ev.origin != "live":
        return f"origin={ev.origin} (only new live alerts)"
    if chat not in cfg.chats:
        return "chat not opted in"
    if chat not in delivered:
        return "original alert not delivered to this chat"
    k = (ev.kind or "").lower()
    if "first" not in k:
        return f"claim type '{ev.kind}' (FIRST only)"
    if "unverified" in k:
        return "unverified claim classification"
    if not ev.mint or not b58_32(ev.mint):
        return "mint unresolved or invalid"
    if any("ambiguous" in str(t) for t in ev.tags):
        return "token ambiguous"
    if ev.share_sol is None or ev.share_sol < cfg.min_sol:
        return f"below min {cfg.min_sol} SOL"
    if cfg.max_per_day <= 0 or cfg.daily_usd <= 0:
        return "auto budget is zero"
    return None


class AutoResearch:
    def __init__(self, cfg: AutoConfig, store: JobStore, cmds, alerts, now=time.time):
        self.cfg, self.store, self.cmds, self.alerts, self.now = cfg, store, cmds, alerts, now

    def _budget_reason(self) -> str | None:
        try:
            a = self.cmds.adapter()
            led, s = a.ledger, a.s
            at, t = led.auto_totals(), led.totals()
            wc = float(s.worst_case_usd.get(s.standard_model, 0.05))
            if at["runs_today"] >= self.cfg.max_per_day:
                return f"auto runs {at['runs_today']}/{self.cfg.max_per_day} today"
            if at["usd_today"] + wc > self.cfg.daily_usd:
                return f"auto budget ${at['usd_today']:.3f}+${wc:.2f} > ${self.cfg.daily_usd:.2f}"
            if t["usd_today"] + wc > s.budget_daily_usd or t["usd_total"] + wc > s.budget_total_usd:
                return "global xAI budget"
            if at["helius_today"] + s.helius_worst_case > self.cfg.daily_helius:
                return "auto Helius budget"
        except Exception as e:
            return f"budget check failed ({type(e).__name__})"
        return None

    def on_alert(self, ev: AlertEvent) -> list[tuple[int, str]]:
        """Called after the original alert was sent. Never raises, never blocks on network. Returns decisions."""
        out = []
        try:
            delivered = self.alerts.find_by_sig(ev.sig) if self.alerts else {}
            chats = sorted(self.cfg.chats) or [None]
            for chat in chats:
                why = decide(ev, self.cfg, chat, delivered)
                if why is None:
                    why = self._budget_reason()
                if why is None:
                    from telegram_cmds import facts_from_meta
                    facts = facts_from_meta({"facts": ev.facts}, ev.mint)
                    jid = self.store.claim(ev.sig, ev.mint, chat, delivered[chat], {"facts": ev.facts})
                    if jid is None:
                        why = "already triggered (persistent dedupe)"
                    else:
                        job = AutoJob(chat, ev.mint, ("auto", ev.sig, ev.mint, chat), facts, stale_at=self.now() + self.cfg.stale_s,
                                      limits=self.cfg.limits, alert_msg_id=delivered[chat], job_id=jid, store=self.store)
                        ok, msg = self.cmds.enqueue_auto(job)
                        if not ok:
                            self.store.set(jid, "skipped", msg); why = msg
                        else:
                            log.info("AUTO queue sig=%s mint=%s chat=%s job=%s (eligible)", ev.sig[:12], ev.mint, chat, jid)
                            out.append((chat, "queued")); continue
                elif ev.mint and chat is not None and why not in ("disabled",):
                    self.store.skip(ev.sig, ev.mint, chat, why)
                log.info("AUTO skip sig=%s mint=%s chat=%s: %s", (ev.sig or "")[:12], ev.mint, chat, why)
                out.append((chat, why))
        except Exception as e:
            log.warning("auto research decision failed: %s", type(e).__name__)
        return out

    def recover(self):
        """Startup: settle unknown outcomes; re-enqueue fresh queued jobs only if still enabled."""
        try:
            fresh = self.store.recover(self.cfg.stale_s)
            for r in fresh:
                if not (self.cfg.enabled and r["chat"] in self.cfg.chats):
                    self.store.set(r["id"], "expired", "auto research disabled after restart"); continue
                from telegram_cmds import facts_from_meta
                meta = json.loads(r["meta"] or "{}")
                job = AutoJob(r["chat"], r["mint"], ("auto", r["sig"], r["mint"], r["chat"]), facts_from_meta(meta, r["mint"]),
                              stale_at=r["created"] + self.cfg.stale_s, limits=self.cfg.limits, alert_msg_id=r["alert_msg"],
                              job_id=r["id"], store=self.store)
                ok, msg = self.cmds.enqueue_auto(job)
                if not ok:
                    self.store.set(r["id"], "skipped", msg)
        except Exception as e:
            log.warning("auto recover failed: %s", type(e).__name__)
