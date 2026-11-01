"""Tier 0 data for Telegram commands: direct API reads, no LLM. Cached, counted, never raises to callers.

- DexScreener (free, no key)            -> /liquidity
- Helius standard RPC on QUEEN's key     -> /holders  (never GitClaim's production key)
- GitHub REST API (unauthenticated)      -> /github
Secrets are read programmatically and never logged.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
GH_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
GH_REPO_RE = re.compile(r"^([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})$")
PROGRAM_LABELS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun bonding curve",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap pool",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM pool",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM pool",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2 pool",
}
CALLS = Counter()          # direct-command API calls since bot start (Helius counts are credits ~= calls here)
_lock = threading.Lock()
_cache: dict = {}


def is_mint(s: str | None) -> bool:
    return bool(s) and bool(MINT_RE.match(s))


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = fn()
    if val is not None:
        with _lock:
            _cache[key] = (now + ttl, val)
    return val


def money(v) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return f"${v/div:.1f}{suf}".replace(".0" + suf, suf)
    return f"${v:,.2f}" if abs(v) < 10 else f"${v:,.0f}"


# ---- DexScreener --------------------------------------------------------------------------------
def dex_pairs(mint: str, http=requests):
    def fetch():
        CALLS["dexscreener"] += 1
        r = http.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}", timeout=6)
        if not r.ok:
            return None
        j = r.json()
        return j if isinstance(j, list) else []
    try:
        return _cached(("dex", mint), 60, fetch)
    except Exception:
        return None


def liquidity(mint: str, http=requests) -> dict:
    pairs = dex_pairs(mint, http)
    if pairs is None:
        return {"ok": False, "error": "DexScreener unavailable"}
    pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint] or pairs
    if not pairs:
        return {"ok": True, "pairs": 0}
    pairs.sort(key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0))
    p = pairs[0]
    return {"ok": True, "pairs": len(pairs), "symbol": (p.get("baseToken") or {}).get("symbol"),
            "name": (p.get("baseToken") or {}).get("name"), "dex": p.get("dexId"), "pair": p.get("pairAddress"),
            "price_usd": p.get("priceUsd"), "mc": p.get("marketCap"), "fdv": p.get("fdv"),
            "liq_usd": (p.get("liquidity") or {}).get("usd"), "liq_base": (p.get("liquidity") or {}).get("base"),
            "liq_quote": (p.get("liquidity") or {}).get("quote"), "quote": (p.get("quoteToken") or {}).get("symbol"),
            "vol24": (p.get("volume") or {}).get("h24"), "chg24": (p.get("priceChange") or {}).get("h24"),
            "total_liq": sum(((x.get("liquidity") or {}).get("usd") or 0) for x in pairs),
            "created": p.get("pairCreatedAt"), "fetched": time.time()}


# ---- Helius (Queen's key) ------------------------------------------------------------------------
def queen_rpc_url() -> str | None:
    """First key from HELIUS_API_KEYS in Queen's .env (or QUEEN_HELIUS_API_KEY). Never GitClaim's key."""
    key = os.environ.get("QUEEN_HELIUS_API_KEY")
    if not key:
        path = Path(os.environ.get("QUEEN_ENV_FILE", Path.home() / "queen-harness" / ".env"))
        try:
            for line in path.read_text().splitlines():
                if line.startswith("HELIUS_API_KEYS="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'").split(",")[0].strip()
                    break
        except OSError:
            key = None
    return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else None


def _rpc(url, method, params, http=requests):
    CALLS["helius:" + method] += 1
    r = http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=8)
    if not r.ok:
        raise IOError(f"HTTP {r.status_code}")
    j = r.json()
    if "error" in j:
        raise IOError(str(j["error"].get("message", "rpc error"))[:80])
    return j["result"]


def holders(mint: str, http=requests, rpc_url: str | None = None, pool_hint: str | None = None) -> dict:
    url = rpc_url or queen_rpc_url()
    if not url:
        return {"ok": False, "error": "Queen Helius key not configured"}

    def fetch():
        sup = _rpc(url, "getTokenSupply", [mint], http)["value"]
        big = _rpc(url, "getTokenLargestAccounts", [mint, {"commitment": "confirmed"}], http)["value"][:20]
        accts = [b["address"] for b in big]
        infos = _rpc(url, "getMultipleAccounts", [accts, {"encoding": "jsonParsed"}], http)["value"] if accts else []
        owners = [(((i or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner") for i in infos]
        uniq = [o for o in dict.fromkeys(owners) if o]
        oinfo = _rpc(url, "getMultipleAccounts", [uniq, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}], http)["value"] if uniq else []
        prog = {o: (i or {}).get("owner") for o, i in zip(uniq, oinfo)}
        return {"supply": float(sup["uiAmountString"]), "rows": [
            {"account": b["address"], "owner": o, "amount": float(b["uiAmountString"]), "owner_program": prog.get(o)}
            for b, o in zip(big, owners)], "fetched": time.time()}
    try:
        d = _cached(("ho", mint), 120, fetch)
    except Exception as e:
        return {"ok": False, "error": f"Helius error ({str(e)[:60]})"}
    rows, supply = d["rows"], d["supply"] or 1
    for r in rows:
        r["pct"] = 100 * r["amount"] / supply
        lab = PROGRAM_LABELS.get(r["owner_program"] or "")
        if not lab and pool_hint and r["owner"] == pool_hint:
            lab = "DEX pool (DexScreener pair)"
        r["label"] = lab
    wallets = [r for r in rows if not r["label"]]
    return {"ok": True, "supply": supply, "rows": rows, "top10_wallet_pct": sum(r["pct"] for r in wallets[:10]),
            "largest_wallet_pct": wallets[0]["pct"] if wallets else 0.0,
            "pool_pct": sum(r["pct"] for r in rows if r["label"]), "fetched": d["fetched"]}


# ---- GitHub -----------------------------------------------------------------------------------
def _gh(path, http=requests):
    def fetch():
        CALLS["github"] += 1
        r = http.get("https://api.github.com" + path, timeout=6, headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            return {"_missing": True}
        if not r.ok:
            return None
        return {"_json": r.json(), "_link": r.headers.get("Link", "")}
    try:
        return _cached(("gh", path), 600, fetch)
    except Exception:
        return None


def _age(iso: str | None) -> str:
    if not iso:
        return "n/a"
    d = (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days
    return f"{d/365:.1f}y" if d >= 365 else f"{d}d"


def _count_from_link(res) -> int | None:
    if not res or res.get("_missing"):
        return None
    m = re.search(r'[?&]page=(\d+)>; rel="last"', res.get("_link") or "")
    if m:
        return int(m.group(1))
    j = res.get("_json")
    return len(j) if isinstance(j, list) else None


def github_user(login: str | None = None, uid: int | str | None = None, http=requests) -> dict:
    res = _gh(f"/user/{uid}" if uid else f"/users/{login}", http)
    if res is None:
        return {"ok": False, "error": "GitHub API unavailable (rate limit or network)"}
    if res.get("_missing"):
        return {"ok": False, "error": "GitHub account not found"}
    j = res["_json"]
    out = {"ok": True, "kind": "user", "login": j.get("login"), "name": j.get("name"), "id": j.get("id"),
           "type": j.get("type"), "created": j.get("created_at"), "age": _age(j.get("created_at")),
           "followers": j.get("followers"), "repos": j.get("public_repos"), "url": j.get("html_url")}
    rr = _gh(f"/users/{j.get('login')}/repos?sort=pushed&per_page=3", http)
    if rr and isinstance(rr.get("_json"), list):
        out["recent"] = [{"name": r.get("full_name"), "pushed": r.get("pushed_at"), "stars": r.get("stargazers_count"),
                          "fork": r.get("fork")} for r in rr["_json"][:3]]
    return out


def github_repo(owner: str, repo: str, http=requests) -> dict:
    res = _gh(f"/repos/{owner}/{repo}", http)
    if res is None:
        return {"ok": False, "error": "GitHub API unavailable (rate limit or network)"}
    if res.get("_missing"):
        return {"ok": False, "error": "Repository not found"}
    j = res["_json"]
    commits = _count_from_link(_gh(f"/repos/{owner}/{repo}/commits?per_page=1", http))
    contrib = _count_from_link(_gh(f"/repos/{owner}/{repo}/contributors?per_page=1&anon=1", http))
    return {"ok": True, "kind": "repo", "full_name": j.get("full_name"), "created": j.get("created_at"),
            "age": _age(j.get("created_at")), "pushed": j.get("pushed_at"), "stars": j.get("stargazers_count"),
            "forks": j.get("forks_count"), "fork": j.get("fork"), "language": j.get("language"),
            "commits": commits, "contributors": contrib, "url": j.get("html_url"), "description": j.get("description")}
