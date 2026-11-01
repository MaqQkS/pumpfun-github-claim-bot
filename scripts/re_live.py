"""ONE paid /re through the real CommandService path (context gather + Queen grok-4.3 low). No Telegram send.
Usage: python scripts/re_live.py <mint>   (requires running standard Queen; prints Telegram HTML; no secrets)"""
import sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
import telegram_cmds as tc  # noqa
from research.fixtures import find  # noqa
mint = sys.argv[1]
log = (Path.home() / "pumpfun-github-claim-bot" / "bot.log").read_text(errors="replace")
facts = find(log, mint)
out = []
s = tc.CommandService(tc.CmdConfig(enabled=True), lambda c, t: out.append(t), tc.BotContext(lambda: {}, lambda m: facts, lambda m: []))
t0 = time.time()
text = s._run_research(tc._Job(0, mint, "k", t0) if False else type("J", (), {"mint": mint})())
print(f"[{time.time()-t0:.1f}s total, {len(text)} chars]\n{text}")
