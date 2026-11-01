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
    rep = preserve_addresses(answer.answer, facts.addresses())
    ts = at.strftime("%Y-%m-%d %H:%M UTC")
    L = [f"A. Verified GitClaim facts (from the scanner, not from AI)"] + _fmt_facts(facts)
    L += ["", f"B. External on-chain / market data (read by Queen's tools at {ts})"]
    tools = answer.tools or []
    if tools:
        L += [f"- {t}: {TOOL_PROVIDERS.get(t, 'see tool')}" for t in tools]
    else:
        L.append("- No tool reads reported.")
    if answer.sources:
        L.append("- Sources: " + ", ".join(s if isinstance(s, str) else str(s.get("url") or s.get("title") or s) for s in answer.sources[:12]))
    L.append("- Figures in C that came from these tools are relayed by the model; the adapter does not re-verify them.")
    L += ["", f"C. AI interpretation ({plan.model}, reasoning {plan.effort}; interpretation, not verified fact)", rep.text.strip()]
    lim = []
    if plan.note:
        lim.append(plan.note)
    if usage and usage.get("http_errors"):
        lim.append("Upstream errors: " + ", ".join(sorted(set(usage["http_errors"]))))
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
