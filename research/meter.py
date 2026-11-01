"""Real usage from Queen's meter file (queen/meter.mjs, loaded with NODE_OPTIONS=--import).

The meter appends one JSON line per xAI completion (with xAI's `usage`, incl. cost_in_usd_ticks = 1e-10 USD)
and per Helius RPC body. It never records URLs' query strings, headers or keys. With one request in flight,
lines appended between a request's start and end belong to that request.
"""
from __future__ import annotations

import json
import os
from collections import Counter

from .config import PRICES, Price

# Helius credit weights (Helius pricing page: standard RPC 1, DAS/getProgramAccounts 10, enhanced history 100)
HELIUS_WEIGHTS = {"getAsset": 10, "getAssetBatch": 10, "getTokenAccounts": 10, "searchAssets": 10, "getAssetsByOwner": 10,
                  "getSignaturesForAsset": 10, "getProgramAccounts": 10, "getTransactionsForAddress": 100}


def price_cost(usage: dict, price: Price) -> float:
    inp = usage.get("prompt_tokens", 0) or 0
    cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)) or 0
    out = usage.get("completion_tokens", 0) or 0
    reasoning = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)) or 0
    # xAI bills reasoning tokens as output; completion_tokens excludes them in xAI's usage object
    return ((inp - cached) * price.input + cached * price.cached + (out + reasoning) * price.output) / 1e6


class Meter:
    def __init__(self, path: str, prices: dict | None = None):
        self.path = path
        self.prices = prices or PRICES

    def mark(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def read_since(self, offset: int) -> dict | None:
        """Aggregate usage since `offset`. None if the meter file is absent (Queen not metered)."""
        if not os.path.exists(self.path):
            return None
        with open(self.path, "rb") as f:
            f.seek(offset)
            raw = f.read()
        rpc: Counter = Counter()
        hosts: Counter = Counter()
        http_errors: list[str] = []
        u = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "llm_calls": 0,
             "llm_errors": 0, "models": [], "ticks": 0, "price_usd": 0.0, "ticks_complete": True}
        for line in raw.decode("utf8", "replace").splitlines():
            try:
                x = json.loads(line)
            except ValueError:
                continue
            k = x.get("kind")
            if k == "rpc":
                rpc.update(x.get("rpc") or [])
            elif k == "req":
                hosts[x.get("host", "?")] += 1
            elif k == "httperr":
                http_errors.append(f"{x.get('host')} {x.get('status')}")
            elif k == "llm":
                u["llm_calls"] += 1
                m = x.get("model")
                if m and m not in u["models"]:
                    u["models"].append(m)
                if (x.get("status") or 0) >= 400:
                    u["llm_errors"] += 1
                us = x.get("usage")
                if not us:
                    continue
                u["input_tokens"] += us.get("prompt_tokens", 0) or 0
                u["cached_tokens"] += (us.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                u["output_tokens"] += us.get("completion_tokens", 0) or 0
                u["reasoning_tokens"] += (us.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
                if "cost_in_usd_ticks" in us:
                    u["ticks"] += int(us["cost_in_usd_ticks"])
                else:
                    u["ticks_complete"] = False
                p = self.prices.get(m) if m else None
                if p:
                    u["price_usd"] += price_cost(us, p)
        if u["llm_calls"] and u["ticks_complete"] and u["ticks"]:
            u["actual_usd"], u["cost_source"] = u["ticks"] / 1e10, "xai_usage_ticks"
        elif u["llm_calls"]:
            u["actual_usd"], u["cost_source"] = u["price_usd"], "price_table"
        else:
            u["actual_usd"], u["cost_source"] = 0.0, "no_llm_calls"
        u["helius_calls"] = sum(rpc.values())
        u["helius_credits"] = sum(c * HELIUS_WEIGHTS.get(m, 1) for m, c in rpc.items())
        u["rpc"], u["hosts"], u["http_errors"] = dict(rpc), dict(hosts), http_errors
        return u
