"""GitClaim Telegram command layer (Phase 3).

One bot identity: GitClaim. Queen is reached only through research.ResearchAdapter (local HTTP).
- parse_command(): exact command names, /cmd@OurBot accepted, /cmd@OtherBot ignored, /d deep prefix.
- REGISTRY: the full planned command list as data; only OPERATIONAL commands run.
- CommandService.handle(): called from the Telegram poll thread. It only checks access, dedupes and
  enqueues; all network work runs on worker threads, so polling and alert delivery never wait on it.
- Replies go only to the requesting chat (never tg_send_all).
"""
from __future__ import annotations

import html
import logging
import os
import queue
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import direct_data as dd

log = logging.getLogger("bot.cmds")
E = html.escape
TG_LIMIT = 4096
CHUNK = 3900


# ---- registry (data) ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    name: str
    aliases: tuple
    group: str            # core | advanced | system
    summary: str
    operational: bool = False
    ai: bool = False      # needs Queen / an LLM
    reason: str = ""      # why it is not available yet


_LATER = "Queen-powered; planned for a later phase."
_LAUNCH = "Needs Helius launch-history access, which is off to protect credits."
REGISTRY: list[Spec] = [
    Spec("research", ("re",), "core", "AI token research (grok-4.3)", True, True),
    Spec("liquidity", ("liq",), "core", "DexScreener price & liquidity", True),
    Spec("holders", ("ho",), "core", "Top holders & concentration", True),
    Spec("github", ("gh",), "core", "GitHub account / repo stats", True),
    Spec("narrative", ("nar",), "core", "Narrative from site + on-chain evidence", ai=True, reason=_LATER),
    Spec("developer", ("dev",), "core", "Developer investigation", ai=True, reason=_LATER),
    Spec("wallets", ("wa",), "core", "Wallet relationships", ai=True, reason=_LATER),
    Spec("risk", ("rk",), "core", "Explain verified risk flags", ai=True, reason=_LATER),
    Spec("bundles", ("bu",), "core", "Launch bundles", ai=True, reason=_LAUNCH),
    Spec("snipers", ("sn",), "core", "Early buyers / snipers", ai=True, reason=_LAUNCH),
    Spec("socials", ("soc",), "core", "Social evidence", ai=True, reason="Needs a paid X search source; X search is off."),
    Spec("zauth", ("za",), "core", "ZAUTH check", reason="ZAUTH integration is not built yet."),
    Spec("compare", ("cmp",), "advanced", "Compare two tokens", ai=True, reason="Planned."),
    Spec("history", ("hist",), "advanced", "Token / dev history", reason="Planned."),
    Spec("changes", ("chg",), "advanced", "What changed since last check", reason="Planned."),
    Spec("funding", ("fund",), "advanced", "Wallet funding chain", ai=True, reason="Planned (deep research)."),
    Spec("launches", ("la", "ln"), "advanced", "Dev's other launches", reason=_LAUNCH),
    Spec("claims", ("cl",), "advanced", "Recent GitClaim alerts", reason="Planned."),
    Spec("scanrepo", ("sr",), "advanced", "Scan a GitHub repo", ai=True, reason="Planned."),
    Spec("ask", ("q",), "advanced", "Free-form question to Queen", ai=True, reason="Planned."),
    Spec("sources", ("src",), "advanced", "Sources behind the last answer", reason="Planned."),
    Spec("status", ("st",), "system", "Scanner, Queen & budget health", True),
    Spec("help", ("h",), "system", "This menu", True),
]
SPECS = {s.name: s for s in REGISTRY}
ALIASES = {}
for _s in REGISTRY:
    for _n in (_s.name, *_s.aliases):
        ALIASES[_n] = _s.name
DEEP = "d"
DEEP_CAPABLE = {"research", "developer", "wallets"}     # /d re, /d dev, /d wa (grok-4.7, disabled this phase)
LIFECYCLE = {"start", "stop"}                          # handled by bot.py (subscriptions)


# ---- parsing -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Parsed:
    name: str            # canonical command name, or the raw word for start/stop/unknown
    args: tuple
    deep: bool = False
    known: bool = True


def _head(tok: str, bot_username: str | None):
    """'/cmd@Bot' -> ('cmd', ok). ok=False when addressed to a different bot."""
    word, _, target = tok[1:].partition("@")
    if target and (not bot_username or target.lower() != bot_username.lower().lstrip("@")):
        return word.lower(), False
    return word.lower(), True


def parse_command(text: str | None, bot_username: str | None) -> Parsed | None:
    """None for non-commands and commands addressed to another bot. Command words match exactly."""
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/") or len(parts[0]) < 2:
        return None
    word, ok = _head(parts[0], bot_username)
    if not ok:
        return None
    args = tuple(parts[1:])        # kept exactly as typed (case preserved)
    if word in LIFECYCLE:
        return Parsed(word, args)
    if word == DEEP:
        if not args:
            return Parsed(DEEP, (), deep=True)
        sub = args[0].lstrip("/").split("@", 1)[0].lower()
        name = ALIASES.get(sub)
        return Parsed(name or sub, args[1:], deep=True, known=bool(name))
    name = ALIASES.get(word)
    return Parsed(name or word, args, known=bool(name))


