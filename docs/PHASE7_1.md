# Phase 7.1: outbound webhook (offline build, NOT deployed)

- `webhook.py`: config, URL/SSRF checks, signing, event builders, SQLite outbox, delivery worker.
- `bot.py`: claim hook after the Telegram alert + auto research hook. Startup logs `webhooks off` / `ON -> host` / `OFF (fail-closed: …)`.
- `telegram_cmds.py`: `on_auto_research` hook in `_run_auto`, after the Telegram report.
- Config: `research/webhook.env` (gitignored; template `research/webhook.env.example`). Outbox: `research/data/webhook_outbox.sqlite3`.
- Receiver protocol: `docs/WEBHOOK_PROTOCOL.md`. Example receiver: `examples/webhook_receiver.py`. Fixtures: `scripts/webhook_fixtures.py`.

## Guarantees
- Off unless `WEBHOOK_ENABLED=true` + valid https URL + secret ≥ 32 chars + an event type. Any problem → off, logged once.
- Hooks run after the alert send, only enqueue (one SQLite insert), never raise. The scanner never waits on the network.
- One worker thread; one request at a time; at-least-once; stable ids; capped queue, TTL, bounded retries.
- No xAI/Helius/GitHub calls from the webhook path (GitHub profile comes from the alert's in-memory cache).

## Deploy (only after Bala approves AND Jed's endpoint is agreed)
1. `git merge --ff-only phase7-1-webhook` on main, run the suite, push. With no `research/webhook.env`, `./start.sh` logs `webhooks off`.
2. Create `research/webhook.env` (chmod 600) from the example with Jed's URL + secret, `WEBHOOK_ENABLED=true`, one event type on.
3. `./start.sh` (bot only). Log must show `webhooks ON -> <jed host>`.

## Kill switch / rollback
- `WEBHOOK_ENABLED=false` (or delete `research/webhook.env`), then `./start.sh`. Pending events stay in the outbox and expire.
- Code: `git revert` the Phase 7.1 commits on main, then `./start.sh`. The outbox file can be deleted safely.
