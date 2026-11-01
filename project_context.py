"""Free project context for /re: what the coin is about and what the GitHub-linked claimant builds.

Gathered by Python (not by the model's tool choice), short timeouts, cached, never raises.
Sources: DexScreener token info (websites/socials), Helius getAsset on QUEEN's key (same read path as /ho)
+ the token's metadata JSON (description/website/twitter/telegram), website <title>/<meta description>
(no JS), and the claimant's public GitHub repos. Optional read-only GITHUB_TOKEN from research/research.env.
Everything here is UNVERIFIED project self-description; it is labeled so in the prompt.
"""
from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

import requests

import direct_data as dd

MAX_DESC = 300
MAX_SNIPPET = 300
MAX_REPOS = 5
MAX_BLOCK = 1800          # hard cap on the context block (chars) so prompt cost stays bounded
GH_STATUS = {"rate_remaining": None, "authed": False}


def github_token() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip() or None
    try:
        from research.config import HERE, _parse_env_file
        return (_parse_env_file(HERE / "research.env").get("GITHUB_TOKEN") or "").strip() or None
    except Exception:
        return None


def clean_url(u) -> str | None:
    """Keep a structured URL exactly as given (original case) if it is a well-formed public http(s) URL."""
    if not isinstance(u, str):
        return None
    u = u.strip()
    if not u or len(u) > 200 or any(c in u for c in ' <>"\'\n\r\t'):
        return None
    try:
        p = urlparse(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
        return None
    return u


def _public_host(host: str) -> bool:
    try:
        for info in socket.getaddrinfo(host, None)[:4]:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


def _text(s, cap) -> str | None:
    if not isinstance(s, str):
        return None
    s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()
    if not s:
        return None
    return s if len(s) <= cap else s[: cap - 1].rsplit(" ", 1)[0] + "…"


def _get(http, url, timeout=5, **kw):
    dd.CALLS["context"] += 1
    return http.get(url, timeout=timeout, **kw)


_CID = re.compile(r"(?:ipfs://|/ipfs/)([A-Za-z0-9]{46,64})")


def _ipfs(uri: str) -> str:
    return "https://ipfs.io/ipfs/" + uri[7:] if uri.startswith("ipfs://") else uri


def _uri_candidates(uri: str) -> list[str]:
    """ipfs.io/dweb.link now 429 for API clients; fall back to Pinata's public gateway for the same CID."""
    out = []
    m = _CID.search(uri or "")
    if m:
        out.append(f"https://gateway.pinata.cloud/ipfs/{m.group(1)}")
    u = clean_url(_ipfs(uri or ""))
    if u and "ipfs.io" not in u and "dweb.link" not in u and _public_host(urlparse(u).hostname):
        out.insert(0, u)
    return out[:2]


def metadata(mint: str, http=requests, rpc_url: str | None = None) -> dict:
    """Helius getAsset (Queen's key) -> metadata JSON. Returns {} on any failure."""
    url = rpc_url or dd.queen_rpc_url()
    if not url:
        return {}

    def fetch():
        a = dd._rpc(url, "getAsset", {"id": mint}, http)
        c = (a or {}).get("content") or {}
        md = dict(c.get("metadata") or {})
        uri = c.get("json_uri") or ""
        for u in _uri_candidates(uri):
            try:
                r = _get(http, u, timeout=6, stream=True)
                if not r.ok:
                    continue
                body = r.raw.read(20000, decode_content=True) if getattr(r, "raw", None) else r.content[:20000]
                import json
                j = json.loads(body)
                if isinstance(j, dict):
                    md.update({k: j.get(k) for k in ("description", "website", "twitter", "telegram") if j.get(k)})
                    break
            except Exception:
                continue
        return md
    try:
        return dd._cached(("meta", mint), 3600, fetch) or {}
    except Exception:
        return {}


def website_snippet(url: str, http=requests) -> str | None:
    url = clean_url(url)
    if not url or not _public_host(urlparse(url).hostname):
        return None

    def fetch():
        r = _get(http, url, timeout=5, stream=True, headers={"User-Agent": "Mozilla/5.0 GitClaim/1.0"}, allow_redirects=True)
        if not r.ok or "html" not in (r.headers.get("Content-Type") or "html"):
            return ""
        body = (r.raw.read(60000, decode_content=True) if getattr(r, "raw", None) else r.content[:60000])
        body = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        t = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        d = (re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']*)', body, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\'](?:og:)?description', body, re.I))
        parts = [x for x in (_text(t.group(1), 120) if t else None, _text(d.group(1), 240) if d else None) if x]
        return _text(" — ".join(parts), MAX_SNIPPET) or ""
    try:
        return dd._cached(("site", url), 3600, fetch) or None
    except Exception:
        return None


