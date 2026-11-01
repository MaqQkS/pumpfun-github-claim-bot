# Phase 4: reply-to-alert research

Reply `/re` `/research` `/ho` `/holders` `/gh` `/github` `/liq` `/liquidity` (optionally `@GitClaimed_bot`) to a GitClaim
claim alert. An explicit argument (`/re <CA>`, `/gh <login>`) always wins over the reply. Testers/chats allowlists unchanged.

## How a reply is resolved (`alert_map.py`)
1. Accept only if `reply_to_message.from.id` == our bot id (from getMe) and `is_bot`, same chat, and none of
   `forward_origin/forward_from/forward_from_chat/forward_date/forward_sender_name/forward_signature/
   forward_from_message_id/is_automatic_forward/via_bot/sender_chat`. Otherwise: "That reply isn't to a GitClaim alert".
2. Stored mapping `(chat_id, message_id)` -> mints, claim tx, verified metadata (`research/data/alert_map.sqlite3`,
   WAL, one transaction per write, retention 30 days / 20k rows, pruned at startup). Recorded in `tg_send_all` after
   Telegram returns `message_id`; the alert payload is unchanged. Metadata is built from the alert GitClaim generated
   (same code path as Phase 3 `/re` facts), never from Telegram text.
3. No mapping (older alerts): parse the replied-to message `text` + `entities` (UTF-16 offsets) using the alert layout:
   header must start with `🆕 FIRST CLAIM`/`🔁 REPEAT CLAIM` and contain `💸 Claim:`; CAs are `code` entities that are a
   whole token-block line or the tail of a `• $SYM …` "Also first claims" line; each must base58-decode to 32 bytes
   (case kept). Claim tx from the `solscan.io/tx/` text_link. The parsed result is a mint only; `/re` then uses
   GitClaim's own log record for that mint if one exists (same as `/re <CA>`), and labels the source.
4. >1 distinct mint -> "That alert lists N tokens. Which one?" with `/re <CA>` lines. None -> ask for a CA.

Output labels: `📎 From the replied-to alert (GitClaim's stored record)` or
`📎 Older alert: CA read from the alert message; claim facts from GitClaim's own log | unavailable (not verified)`.
Verified facts stay in ✅ GitClaim facts; AI text stays in 🧠 AI take.

Reuse: `/re` uses stored ClaimFacts (no bot.log scan); `/gh` uses stored GitHub ids (no state lookup).

## setMyCommands (documented only, not implemented)
Scope the menu to the private group only, never default scope:
`setMyCommands {"commands":[{"command":"re","description":"AI research (reply to an alert)"},…],
"scope":{"type":"chat","chat_id":-5178862838}}` and optionally `{"type":"chat_member","chat_id":-5178862838,
"user_id":2038104261}`. Remove with `deleteMyCommands` using the same scope. Non-testers would still be refused by the
allowlist; the menu is cosmetic.

## Token rotation (before deployment)
1. BotFather -> /mybots -> @GitClaimed_bot -> API Token -> Revoke current token. Copy the new token (don't paste it in chat).
2. `cd ~/pumpfun-github-claim-bot && scripts/rotate_telegram_token.sh` (needs this branch merged, or copy the script).
   Hidden input; checks new getMe=200 and old getMe=401 (status codes only); atomic `.env` replace (600, private
   backup `.env.bak-*`); `./start.sh`; checks only NEW log lines contain 0 occurrences of the new token.
3. Restart scope: only bot.py (start.sh recreates run.sh + caffeinate). Queen holds no Telegram token and keeps
   running (queen_up.sh is a no-op when already supervised). Downtime ~3–5 s. There is no startup backfill of the claim
   websocket, so a claim landing in those seconds would not alert; pick a quiet moment. Telegram queues commands sent
   meanwhile (offset kept in state.json). Historical logs with the old token stay in place; after rotation that token
   returns 401. Delete or redact them only when Bala decides they are no longer needed.

## Deploy (needs approval)
1. Rotate token (above). 2. `cd ~/pumpfun-github-claim-bot && git merge --ff-only phase4-reply-to-alert`
3. Run tests: `.venv/bin/python -m unittest discover -s . -p 'test_*.py'`. 4. `./start.sh`.
5. Verify: one bot.py, `tg poll started`, `ws connected`, `bot username`, subscribers unchanged, Queen /health ok.
6. Reply `/liq` to a NEW alert (stored path, free), then to an OLD alert (parsed path, free), then one `/re` (~$0.02).

## Rollback
`cd ~/pumpfun-github-claim-bot && git revert --no-edit 23aa16d..<phase4 head> && ./start.sh` (history kept, no force
push). Fast alternative before pushing: `git reset --keep 23aa16d && ./start.sh`. `alert_map.sqlite3` is unused by the
old code and can stay. state.json/subscribers are never written by Phase 4 code.
