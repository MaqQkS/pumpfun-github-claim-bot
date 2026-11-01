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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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


class CommandService:
    def __init__(self, cfg: CmdConfig, send, ctx: BotContext | None = None, *, adapter_factory=None, http=None,
                 rpc_url: str | None = None):
        self.cfg, self.send, self.ctx = cfg, send, ctx or BotContext()
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

    # access ---------------------------------------------------------------
    def allowed(self, chat_id, user_id) -> bool:
        return self.cfg.enabled and chat_id in self.cfg.chats and user_id in self.cfg.testers

    # entry point from the poll thread: must not block -----------------------
    def handle(self, chat_id: int, user_id: int | None, p: Parsed) -> bool:
        """Returns True when the command was accepted (reply will follow), False when ignored."""
        if not self.allowed(chat_id, user_id):
            log.info("cmd /%s ignored (chat=%s user=%s not allowlisted or commands disabled)", p.name, chat_id, user_id)
            return False
        key = (chat_id, p.name, p.deep, p.args)
        with self._lock:
            if key in self._inflight:
                log.info("cmd /%s duplicate in flight for chat %s; ignored", p.name, chat_id)
                return False
            self._inflight.add(key)
        try:
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
        self._submit_direct(key, lambda: note + handler(p.args), chat, track=True)
        return True

    def _submit_direct(self, key, fn, chat, track=False):
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
                self._reply(chat, fn())
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
            self._submit_direct(key, lambda: "Send a CA: <code>/re &lt;CA&gt;</code> (base58, 32–44 chars). Reply-to-alert comes later.", chat)
            return True
        job = _Job(chat, mint, key)
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

    def _research_loop(self):
        while True:
            try:
                job = self._q.get(timeout=60)
            except queue.Empty:
                with self._lock:
                    if self._q.empty():
                        self._worker = None
                        return
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
            except Exception as e:
                log.warning("research reply failed: %s", str(e)[:120])
            finally:
                with self._lock:
                    self._running = None
                    self._inflight.discard(job.key)

    def adapter(self):
        if self._adapter is None:
            self._adapter = self._adapter_factory()
        return self._adapter

    def _run_research(self, job: _Job) -> str:
        try:
            facts = self.ctx.facts_for(job.mint)
        except Exception:
            facts = None
        self.llm_calls += 1
        res = self.adapter().research(f"re {job.mint}", facts)      # never raises; budget + Queen checks inside
        return format_research(job.mint, res)

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
        lq = dd.liquidity(mint, http=self.http)
        d = dd.holders(mint, http=self.http, rpc_url=self.rpc_url, pool_hint=lq.get("pair") if lq.get("ok") else None)
        return format_holders(mint, d, lq)

    def _cmd_github(self, args):
        if not args:
            return "Send a GitHub user, owner/repo or a CA: <code>/gh NubsCarson</code>"
        a = args[0].strip().removeprefix("https://github.com/").removeprefix("github.com/").strip("/")
        if dd.is_mint(a) and "/" not in a and (len(a) >= 32):
            ids = list(dict.fromkeys(self.ctx.github_ids_for(a) or []))
            if not ids:
                return f"No GitHub account is linked to a fee claim GitClaim has seen for <code>{E(a)}</code>."
            blocks = [format_github(dd.github_user(uid=i, http=self.http)) for i in ids[:3]]
            return f"🐙 GitHub linked to fee claims on <code>{E(a)}</code>\n\n" + "\n\n".join(blocks)
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
        L.append(f"• Scanner: {'🟢 websocket connected' if ws else '🔴 websocket down'}" if ws is not None else "• Scanner: n/a")
        if st.get("last_ws_msg"):
            L.append(f"• Last stream event: {int(time.time() - st['last_ws_msg'])}s ago")
        if st.get("last_alert"):
            L.append(f"• Last alert: {_utc(st['last_alert'])}")
        if "subscribers" in st:
            L.append(f"• Subscribers: {st['subscribers']}")
        if st.get("helius_calls") is not None:
            L.append(f"• Scanner Helius calls since start: {st['helius_calls']:,}")
        q = self.queue_state()
        try:
            from research.client import QueenClient
            from research.config import load_settings
            from research.ledger import Ledger
            s = load_settings()
            up = QueenClient(s.standard_url).health()
            L.append(f"• Queen (grok-4.3): {'🟢 online' if up else '⚪ offline (start on demand)'} · deep: off")
            lg = Ledger(s.ledger_path)
            t = lg.totals(); lg.close()
            L.append(f"• AI budget: ${max(0, s.budget_daily_usd - t['usd_today']):.2f} left today · "
                     f"${max(0, s.budget_total_usd - t['usd_total']):.2f} left total (local ledger)")
            L.append(f"• Queen Helius: ~{int(t['helius_today'])} credits today / {s.helius_daily_credits:,} cap (estimate)")
        except Exception as e:
            L.append(f"• Queen/ledger: n/a ({type(e).__name__})")
        L.append(f"• Research queue: {'1 running' if q['running'] else 'idle'}, {q['waiting']}/{q['max']} waiting")
        direct = sum(v for k, v in dd.CALLS.items() if k.startswith("helius:"))
        L.append(f"• Command Helius calls (Queen key): {direct}")
        return "\n".join(L)


def _default_adapter():
    from research.adapter import ResearchAdapter
    return ResearchAdapter()