def github_repos(login: str, http=requests) -> dict:
    if not login or not dd.GH_LOGIN_RE.match(login):
        return {"ok": False, "error": "no login"}
    tok = github_token()
    hdr = {"Accept": "application/vnd.github+json"}
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"

    def fetch():
        r = _get(http, f"https://api.github.com/users/{login}/repos?sort=pushed&per_page=10&type=owner", timeout=6, headers=hdr)
        GH_STATUS["authed"] = bool(tok)
        rem = r.headers.get("X-RateLimit-Remaining") if hasattr(r, "headers") else None
        GH_STATUS["rate_remaining"] = int(rem) if rem and str(rem).isdigit() else None
        if r.status_code in (403, 429):
            return {"ok": False, "error": "rate_limited"}
        if not r.ok:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        rows = []
        for x in r.json() if isinstance(r.json(), list) else []:
            rows.append({"name": x.get("name"), "description": _text(x.get("description"), 140),
                         "language": x.get("language"), "stars": x.get("stargazers_count"),
                         "fork": bool(x.get("fork")), "pushed": (x.get("pushed_at") or "")[:10],
                         "homepage": clean_url(x.get("homepage"))})
        rows.sort(key=lambda x: (x["fork"], ))          # owned first, keep pushed order otherwise
        return {"ok": True, "repos": rows[:MAX_REPOS]}
    try:
        return dd._cached(("ghrepos", login), 900, fetch) or {"ok": False, "error": "unavailable"}
    except Exception:
        return {"ok": False, "error": "unavailable"}


def gather(mint: str, github_login: str | None = None, http=requests, rpc_url: str | None = None) -> dict:
    """-> {"description", "website", "links": [(label,url)], "site_snippet", "repos", "sources_ok", "sources_failed"}"""
    ok, failed, links = [], [], []
    desc = website = None
    pairs = dd.dex_pairs(mint, http)
    if pairs is None:
        failed.append("dexscreener")
    else:
        ok.append("dexscreener")
        for p in pairs:
            info = p.get("info") or {}
            for w in info.get("websites") or []:
                u = clean_url((w or {}).get("url"))
                if u:
                    links.append(((w.get("label") or "Website")[:20], u))
            for s in info.get("socials") or []:
                u = clean_url((s or {}).get("url"))
                if u:
                    links.append(((s.get("type") or "social").capitalize()[:20], u))
            if links:
                break
    md = metadata(mint, http, rpc_url)
    if md:
        ok.append("metadata")
        desc = _text(md.get("description"), MAX_DESC)
        for k, lab in (("website", "Website"), ("twitter", "Twitter"), ("telegram", "Telegram")):
            u = clean_url(md.get(k))
            if u:
                links.append((lab, u))
    else:
        failed.append("metadata")
    seen, uniq = set(), []
    for lab, u in links:
        if u.rstrip("/") not in seen:
            seen.add(u.rstrip("/")); uniq.append((lab, u))
    relab = []
    for lab, u in uniq:
        h = (urlparse(u).hostname or "").lower()
        lab = ("GitHub" if h.endswith("github.com") else "X" if h in ("x.com", "twitter.com", "www.x.com", "www.twitter.com")
               else "Telegram" if h in ("t.me", "telegram.me") else lab)
        if lab not in [x[0] for x in relab]:          # one link per label (e.g. one Website, one X)
            relab.append((lab, u))
    links = relab[:4]
    website = next((u for lab, u in links if lab.lower() not in ("twitter", "telegram", "discord", "x", "github")
                    and not re.search(r"(?:^|\.)(x|twitter|t|telegram)\.(com|me)$", urlparse(u).hostname or "")), None)
    snippet = website_snippet(website, http) if website else None
    (ok if snippet else failed).append("website") if website else None
    repos = github_repos(github_login, http) if github_login else {"ok": False, "error": "no login"}
    if github_login:
        (ok if repos.get("ok") else failed).append("github" + ("" if repos.get("ok") else f"({repos.get('error')})"))
    return {"description": desc, "website": website, "links": links, "site_snippet": snippet,
            "repos": repos.get("repos") or [], "github_login": github_login, "sources_ok": ok, "sources_failed": failed}


def context_block(ctx: dict | None) -> str:
    """Compact text for the prompt. Empty string if nothing useful."""
    if not ctx:
        return ""
    L = []
    if ctx.get("description"):
        L.append(f"Token description (metadata): {ctx['description']}")
    for lab, u in ctx.get("links") or []:
        L.append(f"Link ({lab}): {u}")
    if ctx.get("site_snippet"):
        L.append(f"Website title/description: {ctx['site_snippet']}")
    repos = ctx.get("repos") or []
    if repos:
        L.append(f"Claimant {ctx.get('github_login')} recent public repos:")
        for r in repos:
            bits = [r.get("language") or "", f"★{r.get('stars') or 0}", f"pushed {r.get('pushed')}" if r.get("pushed") else "",
                    "fork" if r.get("fork") else ""]
            L.append(f"- {r.get('name')}: {r.get('description') or 'no description'} ({', '.join(b for b in bits if b)})")
    s = "\n".join(L)
    return s if len(s) <= MAX_BLOCK else s[: MAX_BLOCK - 1] + "…"
