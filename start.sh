#!/bin/bash
# Start GitClaimed on this Mac in the background, keeping the Mac awake while it runs.
cd "$(dirname "$0")"
source .env
for p in $(pgrep -f "caffeinate -is ./run.sh"); do [ "$(lsof -a -p $p -d cwd -Fn 2>/dev/null | grep ^n | cut -c2-)" = "/Users/balachinthala/pumpfun-github-claim-bot" ] && { pkill -P $p; kill $p 2>/dev/null; }; done
for p in $(pgrep -f "run.sh"); do [ "$(lsof -a -p $p -d cwd -Fn 2>/dev/null | grep ^n | cut -c2-)" = "/Users/balachinthala/pumpfun-github-claim-bot" ] && kill $p; done
for p in $(pgrep -f "bot.py"); do [ "$(lsof -a -p $p -d cwd -Fn 2>/dev/null | grep ^n | cut -c2-)" = "/Users/balachinthala/pumpfun-github-claim-bot" ] && kill $p; done
sleep 1
nohup caffeinate -is ./run.sh >/dev/null 2>&1 &
echo "started"