# ---- config ------------------------------------------------------------------------------------
def _ids(v: str) -> frozenset:
    out = set()
    for x in (v or "").replace(" ", "").split(","):
        if x.lstrip("-").isdigit():
            out.add(int(x))
    return frozenset(out)


@dataclass(frozen=True)
class CmdConfig:
    enabled: bool = False                                   # RESEARCH_COMMANDS_ENABLED (master switch)
    testers: frozenset = frozenset({2038104261})            # RESEARCH_TESTER_IDS (Bala; add Jed when confirmed)
    chats: frozenset = frozenset({-5178862838, 2038104261}) # RESEARCH_CHAT_IDS (Queen Harness Tests + Bala DM)
    max_queue: int = 3                                      # research jobs waiting behind the running one
    direct_workers: int = 2

    @classmethod
    def from_env(cls, env=None, env_file=None):
        """Process env wins; otherwise research/research.env (gitignored), so the switch can be flipped by
        editing that file and restarting only bot.py (run.sh keeps its environment)."""
        if env is None:
            from research.config import HERE, _parse_env_file
            env = {k: v for k, v in _parse_env_file(Path(env_file) if env_file else HERE / "research.env").items()
                   if k.startswith("RESEARCH_")}
            env.update({k: v for k, v in os.environ.items() if k.startswith("RESEARCH_")})
        d = cls()
        return cls(enabled=env.get("RESEARCH_COMMANDS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
                   testers=_ids(env["RESEARCH_TESTER_IDS"]) if env.get("RESEARCH_TESTER_IDS") else d.testers,
                   chats=_ids(env["RESEARCH_CHAT_IDS"]) if env.get("RESEARCH_CHAT_IDS") else d.chats,
                   max_queue=int(env.get("RESEARCH_MAX_QUEUE", d.max_queue)))


# ---- bot context (what the commands may read from GitClaim) -----------------------------------
@dataclass
class BotContext:
    status: callable = lambda: {}                 # -> dict of scanner health
    facts_for: callable = lambda mint: None       # -> research.prompt.ClaimFacts | None
    github_ids_for: callable = lambda mint: []    # -> GitHub numeric ids linked to fee claims on this mint


# ---- formatting helpers ------------------------------------------------------------------------
def split_message(text: str, limit: int = CHUNK) -> list[str]:
    """Split HTML text on line boundaries. Callers keep tags within single lines."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                   # very long line: hard split outside of tags is the caller's job
            if cur:
                out.append(cur); cur = ""
            out.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def _short(a: str) -> str:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 12 else (a or "?")


def _pct(v) -> str:
    return f"{v:.2f}%" if v < 10 else f"{v:.1f}%"


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def help_text(cfg: CmdConfig | None = None) -> str:
    av = [s for s in REGISTRY if s.operational]
    soon = [s for s in REGISTRY if not s.operational]
    fmt = lambda s: f"/{s.name} /{s.aliases[0]}" if s.aliases else f"/{s.name}"
    L = ["🐙 <b>GitClaim commands</b> (test build)", "", "✅ <b>Available</b>"]
    L += [f"{fmt(s)} — {E(s.summary)}" for s in av]
    L += ["", "Use a CA: <code>/re &lt;CA&gt;</code> · <code>/gh &lt;user|owner/repo|CA&gt;</code>",
          "💬 Or reply to a GitClaim alert with /re /ho /gh /liq (no CA needed).",
          "", "🕒 <b>Coming soon</b>"]
    L.append(" ".join(f"/{s.aliases[0] if s.aliases else s.name}" for s in soon))
    L += ["", "🔬 <code>/d re|dev|wa &lt;CA&gt;</code> deep mode (grok-4.7) — disabled for now",
          "/start subscribe to alerts · /stop unsubscribe"]
    return "\n".join(L)


# ---- service -----------------------------------------------------------------------------------
@dataclass
class _Job:
    chat: int
    mint: str
    key: tuple
    facts: object = None
    enqueued: float = field(default_factory=time.time)
    origin: str | None = None        # reply-to-alert: "stored" | "parsed"; None for /re <CA>
    auto: bool = False               # Phase 6 automatic research job
    stale_at: float | None = None


class CommandService:
    def __init__(self, cfg: CmdConfig, send, ctx: BotContext | None = None, *, adapter_factory=None, http=None,
                 rpc_url: str | None = None, send_reply=None, auto_max_queue: int = 2, durable_helius: bool = False):
        self.cfg, self.send, self.ctx = cfg, send, ctx or BotContext()
        self.send_reply = send_reply                     # (chat, text, reply_to_message_id) -> bool; auto reports
        self._auto_q: deque = deque()                    # Phase 6: auto jobs wait here; manual _q always first
        self.auto_max_queue = auto_max_queue
        self.auto_calls = 0
        if durable_helius:                               # direct Helius reads on Queen's key: reserve first, fail closed
            dd.HELIUS_GUARD[0] = self._helius_guard
        self._reply_ctx = None
        self._adapter_factory = adapter_factory or _default_adapter
        self._adapter = None
        self.http = http or dd.requests
        self.rpc_url = rpc_url
        self._lock = threading.Lock()
        self._inflight: set = set()                      # (chat, command, args) currently queued/running
        self._pool = ThreadPoolExecutor(max_workers=cfg.direct_workers, thread_name_prefix="cmd")
        self._out = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research-out")   # FIFO: ack before result
        self._direct_pending = 0
        self._q: queue.Queue = queue.Queue(maxsize=max(0, cfg.max_queue))
        self._running: _Job | None = None
        self._worker = None
        self.llm_calls = 0                               # research jobs handed to the adapter (tests assert on it)

    def _helius_guard(self, method, credits, source="manual", limits=None) -> bool:
        a = self.adapter()
        return a.ledger.reserve_direct_helius(a.s, method, credits, source, limits)   # raises -> dd fails closed

    _record_helius = _helius_guard                   # name used by bot.main()

    def _helius_ok(self, need: int) -> str | None:
        """Pre-check so /ho doesn't start a partial lookup. Fails CLOSED on ledger errors when enforcement is on."""
        if dd.HELIUS_GUARD[0] is None:
            return None
        try:
            a = self.adapter()
            dec = a.ledger.helius_allowed(a.s, need)
            return None if dec.ok else dec.reason
        except Exception:
            return "ledger unavailable"

    def _budget_precheck(self, source="manual", limits=None) -> str | None:
        """xAI/Helius budget check BEFORE any free context gathering, so a refused job makes no direct Helius calls."""
        try:
            a = self.adapter()
            led, s = getattr(a, "ledger", None), getattr(a, "s", None)
            if led is None or s is None:
                return None                              # test doubles without a ledger
            dec = led.check(settings=s, model=s.standard_model, source=source, auto=limits)
            return None if dec.ok else dec.reason
        except Exception:
            return "ledger unavailable"

    # access ---------------------------------------------------------------
    def allowed(self, chat_id, user_id) -> bool:
        return self.cfg.enabled and chat_id in self.cfg.chats and user_id in self.cfg.testers

    # entry point from the poll thread: must not block -----------------------
    def handle(self, chat_id: int, user_id: int | None, p: Parsed, t_recv: float | None = None, reply=None) -> bool:
        """Returns True when the command was accepted (reply will follow), False when ignored.
        `reply` is an alert_map.Resolution when the command replied to a message (Phase 4), else None."""
        if not self.allowed(chat_id, user_id):
            log.info("cmd /%s ignored (chat=%s user=%s not allowlisted or commands disabled)", p.name, chat_id, user_id)
            return False
        self._reply_ctx = None                  # only read synchronously on the poll thread (captured for /gh)
        if reply is not None and p.known and not p.deep and p.name in REPLY_COMMANDS and not _explicit_target(p):
            if reply.error:
                msg = _choices_text(p.name, reply.choices) if reply.error == "multi" else reply.error
                self._submit_direct(("reply-err", chat_id), lambda: msg, chat_id)
                return True
            p = replace(p, args=(reply.mint,) + tuple(p.args))
            self._reply_ctx = reply
        key = (chat_id, p.name, p.deep, p.args)
        with self._lock:
            if key in self._inflight:
                log.info("cmd /%s duplicate in flight for chat %s; ignored", p.name, chat_id)
                return False
            self._inflight.add(key)
        try:
            self._t_recv = t_recv or time.time()
            return self._dispatch(chat_id, p, key)
        except Exception:
            self._done(key)
            raise

    def _done(self, key):
        with self._lock:
            self._inflight.discard(key)

    def _reply(self, chat, text):
        for part in split_message(text):
            try:
                self.send(chat, part)
            except Exception as e:
                log.warning("cmd reply to %s failed: %s", chat, str(e)[:120])

    def _dispatch(self, chat, p: Parsed, key) -> bool:
        if p.name == DEEP and p.deep and not p.args:
            self._done(key); self._submit_direct(key, lambda: "🔬 Use <code>/d re|dev|wa &lt;CA&gt;</code>. Deep mode (grok-4.7) is disabled for now.", chat)
            return True
        spec = SPECS.get(p.name) if p.known else None
        if spec is None:
            self._done(key)
            self._submit_direct(key, lambda: f"Unknown command /{E(p.name)}. Send /h for the list.", chat)
            return True
        if p.deep and spec.ai:
            why = "Deep mode (grok-4.7) is disabled for now; no AI call was made." if spec.name in DEEP_CAPABLE \
                else f"/d is only for re, dev and wa. /{spec.aliases[0]} has no deep mode."
            self._done(key); self._submit_direct(key, lambda: "🔬 " + why + (f" Use <code>/re &lt;CA&gt;</code> for standard research." if spec.name == "research" else ""), chat)
            return True
        if not spec.operational:
            self._done(key)
            self._submit_direct(key, lambda: f"🕒 /{E(spec.name)} is coming soon. {E(spec.reason)}", chat)
            return True
        if spec.name == "research":
            return self._enqueue_research(chat, p, key)
        handler = getattr(self, "_cmd_" + spec.name)
        note = "ℹ️ Deep analysis doesn't apply to this command; normal result below.\n\n" if p.deep else ""
        rc = self._reply_ctx
        self._submit_direct(key, lambda: note + (handler(p.args, rc) if spec.name == "github" else handler(p.args)), chat, track=True)
        return True

    def _submit_direct(self, key, fn, chat, track=False):
        t_recv, name = getattr(self, "_t_recv", time.time()), key[1]
        with self._lock:
            if self._direct_pending >= 10:
                if track: self._inflight.discard(key)
                busy = True
            else:
                self._direct_pending += 1; busy = False
        if busy:
            self._reply(chat, "⏳ Busy, try again in a moment.")
            return

        def run():
            try:
                t0 = time.time()
                out = fn()
                t1 = time.time()
                self._reply(chat, out)
                log.info("cmd /%s chat=%s recv->reply %dms (handler %dms, send %dms)", name, chat,
                         (time.time() - t_recv) * 1000, (t1 - t0) * 1000, (time.time() - t1) * 1000)
            except Exception as e:
                log.exception("cmd failed: %s", e)
                self._reply(chat, "⚠️ Command failed. Alerts are unaffected.")
            finally:
                with self._lock:
                    self._direct_pending -= 1
                    if track: self._inflight.discard(key)
        self._pool.submit(run)

    # ---- /re ------------------------------------------------------------
    def _enqueue_research(self, chat, p: Parsed, key) -> bool:
        mint = next((a for a in p.args if dd.is_mint(a)), None)
        if not mint:
            self._done(key)
            self._submit_direct(key, lambda: "Send a CA: <code>/re &lt;CA&gt;</code> (base58, 32–44 chars). Or reply <code>/re</code> to a GitClaim alert.", chat)
            return True
        job = _Job(chat, mint, key)
        rc = getattr(self, "_reply_ctx", None)
        if rc is not None and rc.mint == mint:
            job.origin = rc.source
            job.facts = facts_from_meta(rc.meta, mint) if rc.source == "stored" else None
        job.enqueued = getattr(self, "_t_recv", time.time())
        with self._lock:
            busy = self._running is not None
            try:
                self._q.put_nowait(job)
            except queue.Full:
                self._inflight.discard(key)
                full = True
            else:
                full = False
                pos = self._q.qsize() + (1 if busy else 0)
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._research_loop, name="research-worker", daemon=True)
                    self._worker.start()
        if full:
            self._out.submit(self._reply, chat, "⏳ Research is busy (queue full). Try again in a few minutes.")
        elif pos <= 1:
            self._out.submit(self._reply, chat, f"🔎 Researching <code>{E(mint)}</code> with grok-4.3… usually 15–35s.")
        else:
            self._out.submit(self._reply, chat, f"⏳ Queued, position {pos}. Research runs one at a time.")
        return True

    def queue_state(self) -> dict:
        with self._lock:
            return {"running": self._running is not None, "waiting": self._q.qsize(), "max": self.cfg.max_queue}

    def _next_job(self, idle_s: float = 60.0):
        """Manual jobs always first; an auto job runs only when no manual job is waiting."""
        end = time.time() + idle_s
        while True:
            try:
                return self._q.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                while self._auto_q:
                    job = self._auto_q.popleft()
                    if job.stale_at and time.time() > job.stale_at:
                        self._finish_auto(job, "expired", "stale before a slot was free")
                        continue
                    return job
            try:
                return self._q.get(timeout=0.2)
            except queue.Empty:
                if time.time() > end:
                    raise

    def enqueue_auto(self, job) -> tuple[bool, str]:
        """Bounded auto queue. Never blocks; never displaces manual work."""
        with self._lock:
            if len(self._auto_q) >= self.auto_max_queue:
                return False, f"auto queue full ({self.auto_max_queue})"
            self._auto_q.append(job)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._research_loop, name="research-worker", daemon=True)
                self._worker.start()
        return True, "queued"

    def _finish_auto(self, job, status, detail=""):
        cb = getattr(job, "on_done", None)
        if cb:
            try:
                cb(status, detail)
            except Exception as e:
                log.warning("auto job callback failed: %s", type(e).__name__)

    def _research_loop(self):
        while True:
            try:
                job = self._next_job()
            except queue.Empty:
                with self._lock:
                    if self._q.empty() and not self._auto_q:
                        self._worker = None
                        return
                continue
            if getattr(job, "auto", False):
                with self._lock:
                    self._running = job
                try:
                    self._run_auto(job)
                except Exception as e:
                    log.exception("auto research job failed: %s", e)
                    self._finish_auto(job, "failed", type(e).__name__)
                finally:
                    with self._lock:
                        self._running = None
                continue
            with self._lock:
                self._running = job
            try:
                try:
                    out = self._run_research(job)
                except Exception as e:
                    log.exception("research job failed: %s", e)
                    out = "⚠️ Research failed. Alerts are unaffected."
                self._out.submit(self._reply, job.chat, out).result(timeout=120)
                log.info("cmd /research chat=%s recv->reply %dms", job.chat, (time.time() - job.enqueued) * 1000)
            except Exception as e:
                log.warning("research reply failed: %s", str(e)[:120])
            finally:
                with self._lock:
                    self._running = None
                    self._inflight.discard(job.key)

    def _run_auto(self, job):
        """Auto research: same adapter/formatter as /re, source='auto' budget, reply to the original alert."""
        import project_context as pc
        why = self._budget_precheck("auto", job.limits)
        if why:                                          # rejected before ANY context read (no direct Helius calls)
            self._finish_auto(job, "skipped", f"budget: {why}")
            return
        job.mark("running")
        pctx = None
        try:
            with dd.helius_scope("auto", job.limits):
                pctx = pc.gather(job.mint, getattr(job.facts, "github_login", None), http=self.http, rpc_url=self.rpc_url)
        except Exception:
            pass
        self.auto_calls += 1
        res = self.adapter().research(f"re {job.mint}", job.facts, pc.context_block(pctx) if pctx else None,
                                      source="auto", auto=job.limits)
        if not res.ok:
            self._finish_auto(job, "skipped" if res.error in ("budget", "deep_disabled") else "failed",
                              f"{res.error}: {res.text[:120]}")
            return
        text = format_auto_research(job.mint, res, job.facts, pctx, job.alert_msg_id)
        sent = False
        try:
            sent = bool(self.send_reply(job.chat, text, job.alert_msg_id)) if self.send_reply else False
        except Exception as e:
            log.warning("auto report send failed: %s", type(e).__name__)
        cost = (res.usage or {}).get("actual_usd")
        self._finish_auto(job, "completed" if sent else "undelivered",
                          f"cost={cost} cached={res.cached} ms={res.latency_ms}")

    def adapter(self):
        if self._adapter is None:
            self._adapter = self._adapter_factory()
        return self._adapter

    def _run_research(self, job: _Job) -> str:
        facts = job.facts                       # stored verified alert metadata (reply-to-alert) wins
        if facts is None:
            try:
                facts = self.ctx.facts_for(job.mint)
            except Exception:
                facts = None
        import project_context as pc
        pctx = None
        why = self._budget_precheck()
        if why:
            return f"💸 Research paused: budget limit reached ({E(why)}). Alerts are unaffected.\n<code>{E(job.mint)}</code>"
        try:                                                        # free reads, inside the single research slot
            pctx = pc.gather(job.mint, getattr(facts, "github_login", None), http=self.http,
                             rpc_url=self.rpc_url)
            log.info("re context ok=%s failed=%s", ",".join(pctx["sources_ok"]), ",".join(pctx["sources_failed"]))
        except Exception as e:
            log.warning("re context failed: %s", type(e).__name__)
        self.llm_calls += 1
        res = self.adapter().research(f"re {job.mint}", facts, pc.context_block(pctx) if pctx else None)
        return format_research(job.mint, res, facts, pctx, origin=job.origin)

    # ---- direct handlers (no LLM) ------------------------------------------
    def _mint_arg(self, args):
        if not args:
            return None, "Send a CA, e.g. <code>/liq &lt;CA&gt;</code>."
        if not dd.is_mint(args[0]):
            return None, "That doesn't look like a Solana CA (base58, 32–44 chars)."
        return args[0], None

    def _cmd_help(self, args):
        return help_text(self.cfg)

    def _cmd_liquidity(self, args):
        mint, err = self._mint_arg(args)
        if err:
            return err
        d = dd.liquidity(mint, http=self.http)
        return format_liquidity(mint, d)

    def _cmd_holders(self, args):
        mint, err = self._mint_arg(args)
        if err:
            return err
        why = self._helius_ok(4)
        if why:
            return f"💸 Holder lookups paused: research Helius budget reached ({E(why)}). Alerts are unaffected."
        lq = dd.liquidity(mint, http=self.http)
        d = dd.holders(mint, http=self.http, rpc_url=self.rpc_url, pool_hint=lq.get("pair") if lq.get("ok") else None)
        return format_holders(mint, d, lq)

    def _cmd_github(self, args, rc=None):
        if not args:
            return "Send a GitHub user, owner/repo or a CA: <code>/gh NubsCarson</code>"
        a = args[0].strip().removeprefix("https://github.com/").removeprefix("github.com/").strip("/")
        if dd.is_mint(a) and "/" not in a and (len(a) >= 32):
            stored = [str(i) for i in (rc.meta.get("github_ids") or [])] if rc is not None and rc.mint == a and rc.meta else []
            ids = list(dict.fromkeys(stored or self.ctx.github_ids_for(a) or []))
            if not ids:
                return f"🐙 No linked GitHub claim found for <code>{E(_short(a))}</code> (GitClaim has not seen a GitHub fee claim on this coin). Try <code>/re {E(a)}</code> for its narrative."
            blocks = [format_github(dd.github_user(uid=i, http=self.http)) for i in ids[:3]]
            return f"🐙 GitHub linked to fee claims on <code>{E(_short(a))}</code>\n\n" + "\n\n".join(blocks)
        m = dd.GH_REPO_RE.match(a)
        if m:
            return format_github(dd.github_repo(m.group(1), m.group(2), http=self.http))
        if dd.GH_LOGIN_RE.match(a):
            return format_github(dd.github_user(login=a, http=self.http))
        return "Send a GitHub user, owner/repo or a CA."

    def _cmd_status(self, args):
        st = {}
        try:
            st = self.ctx.status() or {}
        except Exception:
            pass
        L = ["📡 <b>GitClaim status</b>"]
        ws = st.get("ws_connected")
        last = f" · last event {int(time.time() - st['last_ws_msg'])}s ago" if st.get("last_ws_msg") else ""
        L.append(("🟢 Scanner live" if ws else "🔴 Scanner websocket down") + last if ws is not None else "⚪ Scanner: n/a")
        if "subscribers" in st:
            L.append(f"👥 Subscribers: {st['subscribers']}")
        q = self.queue_state()
        try:
            from research.client import QueenClient
            from research.config import load_settings
            from research.ledger import Ledger
            s = load_settings()
            up = QueenClient(s.standard_url).health(timeout_s=1.0)
            L.append(f"🧠 Queen (grok-4.3): {'🟢 online' if up else '⚪ offline'} · deep off")
            lg = Ledger(s.ledger_path)
            t = lg.totals(); lg.close()
            L.append(f"💸 AI budget: ${max(0, s.budget_daily_usd - t['usd_today']):.2f} today · "
                     f"${max(0, s.budget_total_usd - t['usd_total']):.2f} total (local ledger)")
            L.append(f"⛽ Queen Helius: ~{int(t['helius_today'])}/{s.helius_daily_credits:,} today (est.)")
        except Exception as e:
            L.append(f"⚠️ Queen/ledger: n/a ({type(e).__name__})")
        L.append(f"⏳ Research queue: {'1 running' if q['running'] else 'idle'}, {q['waiting']}/{q['max']} waiting")
        return "\n".join(L)


