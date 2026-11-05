"""Deep research via xAI Responses API (grok-4.7 + native x_search/web_search).

Queen's own TOOLS_XSEARCH/TOOLS_NEWS go through OpenRouter and need a separate
TOOLS_XSEARCH_KEY. Native xAI tools use the same xAI key already in Queen\'s .env,
are billed per post/call on top of tokens, and are the cheaper reliable option.
Stdlib only. Never logs the API key.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .client import QueenAnswer, QueenBadResponse, QueenError, QueenHTTPError, QueenTimeout, QueenUnavailable
from .config import Price, _parse_env_file

# Tool invocation prices (USD) from docs.x.ai/developers/pricing (as of 2026-11)
WEB_SEARCH_PER_CALL = 0.005          # $5 / 1k calls
X_POST_PER = 0.005                   # $5 / 1k posts
X_PROFILE_PER = 0.010                # $10 / 1k profiles


class DeepConfigError(QueenError):
    code, reached_queen = "deep_config", False


@dataclass
class DeepUsage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    web_search_calls: int = 0
    x_search_calls: int = 0
    x_posts_fetched: int = 0
    x_users_fetched: int = 0
    token_usd: float = 0.0
    tool_usd: float = 0.0

    @property
    def actual_usd(self) -> float:
        return self.token_usd + self.tool_usd

    def as_meter(self, model: str) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "llm_calls": 1,
            "models": [model],
            "actual_usd": self.actual_usd,
            "cost_source": "xai_responses_tools",
            "helius_credits": 0,
            "web_search_calls": self.web_search_calls,
            "x_search_calls": self.x_search_calls,
            "x_posts_fetched": self.x_posts_fetched,
            "x_users_fetched": self.x_users_fetched,
            "token_usd": self.token_usd,
            "tool_usd": self.tool_usd,
        }


def resolve_xai_api_key(*, api_key: str | None = None, env_file: str | None = None,
                       environ: dict | None = None) -> str | None:
    """Prefer an explicit key, then RESEARCH_XAI_API_KEY / XAI_API_KEY, then LLM_API_KEY from Queen\'s .env."""
    env = environ if environ is not None else os.environ
    for k in (api_key, env.get("RESEARCH_XAI_API_KEY"), env.get("XAI_API_KEY")):
        if k and k.strip():
            return k.strip()
    path = Path(env_file or env.get("RESEARCH_XAI_ENV_FILE") or Path.home() / "queen-harness" / ".env")
    try:
        parsed = _parse_env_file(path)
    except OSError:
        return None
    v = (parsed.get("LLM_API_KEY") or "").strip()
    return v or None


def _tool_cost(details: dict) -> tuple[float, dict]:
    web = int(details.get("web_search_calls") or 0)
    posts = int(details.get("x_posts_fetched") or 0)
    users = int(details.get("x_users_fetched") or 0)
    x_calls = int(details.get("x_search_calls") or 0)
    usd = web * WEB_SEARCH_PER_CALL + posts * X_POST_PER + users * X_PROFILE_PER
    return usd, {"web_search_calls": web, "x_search_calls": x_calls,
                 "x_posts_fetched": posts, "x_users_fetched": users}


def _token_cost(usage: dict, price: Price) -> tuple[float, dict]:
    # Responses API usage fields: input_tokens, output_tokens, and optional details
    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    out_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    reasoning = int(out_details.get("reasoning_tokens") or 0)
    # Billing: reasoning is part of output; if output already includes it, don\'t double-count.
    billable_out = out if out else reasoning
    if out and reasoning and out >= reasoning:
        billable_out = out  # already includes reasoning
    elif reasoning and not out:
        billable_out = reasoning
    usd = ((inp - cached) * price.input + cached * price.cached + billable_out * price.output) / 1e6
    return usd, {"input_tokens": inp, "cached_tokens": cached, "output_tokens": out, "reasoning_tokens": reasoning}


