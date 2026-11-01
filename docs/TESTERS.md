# Testers (adding Jed later)

Access needs BOTH: the user id in `RESEARCH_TESTER_IDS` AND the chat id in `RESEARCH_CHAT_IDS`
(`research/research.env`, gitignored), with `RESEARCH_COMMANDS_ENABLED=true`. Everyone else gets no reply to research
commands. /start and /stop alerts stay open to anyone and are unaffected.

## Get Jed's Telegram user ID (safe ways)
- Easiest: Jed messages @userinfobot (or @RawDataBot) in Telegram; it replies with his numeric id. He sends Bala the number.
- Or: Jed sends any command (e.g. `/h`) in Queen Harness Tests. The bot logs
  `cmd /help ignored (chat=-5178862838 user=<id> …)` because he is not allowlisted yet; read it with
  `grep "cmd /help ignored" ~/pumpfun-github-claim-bot/bot.log | tail -1`. No code change needed.
- Confirm with Jed that the number is his (ids are not secret, but a wrong id grants the wrong person access).

## Add / revoke
1. Edit `research/research.env`: `RESEARCH_TESTER_IDS=2038104261,<jed_id>`.
2. Restart the bot only: `cd ~/pumpfun-github-claim-bot && ./start.sh` (allowlists are read at bot start; Queen keeps running).
3. Revoke immediately: remove the id (or set `RESEARCH_COMMANDS_ENABLED=false` to stop all research commands) and `./start.sh`.
Adding/removing testers never touches `state.json` or subscribers (`tests/test_phase5.py::TestTesters`).

## What testers see / don't see
- Research replies go only to the chat that asked; never broadcast to subscribers.
- AI text is scrubbed of local paths (/Users/…), emails, key/token-looking strings and config names before sending
  (`telegram_cmds.scrub_ai_text`). Verified GitClaim facts are rendered separately from AI interpretation.
- Budget: ledger limits in research.env (currently $5/day, $5 total). Each /re ≈ $0.02.

## Open group access (RESEARCH_OPEN_GROUP_CHAT_IDS)
- Chats listed in `RESEARCH_OPEN_GROUP_CHAT_IDS` (negative group ids only) let **any member** use the operational commands
  (/h /st /re /ho /liq /gh, all aliases, and reply-to-alert). Unfinished commands still say "coming soon"; /d stays disabled.
- It only applies when Telegram reports the chat as `group`/`supergroup`. DMs and every other chat keep the tester + chat allowlist.
  New groups are never opened automatically.
- Paid /re by non-testers in an open group is limited per UTC day: per user (default 3), per group (10), and group USD (0.25).
  Each /re reserves the worst case ($0.20 at high effort) when accepted. Afterwards the reservation drops to the actual cost.
  Cache hits, coalesced duplicates and budget refusals are released (not counted). An attempted Queen call that failed still counts.
  If the quota store errors, /re is refused (fail closed). Global $ and Helius budgets are checked after the quota.
- Revoke immediately: remove the id from `RESEARCH_OPEN_GROUP_CHAT_IDS`, then `./start.sh`.