def _default_adapter():
    from research.adapter import ResearchAdapter
    return ResearchAdapter()


# ---- output formatters (pure; used by tests and examples) --------------------------------------
def _tx_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def format_liquidity(mint: str, d: dict) -> str:
    if not d.get("ok"):
        return f"⚠️ Liquidity unavailable: {E(d.get('error', 'error'))}"
    if not d.get("pairs"):
        if str(mint).endswith("pump"):                                   # pump.fun mint, no pool yet -> bonding curve
            if d.get("symbol"):
                return f"💧 <b>${E(str(d['symbol']))}</b>: on bonding curve\n🔗 <code>{E(_short(mint))}</code>"
            return f"💧 <code>{E(_short(mint))}</code>: on bonding curve"
        return f"💧 No DexScreener pool found for <code>{E(_short(mint))}</code> (not indexed yet)."
    if str(d.get("dex")).lower() == "pumpfun" or d.get("liq_usd") is None:      # pump.fun bonding curve: no pool liquidity
        link = f'<a href="https://dexscreener.com/solana/{E(d["pair"])}">DexScreener</a> · ' if d.get("pair") else ""
        return "\n".join([f"💧 <b>${E(str(d.get('symbol') or '?'))}</b>: on bonding curve",
                          f"📈 MC {dd.money(d.get('mc'))} · Vol 24h {dd.money(d.get('vol24'))}",
                          f"🔗 {link}<code>{E(_short(mint))}</code>"])
    sol = f" ({d['liq_quote']:,.1f} {E(str(d.get('quote')))})" if d.get("liq_quote") else ""
    dex = {"pumpswap": "PumpSwap", "raydium": "Raydium", "meteora": "Meteora", "orca": "Orca"}.get(str(d.get("dex")), str(d.get("dex")))
    more = f" · {d['pairs']} pools, {dd.money(d.get('total_liq'))} total" if d["pairs"] > 1 else ""
    L = [f"💧 <b>${E(str(d.get('symbol') or '?'))}</b> liquidity: <b>{dd.money(d.get('liq_usd'))}</b>{sol} on {E(dex)}{more}",
         f"📈 MC {dd.money(d.get('mc'))} · Vol 24h {dd.money(d.get('vol24'))}"]
    link = f'<a href="https://dexscreener.com/solana/{E(d["pair"])}">DexScreener</a> · ' if d.get("pair") else ""
    L.append(f"🔗 {link}<code>{E(_short(mint))}</code>")
    return "\n".join(L)


