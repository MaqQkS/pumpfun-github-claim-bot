#!/bin/bash
# supervisor loop: restarts bot on crash
cd "$(dirname "$0")"
while true; do
  .venv/bin/python bot.py >> supervisor.log 2>&1
  echo "$(date) bot exited $?, restarting in 5s" >> supervisor.log; sleep 5
done
