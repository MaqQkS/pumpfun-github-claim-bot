#!/bin/bash
# Rotate GitClaim's Telegram bot token. FAILS CLOSED: .env is changed and the bot restarted ONLY if
# the new token's getMe is HTTP 200 AND the old token's getMe is HTTP 401 (revoked in BotFather).
# Never prints a token: curl reads its URL from stdin (not argv), output is status codes and counts only.
#
#   cd ~/pumpfun-github-claim-bot && scripts/rotate_telegram_token.sh
#
# Restarts the bot once via ./start.sh (queen_up.sh no-ops, so Queen keeps running).
# Code rollbacks (git revert/reset) never touch .env: it is gitignored and untracked, as are .env.bak-*.
# Test hooks (used by tests/test_rotate_script.py): ROTATE_CURL (mock curl), ROTATE_START (restart cmd),
# ROTATE_WAIT (seconds), ROTATE_DIR (working dir), ROTATE_SKIP_VERIFY=1.
set -euo pipefail
cd "${ROTATE_DIR:-$(dirname "$0")/..}"
ENV=.env
CURL=${ROTATE_CURL:-curl}
umask 077
die() { echo "ABORT: $*"; echo "Nothing was changed (.env untouched, bot not restarted)."; exit 1; }
[ -f "$ENV" ] || die "no .env in $(pwd)"

getme() {  # $1 token -> HTTP status only
  printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$1" | "$CURL" -s -o /dev/null -w '%{http_code}' -m 10 -K - 2>/dev/null || echo 000
}

OLD=$(grep -E '^(export )?TELEGRAM_BOT_TOKEN=' "$ENV" | tail -1 | sed -E 's/^(export )?TELEGRAM_BOT_TOKEN=//; s/^["'\'']//; s/["'\'']$//' || true)
[ -n "$OLD" ] || die "TELEGRAM_BOT_TOKEN not found in .env"
echo "Step 1/4: Revoke the old token in BotFather first (/mybots -> @GitClaimed_bot -> API Token -> Revoke)."
read -r -s -p "Paste the NEW bot token (input hidden, press Enter): " NEW; echo
[[ "$NEW" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]] || die "that doesn't look like a bot token"
[ "$NEW" != "$OLD" ] || die "that is the current token, not a new one"

echo "Step 2/4: checking tokens with Telegram getMe..."
NEWCODE=$(getme "$NEW"); echo "  new token: HTTP $NEWCODE (need 200)"
[ "$NEWCODE" = 200 ] || die "new token was not accepted by Telegram"
OLDCODE=$(getme "$OLD"); echo "  old token: HTTP $OLDCODE (need 401 = revoked)"
[ "$OLDCODE" = 401 ] || die "old token is not revoked yet (got $OLDCODE). Revoke it in BotFather, then re-run"

echo "Step 3/4: updating .env atomically..."
TS=$(date +%Y%m%d-%H%M%S)
cp -p "$ENV" "$ENV.bak-$TS"; chmod 600 "$ENV.bak-$TS"
TMP=$(mktemp "$ENV.tmp.XXXXXX")
NEW="$NEW" awk 'BEGIN{t=ENVIRON["NEW"]} /^(export )?TELEGRAM_BOT_TOKEN=/{if(!d){p=($0 ~ /^export /)?"export ":""; print p "TELEGRAM_BOT_TOKEN=" t; d=1}; next} {print} END{if(!d) print "export TELEGRAM_BOT_TOKEN=" t}' "$ENV" > "$TMP"
chmod 600 "$TMP"; mv -f "$TMP" "$ENV"
echo "  .env updated (600); backup .env.bak-$TS (600, gitignored)"

echo "Step 4/4: restarting the bot once (Queen untouched)..."
POS_BOT=$(stat -f %z bot.log 2>/dev/null || wc -c < bot.log 2>/dev/null || echo 0)
POS_SUP=$(stat -f %z supervisor.log 2>/dev/null || wc -c < supervisor.log 2>/dev/null || echo 0)
${ROTATE_START:-./start.sh}
sleep "${ROTATE_WAIT:-25}"
count() {  # $1 file $2 offset $3 token -> occurrences in lines written after restart (pattern via pipe, not argv)
  [ -f "$1" ] || { echo 0; return; }
  tail -c +"$(($2 + 1))" "$1" | grep -F -c -f <(printf '%s\n%s\n' "$3" "${3#*:}") || true
}
NEWLOG=$(tail -c +"$((POS_BOT + 1))" bot.log 2>/dev/null || true)
if [ "${ROTATE_SKIP_VERIFY:-0}" != 1 ]; then
  echo "  bot.py processes: $(for p in $(pgrep -f 'bot.py' || true); do [ "$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | grep ^n | cut -c2-)" = "$(pwd)" ] && echo "$p"; done | wc -l | tr -d ' ') (want 1)"
  echo "  Queen /health: $(curl -s -m 3 http://127.0.0.1:8790/health || echo down)"
fi
echo "  polling: $(grep -c 'tg poll started' <<<"$NEWLOG" || true) · websocket connected: $(grep -c 'ws connected' <<<"$NEWLOG" || true) · username: $(grep -c 'bot username' <<<"$NEWLOG" || true) (want >=1 each)"
echo "  subscribers: $(grep -o 'tg poll started (offset [0-9]*, [0-9]* subscribers)' <<<"$NEWLOG" | tail -1 | grep -o '[0-9]* subscribers' || echo '?')"
for f in bot.log supervisor.log; do
  off=$POS_BOT; [ "$f" = supervisor.log ] && off=$POS_SUP
  echo "  $f new lines: new-token hits $(count "$f" "$off" "$NEW"), old-token hits $(count "$f" "$off" "$OLD") (want 0, 0)"
done
unset NEW OLD
echo "Done. Delete .env.bak-$TS once you're happy (it holds the revoked token and your other keys)."
