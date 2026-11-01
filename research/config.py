"""Model routing and budgets as data. Change behavior here or in research/research.env, not in code.

Env overrides come from research/research.env (gitignored) and RESEARCH_* process variables.
The bot's own .env is never read.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent

TIER_DIRECT = 0      # Python / direct APIs, never an LLM call
TIER_AI = 1          # Queen on the standard model
TIER_UNAVAILABLE = -1


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens."""
    input: float
    cached: float
    output: float


# Configurable via RESEARCH_PRICE_<MODEL>=input,cached,output (e.g. RESEARCH_PRICE_GROK_4_3=1.25,0.20,2.50)
PRICES: dict[str, Price] = {
    "grok-4.3": Price(1.25, 0.20, 2.50),
    "grok-4.7": Price(2.00, 0.50, 6.00),
}


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    tier: int
    deep_allowed: bool = False          # only these may be sent to the deep model with /d
    limited: str | None = None          # caveat shown with every answer
    unavailable: str | None = None      # reason, for TIER_UNAVAILABLE
    task: str = ""                      # what Queen is asked to do (Tier 1)


COMMANDS: dict[str, Command] = {c.name: c for c in [
    # Tier 0: answered by Python / direct APIs. /d never adds an LLM call here.
    Command("help", ("h",), TIER_DIRECT),
    Command("status", ("st",), TIER_DIRECT),
    Command("claims", ("cl",), TIER_DIRECT),
    Command("liquidity", ("liq",), TIER_DIRECT),
    Command("holders", ("ho",), TIER_DIRECT),
    Command("github", ("gh",), TIER_DIRECT),
    # Tier 1: Queen on the standard model (grok-4.3, low reasoning)
    Command("research", ("re",), TIER_AI, deep_allowed=True,
            task="Research this token: holder concentration and top wallets, liquidity, launch history, "
                 "the fee claimant's on-chain activity, and risks."),
    Command("developer", ("dev",), TIER_AI, deep_allowed=True,
            task="Investigate the developer side: the GitHub-linked fee claimant's on-chain activity, "
                 "other coins tied to the same wallets, and whether the GitHub identity looks authentic."),
    Command("wallets", ("wa",), TIER_AI, deep_allowed=True,
            task="Map relationships among the top holder wallets and the fee claimant: shared funders, "
                 "clusters and transfers. Mark every link as observed or inferred."),
    Command("risk", ("rk",), TIER_AI,
            task="Explain the verified risk indicators: authorities, LP lock, holder concentration, "
                 "RugCheck flags and liquidity depth."),
    Command("narrative", ("nar",), TIER_AI,
            limited="Limited evidence: X search and news are off, so only the project's site and on-chain data are read.",
            task="Describe the token's narrative using only evidence you can read (its website and on-chain data)."),
    # Unavailable until integrations exist
    Command("bundles", ("bu",), TIER_UNAVAILABLE,
            unavailable="Needs Helius launch-history access (HELIUS_LAUNCH_KEYS), which is off to protect credits."),
    Command("snipers", ("sn",), TIER_UNAVAILABLE,
            unavailable="Needs Helius launch-history access (HELIUS_LAUNCH_KEYS), which is off to protect credits."),
    Command("socials", ("soc",), TIER_UNAVAILABLE,
            unavailable="Needs a paid X search source; X search is off."),
    Command("zauth", ("za",), TIER_UNAVAILABLE,
            unavailable="ZAUTH integration is not built yet."),
]}

ALIASES: dict[str, str] = {}
for _c in COMMANDS.values():
    ALIASES[_c.name] = _c.name
    for _a in _c.aliases:
        ALIASES[_a] = _c.name

DEEP_PREFIX = "d"