def format_holders(mint: str, d: dict, lq: dict | None = None) -> str:
    if not d.get("ok"):
        return f"⚠️ Holders unavailable: {E(d.get('error', 'error'))}"
    sym = (lq or {}).get("symbol")
    L = [f"👥 <b>{'$' + E(sym) if sym else 'Holders'}</b> top holders · <code>{E(_short(mint))}</code>",
         f"📊 Top 10 wallets {_pct(d['top10_wallet_pct'])} · largest {_pct(d['largest_wallet_pct'])}",
         f"🏊 Pool / curve {_pct(d['pool_pct'])}" if d["pool_pct"] else "🏊 Pool / curve: none in top 20", ""]
    for i, r in enumerate(d["rows"][:10], 1):
        o = r["owner"] or r["account"]
        tag = f" · {E(r['label'])}" if r["label"] else ""
        L.append(f'{i}. <a href="https://solscan.io/account/{E(o)}">{E(_short(o))}</a> {_pct(r["pct"])}{tag}')
    L.append(f"🔎 Helius RPC · {_tx_ts(d['fetched'])} · no AI")
    return "\n".join(L)


def format_github(d: dict) -> str:
    if not d.get("ok"):
        return f"⚠️ {E(d.get('error', 'GitHub error'))}"
    if d["kind"] == "repo":
        L = [f"📦 <b>{E(d['full_name'])}</b>" + (" (fork)" if d.get("fork") else "")]
        if d.get("description"):
            L.append(E(d["description"][:200]))
        L += [f"📅 Created {E(d['created'][:10])} ({d['age']} old) · last push {E((d.get('pushed') or 'n/a')[:10])}",
              f"⭐ {d['stars']} · 🍴 {d['forks']} · {E(str(d.get('language') or 'n/a'))}",
              f"🧾 Commits {d['commits'] if d['commits'] is not None else 'n/a'} · contributors {d['contributors'] if d['contributors'] is not None else 'n/a'}",
              f'🔗 <a href="{E(d["url"])}">GitHub</a>']
        return "\n".join(L)
    young = d["age"].endswith("d") and int(d["age"][:-1] or 0) < 180
    flag = "\n⚠️ new or empty account" if (d.get("repos") or 0) == 0 or young else ""
    L = [f"🐙 <b>{E(d['login'])}</b>" + (f" ({E(d['name'])})" if d.get("name") else "") + (f" · {E(d['type'])}" if d.get("type") not in (None, "User") else ""),
         f"👤 {d['followers']} followers · {d['repos']} repos · {d['age']} old (since {E((d.get('created') or '')[:10])}){flag}"]
    rec = d.get("recent") or []
    if rec:
        L.append("🛠 Recent: " + ", ".join(f"{E(r['name'].split('/', 1)[-1])}{' (fork)' if r.get('fork') else ''}" for r in rec)
                 + f" · pushed {E((rec[0].get('pushed') or '')[:10])}")
    L.append(f'🔗 <a href="{E(d["url"])}">GitHub</a>')
    return "\n".join(L)


