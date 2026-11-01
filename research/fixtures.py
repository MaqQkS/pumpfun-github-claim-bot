"""Build ClaimFacts from GitClaim's bot.log ALERT blocks (historical alerts, read-only)."""
from __future__ import annotations

import re

from .prompt import ClaimFacts

TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO ALERT$")


def parse_alerts(text: str) -> list[ClaimFacts]:
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = TS.match(line)
        if not m:
            continue
        block = []
        for l2 in lines[i + 1:i + 25]:
            if re.match(r"^\d{4}-\d\d-\d\d ", l2):
                break
            block.append(l2)
        b = "\n".join(block)
        mint = re.search(r"<code>([1-9A-HJ-NP-Za-km-z]{32,44})</code>", b)
        if not mint:
            continue
        head = block[0] if block else ""
        sym = re.search(r"\$(\S+)", head)
        kind = "FIRST claim ever" if "(ever)" in head else ("FIRST claim on this token" if "FIRST CLAIM" in head else None)
        claim = re.search(r"Claim: ([\d.]+) SOL by (\S+)(?: \(([^)]*)\))?", b)
        part = re.search(r"part of a ([\d.]+) SOL claim across (\d+) coins", b)
        dev = re.search(r"Dev: (.+)", b)
        warn = re.search(r"⚠️ (.+)", b)
        gh = re.search(r'href="(https://github\.com/[^"]+)"', b)
        tx = re.search(r"solscan\.io/tx/([1-9A-HJ-NP-Za-km-z]{60,90})", b)
        mc = re.search(r"MC: (\$[^|\n]+)", b)
        login = claim.group(2) if claim else None
        out.append(ClaimFacts(
            mint=mint.group(1), symbol=("$" + sym.group(1)) if sym else None,
            github_login=login, github_display=(claim.group(3) if claim else None),
            github_url=gh.group(1) if gh else None, github_profile=dev.group(1).strip() if dev else None,
            claim_sol=float(claim.group(1)) if claim else None,
            claim_total_sol=float(part.group(1)) if part else (float(claim.group(1)) if claim else None),
            claim_coins=int(part.group(2)) if part else 1, claim_kind=kind,
            tx_signature=tx.group(1) if tx else None, claim_time=m.group(1) + " (bot.log, Mac local time)",
            signer_tags=tuple(s.strip() for s in warn.group(1).split("·")) if warn else (),
            market_cap=mc.group(1).strip() if mc else None))
    return out


def find(text: str, mint: str) -> ClaimFacts | None:
    return next((f for f in parse_alerts(text) if f.mint == mint), None)
