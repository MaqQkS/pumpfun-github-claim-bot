"""Command text -> plan. Pure function, no I/O. The model comes only from the command and an explicit /d."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ALIASES, COMMANDS, DEEP_PREFIX, TIER_AI, TIER_DIRECT, TIER_UNAVAILABLE, Settings

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass(frozen=True)
class RoutePlan:
    command: str | None
    tier: int | None
    uses_llm: bool
    deep: bool = False
    model: str | None = None
    effort: str | None = None
    url: str | None = None
    timeout_s: float | None = None
    mint: str | None = None
    note: str | None = None
    error: str | None = None      # set when nothing should run


def route(text: str, settings: Settings) -> RoutePlan:
    parts = (text or "").strip().split()
    if not parts:
        return RoutePlan(None, None, False, error="empty")
    deep = False
    head = parts[0].lstrip("/").split("@", 1)[0].lower()
    if head == DEEP_PREFIX:
        deep = True
        parts = parts[1:]
        if not parts:
            return RoutePlan(None, None, False, deep=True, error="deep_needs_command")
        head = parts[0].lstrip("/").split("@", 1)[0].lower()
    name = ALIASES.get(head)
    if not name:
        return RoutePlan(None, None, False, deep=deep, error="unknown_command")
    cmd = COMMANDS[name]
    mint = next((p for p in parts[1:] if MINT_RE.match(p)), None)   # kept exactly as typed
    if cmd.tier == TIER_UNAVAILABLE:
        return RoutePlan(name, cmd.tier, False, deep=deep, mint=mint, error="unavailable", note=cmd.unavailable)
    if cmd.tier == TIER_DIRECT:
        return RoutePlan(name, cmd.tier, False, deep=False, mint=mint,
                         note="Deep analysis is not applicable to this command; returning the normal factual result." if deep else None)
    # Tier 1
    note = cmd.limited
    if deep and not cmd.deep_allowed:
        deep = False
        note = ((note + " ") if note else "") + "Deep mode is only for research, developer and wallets; ran on the standard model."
    if deep and not settings.deep_enabled:
        return RoutePlan(name, TIER_AI, False, deep=True, mint=mint, error="deep_disabled",
                         note="Deep mode (grok-4.7) is turned off in research config.")
    if not mint:
        return RoutePlan(name, TIER_AI, False, deep=deep, error="missing_mint")
    model, effort, url, timeout = settings.model_for(deep)
    return RoutePlan(name, TIER_AI, True, deep=deep, model=model, effort=effort, url=url, timeout_s=timeout, mint=mint, note=note)
