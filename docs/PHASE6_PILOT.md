# Phase 6 auto-research pilot: approval request

**Status: NOT ACTIVE.** Production runs main `a0375f5` with auto research and catch-up off. This pilot needs Bala's explicit approval,
plus deployment of the `phase6-fixes` branch first (fail-closed Helius guard, claim-aware cache key, /liq bonding curve).

## Exact config (append to production `research/research.env`, then `./start.sh`, bot only)
```
AUTO_RESEARCH_ENABLED=true
AUTO_RESEARCH_CHAT_IDS=-5178862838
AUTO_RESEARCH_MAX_PER_DAY=3
AUTO_RESEARCH_DAILY_USD=0.15
AUTO_RESEARCH_DAILY_HELIUS_CREDITS=1000
CATCHUP_ENABLED=false
```
Unchanged: `RESEARCH_TESTER_IDS=2038104261`, `RESEARCH_CHAT_IDS=-5178862838,2038104261`, global caps
`RESEARCH_BUDGET_TOTAL_USD=5.00`, `RESEARCH_BUDGET_DAILY_USD=5.00`, `RESEARCH_HELIUS_DAILY_CREDITS=4000`,
`RESEARCH_HELIUS_TOTAL_CREDITS=100000`. Model is grok-4.3, low reasoning (the standard Queen on :8790). Auto never uses deep/grok-4.7.
No new testers or subscribers.

## Scope (what the code enforces)
- Only chat -5178862838 (Queen Harness Tests). Alerts to other subscribers never trigger research.
- FIRST claims only, >= 0.3 SOL, verified with one unambiguous mint, original alert delivered (message_id stored), not a catch-up or replay.
- One job per (claim tx, mint, chat), stored in `research/data/auto_jobs.sqlite3`. A restart never re-runs a job. A job that was running during a crash becomes `failed_unknown` and is charged worst-case.
- Manual /re always runs first. The auto queue holds at most 2 jobs. Stale jobs expire.
- Budget is checked BEFORE any context read. If auto is refused, no direct Helius call happens. Every direct Helius read reserves credits in the ledger first and fails closed if the ledger errors.

## UTC-day semantics (confirmed)
Ledger `day` = UTC date (`utc_day()`). The daily caps (3 runs, $0.15, 1000 credits, and the global $5/day and 4000 credits) reset at
**00:00 UTC = 7:00 PM CDT (6:00 PM CST after Nov 1)**. Example: the 2 Phase 5 runs at ~8:33 PM CDT Oct 31 are on UTC day 2026-11-01.

## Budget headroom now (ledger, exact)
- xAI: 2 runs total, $0.041935 (both manual). Total left: $4.958065 of $5.00. Today (UTC 11-01): $4.958065 of $5.00.
- Helius (estimated/metered): 410 credits. Today: 3590 of 4000. Total: 99,590 of 100,000.
- Auto: 0 jobs, $0, 0 credits.
- Worst case for the pilot: 3 × ~$0.02 typical (the $0.15/day cap is the hard local stop) and ≤1000 credits/day.
- Helius numbers are local estimates (getAsset ≈ 10, other calls 1, Queen's metered tool calls). Helius' own dashboard is the authoritative bill.

## Test steps
1. Deploy `phase6-fixes` (ff-only), run the full suite, then `./start.sh`. Verify the log shows `auto research off` (before enabling).
2. Add the config lines and run `./start.sh`. The log must show `auto research ON (chats=1, max/day=3, $0.15/day) · catch-up off`.
3. Wait for a real FIRST claim of ≥ 0.3 SOL. Expect: the alert first, then within ~10–60 s a reply "🧠 Queen Auto Research" to that alert, in the test group only.
4. Check `auto_jobs.sqlite3` (1 completed), the ledger (one `source=auto` row, exact cost), and the other subscriber (alert only, no report).
5. Run a manual `/re` while auto is queued. The manual one should run first.
6. After the 3rd report, the next eligible alert should log `skip: auto_runs 3 of 3 today`.

## Kill switch (seconds)
Set `AUTO_RESEARCH_ENABLED=false` (or remove the lines) in `research/research.env`, then `./start.sh` (bot only, Queen untouched).
Queued auto jobs are not run after the restart (they expire). Alerts keep flowing.
Emergency without a restart: stop Queen (`research/queen/queen_up.sh stop`). Auto jobs then fail fast with no xAI cost, and alerts are unaffected.

## Rollback
- Config-only: the kill switch above.
- Code: Use `git revert` (no reset or force-push). For example `git checkout a0375f5 -- .`
  on a revert commit, or `git revert <phase6-fixes commits>`, then `./start.sh`. The ledger, alert_map and auto_jobs sqlite files are
  forward-compatible and can stay.

## Note (10:33 PM CDT Oct 31): /re now runs grok-4.3 at HIGH reasoning effort
The worst-case reservation per grok-4.3 run is now $0.20 (was $0.05). The auto cap is checked against that worst case,
so `AUTO_RESEARCH_DAILY_USD=0.15` would refuse every auto job. If the pilot is approved, it needs
`AUTO_RESEARCH_DAILY_USD=0.60` (3 × $0.20), or auto jobs must keep low effort. That is Bala's decision.
