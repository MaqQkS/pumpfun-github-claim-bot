"""Regenerate tests/fixtures/webhook/*.json (sample payloads, fixed fixture data, no secrets)."""
import json, sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webhook as wh
import telegram_cmds as tc
from research.prompt import ClaimFacts
from research.router import RoutePlan

FFM = "9r33aA9jgEEhaSRzFVr7xa43MMaK7bJRcpjr5RyaAUnZ"
SIG = "675ePaJEeYQ9zsvHvW62QH26RbxaiU8yUdV9weW1WXe3Jepg92Ti2XA26c81uBqzisaQdDdmEvCkaK5tHfYXJXBi"
NOW = 1761966030
claim = {"user_id": "112233445", "amount": 7_250_000_000, "lifetime": 9_000_000_000, "recipient": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
         "pda": "2K8rxRJVf8Sih7C5c55ZbhoKFThQrpw6v3zULHARpump", "ts": 1761966000, "tags": ["automated signer"]}
c = wh.build_claim_event(sig=SIG, mint=FFM, kind="FIRST claim ever", claim=claim, token_lamports=5_520_000_000, n_coins=2, qualifying=1,
                         github={"login": "NubsCarson"}, symbol="FFM", market_cap_display="$56.3k", now=NOW)
text = "NARRATIVE: FFM pitches an AI dating agent; the claimant's GitHub shows a TypeScript repo of the same name.\nVERDICT: high risk (interpretation): tiny liquidity, new token.\nDATA:\n- Liquidity ~$2.5k on PumpSwap\n- Claimant account 1.9y old, 31 public repos"
res = types.SimpleNamespace(ok=True, plan=RoutePlan("research", 1, True, model="grok-4.3", effort="high", mint=FFM),
                            answer=types.SimpleNamespace(answer=text, tools=["token_info", "github_user"], sources=["https://github.com/NubsCarson"]),
                            text=text, usage={"actual_usd": 0.0213}, latency_ms=14100, cached=False)
f = ClaimFacts(mint=FFM, symbol="$FFM", github_login="NubsCarson", claim_sol=5.52, claim_total_sol=7.25, claim_coins=2,
               claim_kind="FIRST claim ever", tx_signature=SIG, claim_time="2025-11-01 03:00:00 UTC", signer_tags=("automated signer",))
r = wh.build_research_event(sig=SIG, mint=FFM, res=res, facts=f, now=NOW + 20, scrub=tc.scrub_ai_text, split=tc._split_answer,
                            pctx={"website": "https://ffm.example.org", "links": [{"url": "https://x.com/ffm_example"}],
                                  "description": "An AI agent that finds you a date.", "repos": [{"name": "ffm-agent", "language": "TypeScript", "stars": 3}]})
out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "webhook"
out.mkdir(parents=True, exist_ok=True)
for name, e in (("claim.detected.json", c), ("research.completed.json", r)):
    (out / name).write_text(json.dumps(e, indent=2, ensure_ascii=False) + "\n")
print("ok")
