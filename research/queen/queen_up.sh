#!/bin/zsh
# Idempotent: start the supervised standard Queen if it is not already running.
#   research/queen/queen_up.sh          start (no-op if running)
#   research/queen/queen_up.sh stop     stop supervisor, its caffeinate and Queen
HERE=${0:A:h}
if [[ ${1:-} == stop ]]; then
  pkill -f "queen_supervisor.sh"; pkill -f "packages/harness/src/server.ts"; echo "queen stopped"; exit 0
fi
if pgrep -f "queen_supervisor.sh" >/dev/null; then echo "queen already supervised"; exit 0; fi
# clean env: Queen never inherits the bot's .env (no Telegram token); it reads its own ~/queen-harness/.env
nohup env -i HOME="$HOME" USER="$USER" PATH="/usr/bin:/bin:/usr/sbin:/sbin" caffeinate -is /bin/zsh "$HERE/queen_supervisor.sh" >/dev/null 2>&1 &
echo "queen started"