_SEC = re.compile(r"^\W*(NARRATIVE|VERDICT|DATA)[*_\s]*[:\-–]?[*_\s]*(.*)$", re.I)


def _split_answer(text: str) -> dict:
    out, cur = {"NARRATIVE": [], "VERDICT": [], "DATA": [], "_pre": []}, "_pre"
    for line in text.split("\n"):
        m = _SEC.match(line)
        if m and m.group(1).upper() in out:
            cur = m.group(1).upper()
            if m.group(2).strip():
                out[cur].append(m.group(2).strip())
        elif line.strip():
            out[cur].append(line.strip())
    return out


def format_research(mint: str, res, facts=None, pctx=None, origin=None) -> str:
    from research.formatter import preserve_addresses, sanitize_urls
    if not res.ok:
        msg = {"budget": "💸 ", "queen_unavailable": "⚪ "}.get(res.error or "", "⚠️ ")
        extra = "\nQueen is offline; start it with research/queen/start_queen.sh standard." if res.error == "queen_unavailable" else ""
        return f"{msg}{E(res.text)}{extra}\n<code>{E(mint)}</code>"
    trusted = facts.addresses() if facts is not None else [mint]
    raw = res.answer.answer if getattr(res, "answer", None) is not None else res.text
    raw = re.sub(r"\n?```token\n.*?\n```\n?", "\n", raw, flags=re.S)
    urep = sanitize_urls(raw, trusted)
    arep = preserve_addresses(urep.text, trusted)
    parts = _split_answer(scrub_ai_text(arep.text))
    narrative = " ".join(parts["NARRATIVE"]) or " ".join([x for x in parts["_pre"] if not x.startswith(("-", "•", "*"))][:3])
    verdict = " ".join(parts["VERDICT"])
    data = [x.lstrip("-•* ").strip() for x in parts["DATA"] if x.lstrip().startswith(("-", "•", "*"))] or \
           [x.lstrip("-•* ").strip() for x in parts["_pre"] if x.lstrip().startswith(("-", "•"))]
    sym = (facts.symbol if facts is not None and facts.symbol else None)
    flags = " · cached" if res.cached else ""
    L = [f"🔎 <b>{E(sym) if sym else 'Research'}</b> · <code>{E(_short(mint))}</code>{flags}", "",
         *([ORIGIN_NOTE[origin](facts)] if origin in ORIGIN_NOTE else []),
         "🧠 <b>AI take</b> <i>(interpretation)</i>", E(narrative if len(narrative) <= 450 else narrative[:447].rsplit(" ", 1)[0] + "…") if narrative else "No narrative returned."]
    if verdict:
        L.append(f"⚠️ {E(verdict[:240])}")
    if facts is None or not (facts.github_login or facts.claim_sol):
        L += ["", "✅ No GitHub claim seen by GitClaim"]
    else:
        L += ["", "✅ <b>GitClaim facts</b>"]
        who = facts.github_login or "?"
        L.append(f"🐙 {E(who)}" + (f" ({E(facts.github_display)})" if facts.github_display else "")
                 + (f" · {E(facts.github_profile)}" if facts.github_profile else ""))
        if facts.claim_sol is not None:
            part = f" of {facts.claim_total_sol:.2f} across {facts.claim_coins} coins" if (facts.claim_coins or 1) > 1 and facts.claim_total_sol else ""
            tags = " · " + " · ".join(E(t) for t in facts.signer_tags) if facts.signer_tags else ""
            L.append(f"💸 {facts.claim_sol:.2f} SOL{part} · {E(facts.claim_kind or 'claim')}{tags}")
    if data:
        L += ["", "📊 <b>Key data</b> <i>(Queen's tools, not re-verified)</i>"] + [f"• {E(x if len(x) <= 160 else x[:157].rsplit(' ', 1)[0] + '…')}" for x in data[:5]]
    links = [f'<a href="https://solscan.io/token/{E(mint)}">Solscan</a>', f'<a href="https://rugcheck.xyz/tokens/{E(mint)}">RugCheck</a>',
             f'<a href="https://dexscreener.com/solana/{E(mint)}">DexScreener</a>']
    for lab, u in ((pctx or {}).get("links") or [])[:3]:          # structured URLs only, original case
        links.append(f'<a href="{E(u)}">{E(lab)}</a>')
    if facts is not None and facts.tx_signature:
        links.append(f'<a href="https://solscan.io/tx/{E(facts.tx_signature)}">Claim tx</a>')
    L += ["", "🔗 " + " · ".join(links)]
    notes = []
    if urep.dropped:
        notes.append(f"{len(urep.dropped)} link omitted (unverified)")
    if arep.unverified:
        notes.append(f"{len(arep.unverified)} AI-only address unverified")
    if notes:
        L.append("ℹ️ " + " · ".join(notes))
    u = res.usage or {}
    meta = [f"🤖 {E(res.plan.model) if res.plan else 'grok-4.3'}"]
    if u.get("actual_usd") is not None:
        meta.append(f"${u['actual_usd']:.4f}")
    if res.latency_ms:
        meta.append(f"{res.latency_ms / 1000:.1f}s")
    L.append(" · ".join(meta))
    return "\n".join(L)


