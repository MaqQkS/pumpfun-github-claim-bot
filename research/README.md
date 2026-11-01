# GitClaim × Crawlnet Queen — research adapter (Phase 2)

Standalone, optional Python (stdlib-only) bridge from GitClaim to a local Crawlnet Queen.
**Nothing in `bot.py` imports it.** No Telegram code: GitClaim stays the only Telegram bot; Queen has no token.

```
command text ──> router.route() ──> RoutePlan (tier, model, url, timeout)      [pure, no I/O]
                     │ Tier 0 / unavailable / deep disabled -> returns, no LLM, no ledger row
                     ▼
ResearchAdapter.research(text, ClaimFacts)        never raises; returns ResearchResult
   ├─ cache (command, mode, model, mint; TTL 15 min) + in-flight dedup
   ├─ bounded concurrency (1)
   ├─ Ledger.reserve()  pre-request: daily/total USD + Helius budgets vs worst case
   ├─ prompt.build_question()  GitClaim facts as escaped, delimited untrusted data
   ├─ QueenClient.ask()  POST /v1/ask, 90s (deep 120s), retries only 429/502/503/conn-refused (max 2)
   ├─ Meter.read_since()  real xAI usage (cost_in_usd_ticks) + Helius RPC counts from queen/meter.mjs
   ├─ Ledger.finish()  reconcile actual cost
   └─ formatter.format_result()  A verified facts / B external data / C AI interpretation + address check
```

| File | Role |
|---|---|
| `config.py` | Command→tier/model table, prices, budgets, env overrides (`research/research.env`, `RESEARCH_*`) |
| `router.py` | Parses `/re <CA>`, `/d re <CA>`; picks model only from command + explicit `/d` |
| `client.py` | Queen HTTP client, response validation, typed errors |
| `ledger.py` | SQLite ledger `data/ledger.sqlite3` (gitignored), budget gate |
| `meter.py` + `queen/meter.mjs` | Real usage from Queen's process (no keys/URLs logged) |
| `prompt.py`, `fixtures.py` | Question builder; ClaimFacts from historical `bot.log` alerts |
| `formatter.py` | Sections A/B/C, address case restoration, unverified-address labels |
| `queen/start_queen.sh` | `standard` (:8790 grok-4.3 low) or `deep` (:8791 grok-4.7 low, explicit only) |
| `real_test.py` | The one controlled paid test |

## Queen API (packages/harness/src/server.ts)
- `GET /health` → `{"ok":true}` (no model / budget info)
- `POST /v1/ask` `{"question","history"}` → `{"answer","sources","followups","by","tools","ms"}`; 400 bad_question/bad_body, 413, 429 busy.
- **No per-request model override and no usage in the response.** So the model is fixed per Queen instance
  (standard :8790, deep :8791), and usage comes from `meter.mjs` loaded via `NODE_OPTIONS=--import`, which appends
  xAI's `usage` (incl. `cost_in_usd_ticks`, 1e-10 USD) and Helius RPC method counts to `data/queen-meter.jsonl`.
  With one request in flight, lines written during a request belong to it. A model mismatch is flagged.
- Our timeout closes the socket; Queen aborts the answer on close.

## Routing (locked in, edit `config.py`)
- Tier 0, no LLM: help/h, status/st, claims/cl, liquidity/liq, holders/ho, github/gh. `/d` adds nothing.
- Tier 1, grok-4.3 low: research/re, developer/dev, wallets/wa, risk/rk, narrative/nar (limited evidence).
- Unavailable (with reasons): bundles/bu, snipers/sn, socials/soc, zauth/za.
- Deep `/d re|dev|wa <CA>` → grok-4.7 low on :8791, only when `RESEARCH_DEEP_ENABLED=true` (default false).
  `/d rk|nar` runs standard. Question length or words like "deep" never change the model. No auto-escalation.

## Budgets
Defaults: total $5.00, daily $0.50 (UTC day), worst case per request $0.05 (4.3) / $0.15 (4.7), Helius 4,000/day and
100,000 total (~205/research est.). A request counts at its worst-case reservation until real usage replaces it;
errors after Queen started keep the reservation (or the measured cost if higher); never-sent requests count 0.

**These are local counters, not a provider hard cap.** They only see traffic through this adapter and Queen's meter;
CLI runs or other apps using the same xAI key are invisible to them, and Helius weights are estimates (Helius billing
is authoritative). Provider side (docs.x.ai "Manage Billing"): on prepaid credits with the default $0 invoiced billing
limit, xAI rejects requests once prepaid credits are depleted — that is the only true hard stop, and it is at the
account balance, not at our budget. Keep auto top-up off and the invoiced billing limit at $0. A single in-flight
request can finish slightly past zero; usage in the console can lag.

## Incompatible tools (kept disabled, not deleted)
See `TOOLS.md`.

## Tests
`.venv/bin/python -m unittest discover -s research/tests -t .` — stub Queen server; no network, no spend.
