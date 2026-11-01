# Operations: GitClaim and Queen restart independently

| Action | Command | Affects |
|---|---|---|
| Restart bot only | `cd ~/pumpfun-github-claim-bot && ./start.sh` | bot.py + run.sh + caffeinate. Queen untouched (queen_up.sh no-ops if supervised). ~3–5 s alert gap, no backfill. |
| Start Queen (if down) | `research/queen/queen_up.sh` | Queen only |
| Stop Queen | `research/queen/queen_up.sh stop` | Queen only; alerts continue, /re replies "Queen is offline" |
| Restart Queen | `research/queen/queen_up.sh stop && research/queen/queen_up.sh` | Queen only |
| Queen crash | automatic: supervisor restarts after 10 s (`research/data/queen-supervisor.log`) | — |
| Check | `curl -s localhost:8790/health` · `tail supervisor.log` · `/st` in Telegram | — |

Queen's environment is clean (no Telegram token); paid tools (X search, news, CT radar, hub) are forced off in
`start_queen.sh`. Deep (grok-4.7) is never started by these scripts.
