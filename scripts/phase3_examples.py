"""Offline example outputs for the Phase 3 commands. No Telegram, no Queen, no xAI.
Free reads only: DexScreener, GitHub (unauthenticated), Helius standard RPC on QUEEN's key (~4 calls).
/re uses the RECORDED Phase 2 Queen answer (research/data/phase2-real-test.json), re-formatted by the Phase 3 code.
Usage: python scripts/phase3_examples.py <mint> [github_login]   (prints Telegram HTML; no secrets printed)"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import telegram_cmds as tc  # noqa: E402
from research.adapter import ResearchResult  # noqa: E402
from research.client import validate_answer  # noqa: E402
from research.fixtures import find  # noqa: E402
from research.formatter import format_result  # noqa: E402
from research.router import RoutePlan  # noqa: E402

mint = sys.argv[1]
login = sys.argv[2] if len(sys.argv) > 2 else None
prod = Path.home() / "pumpfun-github-claim-bot"
facts = find((prod / "bot.log").read_text(errors="replace"), mint)
state = json.loads((prod / "state.json").read_text())
ids = [k.split(":", 1)[0] for k in state.get("pairs", []) if k.endswith(":" + mint)]
ctx = tc.BotContext(lambda: {"ws_connected": None, "subscribers": len(state["subscribers"]), "note": "offline example"},
                    lambda m: facts, lambda m: ids)
s = tc.CommandService(tc.CmdConfig(enabled=True), lambda c, t: None, ctx)

def show(cmd, text):
    print(f"===== {cmd} =====\n{text}\n")

show("/h", s._cmd_help(()))
show(f"/liq {mint}", s._cmd_liquidity((mint,)))
show(f"/ho {mint}", s._cmd_holders((mint,)))
show(f"/gh {mint}", s._cmd_github((mint,)))
if login:
    show(f"/gh {login}", s._cmd_github((login,)))
show("/st  (offline: scanner fields n/a because this is not the running bot)", s._cmd_status(()))
show(f"/d re {mint}", "🔬 Deep mode (grok-4.7) is disabled for now; no AI call was made. Use <code>/re &lt;CA&gt;</code> for standard research.")

rec = json.loads((ROOT / "research/data/phase2-real-test.json").read_text())
srcs = []
for line in (ROOT / "research/data/phase2-real-test.txt").read_text().splitlines():
    if line.startswith("- Sources: "):
        srcs = line[len("- Sources: "):].split(", ")
ans = validate_answer({"answer": rec["raw_answer"], "tools": rec["tools"], "sources": srcs, "by": rec.get("by")})
plan = RoutePlan("research", 1, True, model="grok-4.3", effort="low", mint=mint)
text, rep = format_result(facts=facts, answer=ans, plan=plan, usage=rec["usage"], latency_ms=rec["latency_ms"])
res = ResearchResult(True, plan, text, usage=rec["usage"])
print(f"===== /re {mint}  (RECORDED Phase 2 Queen answer, re-formatted; no new AI call) =====")
print(f"🔎 Researching <code>{mint}</code> with grok-4.3… usually 15–35s.\n---")
for i, part in enumerate(tc.split_message(tc.format_research(mint, res))):
    print(f"[message {i+1}, {len(part)} chars]\n{part}\n")