def _extract_text(obj: dict) -> str:
    """Pull the assistant\'s text from a Responses API body."""
    if isinstance(obj.get("output_text"), str) and obj["output_text"].strip():
        return obj["output_text"].strip()
    chunks: list[str] = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("message", "output_message") or item.get("role") == "assistant":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif isinstance(part, str):
                    chunks.append(part)
        elif item.get("type") == "output_text" and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    text = "\n".join(c.strip() for c in chunks if c and c.strip())
    return text


def parse_deep_response(obj: dict, price: Price, model: str) -> tuple[QueenAnswer, DeepUsage]:
    if not isinstance(obj, dict):
        raise QueenBadResponse("deep response is not a JSON object")
    text = _extract_text(obj)
    if not text:
        raise QueenBadResponse("deep response missing text")
    usage_raw = obj.get("usage") or {}
    token_usd, tok = _token_cost(usage_raw if isinstance(usage_raw, dict) else {}, price)
    details = {}
    if isinstance(usage_raw, dict):
        details = usage_raw.get("server_side_tool_usage_details") or {}
        if not isinstance(details, dict):
            details = {}
    tool_usd, tool = _tool_cost(details)
    du = DeepUsage(token_usd=token_usd, tool_usd=tool_usd, **tok, **tool)
    sources = []
    for s in obj.get("citations") or obj.get("sources") or []:
        if isinstance(s, str):
            sources.append(s)
        elif isinstance(s, dict):
            sources.append(s.get("url") or s.get("title") or str(s))
    tools = []
    if du.web_search_calls:
        tools.append("web_search")
    if du.x_search_calls or du.x_posts_fetched:
        tools.append("x_search")
    ans = QueenAnswer(answer=text, sources=sources, followups=[], by=model, tools=tools, ms=None)
    return ans, du


class DeepClient:
    """POST /v1/responses with x_search + web_search. Drop-in ask() returns (QueenAnswer, attempts, DeepUsage)."""

    def __init__(self, api_key: str, *, base_url: str = "https://api.x.ai/v1", model: str = "grok-4.7",
                 effort: str = "medium", price: Price | None = None, max_turns: int = 6,
                 sleep=time.sleep):
        if not api_key:
            raise DeepConfigError("missing xAI API key for deep research")
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.model = model
        self.effort = effort
        self.price = price or Price(2.00, 0.50, 6.00)
        self.max_turns = max(1, min(12, int(max_turns)))
        self._sleep = sleep

    def health(self, timeout_s: float = 3.0) -> bool:
        # No dedicated health endpoint; a missing key is the only hard fail we can check offline.
        return bool(self.api_key)

    def ask(self, question: str, timeout_s: float) -> tuple[QueenAnswer, int, DeepUsage]:
        payload = {
            "model": self.model,
            "input": [{"role": "user", "content": question}],
            "tools": [{"type": "web_search"}, {"type": "x_search"}],
            "reasoning": {"effort": self.effort},
            "max_turns": self.max_turns,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/responses", data=body, method="POST",
            headers={"content-type": "application/json", "authorization": "Bearer " + self.api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            # Never include response body (may echo prompts); status only.
            if e.code == 429:
                raise QueenHTTPError("deep xAI HTTP 429", 429)
            if e.code in (502, 503):
                raise QueenUnavailable(f"deep xAI HTTP {e.code}", e.code)
            raise QueenHTTPError(f"deep xAI HTTP {e.code}", e.code)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise QueenTimeout(f"no deep answer within {timeout_s:.0f}s")
            raise QueenUnavailable(f"cannot reach xAI: {type(e.reason).__name__}")
        except (socket.timeout, TimeoutError):
            raise QueenTimeout(f"no deep answer within {timeout_s:.0f}s")
        except (ConnectionError, OSError) as e:
            raise QueenUnavailable(f"connection failed: {type(e).__name__}")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise QueenBadResponse("malformed JSON from xAI deep")
        ans, usage = parse_deep_response(obj, self.price, self.model)
        return ans, 1, usage
