#!/bin/zsh
# Auto-restart loop for the STANDARD Queen (grok-4.3 low, 127.0.0.1:8790). Run under caffeinate via queen_up.sh.
# Deep (grok-4.7) is never started here. The bot keeps alerting if Queen is down (/re just reports it offline).
HERE=${0:A:h}
LOG=${HERE:h}/data/queen-supervisor.log
while true; do
  zsh "$HERE/start_queen.sh" standard >> "${HERE:h}/data/queen-serve.log" 2>&1
  echo "$(date) queen exited $?, restarting in 10s" >> "$LOG"; sleep 10
done
