# Phase 6: automatic research after alerts (OFF by default)

## Flow
detect (unchanged) -> original alert sent (payload unchanged) -> alert_map records message_id -> `AUTO.on_alert()`
(decision + one log line `AUTO queue …` / `AUTO skip … : <reason>`) -> job row in `research/data/auto_jobs.sqlite3`
(UNIQUE claim tx + mint + chat) -> CommandService auto queue (bounded, default 2, stale after 600 s; manual /re always
picked first; one global AI slot) -> adapter `source='auto'` (grok-4.3 low; deep refused) -> report sent with
`reply_parameters` to the original alert in the opted-in chat only. `on_alert` never blocks on network or raises.

## Eligibility (all required; first failing rule is logged)
enabled · origin=live (not catch-up / replay) · chat in AUTO_RESEARCH_CHAT_IDS · original alert delivered to that chat
(message_id recorded) · FIRST claim type, not "(unverified)" · primary mint present and base58 32-byte · alert not
"token ambiguous" · this coin's share >= MIN_FIRST_CLAIM_SOL (0.3) · auto limits > 0 · auto + global budget left ·
auto queue capacity · never triggered before (persistent). Multi-token alerts: primary (top) mint only.

## Budgets (research/ledger.sqlite3, column `source` = manual | auto | direct)
Auto requests must fit AUTO_RESEARCH_MAX_PER_DAY, AUTO_RESEARCH_DAILY_USD and AUTO_RESEARCH_DAILY_HELIUS_CREDITS AND
the global RESEARCH_BUDGET_* / Helius caps. Manual requests only see the global caps, so auto can use at most its own
sub-cap of the shared global allowance (pilot: $0.15 of $5/day). Defaults are 0 = auto refused.

## Restart / dedupe
Duplicate ws event: `state['seen']` + an in-progress set in `handle_sig` + the unique job row. On start `recover()`:
`running` -> `failed_unknown` (never retried; its ledger row stays at the worst-case reservation), `queued` older than
the stale TTL -> `expired`; fresh queued jobs are re-queued only if auto is still enabled for that chat. Poor/missing
answers and failed report delivery (`undelivered`) are never retried.

## Helius
Direct research reads on Queen's key (/ho: 4 standard calls; /re context getAsset ≈10) are written to the ledger as
`source='direct'` rows at runtime (`dd.HELIUS_SINK`, set in `bot.main()`), and /ho refuses when the daily/total
research Helius caps would be exceeded, across restarts. Queen's own RPCs are counted from its meter on the request
row, so nothing is counted twice. The production scanner's key/workload is not counted here. All figures are local
estimates; Helius dashboard billing is authoritative.

## Catch-up (CATCHUP_ENABLED=false)
On start, one bounded pass: getSignaturesForAddress(WS_MENTION, limit CATCHUP_MAX_SIGS<=100), keep sigs within
CATCHUP_WINDOW_S (<=3600) not in `state['seen']`, process oldest first through the normal `handle_sig` (origin=catchup):
same detection, FIRST/REPEAT logic and dedupe; alerts are sent as normal; never auto-researched. Cost: 1 call +
the normal per-sig cost on the scanner key, bounded by the sig cap. Limitations: `seen` keeps the last 2000 sigs; a
live event that arrives during catch-up for the same dev+coin may be classified before an older recovered claim;
if the bot was down longer than the window, older claims stay missed (no unlimited backlog by design).

## Deploy (needs approval) / kill switch / rollback
Deploy: ff-merge, tests, `./start.sh` (bot only), check the log line `auto research off · catch-up off`.
Pilot: set AUTO_RESEARCH_ENABLED=true, AUTO_RESEARCH_CHAT_IDS=-5178862838, MAX_PER_DAY=3, DAILY_USD=0.15,
DAILY_HELIUS_CREDITS=1000 in research/research.env, `./start.sh`.
Kill switch: AUTO_RESEARCH_ENABLED=false (or MAX_PER_DAY=0) + `./start.sh`; queued jobs expire on restart.
Rollback: `git revert --no-edit <base>..<head>` + `./start.sh`; the extra sqlite files and the ledger `source` column
are ignored by older code. Credentials (.env) are untracked and never touched by a revert.
