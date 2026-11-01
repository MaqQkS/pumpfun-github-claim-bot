"""One controlled real research (Phase 2). Usage: .venv/bin/python -m research.real_test <mint>
Requires a running standard Queen (queen/start_queen.sh standard). Writes data/phase2-real-test.{json,txt}."""
import json
import sys
from pathlib import Path

from research import ResearchAdapter, load_settings
from research.fixtures import find

REPO = Path(__file__).resolve().parents[1]
mint = sys.argv[1]
facts = find((REPO / "bot.log").read_text(errors="replace"), mint)
assert facts, "mint not found in bot.log alerts"
a = ResearchAdapter(load_settings())
print("health:", a.health())
r = a.research(f"/re {mint}", facts)
out = {"ok": r.ok, "error": r.error, "latency_ms": r.latency_ms, "usage": r.usage, "tools": r.answer.tools if r.answer else None,
       "by": r.answer.by if r.answer else None, "address_fixes": r.address_fixes, "unverified": r.unverified_addresses,
       "raw_answer": r.answer.answer if r.answer else None, "ledger": a.ledger.rows(1), "totals": a.ledger.totals()}
d = Path(load_settings().ledger_path).parent
(d / "phase2-real-test.json").write_text(json.dumps(out, indent=1, default=str))
(d / "phase2-real-test.txt").write_text(r.text)
print(r.text)
print(json.dumps({k: out[k] for k in ("ok", "error", "latency_ms", "address_fixes", "unverified")}, default=str))
print(json.dumps({k: v for k, v in (r.usage or {}).items() if k != "rpc"}, default=str))
