# Queen tools disabled for xAI

`TOOLS_DISABLE=chain_tvl_ranking,dex_pair_lookup,prediction_odds,protocol_tvl,sol_ticker_mint,stablecoin_supply,top_protocols,unlock_schedule,yield_pools`
(in `~/queen-harness/.env` and `queen/start_queen.sh`). Implementations are untouched.

**Why:** each of these declares a string parameter with `pattern: QUERY`, where
`packages/tools/src/schema.ts` defines `QUERY = "^[\\p{L}\\p{N} $#@&'.,_+\\-/()?!]{1,120}$"`.
`\p{L}` / `\p{N}` are Unicode property escapes. xAI's function-schema validator rejects the whole request
(HTTP 400) when any tool schema has them, so with these tools enabled the model gets no tools at all
(Phase 0 runs 1–2 fell back to Queen's preset reads).

**Can they be made compatible?** Yes. Options: (a) drop `pattern` from the JSON schema sent to the model and
keep the same regex check inside the tool's `run()` (Queen already validates args), or (b) make the schema
pattern provider-aware: ASCII class `^[A-Za-z0-9 $#@&'.,_+\-/()?!]{1,120}$` for strict providers. (a) keeps
Unicode support and is a ~one-line change in how schemas are emitted.

**Upstream PR to Crawlnet?** Worth it: a small, provider-neutral fix (strip unsupported regex features from emitted
schemas, validate server-side) would make Queen work out of the box on xAI. Not opened; needs your approval.
