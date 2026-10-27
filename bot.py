#!/usr/bin/env python3
"""pump.fun GitHub social-fee FIRST-claim alert bot."""
import asyncio, html, re, base64, json, logging, os, struct, sys, threading, time
import base58, requests, websockets

HELIUS = os.environ["HELIUS_API_KEY"]; TG = os.environ["TELEGRAM_BOT_TOKEN"]
RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS}"
WSS = os.environ.get("LOGS_WSS") or f"wss://mainnet.helius-rpc.com/?api-key={HELIUS}"
WSS_NAME = "custom" if os.environ.get("LOGS_WSS") else "helius"
# Every social-fee claim is co-signed by pump.fun's social_claim_authority. Subscribing to logs that mention it
# (instead of the whole fee program, which is CPI'd by EVERY pump.fun trade, ~1 MB/s) cuts the stream to ~claims only.
WS_MENTION = os.environ.get("WS_MENTION", "2sMrGNK8i36YRkF5WWCwnaUYuwDJhHe1g2xA8aPvhkjM")
WS_BYTES = [0]
FEE_GLOBAL = "CHqnuTkj6sXDFknM652aEFPECZh9qVsBXWkhPohmV9dA"  # PDA ["fee-program-global"]
TGAPI = f"https://api.telegram.org/bot{TG}"
FEE_PROG = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
PUMP_PROG = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
EVENT_CPI = bytes.fromhex("e445a52e51cb9a1d")
EV_CLAIMED = bytes.fromhex("3212c141edd2eaec")
DISTRIBUTE = bytes.fromhex("a572670079cef751")
DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "state.json")
log = logging.getLogger("bot")
_lock = threading.Lock()

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {"subscribers": [], "tg_offset": 0, "seen": []}
def save_state(s):
    with _lock:
        tmp = STATE + ".tmp"; json.dump(s, open(tmp, "w")); os.replace(tmp, STATE)
state = load_state()

from collections import Counter
CALLS = Counter()          # per-method Helius call counter (logged hourly)
CREDIT_COST = {"getProgramAccounts": 10, "getAsset": 10}
def rpc(method, params, tries=3):
    for i in range(tries):
        CALLS[method] += 1
        try:
            r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
            if r.status_code == 429 or r.status_code >= 500: raise IOError(f"HTTP {r.status_code}")
            j = r.json()
            if "error" in j:  # deterministic RPC error: do not retry
                log.warning("rpc %s error (no retry): %s", method, str(j["error"])[:160]); CALLS[method + ":error"] += 1
                return None
            return j["result"]
        except Exception as e:
            log.warning("rpc %s failed (%s), retry %d", method, str(e)[:120], i); time.sleep(2 * (i + 1))
    return None

