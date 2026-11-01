#!/bin/zsh
# Auto-restart loop for the STANDARD Queen (grok-4.3 low, 127.0.0.1:8790). Run under caffeinate via queen_up.sh.
# Deep (grok-4.7) is never started here. The bot keeps alerting if Queen is down (/re just reports it offline).
HERE=${0:A:h}
DATA=${QUEEN_SUP_DATA:-${HERE:h}/data}           # test hooks: QUEEN_SUP_DATA, QUEEN_START_CMD, QUEEN_RESTART_S, QUEEN_MAX_RESTARTS
LOG=$DATA/queen-supervisor.log
n=0
while true; do
  if [[ -n ${QUEEN_START_CMD:-} ]]; then ( eval "$QUEEN_START_CMD" ) >> "$DATA/queen-serve.log" 2>&1
  else zsh "$HERE/start_queen.sh" standard >> "$DATA/queen-serve.log" 2>&1; fi
  rc=$?; n=$((n+1))
  echo "$(date) queen exited $rc, restarting in ${QUEEN_RESTART_S:-10}s (restart #$n)" >> "$LOG"
  [[ -n ${QUEEN_MAX_RESTARTS:-} && $n -ge $QUEEN_MAX_RESTARTS ]] && exit 0
  sleep ${QUEEN_RESTART_S:-10}
done
