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
4. Reply commands default to the headline token, even when "Also first claims" lists other tokens.
   New mappings record `primary_mint`; older mappings use the primary claim facts or the CA directly below
   the token-bearing alert header. Unmapped historical alerts use that same header layout. Explicit command
   arguments still override the reply. Multiple tokens with no identifiable headline still prompt for a choice;
   no token found asks for a CA.

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
1. BotFather -> /mybots -> @GitClaimed_bot -> API Token -> Revoke current token. Copy the new token (never paste it in chat).
2. `cd ~/pumpfun-github-claim-bot && scripts/rotate_telegram_token.sh`
   Fails closed: aborts with "Nothing was changed" unless new getMe = 200 AND old getMe = 401. Hidden input; tokens go
   to curl via stdin (never argv); output is status codes and counts only. On success: `.env.bak-<timestamp>` (600),
   atomic `.env` replace (600), ONE `./start.sh` (bot only; Queen untouched), then prints bot.py count, polling /
   websocket / username lines, subscriber count, Queen /health, and new/old-token hits in post-restart log lines (want 0).
   Tested by `tests/test_rotate_script.py` with a mock curl.
3. Restart scope: only bot.py. Downtime ~3–5 s; no claim backfill, so pick a quiet moment. Historical logs are not
   touched; the old token is dead afterwards (401).
4. Code rollback never touches credentials: `.env`, `.env.bak-*` and `research/research.env` are gitignored and
   untracked, so `git revert` / `git reset --keep` leave the new token and state.json (subscribers) as they are.

## Deploy (needs approval)
1. Rotate token (above). 2. `cd ~/pumpfun-github-claim-bot && git merge --ff-only phase4-reply-to-alert`
3. Run tests: `.venv/bin/python -m unittest discover -s . -p 'test_*.py'`. 4. `./start.sh`.
5. Verify: one bot.py, `tg poll started`, `ws connected`, `bot username`, subscribers unchanged, Queen /health ok.
6. Reply `/liq` to a NEW alert (stored path, free), then to an OLD alert (parsed path, free), then one `/re` (~$0.02).

## Rollback
`cd ~/pumpfun-github-claim-bot && git revert --no-edit 23aa16d..<phase4 head> && ./start.sh` (history kept, no force
push). Fast alternative before pushing: `git reset --keep 23aa16d && ./start.sh`. `alert_map.sqlite3` is unused by the
old code and can stay. state.json/subscribers are never written by Phase 4 code.