def log_usage_loop():
    start = time.time()
    while True:
        time.sleep(3600)
        credits = sum(n * CREDIT_COST.get(m, 1) for m, n in CALLS.items() if ":" not in m)
        log.info("USAGE since start (%.1fh): %s | est RPC credits=%d, ws bytes=%d (~%d credits)",
                 (time.time() - start) / 3600, dict(CALLS), credits, WS_BYTES[0], WS_BYTES[0] * 20 // 1_000_000)

def get_tx(sig):
    for _ in range(4):  # tx may not be queryable for a moment after the log
        t = rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
        if t: return t
        time.sleep(2)
    return None

def tx_keys(t):
    m = t["transaction"]["message"]; la = t["meta"].get("loadedAddresses") or {}
    return m["accountKeys"] + la.get("writable", []) + la.get("readonly", [])

def all_ix(t):
    m = t["transaction"]["message"]
    yield from m["instructions"]
    for g in t["meta"].get("innerInstructions") or []: yield from g["instructions"]

def decode_claims(t):
    keys = tx_keys(t); out = []
    for ix in all_ix(t):
        if keys[ix["programIdIndex"]] != FEE_PROG: continue
        raw = base58.b58decode(ix["data"])
        if raw[:8] != EVENT_CPI or raw[8:16] != EV_CLAIMED: continue
        e = raw[16:]; o = 0
        ts = struct.unpack_from("<q", e, o)[0]; o += 8
        n = struct.unpack_from("<I", e, o)[0]; o += 4; uid = e[o:o+n].decode(); o += n
        plat = e[o]; o += 1
        pk = lambda: base58.b58encode(e[o:o+32]).decode()
        pda = pk(); o += 32; rec = pk(); o += 32; auth = pk(); o += 32
        amt, before, life = struct.unpack_from("<QQQ", e, o)
        out.append(dict(ts=ts, user_id=uid, platform=plat, pda=pda, recipient=rec, amount=amt, lifetime=life))
    return out

def find_mint_in_tx(t, pda):
    keys = tx_keys(t)
    for ix in all_ix(t):
        if keys[ix["programIdIndex"]] != PUMP_PROG: continue
        if base58.b58decode(ix["data"])[:8] != DISTRIBUTE: continue
        accts = [keys[a] for a in ix["accounts"]]
        if pda in accts: return accts[0]
    return None

def resolve_mint(t, sig, pda):
    m = find_mint_in_tx(t, pda)
    if m: return m
    sigs = rpc("getSignaturesForAddress", [pda, {"limit": 10, "before": sig, "commitment": "confirmed"}]) or []
    for s in sigs[:8]:
        if t.get("blockTime") and s.get("blockTime") and t["blockTime"] - s["blockTime"] > 600: break
        pt = get_tx(s["signature"])
        if pt and (m := find_mint_in_tx(pt, pda)): return m
    ms = vault_mints(pda)
    if len(ms) == 1: return ms[0]
    if ms: log.info("vault %s in %d sharing configs, mint ambiguous", pda[:8], len(ms))
    return None

SHARING_DISC = base58.b58encode(bytes.fromhex("d84a0900388c5d4b")).decode()
_vm = {}
def vault_mints(pda, max_slots=10):
    """Mints whose SharingConfig lists this vault (getProgramAccounts, 10 credits each). Cached 10 min."""
    if pda in _vm and time.time() - _vm[pda][0] < 600: return _vm[pda][1]
    mints = []
    for i in range(max_slots):
        res = rpc("getProgramAccounts", [FEE_PROG, {"encoding": "base64", "dataSlice": {"offset": 11, "length": 32},
              "filters": [{"memcmp": {"offset": 0, "bytes": SHARING_DISC}}, {"memcmp": {"offset": 80 + 34 * i, "bytes": pda}}]}])
        if res is None: break
        for a in res:
            m = base58.b58encode(base64.b64decode(a["account"]["data"][0])).decode()
            if m not in mints: mints.append(m)
        if not res and i >= 1 and not mints: break   # vault typically in slot 0/1; stop early when nothing found
    _vm[pda] = (time.time(), mints)
    return mints

_gh = {}
def github_info(uid):
    if uid in _gh: return _gh[uid]
    try:
        r = requests.get(f"https://api.github.com/user/{uid}", timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.ok:
            j = r.json(); _gh[uid] = {k: j.get(k) for k in ("login", "name", "created_at", "followers", "public_repos", "type")}
            return _gh[uid]
    except Exception as e: log.warning("github lookup failed: %s", e)
    return {}

def github_login(uid):
    g = github_info(uid); return (g.get("login"), g.get("name"))

def github_stats(uid):
    g = github_info(uid)
    if not g.get("created_at"): return "GitHub stats n/a"
    import datetime as _d
    age = (_d.datetime.now(_d.timezone.utc) - _d.datetime.fromisoformat(g["created_at"].replace("Z", "+00:00"))).days
    age_s = f"{age/365:.1f}y" if age >= 365 else f"{age}d"
    flag = " · new/empty acct" if age < 180 or (g.get("public_repos") or 0) == 0 else ""
    return f"{g.get('followers')} followers · {g.get('public_repos')} repos · {age_s} old{flag}"

def compact(v):
    if v is None: return "n/a"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if v >= div:
            s = f"{v/div:.1f}".rstrip("0").rstrip(".") if v / div < 100 else f"{v/div:.0f}"
            return f"${s}{suf}"
    return f"${v:.0f}"

_creator = {}
def coin_info(mint):
    sym = mc = ath = None
    try:
        r = requests.get(f"https://frontend-api-v3.pump.fun/coins-v2/{mint}", timeout=10)
        if r.ok:
            d = r.json(); _creator[mint] = d.get("creator"); sym = d.get("symbol"); mc = d.get("usd_market_cap") or d.get("market_cap_usd"); ath = d.get("ath_market_cap")
    except Exception as e: log.warning("pump api: %s", e)
    if not sym or mc is None:
        try:
            p = requests.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}", timeout=10).json()
            if p:
                sym = sym or p[0]["baseToken"]["symbol"]; mc = mc if mc is not None else (p[0].get("marketCap") or p[0].get("fdv"))
        except Exception as e: log.warning("dexscreener: %s", e)
    if not sym:
        a = rpc("getAsset", {"id": mint})
        if a: sym = (a.get("content", {}).get("metadata") or {}).get("symbol")
    return sym or "UNKNOWN", mc, ath

E = html.escape
MIN_FIRST_CLAIM_SOL = float(os.environ.get("MIN_FIRST_CLAIM_SOL", "0.3"))

def _label(kind):
    k = (kind or "").lower()
    if "first" in k: return "🆕 FIRST CLAIM" + (" (ever)" if "ever" in k else "")
    if "repeat" in k: return "🔁 REPEAT CLAIM"
    return "🐙 " + (kind or "CLAIM").upper()

def _token_block(sym, mint, mc, ath):
    ca = f"<code>{E(mint)}</code>" if mint else "(mint not resolved)"
    athv = E(compact(ath)) if ath else "n/a"
    return f"${E(str(sym))}\n{ca}\n\n🧠 Token\n• MC: {E(compact(mc))} | ATH: {athv}"

def _hdr(sym, mint, mc, ath):  # kept for compatibility
    return _token_block(sym, mint, mc, ath)

AXIOM_REF = os.environ.get("AXIOM_REF", "maq")
PUMP_REF = os.environ.get("PUMP_REF", "RoqFIG")

def _links(mint, gh, sig):
    parts = []
    if mint: parts.append(f'<a href="https://axiom.trade/t/{E(mint)}/@{E(AXIOM_REF)}?chain=sol">Axiom</a>')
    if mint: parts.append(f'<a href="https://pump.fun/coin/{E(mint)}?ref={E(PUMP_REF)}">pump.fun</a>')
    parts.append(f'<a href="{E(gh)}">GitHub</a>')
    parts.append(f'<a href="https://solscan.io/tx/{E(sig)}">Tx</a>')
    return "🔗 " + " · ".join(parts)

def format_alert(c, mint, sig, kind="FIRST claim ever"):
    """Returns Telegram HTML."""
    login, name = github_login(c["user_id"])
    who = (login + (f" ({name})" if name else "")) if login else f"id {c['user_id']}"
    gh = f"https://github.com/{login}" if login else f"https://api.github.com/user/{c['user_id']}"
    share = c.get("share")
    amt = f"{(share if share is not None else c['amount'])/1e9:.2f} SOL"
    sweep = ""
    if share is not None and c["amount"] - share > 5_000_000:
        n = c.get("ncoins", 1)
        sweep = f"\n   ↳ part of a {c['amount']/1e9:.2f} SOL claim across {n} coin{'s' if n != 1 else ''}"
    flags = []
    if "unverified" in (kind or ""): flags.append("unverified")
    flags += list(c.get("tags", []))
    rec = c["recipient"]
    if not mint:
        cands = vault_mints(c["pda"])
        if cands:
            blocks = [_token_block(*(lambda r: (r[0], m, r[1], r[2]))(coin_info(m))) for m in cands[:5]]
            flags.append(f"token ambiguous ({len(cands)} fee configs)")
            out = [f"{_label(kind)}", "\n\n".join(blocks), "", f"💸 Claim: {amt} by {E(who)}{E(sweep)}", f"👤 Dev: {E(github_stats(c['user_id']))}",
                   "⚠️ " + " · ".join(E(x) for x in flags), "", _links(None, gh, sig)]
            return "\n".join(out)
    sym, mc, ath = coin_info(mint) if mint else ("UNKNOWN", None, None)
    if mint and _creator.get(mint) == rec: flags.append("self-funded (deployer)")
    elif c.get("sc_admin") == rec: flags.append("self-funded (fee admin)")
    tb = _token_block(sym, mint, mc, ath).split("\n", 1)
    out = [f"{_label(kind)} · {tb[0]}", tb[1], "", f"💸 Claim: {amt} by {E(who)}{E(sweep)}", f"👤 Dev: {E(github_stats(c['user_id']))}"]
    if flags: out.append("⚠️ " + " · ".join(E(x) for x in flags))
    out += ["", _links(mint, gh, sig)]
    return "\n".join(out)

def html_to_plain(t):
    return html.unescape(re.sub(r"<a href=\"([^\"]*)\">([^<]*)</a>", r"\2: \1", re.sub(r"</?code>", "", t)))

def tg_send_all(text):
    for chat in list(state["subscribers"]):
        try:
            r = requests.post(f"{TGAPI}/sendMessage", json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
            if r.status_code == 403:
                state["subscribers"].remove(chat); save_state(state); log.info("removed blocked chat %s", chat); continue
            if not r.ok:
                log.warning("tg HTML send %s failed: %s; retrying plain", chat, r.text[:200])
                r = requests.post(f"{TGAPI}/sendMessage", json={"chat_id": chat, "text": html_to_plain(text), "disable_web_page_preview": True}, timeout=15)
                if not r.ok: log.warning("tg plain send %s: %s", chat, r.text[:200])
        except Exception as e: log.warning("tg send error: %s", e)

def tg_reply(chat, text):
    try: requests.post(f"{TGAPI}/sendMessage", json={"chat_id": chat, "text": text}, timeout=15)
    except Exception as e: log.warning("tg reply %s: %s", chat, e)

def tg_handle_update(u):
    kinds = [k for k in u if k != "update_id"]
    msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
    chat = (msg.get("chat") or {}).get("id")
    txt = (msg.get("text") or "").strip()
    mcm = u.get("my_chat_member")
    if mcm:  # user pressed Start / blocked the bot
        chat = mcm["chat"]["id"]; status = mcm.get("new_chat_member", {}).get("status")
        log.info("update %s my_chat_member chat=%s status=%s", u["update_id"], chat, status)
        if status in ("kicked", "left"):
            if chat in state["subscribers"]: state["subscribers"].remove(chat); log.info("UNSUBSCRIBED %s (blocked)", chat)
            return
        txt = "/start"
    log.info("update %s kinds=%s chat=%s text=%r", u["update_id"], kinds, chat, txt[:40])
    if not chat: return
    if txt.lower().startswith("/stop"):
        if chat in state["subscribers"]: state["subscribers"].remove(chat)
        log.info("UNSUBSCRIBED %s", chat); tg_reply(chat, "Unsubscribed. Send /start to resubscribe."); return
    if chat not in state["subscribers"]:
        state["subscribers"].append(chat); log.info("SUBSCRIBED %s (total %d)", chat, len(state["subscribers"]))
        tg_reply(chat, "✅ Subscribed. You'll get an alert when a GitHub-linked account claims pump.fun fees for the FIRST time. /stop to unsubscribe.")
    elif txt.lower().startswith("/start"):
        tg_reply(chat, "✅ Already subscribed. /stop to unsubscribe.")

def tg_poll():
    log.info("tg poll started (offset %s, %d subscribers)", state["tg_offset"], len(state["subscribers"]))
    while True:
        try:
            r = requests.get(f"{TGAPI}/getUpdates", params={"offset": state["tg_offset"], "timeout": 25,
                             "allowed_updates": json.dumps(["message", "edited_message", "my_chat_member", "channel_post"])}, timeout=40)
            j = r.json()
            if not j.get("ok"):
                log.warning("tg getUpdates not ok: %s %s", r.status_code, j.get("description")); time.sleep(10); continue
            for u in j.get("result", []):
                try: tg_handle_update(u)
                except Exception as e: log.exception("tg update %s failed: %s", u.get("update_id"), e)
                state["tg_offset"] = u["update_id"] + 1
                save_state(state)
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as e:
            log.warning("tg poll: %s", str(e)[:200]); time.sleep(5)

def tg_supervisor():
    while True:
        try: tg_poll()
        except BaseException as e: log.exception("tg poll crashed: %s", e)
        time.sleep(5)

LINK_WINDOW = 300          # distribute counts as "claimed from mint" if within 5 min before a claim
HIST_MAX_TX = int(os.environ.get("HIST_MAX_TX", "150"))   # cap getTransaction calls per history scan

SYS_PROG = "11111111111111111111111111111111"
def dist_amounts(t, pda):
    """{mint: lamports} moved into this vault by pump.fun distribute ixs in one tx (from the inner system transfers)."""
    keys = tx_keys(t); m = t["transaction"]["message"]; res = {}
    groups = {g["index"]: g["instructions"] for g in (t["meta"].get("innerInstructions") or [])}
    for i, ix in enumerate(m["instructions"]):
        cur = curh = None
        for j, x in enumerate([ix] + groups.get(i, [])):
            h = 1 if j == 0 else (x.get("stackHeight") or 2)
            if cur and h <= curh: cur = None
            p = keys[x["programIdIndex"]]; raw = base58.b58decode(x["data"]); accts = [keys[a] for a in x["accounts"]]
            if p == PUMP_PROG and raw[:8] == DISTRIBUTE and pda in accts:
                cur, curh = accts[0], h; res.setdefault(cur, 0)
            elif cur and p == SYS_PROG and raw[:4] == b"\x02\x00\x00\x00" and len(accts) > 1 and accts[1] == pda:
                res[cur] += struct.unpack_from("<Q", raw, 4)[0]
    return res

def tx_events(t, pda):
    """(distributed mints for this vault, is_claim) for one tx."""
    keys = tx_keys(t); mints = []; claim = False
    for ix in all_ix(t):
        p = keys[ix["programIdIndex"]]; raw = base58.b58decode(ix["data"])
        if p == PUMP_PROG and raw[:8] == DISTRIBUTE:
            accts = [keys[a] for a in ix["accounts"]]
            if pda in accts: mints.append(accts[0])
        elif p == FEE_PROG and raw[:8] == EVENT_CPI and raw[8:16] == EV_CLAIMED:
            claim = True
    return mints, claim

def vault_history(pda, before_sig):
    """Scan the vault's txs older than before_sig (cached per vault). Returns cache dict."""
    if state.get("vault_hist_v") != 2: state["vault_hist"] = {}; state["vault_hist_v"] = 2
    vh = state.setdefault("vault_hist", {}).setdefault(pda, {"newest": None, "oldest": None, "complete": False, "dists": [], "claims": []})
    budget = HIST_MAX_TX
    def scan(params):
        nonlocal budget
        sigs_all = []
        while True:
            page = rpc("getSignaturesForAddress", [pda, dict(limit=1000, commitment="confirmed", **params)]) or []
            sigs_all += page
            if len(page) < 1000: return sigs_all, True
            params = {**params, "before": page[-1]["signature"]}
            if len(sigs_all) >= 5000: return sigs_all, False
    def ingest(sigs):
        nonlocal budget
        done = 0
        for s in sigs:
            if s.get("err"): done += 1; continue
            if budget <= 0: break
            t = get_tx(s["signature"]); budget -= 1; done += 1
            if not t: continue
            mints, claim = tx_events(t, pda); bt = t.get("blockTime") or 0
            amts = dist_amounts(t, pda)
            for m in dict.fromkeys(mints): vh["dists"].append([m, bt, amts.get(m, 0)])
            if claim: vh["claims"].append(bt)
        return done
    # newer-than-cache part (between cached newest and the current tx)
    if vh["newest"]:
        sigs, _ = scan({"before": before_sig, "until": vh["newest"]})
        n = ingest(sigs)
        if n < len(sigs): vh["complete"] = False   # gap left; mark partial
        if sigs and n == len(sigs): vh["newest"] = sigs[0]["signature"]
    else:
        sigs, full = scan({"before": before_sig})
        n = ingest(sigs)
        vh["newest"] = before_sig
        vh["complete"] = full and n == len(sigs)
        vh["oldest"] = sigs[n-1]["signature"] if n else None
    log.info("vault %s history: %d dists, %d claims, complete=%s, tx fetched=%d", pda[:8], len(vh["dists"]), len(vh["claims"]), vh["complete"], HIST_MAX_TX - budget)
    save_state(state)
    return vh

def claimed_mint_before(vh, mint):
    for m, bt, *_ in vh["dists"]:
        if m == mint and any(0 <= c - bt <= LINK_WINDOW for c in vh["claims"]): return True
    return False

def evaluate(sig, t, c):
    """A claim sweeps the whole vault, which can hold fees from many coins. Returns
    [(kind, mint, key, share_lamports_or_None)] with each coin's share of this claim."""
    pairs = state.setdefault("pairs", [])
    first_ever = c["amount"] == c["lifetime"] and c["amount"] > 0
    bt = t.get("blockTime") or 0
    shares = dict(dist_amounts(t, c["pda"]))
    vh = vault_history(c["pda"], sig)
    prev = max([x for x in vh["claims"] if x < bt] or [0])
    for d in vh["dists"]:
        if len(d) >= 3 and prev < d[1] <= bt: shares[d[0]] = shares.get(d[0], 0) + d[2]
    shares = {m: v for m, v in shares.items() if v > 0}
    if not shares:   # couldn't see where the money came from: old single-coin guess, amount = whole claim
        mint = resolve_mint(t, sig, c["pda"]); key = f'{c["user_id"]}:{mint}'
        if first_ever: return [("FIRST claim ever", mint, key, None)]
        if not mint or key in pairs or claimed_mint_before(vh, mint): return [(None, mint, key, None)]
        return [("FIRST claim on this token" if vh["complete"] else "FIRST claim on this token (unverified)", mint, key, None)]
    out = []
    for mint, lam in sorted(shares.items(), key=lambda kv: -kv[1]):
        key = f'{c["user_id"]}:{mint}'
        if key in pairs: kind = None
        elif first_ever: kind = "FIRST claim ever"
        elif claimed_mint_before(vh, mint): kind = None
        else: kind = "FIRST claim on this token" if vh["complete"] else "FIRST claim on this token (unverified)"
        out.append((kind, mint, key, lam))
    return out

AUTHORITY = "2sMrGNK8i36YRkF5WWCwnaUYuwDJhHe1g2xA8aPvhkjM"
def enrich(t, c, mint, record=True):
    keys = tx_keys(t); ns = t["transaction"]["message"]["header"]["numRequiredSignatures"]; signers = keys[:ns]
    tags = []
    if signers[0] == AUTHORITY: tags.append("app claim")
    elif c["recipient"] in signers: tags.append("automated signer")
    sc = None
    for ix in all_ix(t):
        if keys[ix["programIdIndex"]] == PUMP_PROG and base58.b58decode(ix["data"])[:8] == DISTRIBUTE and c["pda"] in [keys[a] for a in ix["accounts"]]:
            sc = keys[ix["accounts"][2]]
    if sc:
        r = rpc("getAccountInfo", [sc, {"encoding": "base64", "dataSlice": {"offset": 43, "length": 32}}])
        if r and r.get("value"): c["sc_admin"] = base58.b58encode(base64.b64decode(r["value"]["data"][0])).decode()
    elif mint and c.get("share") is None: tags.append("token inferred")
    now = time.time(); uc = state.setdefault("user_claims", {})
    hist = [x for x in uc.get(c["user_id"], []) if now - x < 86400]
    if record: hist.append(now); uc[c["user_id"]] = hist[-200:]
    if len(hist) >= 5: tags.append(f"{len(hist)} claims/24h")
    if c["lifetime"] >= 1000e9 and c["amount"] != c["lifetime"]: tags.append(f"vault lifetime {c['lifetime']/1e9:,.0f} SOL")
    c["tags"] = tags

def muted_set():
    """GitHub ids/logins to mute: env MUTED_GITHUB (comma list) + state['muted_github']. Default: UsePaid (id 322216527)."""
    raw = os.environ.get("MUTED_GITHUB", "322216527,UsePaid") + "," + ",".join(state.get("muted_github", []))
    return {x.strip().lower() for x in raw.split(",") if x.strip()}

def is_muted(uid):
    m = muted_set()
    if str(uid).lower() in m: return True
    login = github_login(uid)[0]
    return bool(login) and login.lower() in m

def handle_sig(sig, send=True, prefix=""):
    if send and sig in state["seen"]: return None
    t = get_tx(sig)
    if not t or t["meta"].get("err"): return None
    msgs = []
    for c in decode_claims(t):
        if c["platform"] != 2: continue
        res = evaluate(sig, t, c)
        if send:
            uc = state.setdefault("user_claims", {}); uc[c["user_id"]] = ([x for x in uc.get(c["user_id"], []) if time.time() - x < 86400] + [time.time()])[-200:]
        hits = []
        for kind, mint, key, lam in res:
            share = (lam if lam is not None else c["amount"]) / 1e9
            log.info("claim %s user=%s mint=%s share=%.3f amt=%.3f lifetime=%.3f coins=%d -> %s", sig[:12], c["user_id"], mint, share, c["amount"]/1e9, c["lifetime"]/1e9, len(res), kind)
            if send and mint and key not in state["pairs"]: state["pairs"].append(key)
            if not kind: continue
            if "first" in kind.lower() and share < MIN_FIRST_CLAIM_SOL:
                log.info("BELOW MIN claim %s user=%s mint=%s share=%.3f < %.2f (%s)", sig[:12], c["user_id"], mint, share, MIN_FIRST_CLAIM_SOL, kind); continue
            if is_muted(c["user_id"]):
                log.info("MUTED claim %s user=%s mint=%s (%s)", sig[:12], c["user_id"], mint, kind); continue
            hits.append((kind, mint, lam))
        if hits:   # one message per claim: biggest coin up top, any other qualifying coins listed under it
            kind, mint, lam = hits[0]
            cc = dict(c, share=lam, ncoins=len(res))
            enrich(t, cc, mint, record=False)
            if len(res) >= 5: cc["tags"].append(f"{len(res)} coins in one claim")
            text = format_alert(cc, mint, sig, kind)
            if len(hits) > 1:
                extra = []
                for k2, m2, l2 in hits[1:8]:
                    sym2 = coin_info(m2)[0]
                    extra.append(f'• ${E(str(sym2))} {l2/1e9:.2f} SOL · <a href="https://axiom.trade/t/{E(m2)}/@{E(AXIOM_REF)}?chain=sol">Axiom</a> · <code>{E(m2)}</code>')
                if len(hits) > 8: extra.append(f"• +{len(hits)-8} more")
                text = text.replace("\n\n🔗 ", "\n\n➕ Also first claims:\n" + "\n".join(extra) + "\n\n🔗 ", 1)
            text = prefix + text; msgs.append(text); log.info("ALERT\n%s", text)
            if send: tg_send_all(text)
    if send:
        state["seen"] = (state["seen"] + [sig])[-2000:]
    save_state(state)
    return msgs

def authority_check_loop():
    """Every 6h verify pump.fun's social_claim_authority is still WS_MENTION (1 credit)."""
    while True:
        res = rpc("getAccountInfo", [FEE_GLOBAL, {"encoding": "base64"}])
        if res and res.get("value"):
            data = base64.b64decode(res["value"]["data"][0])
            if base58.b58decode(WS_MENTION) not in data:
                log.error("social_claim_authority CHANGED: %s not in fee_program_global. Update WS_MENTION!", WS_MENTION)
                tg_send_all("⚠️ GitClaimed bot: pump.fun social claim authority changed; claim detection may be blind. Check the bot.")
            else: log.info("authority check ok")
        time.sleep(6 * 3600)

async def ws_loop():
    backoff = 1
    while True:
        try:
            async with websockets.connect(WSS, ping_interval=30, ping_timeout=20, max_size=2**22) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                          "params": [{"mentions": [WS_MENTION]}, {"commitment": "confirmed"}]}))
                log.info("ws connected (%s), logs mentioning %s", WSS_NAME, WS_MENTION); backoff = 1
                async for raw in ws:
                    WS_BYTES[0] += len(raw)
                    v = json.loads(raw).get("params", {}).get("result", {}).get("value")
                    if not v or v.get("err"): continue
                    if not any("Instruction: ClaimSocialFeePda" in l for l in v.get("logs", [])): continue
                    asyncio.get_running_loop().run_in_executor(None, handle_sig, v["signature"])
        except Exception as e:
            log.warning("ws error: %s; reconnect in %ss", str(e)[:200], backoff)
        await asyncio.sleep(backoff); backoff = min(backoff * 2, 60)

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(DIR, "bot.log")), logging.StreamHandler()])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if len(sys.argv) > 2 and sys.argv[1] == "--replay":
        msgs = handle_sig(sys.argv[2], send=False, prefix="🧪 TEST (replay of past claim)\n")
        if not msgs: print("NO ALERT for", sys.argv[2][:12]); return
        for text in msgs:
            print(text)
            if "--send" in sys.argv and state["subscribers"]: tg_send_all(text); print("sent to", len(state["subscribers"]))
        return
    threading.Thread(target=tg_supervisor, daemon=True).start()
    threading.Thread(target=log_usage_loop, daemon=True).start()
    threading.Thread(target=authority_check_loop, daemon=True).start()
    asyncio.run(ws_loop())

if __name__ == "__main__": main()
