#!/usr/bin/env python3
"""pump.fun GitHub social-fee FIRST-claim alert bot."""
import asyncio, base64, json, logging, os, struct, sys, threading, time
import base58, requests, websockets

HELIUS = os.environ["HELIUS_API_KEY"]; TG = os.environ["TELEGRAM_BOT_TOKEN"]
RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS}"
WSS = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS}"
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

def rpc(method, params, tries=4):
    for i in range(tries):
        try:
            r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
            j = r.json()
            if "error" in j: raise RuntimeError(j["error"])
            return j["result"]
        except Exception as e:
            log.warning("rpc %s failed (%s), retry %d", method, str(e)[:120], i); time.sleep(1.5 * (i + 1))
    return None

def get_tx(sig):
    for _ in range(6):  # tx may not be queryable for a moment after the log
        t = rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
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
    sigs = rpc("getSignaturesForAddress", [pda, {"limit": 6, "before": sig}]) or []
    for s in sigs[:4]:
        if t.get("blockTime") and s.get("blockTime") and t["blockTime"] - s["blockTime"] > 300: break
        pt = get_tx(s["signature"])
        if pt and (m := find_mint_in_tx(pt, pda)): return m
    return None

_gh = {}
def github_login(uid):
    if uid in _gh: return _gh[uid]
    try:
        r = requests.get(f"https://api.github.com/user/{uid}", timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.ok: _gh[uid] = (r.json()["login"], r.json().get("name")); return _gh[uid]
    except Exception as e: log.warning("github lookup failed: %s", e)
    return (None, None)

def compact(v):
    if v is None: return "n/a"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if v >= div:
            s = f"{v/div:.1f}".rstrip("0").rstrip(".") if v / div < 100 else f"{v/div:.0f}"
            return f"${s}{suf}"
    return f"${v:.0f}"

def coin_info(mint):
    sym = mc = ath = None
    try:
        r = requests.get(f"https://frontend-api-v3.pump.fun/coins-v2/{mint}", timeout=10)
        if r.ok:
            d = r.json(); sym = d.get("symbol"); mc = d.get("usd_market_cap") or d.get("market_cap_usd"); ath = d.get("ath_market_cap")
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

def format_alert(c, mint, sig):
    login, name = github_login(c["user_id"])
    sym, mc, ath = coin_info(mint) if mint else ("UNKNOWN", None, None)
    who = f"{login}" + (f" ({name})" if name else "") if login else f"GitHub id {c['user_id']}"
    gh = f"https://github.com/{login}" if login else f"https://api.github.com/user/{c['user_id']}"
    lines = [f"${sym}", mint or "(mint not resolved)", f"• MC: {compact(mc)} ATH: {compact(ath)[1:] if ath else 'n/a'}",
             f"• 🐙 FIRST claim by GitHub {who}: {c['amount']/1e9:.2f} SOL",
             (f"pump.fun: https://pump.fun/coin/{mint}\n" if mint else "") + f"GitHub: {gh}\nTx: https://solscan.io/tx/{sig}"]
    return "\n".join(lines)

def tg_send_all(text):
    for chat in list(state["subscribers"]):
        try:
            r = requests.post(f"{TGAPI}/sendMessage", json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
            if r.status_code == 403:
                state["subscribers"].remove(chat); save_state(state); log.info("removed blocked chat %s", chat)
            elif not r.ok: log.warning("tg send %s: %s", chat, r.text[:200])
        except Exception as e: log.warning("tg send error: %s", e)

def tg_poll():
    while True:
        try:
            r = requests.get(f"{TGAPI}/getUpdates", params={"offset": state["tg_offset"], "timeout": 50}, timeout=60).json()
            for u in r.get("result", []):
                state["tg_offset"] = u["update_id"] + 1
                msg = u.get("message") or {}; txt = (msg.get("text") or "").strip(); chat = msg.get("chat", {}).get("id")
                if not chat: continue
                if txt.startswith("/start"):
                    if chat not in state["subscribers"]: state["subscribers"].append(chat)
                    reply = "✅ Subscribed. You'll get an alert when a GitHub-linked account claims pump.fun fees for the FIRST time. /stop to unsubscribe."
                elif txt.startswith("/stop"):
                    if chat in state["subscribers"]: state["subscribers"].remove(chat)
                    reply = "Unsubscribed."
                else: continue
                save_state(state); log.info("chat %s: %s", chat, txt.split()[0])
                requests.post(f"{TGAPI}/sendMessage", json={"chat_id": chat, "text": reply}, timeout=15)
            save_state(state)
        except Exception as e:
            log.warning("tg poll: %s", e); time.sleep(5)

def handle_sig(sig, send=True):
    if sig in state["seen"]: return None
    t = get_tx(sig)
    if not t or t["meta"].get("err"): return None
    msgs = []
    for c in decode_claims(t):
        first = c["amount"] == c["lifetime"] and c["amount"] > 0
        log.info("claim %s user=%s amt=%.3f lifetime=%.3f first=%s", sig[:12], c["user_id"], c["amount"]/1e9, c["lifetime"]/1e9, first)
        if not first or c["platform"] != 2: continue
        mint = resolve_mint(t, sig, c["pda"])
        text = format_alert(c, mint, sig); msgs.append(text); log.info("ALERT\n%s", text)
        if send: tg_send_all(text)
    if send:
        state["seen"] = (state["seen"] + [sig])[-2000:]; save_state(state)
    return msgs

async def ws_loop():
    backoff = 1
    while True:
        try:
            async with websockets.connect(WSS, ping_interval=30, ping_timeout=20, max_size=2**22) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                          "params": [{"mentions": [FEE_PROG]}, {"commitment": "confirmed"}]}))
                log.info("ws connected, subscribed to %s logs", FEE_PROG); backoff = 1
                async for raw in ws:
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
        sig = sys.argv[2]; t = get_tx(sig)
        for c in decode_claims(t):
            text = format_alert(c, resolve_mint(t, sig, c["pda"]), sig)
            text = "🧪 TEST (replay of past claim)\n" + text
            print(text)
            if state["subscribers"]: tg_send_all(text); print("sent to", len(state["subscribers"]), "subscriber(s)")
            else: print("(no subscribers yet; not sent)")
        return
    threading.Thread(target=tg_poll, daemon=True).start()
    asyncio.run(ws_loop())

if __name__ == "__main__": main()
