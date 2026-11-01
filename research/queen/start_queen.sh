#!/bin/zsh
# Start a local Queen instance for the adapter. No Telegram token; listens on 127.0.0.1 only.
#   start_queen.sh standard   -> :8790, model from Queen's .env (grok-4.3, reasoning low)
#   start_queen.sh deep       -> :8791, grok-4.7 reasoning low. Paid and ~4x cost: start only on explicit request.
# The meter (meter.mjs) writes xAI usage + Helius RPC counts to research/data/queen-meter.jsonl (no keys/URLs).
set -eu
MODE=${1:-standard}
QUEEN_DIR=${QUEEN_DIR:-$HOME/queen-harness}
HERE=${0:A:h}
DATA=${HERE:h}/data
mkdir -p "$DATA"
export PATH="$HOME/.local/node/bin:$PATH"
export METER_OUT="$DATA/queen-meter.jsonl"
export NODE_OPTIONS="--import $HERE/meter.mjs"
export HOST=127.0.0.1
# the 9 xAI-incompatible tools stay disabled (also set in Queen's .env)
export TOOLS_DISABLE=chain_tvl_ranking,dex_pair_lookup,prediction_odds,protocol_tvl,sol_ticker_mint,stablecoin_supply,top_protocols,unlock_schedule,yield_pools
export TOOLS_MAX_CALLS_PER_ANSWER=${TOOLS_MAX_CALLS_PER_ANSWER:-10}
export HARNESS_IN_FLIGHT_MAX=1
# never enabled here: paid search / launch history / hub
export TOOLS_XSEARCH=0 TOOLS_NEWS=0 TOOLS_CTRADAR=0 TOOLS_HUB=0
cd "$QUEEN_DIR"
case $MODE in
  standard) export PORT=8790 LLM_MODEL=grok-4.3 LLM_EXTRA_BODY='{"reasoning_effort":"low"}' ;;
  deep)     export PORT=8791 LLM_MODEL=grok-4.7 LLM_EXTRA_BODY='{"reasoning_effort":"low"}'
            export METER_OUT="$DATA/queen-meter-deep.jsonl" ;;
  *) echo "usage: $0 standard|deep" >&2; exit 2 ;;
esac
exec node packages/harness/src/server.ts