# ---- Phase 4: reply-to-alert helpers ---------------------------------------------------------
from alert_map import REPLY_COMMANDS  # noqa: E402

ORIGIN_NOTE = {
    "stored": lambda f: "📎 <i>From the replied-to alert (GitClaim's stored record).</i>\n",
    "parsed": lambda f: ("📎 <i>Older alert: CA read from the alert message; claim facts "
                         + ("from GitClaim's own log." if f is not None and (f.github_login or f.claim_sol) else "unavailable (not verified).")
                         + "</i>\n"),
}


def _explicit_target(p) -> bool:
    """An explicit argument always beats reply context (/gh also accepts a login or owner/repo)."""
    if p.name == "github":
        return bool(p.args)
    return any(dd.is_mint(a) for a in p.args) or bool(p.args)


def _choices_text(name: str, mints) -> str:
    alias = {"research": "re", "holders": "ho", "github": "gh", "liquidity": "liq"}.get(name, name)
    rows = "\n".join(f"• <code>/{alias} {E(m)}</code>" for m in list(mints)[:8])
    return f"🤔 That alert lists {len(mints)} tokens. Which one?\n{rows}"


def facts_from_meta(meta: dict | None, mint: str):
    """ClaimFacts from the metadata GitClaim stored when it SENT the alert (verified at send time)."""
    from research.prompt import ClaimFacts
    f = dict((meta or {}).get("facts") or {})
    if not f or f.get("mint") != mint:
        return None
    allowed = set(ClaimFacts.__dataclass_fields__)
    f = {k: v for k, v in f.items() if k in allowed}
    for k in ("signer_tags", "claimant_wallets"):
        if k in f:
            f[k] = tuple(f[k] or ())
    try:
        return ClaimFacts(**f)
    except TypeError:
        return None


