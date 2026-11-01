"""Phase 7.1: outbound signed webhooks (GitClaim = producer). OFF by default, fail-closed.

Events: claim.detected (new live qualifying claims, one per token) and research.completed (Phase 6 AUTO research only).
Durable SQLite outbox + one bounded background worker. At-least-once delivery; stable event ids; receivers dedupe.
Never blocks or alters Telegram alerts; never makes xAI/Helius calls. See docs/WEBHOOK_PROTOCOL.md.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import re
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

log = logging.getLogger("gitclaim.webhook")

SCHEMA_VERSION = "1.0"
SIG_VERSION = "1"                       # X-GitClaim-Version; signature scheme "v1"
DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV = os.path.join(DIR, "research", "webhook.env")     # gitignored (*.env)
DEFAULT_DB = os.path.join(DIR, "research", "data", "webhook_outbox.sqlite3")
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MAX_BACKOFF_S = 900.0
MAX_RETRY_AFTER_S = 3600.0


def _bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _read_env(path: str) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def is_mint(s) -> bool:
    return isinstance(s, str) and 32 <= len(s) <= 44 and bool(B58.match(s))


def is_sig(s) -> bool:
    return isinstance(s, str) and 64 <= len(s) <= 90 and bool(B58.match(s))


# ---------------------------------------------------------------- config ----
@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""
    secret: str = field(default="", repr=False)
    secret_next: str = field(default="", repr=False)   # rotation: while set, requests carry BOTH signatures
    send_claims: bool = False
    send_research: bool = False
    send_catchup: bool = False                         # historical/catch-up events: never by default
    timeout_s: float = 8.0
    max_retries: int = 5
    queue_limit: int = 1000
    ttl_hours: float = 24.0
    test_mode: bool = False                            # ONLY for isolated local tests: allows http://127.0.0.1/localhost
    db_path: str = DEFAULT_DB
    problems: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.enabled and not self.problems and (self.send_claims or self.send_research)

    @classmethod
    def from_env(cls, env_file: str | None = None, environ: dict | None = None, **over) -> "WebhookConfig":
        e = _read_env(env_file or DEFAULT_ENV)
        e.update({k: v for k, v in (os.environ if environ is None else environ).items() if k.startswith("WEBHOOK_")})
        def num(k, d, t=float):
            try:
                return t(e.get(k, d))
            except (TypeError, ValueError):
                return None
        c = cls(enabled=_bool(e.get("WEBHOOK_ENABLED", "false")), url=e.get("WEBHOOK_URL", ""),
                secret=e.get("WEBHOOK_SECRET", ""), secret_next=e.get("WEBHOOK_SECRET_NEXT", ""),
                send_claims=_bool(e.get("WEBHOOK_SEND_CLAIMS", "false")), send_research=_bool(e.get("WEBHOOK_SEND_RESEARCH", "false")),
                send_catchup=_bool(e.get("WEBHOOK_SEND_CATCHUP", "false")), test_mode=_bool(e.get("WEBHOOK_TEST_MODE", "false")))
        for attr, k, d, t in (("timeout_s", "WEBHOOK_TIMEOUT_SECONDS", 8, float), ("max_retries", "WEBHOOK_MAX_RETRIES", 5, int),
                              ("queue_limit", "WEBHOOK_QUEUE_LIMIT", 1000, int), ("ttl_hours", "WEBHOOK_EVENT_TTL_HOURS", 24, float)):
            v = num(k, d, t)
            if v is None:
                c.problems.append(f"{k} is not a number")
            else:
                setattr(c, attr, v)
        for k, v in over.items():
            setattr(c, k, v)
        c.problems += c.validate()
        return c

    def validate(self) -> list:
        if not self.enabled:
            return []
        p = []
        err = url_problem(self.url, self.test_mode)
        if err:
            p.append(f"WEBHOOK_URL {err}")
        for name, s in (("WEBHOOK_SECRET", self.secret), ("WEBHOOK_SECRET_NEXT", self.secret_next)):
            if (name == "WEBHOOK_SECRET" or s) and (len(s) < 32 or s.lower() in ("changeme", "secret") or len(set(s)) < 8):
                p.append(f"{name} missing or too weak (need >= 32 random characters)")
        if not (self.send_claims or self.send_research):
            p.append("no event type enabled (WEBHOOK_SEND_CLAIMS / WEBHOOK_SEND_RESEARCH)")
        if not (1 <= self.timeout_s <= 30): p.append("WEBHOOK_TIMEOUT_SECONDS must be 1-30")
        if not (0 <= self.max_retries <= 20): p.append("WEBHOOK_MAX_RETRIES must be 0-20")
        if not (1 <= self.queue_limit <= 100000): p.append("WEBHOOK_QUEUE_LIMIT must be 1-100000")
        if not (0 < self.ttl_hours <= 168): p.append("WEBHOOK_EVENT_TTL_HOURS must be (0,168]")
        return p

    def safe_target(self) -> str:
        """Host only, for logs (never the path/query, which may carry a token)."""
        try:
            return urlsplit(self.url).hostname or "?"
        except Exception:
            return "?"


_LOCAL_TEST_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _ip_blocked(ip) -> bool:
    ip = ipaddress.ip_address(ip)
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or (ip.version == 6 and ip.ipv4_mapped is not None and _ip_blocked(ip.ipv4_mapped)))


def url_problem(url: str, test_mode: bool = False) -> str | None:
    """None if acceptable. Production: https, public hostname, no credentials/fragment, sane port."""
    if not url or len(url) > 2048 or any(ch.isspace() for ch in url):
        return "missing or malformed"
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return "malformed"
    host = (u.hostname or "").lower()
    if not host:
        return "has no host"
    if u.username or u.password or "@" in u.netloc:
        return "must not contain credentials"
    if u.fragment:
        return "must not contain a fragment"
    if test_mode and host in _LOCAL_TEST_HOSTS and u.scheme in ("http", "https"):
        return None                                           # isolated local test receiver only
    if u.scheme != "https":
        return "must use https"
    if port not in (None, 443) and not (1024 <= port <= 65535):
        return "uses a disallowed port"
    try:
        if _ip_blocked(host):
            return "points at a private/loopback/reserved address"
    except ValueError:
        if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost", ".lan", ".home.arpa")) or "." not in host:
            return "is not a public hostname"
    return None


def resolve_ok(url: str, test_mode: bool, resolver=socket.getaddrinfo) -> str | None:
    """Per-delivery check that the configured host resolves only to public addresses (anti-SSRF / DNS rebinding)."""
    u = urlsplit(url)
    host = u.hostname or ""
    if test_mode and host.lower() in _LOCAL_TEST_HOSTS:
        return None
    try:
        infos = resolver(host, u.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return f"dns: {type(e).__name__}"
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return "dns: no address"
    if any(_ip_blocked(a.split("%")[0]) for a in addrs):
        return "dns: resolves to a non-public address"
    return None


# --------------------------------------------------------------- signing ----
def canonical_body(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign(secret: str, timestamp: str, event_id: str, version: str, body: bytes) -> str:
    msg = f"{timestamp}.{event_id}.{version}.".encode("utf-8") + body
    return "v1=" + hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def headers_for(cfg: WebhookConfig, event_id: str, event_type: str, body: bytes, now: float | None = None) -> dict:
    ts = str(int(now if now is not None else time.time()))
    sigs = [sign(cfg.secret, ts, event_id, SIG_VERSION, body)]
    if cfg.secret_next:
        sigs.append(sign(cfg.secret_next, ts, event_id, SIG_VERSION, body))
    return {"Content-Type": "application/json; charset=utf-8", "User-Agent": "GitClaim-Webhook/1",
            "X-GitClaim-Event-ID": event_id, "X-GitClaim-Event-Type": event_type, "X-GitClaim-Timestamp": ts,
            "X-GitClaim-Version": SIG_VERSION, "X-GitClaim-Signature": ",".join(sigs)}


def verify(secret: str, headers: dict, body: bytes, *, tolerance_s: int = 300, now: float | None = None) -> tuple[bool, str]:
    """Reference receiver-side verification (also used by the example receiver and tests)."""
    h = {k.lower(): v for k, v in headers.items()}
    eid, ts, ver, sig = (h.get("x-gitclaim-event-id"), h.get("x-gitclaim-timestamp"),
                         h.get("x-gitclaim-version"), h.get("x-gitclaim-signature"))
    if not (eid and ts and ver and sig):
        return False, "missing headers"
    if ver != SIG_VERSION:
        return False, "unsupported version"
    try:
        t = int(ts)
    except ValueError:
        return False, "bad timestamp"
    if abs((now if now is not None else time.time()) - t) > tolerance_s:
        return False, "timestamp outside tolerance"
    want = sign(secret, ts, eid, ver, body)
    if not any(hmac.compare_digest(want, s.strip()) for s in sig.split(",")):
        return False, "bad signature"
    try:
        if json.loads(body).get("event_id") != eid:
            return False, "event id mismatch"
    except ValueError:
        return False, "bad json"
    return True, "ok"


# ---------------------------------------------------------------- events ----
def _iso(ts: float | None) -> str | None:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def claim_event_id(sig: str, mint: str) -> str:
    return "gc_claim_" + hashlib.sha256(f"claim.detected|v1|{sig}|{mint}".encode()).hexdigest()[:32]


def research_event_id(sig: str, mint: str) -> str:
    return "gc_research_" + hashlib.sha256(f"research.completed|v1|{sig}|{mint}".encode()).hexdigest()[:32]


def classify(kind: str | None) -> str:
    k = (kind or "").lower()
    return "FIRST" if "first" in k else "REPEAT" if "repeat" in k else "UNKNOWN"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_claim_event(*, sig: str, mint: str, kind: str, claim: dict, token_lamports, n_coins: int, qualifying: int,
                      github: dict | None = None, symbol: str | None = None, market_cap_display: str | None = None,
                      origin: str = "live", now: float | None = None) -> dict | None:
    """claim: decoded on-chain claim (user_id, amount, lifetime, recipient, pda, ts, tags). github: cached profile only."""
    if not (is_sig(sig) and is_mint(mint)):
        return None
    gh = github or {}
    login = gh.get("login") if isinstance(gh.get("login"), str) and re.fullmatch(r"[A-Za-z0-9-]{1,39}", gh.get("login") or "") else None
    uid = str(claim.get("user_id")) if str(claim.get("user_id") or "").isdigit() else None
    total = _num(claim.get("amount"))
    share = _num(token_lamports)
    if share is None and n_coins == 1:
        share = total                                         # single-coin claim: whole claim belongs to this coin
    tags = [str(t) for t in claim.get("tags", [])]
    signer = ("app_claim" if "app claim" in tags else "automated_signer" if "automated signer" in tags
              else "third_party_trigger" if any("third-party" in t for t in tags) else "unknown")
    return {
        "schema_version": SCHEMA_VERSION, "event_id": claim_event_id(sig, mint), "event_type": "claim.detected",
        "emitted_at": _iso(now if now is not None else time.time()), "chain": "solana", "origin": origin,
        "verified": {
            "source": "gitclaim_onchain",
            "claim": {"tx_signature": sig, "classification": classify(kind), "classification_detail": kind,
                      "block_time": _iso(_num(claim.get("ts"))),
                      "token_fee_share_lamports": share, "token_fee_share_sol": None if share is None else share / 1e9,
                      "token_fee_share_attribution": "exact" if token_lamports is not None else ("single_coin_claim" if share is not None else "unknown"),
                      "vault_claim_total_lamports": total, "vault_claim_total_sol": None if total is None else total / 1e9,
                      "vault_lifetime_lamports": _num(claim.get("lifetime")),
                      "coins_in_claim": n_coins, "qualifying_coins_in_alert": qualifying,
                      "signer_classification": signer, "signer_tags": tags,
                      "token_inferred": "token inferred" in tags},
            "token": {"mint": mint},
            "claimant": {"role": "github_linked_fee_claimant", "creator_relationship": "not_verified",
                         "github_user_id": uid, "github_login": login,
                         "recipient_wallet": claim.get("recipient") if is_mint(claim.get("recipient")) else None,
                         "fee_vault": claim.get("pda") if is_mint(claim.get("pda")) else None},
        },
        "third_party": {"symbol": symbol if isinstance(symbol, str) and len(symbol) <= 32 else None,
                        "symbol_source": "pump.fun / dexscreener at alert time" if symbol else None,
                        "market_cap_display": market_cap_display if isinstance(market_cap_display, str) and len(market_cap_display) <= 32 else None},
        "links": {"solscan_tx": f"https://solscan.io/tx/{sig}", "solscan_token": f"https://solscan.io/token/{mint}",
                  "pump_fun": f"https://pump.fun/coin/{mint}", "dexscreener": f"https://dexscreener.com/solana/{mint}",
                  "github_profile": f"https://github.com/{login}" if login else None},
    }


_URL_OK = re.compile(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s<>\"']*)?$")


def _public_url(u) -> str | None:
    if not isinstance(u, str) or len(u) > 300 or not _URL_OK.match(u):
        return None
    return None if url_problem(u) else u


def build_research_event(*, sig: str, mint: str, res, facts=None, pctx: dict | None = None,
                         scrub=lambda s: s, split=None, now: float | None = None) -> dict | None:
    """From an ALREADY COMPLETED Phase 6 auto research result (no new calls)."""
    if not (res is not None and getattr(res, "ok", False) and is_mint(mint) and is_sig(sig)):
        return None
    ans = getattr(res, "answer", None)
    raw = getattr(ans, "answer", None) or getattr(res, "text", "") or ""
    parts = split(raw) if split else {"NARRATIVE": [raw], "VERDICT": [], "DATA": []}
    clean = lambda xs, n: scrub(" ".join(xs))[:n].strip() or None
    plan = getattr(res, "plan", None)
    usage = getattr(res, "usage", None) or {}
    cost = usage.get("actual_usd")
    vf = facts.as_data() if facts is not None and hasattr(facts, "as_data") else None
    if vf:
        vf = {k: v for k, v in vf.items() if k in ("mint", "symbol", "github_login", "claim_sol", "claim_total_sol", "claim_coins",
                                                     "claim_kind", "tx_signature", "claim_time", "signer_tags")}
    p = pctx or {}
    return {
        "schema_version": SCHEMA_VERSION, "event_id": research_event_id(sig, mint), "event_type": "research.completed",
        "emitted_at": _iso(now if now is not None else time.time()), "chain": "solana",
        "claim_event_id": claim_event_id(sig, mint), "claim_tx_signature": sig, "token": {"mint": mint},
        "verified": {"source": "gitclaim_onchain", "facts": vf},
        "third_party": {"label": "unverified data from the project's own pages / public APIs",
                        "website": _public_url(p.get("website")),
                        "links": [x for x in (_public_url(l.get("url") if isinstance(l, dict) else l) for l in (p.get("links") or [])[:5]) if x],
                        "description": scrub(str(p.get("description") or ""))[:400] or None,
                        "repos": [{"name": str(r.get("name"))[:100], "language": r.get("language"),
                                   "stars": _num(r.get("stars"))} for r in (p.get("repos") or [])[:5] if isinstance(r, dict)]},
        "ai_interpretation": {"label": "AI-generated interpretation by Queen; not verified; not a trading recommendation or safety verdict",
                              "narrative": clean(parts.get("NARRATIVE", []), 800),
                              "risk_verdict": clean(parts.get("VERDICT", []), 400),
                              "key_findings": [scrub(x)[:240] for x in parts.get("DATA", [])[:6] if scrub(x).strip()],
                              "tool_references": [t for t in (getattr(ans, "tools", None) or []) if isinstance(t, str) and re.fullmatch(r"[a-z0-9_]{1,40}", t)][:10],
                              "source_urls": [x for x in (_public_url(s if isinstance(s, str) else (s or {}).get("url")) for s in (getattr(ans, "sources", None) or [])[:8]) if x]},
        "research": {"model": getattr(plan, "model", None), "reasoning_effort": getattr(plan, "effort", None),
                     "trigger": "auto", "completed_at": _iso(now if now is not None else time.time()),
                     "duration_ms": getattr(res, "latency_ms", None),
                     "cost_usd": cost if isinstance(cost, (int, float)) else None,
                     "cost_source": "measured" if isinstance(cost, (int, float)) else "unknown", "cached": bool(getattr(res, "cached", False))},
    }


# ---------------------------------------------------------------- outbox ----
class Outbox:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, body BLOB NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_at REAL NOT NULL, last_status INTEGER, last_error TEXT, updated_at REAL)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, ts REAL NOT NULL,
            http_status INTEGER, error TEXT, ms INTEGER)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS ev_due ON events(status, next_at)")

    def enqueue(self, event: dict, *, limit: int, ttl_s: float, now: float | None = None) -> str:
        """'queued' | 'duplicate' | 'full'. Body bytes are frozen here, so every retry sends identical bytes and id."""
        now = now or time.time()
        body = canonical_body(event)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._db.execute("SELECT 1 FROM events WHERE event_id=?", (event["event_id"],)).fetchone():
                    self._db.execute("COMMIT"); return "duplicate"
                n = self._db.execute("SELECT COUNT(*) FROM events WHERE status IN ('pending','delivering')").fetchone()[0]
                if n >= limit:
                    self._db.execute("COMMIT"); return "full"
                self._db.execute("INSERT INTO events(event_id,event_type,body,created_at,expires_at,status,next_at,updated_at) "
                                 "VALUES(?,?,?,?,?,'pending',?,?)",
                                 (event["event_id"], event["event_type"], body, now, now + ttl_s, now, now))
                self._db.execute("COMMIT"); return "queued"
            except Exception:
                self._db.execute("ROLLBACK"); raise

    def recover(self) -> int:
        """After a restart: in-flight attempts have unknown outcome -> pending again (at-least-once)."""
        with self._lock:
            return self._db.execute("UPDATE events SET status='pending' WHERE status='delivering'").rowcount

    def expire(self, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            return self._db.execute("UPDATE events SET status='expired', updated_at=? WHERE status='pending' AND expires_at<=?",
                                    (now, now)).rowcount

    def next_due(self, now: float | None = None):
        now = now or time.time()
        with self._lock:
            r = self._db.execute("SELECT event_id,event_type,body,attempts FROM events WHERE status='pending' AND next_at<=? "
                                 "AND expires_at>? ORDER BY next_at, created_at LIMIT 1", (now, now)).fetchone()
            if r:
                self._db.execute("UPDATE events SET status='delivering', updated_at=? WHERE event_id=?", (now, r[0]))
            return r

    def record(self, event_id, *, status, attempts, next_at=None, http_status=None, error=None, ms=None):
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO attempts(event_id,ts,http_status,error,ms) VALUES(?,?,?,?,?)", (event_id, now, http_status, error, ms))
            self._db.execute("UPDATE events SET status=?, attempts=?, next_at=COALESCE(?,next_at), last_status=?, last_error=?, updated_at=? "
                             "WHERE event_id=?", (status, attempts, next_at, http_status, error, now, event_id))

    def get(self, event_id) -> dict | None:
        r = self._db.execute("SELECT event_id,event_type,status,attempts,next_at,last_status,last_error FROM events WHERE event_id=?",
                             (event_id,)).fetchone()
        return dict(zip(("event_id", "event_type", "status", "attempts", "next_at", "last_status", "last_error"), r)) if r else None

    def counts(self) -> dict:
        return dict(self._db.execute("SELECT status, COUNT(*) FROM events GROUP BY status").fetchall())

    def attempts(self, event_id) -> list:
        return [dict(zip(("ts", "http_status", "error", "ms"), r)) for r in
                self._db.execute("SELECT ts,http_status,error,ms FROM attempts WHERE event_id=? ORDER BY id", (event_id,))]

    def prune(self, keep_days: float = 7, now: float | None = None):
        cut = (now or time.time()) - keep_days * 86400
        with self._lock:
            self._db.execute("DELETE FROM attempts WHERE event_id IN (SELECT event_id FROM events WHERE status IN "
                             "('delivered','failed','expired') AND updated_at<?)", (cut,))
            self._db.execute("DELETE FROM events WHERE status IN ('delivered','failed','expired') AND updated_at<?", (cut,))


def backoff_s(attempt: int, rnd=random.random) -> float:
    """attempt 1 -> ~2-4s, 2 -> ~4-8s ... capped at MAX_BACKOFF_S (full jitter in [base/2, base])."""
    base = min(MAX_BACKOFF_S, 2.0 * (2 ** max(0, attempt)))
    return base / 2 + rnd() * base / 2


def retry_after_s(v, now: float | None = None) -> float | None:
    if not v:
        return None
    try:
        return max(0.0, min(MAX_RETRY_AFTER_S, float(v)))
    except ValueError:
        try:
            return max(0.0, min(MAX_RETRY_AFTER_S, parsedate_to_datetime(v).timestamp() - (now or time.time())))
        except Exception:
            return None


# -------------------------------------------------------------- producer ----
class Producer:
    """Facade used by bot.py. Every public method is non-blocking and never raises."""

    def __init__(self, cfg: WebhookConfig, outbox: Outbox | None = None, http=None, resolver=socket.getaddrinfo,
                 sleep=time.sleep, clock=time.time):
        self.cfg = cfg
        self.outbox = outbox
        self.http = http
        self.resolver = resolver
        self.sleep, self.clock = sleep, clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.stats = {"queued": 0, "duplicate": 0, "full": 0, "skipped": 0, "error": 0}
        if cfg.active and self.outbox is None:
            try:
                self.outbox = Outbox(cfg.db_path)
            except Exception as e:
                log.warning("webhook outbox unavailable (%s): webhooks disabled", type(e).__name__)
                cfg.problems.append("outbox unavailable")

    @classmethod
    def from_env(cls, **kw) -> "Producer":
        try:
            return cls(WebhookConfig.from_env(), **kw)
        except Exception as e:
            log.warning("webhook config error (%s): webhooks disabled", type(e).__name__)
            return cls(WebhookConfig(problems=["config error"]), **kw)

    def describe(self) -> str:
        c = self.cfg
        if c.active:
            return (f"webhooks ON -> {c.safe_target()} (claims={'on' if c.send_claims else 'off'}, research={'on' if c.send_research else 'off'}"
                    f"{', TEST MODE' if c.test_mode else ''})")
        if c.enabled and c.problems:
            return "webhooks OFF (fail-closed: " + "; ".join(c.problems) + ")"
        return "webhooks off"

    def _submit(self, event) -> str:
        if event is None:
            self.stats["skipped"] += 1; return "skipped"
        try:
            r = self.outbox.enqueue(event, limit=self.cfg.queue_limit, ttl_s=self.cfg.ttl_hours * 3600, now=self.clock())
        except Exception as e:
            self.stats["error"] += 1
            log.warning("webhook enqueue failed: %s", type(e).__name__); return "error"
        self.stats[r] += 1
        if r == "full":
            log.warning("webhook queue full (%d): dropped %s", self.cfg.queue_limit, event["event_id"])
        else:
            log.info("webhook %s %s %s", r, event["event_type"], event["event_id"])
        self._wake.set()
        return r

    def on_claim(self, *, sig, hits, claim, n_coins, origin="live", github=None, symbols=None, market_cap=None) -> list:
        """hits: [(kind, mint, lamports_or_None)] already qualified for the alert. One event per verified token."""
        out = []
        try:
            if not (self.cfg.active and self.cfg.send_claims):
                return out
            if origin != "live" and not self.cfg.send_catchup:
                log.info("webhook skip claim %s: origin=%s (catch-up/replay not published)", str(sig)[:12], origin); return out
            for i, (kind, mint, lam) in enumerate(hits):
                if not is_mint(mint):
                    log.info("webhook skip claim %s: no verified mint", str(sig)[:12]); self.stats["skipped"] += 1; continue
                ev = build_claim_event(sig=sig, mint=mint, kind=kind, claim=claim, token_lamports=lam, n_coins=n_coins,
                                       qualifying=len(hits), github=github, symbol=(symbols or {}).get(mint),
                                       market_cap_display=market_cap if i == 0 else None, origin=origin, now=self.clock())
                out.append(self._submit(ev))
        except Exception as e:
            log.warning("webhook claim hook failed: %s", type(e).__name__)
        return out

    def on_auto_research(self, *, sig, mint, res, facts=None, pctx=None, scrub=lambda s: s, split=None) -> str:
        try:
            if not (self.cfg.active and self.cfg.send_research):
                return "off"
            return self._submit(build_research_event(sig=sig, mint=mint, res=res, facts=facts, pctx=pctx,
                                                     scrub=scrub, split=split, now=self.clock()))
        except Exception as e:
            log.warning("webhook research hook failed: %s", type(e).__name__); return "error"

    # ---- worker ----
    def start(self):
        if not self.cfg.active or self._thread:
            return
        n = self.outbox.recover()
        if n:
            log.info("webhook: %d in-flight events re-queued after restart", n)
        self._thread = threading.Thread(target=self._loop, name="webhook-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set(); self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception as e:
                log.warning("webhook worker error: %s", type(e).__name__); busy = False
            if not busy:
                self._wake.wait(2.0); self._wake.clear()

    def run_once(self) -> bool:
        """Deliver at most one due event. Returns True if one was attempted."""
        now = self.clock()
        self.outbox.expire(now)
        r = self.outbox.next_due(now)
        if not r:
            return False
        eid, etype, body, attempts = r
        self._deliver(eid, etype, bytes(body), attempts + 1)
        return True

    def _deliver(self, eid, etype, body, attempt):
        cfg, t0 = self.cfg, time.time()
        status, err, ra = None, None, None
        bad = resolve_ok(cfg.url, cfg.test_mode, self.resolver)
        if bad:
            err = bad
        else:
            try:
                http = self.http
                if http is None:
                    import requests; http = requests
                resp = http.post(cfg.url, data=body, headers=headers_for(cfg, eid, etype, body, time.time()),
                                 timeout=(min(cfg.timeout_s, 5.0), cfg.timeout_s), allow_redirects=False)
                status = resp.status_code
                ra = retry_after_s((resp.headers or {}).get("Retry-After"), self.clock())
            except Exception as e:
                err = type(e).__name__
        ms = int((time.time() - t0) * 1000)
        if status is not None and 200 <= status < 300:
            self.outbox.record(eid, status="delivered", attempts=attempt, http_status=status, ms=ms)
            log.info("webhook delivered %s (%s) in %dms", eid, status, ms); return
        retryable = status is None or status in RETRY_STATUSES
        if not retryable or attempt > cfg.max_retries:
            why = f"HTTP {status}" if status is not None else err
            self.outbox.record(eid, status="failed", attempts=attempt, http_status=status, error=str(err or why)[:120], ms=ms)
            log.warning("webhook failed %s permanently: %s (attempt %d)", eid, why, attempt); return
        wait = ra if ra is not None and status in (429, 503) else backoff_s(attempt)
        self.outbox.record(eid, status="pending", attempts=attempt, next_at=self.clock() + wait, http_status=status,
                           error=(str(err)[:120] if err else None), ms=ms)
        log.info("webhook retry %s in %.0fs (%s, attempt %d)", eid, wait, f"HTTP {status}" if status else err, attempt)
