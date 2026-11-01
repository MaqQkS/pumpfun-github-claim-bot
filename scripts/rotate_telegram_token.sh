#!/bin/bash
# Rotate GitClaim's Telegram bot token. Run it YOURSELF in Terminal on the Mac, AFTER BotFather /revoke (which issues
# the new token and kills the old one). The token is never echoed, never put on a command line (curl reads it from
# stdin), never logged. Only HTTP status codes and counts are printed.
#   cd ~/pumpfun-github-claim-bot && scripts/rotate_telegram_token.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=.env
[ -f "$ENV" ] || { echo "no .env here"; exit 1; }
umask 077

getme() {  # $1 = token; prints HTTP status only
  printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$1" | curl -s -o /dev/null -w '%{http_code}' -m 10 -K -
}

OLD=$(grep -E '^(export )?TELEGRAM_BOT_TOKEN=' "$ENV" | tail -1 | sed -E 's/^(export )?TELEGRAM_BOT_TOKEN=//; s/^["'\'']//; s/["'\'']$//')
read -r -s -p "Paste the NEW bot token from BotFather (input hidden): " NEW; echo
[[ "$NEW" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]] || { echo "That doesn't look like a bot token; nothing changed."; exit 1; }
[ "$NEW" != "$OLD" ] || { echo "Same as the current token; nothing changed."; exit 1; }

echo "new token getMe: HTTP $(getme "$NEW") (want 200)"
[ "$(getme "$NEW")" = 200 ] || { echo "New token rejected by Telegram; nothing changed."; exit 1; }
OLDCODE=$(getme "$OLD"); echo "old token getMe: HTTP $OLDCODE (want 401 = revoked)"
[ "$OLDCODE" = 401 ] || echo "WARNING: old token still valid. Use BotFather /revoke, then re-run. Continuing with the update."

# atomic replace, 600 perms, keep a private backup of the previous file
cp -p "$ENV" "$ENV.bak-$(date +%Y%m%d%H%M%S)"; chmod 600 "$ENV".bak-*
TMP=$(mktemp "$ENV.XXXXXX")
NEW="$NEW" awk 'BEGIN{t=ENVIRON["NEW"]} /^(export )?TELEGRAM_BOT_TOKEN=/{p=($0 ~ /^export /)?"export ":""; print p "TELEGRAM_BOT_TOKEN=" t; d=1; next} {print} END{if(!d) print "export TELEGRAM_BOT_TOKEN=" t}' "$ENV" > "$TMP"
chmod 600 "$TMP"; mv -f "$TMP" "$ENV"
echo ".env updated (600)."

# restart the bot only (Queen has no Telegram token and keeps running)
POS_BOT=$(stat -f %z bot.log 2>/dev/null || echo 0); POS_SUP=$(stat -f %z supervisor.log 2>/dev/null || echo 0)
./start.sh
echo "waiting 25s for the bot to come up..."; sleep 25
tail -c +"$((POS_BOT + 1))" bot.log | grep -E "tg poll started|ws connected|bot username" | tail -3 || true
# redaction check on NEW log lines only (historical logs are not touched); patterns come from a pipe, not argv
check() {  # $1 file, $2 start offset
  [ -f "$1" ] || return 0
  n=$(tail -c +"$(($2 + 1))" "$1" | grep -F -c -f <(printf '%s\n%s\n' "$NEW" "${NEW#*:}") || true)
  echo "$1: new-token occurrences in new lines: ${n:-0} (want 0)"
}
check bot.log "$POS_BOT"; check supervisor.log "$POS_SUP"
unset NEW OLD
echo "Done. Old backups: .env.bak-* (600). Delete them once you're happy."