# ---- Phase 5: scrub AI text of local/secret-looking details before it reaches Telegram ------------
_SCRUB = [
    (re.compile(r"(?:/Users|/home|/private/var|/var/folders|/tmp)/[^\s<>\"')]+"), "[local path]"),
    (re.compile(r"[A-Za-z]:\\(?:Users|Windows)\\[^\s<>\"')]+"), "[local path]"),
    (re.compile(r"\bxai-[A-Za-z0-9]{10,}"), "[redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"), "[redacted]"),
    (re.compile(r"(?i)api[-_]?key=[^\s&<>\"']+"), "api-key=[redacted]"),
    (re.compile(r"\b[A-Z][A-Z0-9_]*_(?:API_KEYS?|KEY|TOKEN|SECRET|PASSWORD)S?\b(?:\s*=\s*\S+)?"), "[config name]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
]


def scrub_ai_text(text: str) -> str:
    """AI text only (verified GitClaim facts are rendered separately). Never touches Solana addresses or https URLs."""
    for rx, rep in _SCRUB:
        text = rx.sub(rep, text)
    return text


# ---- Phase 6: automatic research report -------------------------------------------------------
AUTO_DISCLAIMER = "ℹ️ <i>Automatic research. Not a trading recommendation or a safety verdict.</i>"


def format_auto_research(mint: str, res, facts=None, pctx=None, alert_msg_id=None) -> str:
    """The /re report (same sanitizers + scrubber) with an auto header, alert reference and disclaimer."""
    body = format_research(mint, res, facts, pctx)
    lines = body.split("\n")
    name = E(facts.symbol) if facts is not None and facts.symbol else "Token"
    head = [f"🧠 <b>Queen Auto Research</b> · {name}", f"<code>{E(mint)}</code>",
            "↩️ <i>Follow-up to the claim alert above.</i>" if alert_msg_id else "↩️ <i>Follow-up to a GitClaim claim alert.</i>"]
    rest = lines[1:]                                    # drop the /re header line (symbol + short CA)
    foot = rest.pop() if rest else ""
    return "\n".join(head + rest + [AUTO_DISCLAIMER, foot])
