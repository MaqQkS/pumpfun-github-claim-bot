"""Sections A (verified GitClaim facts), B (external data via Queen's tools), C (AI interpretation).

Address rule: canonical addresses come only from trusted inputs (GitClaim facts). An AI-written token that
matches a canonical address case-insensitively but differs in case is replaced by the canonical form and
reported. Other address-like strings in AI text are listed as AI-reported, unverified. Verified addresses
are never case-transformed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# wide on purpose: a lowercased base58 address can contain 'l', which base58 itself excludes
ADDR_LIKE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Za-z]{32,88}(?![0-9A-Za-z])")
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,88}$")

# Queen tool -> upstream provider (for section B labels)
TOOL_PROVIDERS = {
    "sol_account_basics": "Helius RPC", "sol_largest_accounts": "Helius RPC", "sol_holder_spread": "Helius RPC",
    "sol_holder_links": "Helius RPC", "sol_token_origin": "Helius RPC + pump.fun", "sol_wallet_trace": "Helius RPC",
    "sol_balance_changes": "Helius RPC", "token_pairs": "DexScreener", "token_risk_flags": "RugCheck",
    "site_snapshot": "website / web.archive.org",
}


@dataclass
class AddressReport:
    text: str
    fixed: list = field(default_factory=list)          # (ai_form, canonical)
    unverified: list = field(default_factory=list)     # AI-only address-like strings


def preserve_addresses(text: str, canonical: list[str]) -> AddressReport:
    by_lower = {}
    for a in canonical:
        if a:
            by_lower.setdefault(a.lower(), a)
    rep = AddressReport(text)
    canon_set = set(canonical)

    def sub(m: re.Match) -> str:
        tok = m.group(0)
        if tok in canon_set:
            return tok
        c = by_lower.get(tok.lower())
        if c is not None:
            rep.fixed.append((tok, c))
            return c
        if (BASE58.match(tok) or tok.lower() == tok) and any(ch.isdigit() for ch in tok) and tok not in rep.unverified:
            rep.unverified.append(tok)
        return tok
    rep.text = ADDR_LIKE.sub(sub, text)
    return rep


URL_RE = re.compile(r"https?://[^\s<>\"')\],]+")
URL_ADDR = re.compile(r"[0-9A-Za-z]{32,88}")


@dataclass
class UrlReport:
    text: str
    kept: list = field(default_factory=list)
    restored: list = field(default_factory=list)     # (bad_url, fixed_url) case restored from trusted input
    dropped: list = field(default_factory=list)


def sanitize_urls(text: str, trusted: list[str]) -> UrlReport:
    """Every address/signature-like path or query component of a URL must exactly match a trusted value
    (GitClaim facts). A case-insensitive match is restored to the trusted canonical form; anything else drops
    the whole URL with a note. URLs without address-like components are kept unchanged. Never builds URLs."""
    canon = {a for a in trusted if a}
    by_lower = {a.lower(): a for a in canon}
    rep = UrlReport(text)

    def sub(m: re.Match) -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".;:!?":
            trail = url[-1] + trail; url = url[:-1]
        host_end = url.find("/", url.find("//") + 2)
        host, rest = (url, "") if host_end < 0 else (url[:host_end], url[host_end:])
        fixed, ok = rest, True
        for tok in set(URL_ADDR.findall(rest)):
            if tok in canon:
                continue
            c = by_lower.get(tok.lower())
            if c is None:
                ok = False; break
            fixed = fixed.replace(tok, c)
        if not ok:
            rep.dropped.append(url)
            return "[link omitted (unverified)]" + trail
        if fixed != rest:
            rep.restored.append((url, host + fixed))
        rep.kept.append(host + fixed)
        return host + fixed + trail
    rep.text = URL_RE.sub(sub, text)
    return rep


def _fmt_facts(facts) -> list[str]:
    d = facts.as_data()
    order = [("mint", "Mint"), ("symbol", "Symbol"), ("name", "Name"), ("github_login", "GitHub-linked fee claimant"),
             ("github_display", "GitHub display name"), ("github_url", "GitHub"), ("github_profile", "GitHub profile"),
             ("claim_sol", "Claim on this coin (SOL)"), ("claim_total_sol", "Whole claim tx (SOL)"), ("claim_coins", "Coins in claim tx"),
             ("claim_kind", "Claim type"), ("tx_signature", "Claim tx"), ("claim_time", "Claim time"),
             ("signer_tags", "Signer tags"), ("market_cap", "MC at alert")]
    out = []
    for k, label in order:
        v = d.get(k)
        if v in (None, [], ""):
            continue
        out.append(f"- {label}: {', '.join(v) if isinstance(v, list) else v}")
    out.append("- Note: the GitHub account is linked to the fee claim; it is not assumed to be the token creator.")
    return out


def format_result(*, facts, answer, plan, usage: dict | None, latency_ms: int | None, at: datetime | None = None) -> tuple[str, AddressReport]:
    at = at or datetime.now(timezone.utc)
    urep = sanitize_urls(answer.answer, facts.addresses())
    rep = preserve_addresses(urep.text, facts.addresses())
    ts = at.strftime("%Y-%m-%d %H:%M UTC")
    L = [f"A. Verified GitClaim facts (from the scanner, not from AI)"] + _fmt_facts(facts)
    L += ["", f"B. External on-chain / market data (read by Queen's tools at {ts})"]
    tools = answer.tools or []
    if tools:
        L += [f"- {t}: {TOOL_PROVIDERS.get(t, 'see tool')}" for t in tools]
    else:
        L.append("- No tool reads reported.")
    if answer.sources:
        raw = [s if isinstance(s, str) else str(s.get("url") or s.get("title") or s) for s in answer.sources[:12]]
        srep = sanitize_urls("\n".join(raw), facts.addresses())
        urep.dropped += srep.dropped; urep.restored += srep.restored
        L.append("- Sources: " + ", ".join(x for x in srep.text.split("\n") if x))
    L.append("- Figures in C that came from these tools are relayed by the model; the adapter does not re-verify them.")
    L += ["", f"C. AI interpretation ({plan.model}, reasoning {plan.effort}; interpretation, not verified fact)", rep.text.strip()]
    lim = []
    if plan.note:
        lim.append(plan.note)
    if usage and usage.get("http_errors"):
        lim.append("Upstream errors: " + ", ".join(sorted(set(usage["http_errors"]))))
    if urep.restored:
        lim.append(f"{len(urep.restored)} link(s) had wrong letter case in an address and were restored from verified GitClaim data.")
    if urep.dropped:
        lim.append(f"{len(urep.dropped)} link(s) omitted: their address/signature did not exactly match verified data.")
    if rep.fixed:
        lim.append(f"{len(rep.fixed)} address(es) in the AI text had wrong letter case and were restored from verified data.")
    if rep.unverified:
        lim.append(f"{len(rep.unverified)} address(es) appear only in AI text and are not verified: " + ", ".join(rep.unverified[:6]))
    if answer.incomplete:
        lim.append("Queen response missing fields: " + ", ".join(answer.incomplete))
    L += ["", "Limitations"] + ([f"- {x}" for x in lim] or ["- none noted"])
    meta = [f"Researched {ts}", f"model {plan.model}"]
    if usage:
        meta.append(f"cost ${usage.get('actual_usd', 0):.4f} ({usage.get('cost_source')})")
        meta.append(f"Helius ~{usage.get('helius_credits', 0)} credits (estimate)")
    if latency_ms is not None:
        meta.append(f"{latency_ms / 1000:.1f}s")
    L += ["", " · ".join(meta)]
    return "\n".join(L), rep