@dataclass(frozen=True)
class Settings:
    # Queen instances: the model is fixed per instance (Queen has no per-request model override)
    standard_url: str = "http://127.0.0.1:8790"
    standard_model: str = "grok-4.3"
    standard_effort: str = "low"
    deep_url: str = "http://127.0.0.1:8791"
    deep_model: str = "grok-4.7"
    deep_effort: str = "low"
    deep_enabled: bool = False                  # /d is refused unless this is true
    timeout_standard_s: float = 90.0
    timeout_deep_s: float = 120.0
    max_in_flight: int = 1
    max_retries: int = 2                        # transient errors only (connection refused, 502/503, 429)
    retry_backoff_s: float = 2.0
    cache_ttl_s: float = 900.0
    # USD budgets (local accounting, see README: not a provider hard cap)
    budget_total_usd: float = 5.00
    budget_daily_usd: float = 0.50
    worst_case_usd: dict = field(default_factory=lambda: {"grok-4.3": 0.05, "grok-4.7": 0.15})
    # Helius credits (Queen's own key; estimates, not Helius billing)
    helius_est_per_research: int = 205
    helius_worst_case: int = 400
    helius_daily_credits: int = 4000
    helius_total_credits: int = 100_000
    ledger_path: str = str(HERE / "data" / "ledger.sqlite3")
    meter_path: str = str(HERE / "data" / "queen-meter.jsonl")   # written by queen/meter.mjs
    prices: dict = field(default_factory=lambda: dict(PRICES))

    def model_for(self, deep: bool) -> tuple[str, str, str, float]:
        if deep:
            return self.deep_model, self.deep_effort, self.deep_url, self.timeout_deep_s
        return self.standard_model, self.standard_effort, self.standard_url, self.timeout_standard_s


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


_FIELDS = {
    "RESEARCH_STANDARD_URL": ("standard_url", str), "RESEARCH_STANDARD_MODEL": ("standard_model", str),
    "RESEARCH_STANDARD_EFFORT": ("standard_effort", str), "RESEARCH_DEEP_URL": ("deep_url", str),
    "RESEARCH_DEEP_MODEL": ("deep_model", str), "RESEARCH_DEEP_EFFORT": ("deep_effort", str),
    "RESEARCH_DEEP_ENABLED": ("deep_enabled", _bool), "RESEARCH_TIMEOUT_STANDARD_S": ("timeout_standard_s", float),
    "RESEARCH_TIMEOUT_DEEP_S": ("timeout_deep_s", float), "RESEARCH_MAX_IN_FLIGHT": ("max_in_flight", int),
    "RESEARCH_MAX_RETRIES": ("max_retries", int), "RESEARCH_CACHE_TTL_S": ("cache_ttl_s", float),
    "RESEARCH_BUDGET_TOTAL_USD": ("budget_total_usd", float), "RESEARCH_BUDGET_DAILY_USD": ("budget_daily_usd", float),
    "RESEARCH_HELIUS_DAILY_CREDITS": ("helius_daily_credits", int),
    "RESEARCH_HELIUS_TOTAL_CREDITS": ("helius_total_credits", int),
    "RESEARCH_LEDGER_PATH": ("ledger_path", str), "RESEARCH_METER_PATH": ("meter_path", str),
}


def load_settings(env_file: str | Path | None = None, environ: dict | None = None, **overrides) -> Settings:
    env = _parse_env_file(Path(env_file) if env_file else HERE / "research.env")
    env.update({k: v for k, v in (os.environ if environ is None else environ).items() if k.startswith("RESEARCH_")})
    kw: dict = {}
    for key, (name, conv) in _FIELDS.items():
        if key in env and env[key] != "":
            kw[name] = conv(env[key])
    s = Settings(**kw)
    wc = dict(s.worst_case_usd)
    prices = dict(s.prices)
    for k, v in env.items():
        if k.startswith("RESEARCH_WORST_CASE_"):
            wc[k[len("RESEARCH_WORST_CASE_"):].lower().replace("_", "-", 1).replace("_", ".")] = float(v)
        if k.startswith("RESEARCH_PRICE_"):
            m = k[len("RESEARCH_PRICE_"):].lower().replace("_", "-", 1).replace("_", ".")
            a, b, c = (float(x) for x in v.split(","))
            prices[m] = Price(a, b, c)
    s = replace(s, worst_case_usd=wc, prices=prices)
    return replace(s, **overrides) if overrides else s
