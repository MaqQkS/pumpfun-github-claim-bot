# Phase 3: Telegram commands (one bot: GitClaim)

GitClaim is the only Telegram identity. Queen has no token and no poller; `/re` reaches it through
`research.ResearchAdapter` over 127.0.0.1. If Queen is offline or out of budget, `/re` says so and
alerts keep flowing.

## Files
- `bot.py`: Telegram handling only (subscriptions, parsing, `CMDS.handle`), `tg_reply_html`, ws status flags.
  Alert detection, `format_alert`, `tg_send_all` are byte-identical to main (tested).
- `telegram_cmds.py`: full command registry (data), parser, access control, worker pool, research queue, output formatters.
- `direct_data.py`: no-AI reads (DexScreener, Helius on Queen's key, GitHub API) with caching and call counters.
- `research/formatter.py`: `sanitize_urls()` drops or case-restores source URLs (trusted values only).
- `tests/test_phase3.py`, `scripts/phase3_examples.py`.

## Telegram behavior changes vs main
- No auto-subscribe: plain messages and unknown commands never subscribe. `/start` (or `/start@<bot>`) does.
  Adding the bot to a group no longer subscribes it; removal/block still unsubscribes.
- Exact command words: `/stop` matches, `/stopwatch` doesn't. `/cmd@OtherBot` is ignored.
- `edited_message` updates never run commands; duplicate `update_id`s are skipped.
- Bot username: `TELEGRAM_BOT_USERNAME` env, else one `getMe` call when polling starts.

## Commands
Operational (everyone): `/help /h`, `/status /st`, `/liquidity /liq`, `/holders /ho`, `/github /gh`, `/research /re` (grok-4.3 low).
Coming soon (reply with reason, no AI): nar, dev, wa, risk/rk, bu, sn, soc, za, cmp, hist, chg, fund, la/ln, cl, sr, q, src.
`/d re|dev|wa <CA>`: replies "deep mode disabled", no AI call. `/d` on a no-AI command runs it normally with a note.

## Access
`RESEARCH_COMMANDS_ENABLED` (default false) is the global command switch. Set it in the process
environment or `research/research.env`. When enabled, every user and chat can use commands.
Legacy `RESEARCH_TESTER_IDS` and `RESEARCH_CHAT_IDS` are ignored. Replies go only to the requesting chat.
Budget and queue limits still apply; automatic research destinations are configured separately.

## Concurrency
The poll thread only parses, checks access and enqueues. Direct commands run on a 2-thread pool (max 10 pending).
Research runs on one worker (global AI concurrency 1) with a queue of `RESEARCH_MAX_QUEUE` (3) waiting jobs;
overflow replies "busy". Same chat + command + args can't be queued twice; the adapter also caches (15 min) and dedupes.

## Starting Queen (on demand, standard only)
    zsh research/queen/start_queen.sh standard     # 127.0.0.1:8790, grok-4.3 low; Ctrl-C to stop
Never start `deep` in this phase.