# ---- output formatters (pure; used by tests and examples) --------------------------------------
def _tx_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_liquidity(mint: str, d: dict) -> str:
    if not d.get("ok"):
        return f"⚠️ Liquidity unavailable: {E(d.get('error', 'error'))}"
    if not d.get("pairs"):
        return f"💧 No DexScreener pairs for <code>{E(mint)}</code> yet (still on the bonding curve or not indexed)."
    sym = E(str(d.get("symbol") or "?"))
    L = [f"💧 <b>${sym}</b> liquidity", f"<code>{E(mint)}</code>", "",
         f"• Price: ${E(str(d.get('price_usd') or 'n/a'))} · 24h {d['chg24']:+.1f}%" if d.get("chg24") is not None else f"• Price: ${E(str(d.get('price_usd') or 'n/a'))}",
         f"• MC: {dd.money(d.get('mc'))} · FDV: {dd.money(d.get('fdv'))}",
         f"• Liquidity: {dd.money(d.get('liq_usd'))} on {E(str(d.get('dex')))}" +
         (f" ({d['liq_quote']:,.1f} {E(str(d.get('quote')))})" if d.get("liq_quote") else ""),
         f"• Vol 24h: {dd.money(d.get('vol24'))}"]
    if d["pairs"] > 1:
        L.append(f"• {d['pairs']} pairs, total liquidity {dd.money(d.get('total_liq'))}")
    if d.get("pair"):
        L.append(f'🔗 <a href="https://dexscreener.com/solana/{E(d["pair"])}">DexScreener</a>')
    L.append(f"Source: DexScreener API · {_tx_ts(d['fetched'])} · no AI")
    return "\n".join(L)


def format_holders(mint: str, d: dict, lq: dict | None = None) -> str:
    if not d.get("ok"):
        return f"⚠️ Holders unavailable: {E(d.get('error', 'error'))}"
    sym = (lq or {}).get("symbol")
    L = [f"👥 <b>{'$' + E(sym) if sym else 'Holders'}</b> top holders", f"<code>{E(mint)}</code>", "",
         f"• Top 10 wallets: {_pct(d['top10_wallet_pct'])} · largest {_pct(d['largest_wallet_pct'])}",
         f"• Pool / curve: {_pct(d['pool_pct'])}" if d["pool_pct"] else "• Pool / curve: none identified in top 20", ""]
    for i, r in enumerate(d["rows"][:10], 1):
        o = r["owner"] or r["account"]
        tag = f" · {E(r['label'])}" if r["label"] else ""
        L.append(f'{i}. <a href="https://solscan.io/account/{E(o)}">{E(_short(o))}</a> {_pct(r["pct"])}{tag}')
    L.append(f"Source: Helius RPC (getTokenLargestAccounts) · {_tx_ts(d['fetched'])} · no AI")
    return "\n".join(L)


def format_github(d: dict) -> str:
    if not d.get("ok"):
        return f"⚠️ {E(d.get('error', 'GitHub error'))}"
    if d["kind"] == "repo":
        L = [f"📦 <b>{E(d['full_name'])}</b>" + (" (fork)" if d.get("fork") else "")]
        if d.get("description"):
            L.append(E(d["description"][:200]))
        L += [f"• Created {E(d['created'][:10])} ({d['age']} old) · last push {E((d.get('pushed') or 'n/a')[:10])}",
              f"• ⭐ {d['stars']} · forks {d['forks']} · {E(str(d.get('language') or 'n/a'))}",
              f"• Commits: {d['commits'] if d['commits'] is not None else 'n/a'} · contributors: {d['contributors'] if d['contributors'] is not None else 'n/a'}",
              f'🔗 <a href="{E(d["url"])}">GitHub</a> · Source: GitHub API · no AI']
        return "\n".join(L)
    flag = " · ⚠️ new/empty acct" if (d.get("repos") or 0) == 0 or d["age"].endswith("d") and int(d["age"][:-1] or 0) < 180 else ""
    L = [f"👤 <b>{E(d['login'])}</b>" + (f" ({E(d['name'])})" if d.get("name") else "") + (f" · {E(d['type'])}" if d.get("type") not in (None, "User") else ""),
         f"• {d['followers']} followers · {d['repos']} repos · {d['age']} old (since {E((d.get('created') or '')[:10])}){flag}"]
    for r in d.get("recent") or []:
        L.append(f"• {E(r['name'])}{' (fork)' if r.get('fork') else ''} · pushed {E((r.get('pushed') or '')[:10])} · ⭐ {r.get('stars')}")
    L.append(f'🔗 <a href="{E(d["url"])}">GitHub</a> · Source: GitHub API · no AI')
    return "\n".join(L)


def format_research(mint: str, res) -> str:
    if not res.ok:
        msg = {"budget": "💸 ", "queen_unavailable": "⚪ "}.get(res.error or "", "⚠️ ")
        extra = "\nQueen is offline; start it with research/queen/start_queen.sh standard." if res.error == "queen_unavailable" else ""
        return f"{msg}{E(res.text)}{extra}\n<code>{E(mint)}</code>"
    flags = " (cached)" if res.cached else (" (shared with an identical request)" if res.deduped else "")
    head = f"🔎 <b>Research</b>{flags}\n<code>{E(mint)}</code>\n"
    body = []
    text = re.sub(r"\n?```token\n.*?\n```\n?", "\n", res.text, flags=re.S)   # Queen UI card JSON: not for Telegram
    for line in text.split("\n"):
        esc = E(line)
        if line[:3] in ("A. ", "B. ", "C. ") or line in ("Limitations",):
            esc = f"<b>{esc}</b>"
        body.append(esc)
    return head + "\n" + "\n".join(body)
