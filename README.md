# pumpfun-github-claim-bot

Telegram alerts when a GitHub-linked pump.fun creator-fee vault (Social Fee PDA) makes its **first** claim.
See `research.md` for how it works on-chain.

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export HELIUS_API_KEY=...       # Helius RPC/websocket key (free tier OK)
export TELEGRAM_BOT_TOKEN=...   # from @BotFather
nohup setsid ./run.sh >/dev/null 2>&1 &   # restart-on-crash loop; logs: bot.log, supervisor.log
```
Then message the bot `/start` in Telegram to subscribe (`/stop` to unsubscribe). Subscribers persist in `state.json`.

Replay/test a past claim: `.venv/bin/python bot.py --replay <signature>` (sends only if someone has /start-ed).

## Detection
- One Helius `logsSubscribe` on Pump Fees program `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`; only txs whose logs contain `Instruction: ClaimSocialFeePda*` are fetched.
- Decodes the `SocialFeePdaClaimed` event; first claim = `amount_claimed == lifetime_claimed`, platform 2 (GitHub).
- Mint from pump `distribute_creator_fees` (same tx or a prior tx on the same vault within 5 min).
- Ticker/MC/ATH from pump.fun `frontend-api-v3.pump.fun/coins-v2/<mint>`, fallback DexScreener / Helius getAsset.
