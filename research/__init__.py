"""GitClaim -> Crawlnet Queen research adapter (Phase 2).

Standalone and optional: nothing in bot.py imports this package. Every public call returns a
ResearchResult and never raises, so a Queen outage or an exhausted budget cannot reach the scanner.
"""
from .config import Settings, load_settings, COMMANDS, PRICES  # noqa: F401
from .router import route, RoutePlan  # noqa: F401
from .adapter import ResearchAdapter, ResearchResult  # noqa: F401
