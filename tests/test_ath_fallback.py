"""ATH: pump.fun -> GeckoTerminal -> DexScreener; honest labels; GT 429 retry + global backoff. Mocked HTTP only."""
import time
import types
import unittest
from unittest import mock

import requests as real
from tests.test_phase3 import bot

PUMP = "FfH6zeDiDYjySz4npwSzT2iwkezyJpopfS8JFvEVpump"
OTHER = "9r33aA9jgEEhaSRzFVr7xa43MMaK7bJRcpjr5RyaAUnZ"
CF = "<!DOCTYPE html><html><head><title>Just a moment...</title>"


class R:
    def __init__(self, status=200, js=None, text="", headers=None):
        self.status_code, self._js, self.text, self.headers = status, js, text, headers or {}
        self.ok = 200 <= status < 300

    def json(self):
        if self._js is None: raise ValueError("no json")
        return self._js


def gt_pools(addr="POOL1"): return R(200, {"data": [{"attributes": {"address": addr}}]})
def gt_ohlcv(highs): return R(200, {"data": {"attributes": {"ohlcv_list": [[0, 1, h, 0.5, 1, 9] for h in highs]}}})
DEX = R(200, [{"baseToken": {"symbol": "FFH"}, "marketCap": 29200}])
DEX_ATH = R(200, [{"baseToken": {"symbol": "FFH"}, "marketCap": 10000,
                   "liquidity": {"usd": 5000},
                   "priceChange": {"h1": -50.0, "h6": -20.0, "h24": -75.0}}])  # implied peak 40k
DEX_NO_CH = R(200, [{"baseToken": {"symbol": "FFH"}, "marketCap": 10000, "liquidity": {"usd": 1}}])
DEX_EMPTY = R(200, [])


class Base(unittest.TestCase):
    def setUp(self):
        for d in (bot._GT_CACHE, bot._ATH_NOTE, bot._creator, bot._DS_CACHE): d.clear()
        bot._GT_CALLS.clear(); bot._DS_CALLS.clear()
        bot._GT_BACKOFF_UNTIL = 0.0
        bot._PF.update(fail=0, fail_streak=0, blocked=False, last_warn=0.0)
        self.calls = []
        self.sleeps = []
        p = mock.patch.object(bot, "rpc", lambda *a, **k: None); p.start(); self.addCleanup(p.stop)
        sp = mock.patch.object(bot.time, "sleep", side_effect=lambda s: self.sleeps.append(s))
        sp.start(); self.addCleanup(sp.stop)

    def route(self, table, sequential=None):
        """table: substring -> Response|Exception|list of responses (popped in order)."""
        seq = {k: list(v) if isinstance(v, list) else None for k, v in table.items()}

        def get(url, **k):
            self.calls.append(url)
            for key, resp in sorted(table.items(), key=lambda kv: ("ohlcv" not in kv[0], "pools?page" not in kv[0])):
                if key in url:
                    if seq[key] is not None:
                        if not seq[key]:
                            raise AssertionError("no more responses for " + key)
                        resp = seq[key].pop(0)
                    if isinstance(resp, Exception):
                        raise resp
                    return resp
            raise AssertionError("unexpected url " + url)

        p = mock.patch.object(bot, "requests", types.SimpleNamespace(get=get, post=None, exceptions=real.exceptions))
        p.start(); self.addCleanup(p.stop)

    def line(self, mint):
        sym, mc, ath = bot.coin_info(mint)
        return bot._token_block(sym, mint, mc, ath).split("\n")[-1]


class TestAth(Base):
    def test_pumpfun_ok(self):
        self.route({"pump.fun": R(200, {"symbol": "FFH", "usd_market_cap": 29200, "ath_market_cap": 31100, "creator": "C"})})
        self.assertIn("ATH: $31.1k", self.line(PUMP))
        self.assertFalse(any("geckoterminal" in u for u in self.calls)); self.assertEqual(bot._creator[PUMP], "C")

    def test_403_cloudflare_falls_back_to_gt(self):
        self.route({"pump.fun": R(403, None, CF, {"server": "cloudflare"}), "dexscreener": DEX,
                    "pools?page": gt_pools(), "/ohlcv/hour": gt_ohlcv([1e-5, 3.11e-5, 2e-5])})
        with self.assertLogs("bot", level="WARNING") as L:
            self.assertIn("ATH: ~$31.1k (GT)", self.line(PUMP))
        self.assertTrue(any("blocked (HTTP 403" in x and "deployer" in x for x in L.output))
        self.assertNotIn(PUMP, bot._creator)
        n = len(self.calls); self.line(PUMP)
        self.assertEqual(sum("geckoterminal" in u for u in self.calls[n:]), 0)

    def test_gt_ath_never_below_current_mc(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX, "pools?page": gt_pools(), "/ohlcv/hour": gt_ohlcv([1e-5])})
        self.assertIn("ATH: ~$29.2k (GT)", self.line(PUMP))

    def test_gt_500_then_ds_success(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_ATH, "geckoterminal": R(500)})
        with self.assertLogs("bot", level="INFO") as L:
            self.assertIn("ATH: ~$40k (DS)", self.line(PUMP))
        self.assertTrue(any("geckoterminal ATH" in x and "gt_error" in x for x in L.output))

    def test_gt_500_ds_miss_labeled_not_indexed(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_EMPTY, "geckoterminal": R(500)})
        self.assertIn("ATH: n/a (not indexed)", self.line(PUMP))

    def test_gt_500_ds_no_pricechange_not_indexed(self):
        """Bare current MC is not an ATH — stay honest."""
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_NO_CH, "geckoterminal": R(500)})
        self.assertIn("ATH: n/a (not indexed)", self.line(PUMP))

    def test_not_on_gt_falls_to_ds(self):
        self.route({"pump.fun": R(404, None, "nf"), "dexscreener": DEX_ATH, "geckoterminal": R(200, {"data": []})})
        self.assertIn("ATH: ~$40k (DS)", self.line(PUMP))

    def test_not_on_gt_http_404_then_ds_miss(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_EMPTY, "geckoterminal": R(404)})
        self.assertIn("ATH: n/a (not indexed)", self.line(PUMP))

    def test_pf_500_gt_500_ds_error(self):
        self.route({"pump.fun": R(500, None, "err"), "dexscreener": R(500), "geckoterminal": R(500)})
        self.assertIn("ATH: n/a (not indexed)", self.line(PUMP))

    def test_timeouts_then_ds(self):
        self.route({"pump.fun": real.exceptions.ReadTimeout("t"), "dexscreener": DEX_ATH,
                    "geckoterminal": real.exceptions.ConnectTimeout("t")})
        self.assertIn("ATH: ~$40k (DS)", self.line(PUMP))

    def test_429_retries_once_then_ds_success(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_ATH,
                    "geckoterminal": [R(429, None, "slow", {"Retry-After": "1"}), R(429, None, "slow")]})
        with self.assertLogs("bot", level="INFO") as L:
            self.assertIn("ATH: ~$40k (DS)", self.line(PUMP))
        self.assertEqual(len(self.sleeps), 1)
        self.assertLessEqual(self.sleeps[0], bot.GT_RETRY_CAP_S)
        self.assertTrue(any("429" in x and "retrying" in x for x in L.output))
        self.assertTrue(any("global backoff" in x for x in L.output))
        self.assertGreater(bot._GT_BACKOFF_UNTIL, time.time())

    def test_429_retry_succeeds_without_ds(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX,
                    "pools?page": [R(429, None, "x", {"Retry-After": "1"}), gt_pools()],
                    "/ohlcv/hour": gt_ohlcv([3.11e-5])})
        self.assertIn("ATH: ~$31.1k (GT)", self.line(PUMP))
        self.assertEqual(len(self.sleeps), 1)

    def test_global_backoff_skips_further_gt_calls(self):
        other = "AnotherMint1111111111111111111111111111111pump"
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_ATH,
                    "geckoterminal": [R(429, None, "x", {"Retry-After": "45"}), R(429, None, "x")]})
        self.assertIn("ATH: ~$40k (DS)", self.line(PUMP))  # arms backoff + DS success
        n = len(self.calls)
        bot._GT_CACHE.clear(); bot._ATH_NOTE.clear(); bot._DS_CACHE.clear()
        self.assertIn("ATH: ~$40k (DS)", self.line(other))
        # Second alert must not hit GT while backoff is active.
        self.assertEqual(sum("geckoterminal" in u for u in self.calls[n:]), 0)

    def test_rate_limited_not_cached_long(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_EMPTY, "geckoterminal": R(429)})
        self.line(PUMP)
        exp, payload = bot._GT_CACHE[PUMP]
        self.assertIsNone(payload[0])
        self.assertEqual(payload[1], "rate_limited")
        self.assertLessEqual(exp - time.time(), bot.GT_NEG_CACHE_S["rate_limited"] + 1)

    def test_non_pump_mint_uses_gt_supply_or_ds(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX, "pools?page": gt_pools(),
                    f"/tokens/{OTHER}": R(200, {"data": {"attributes": {"price_usd": "0.002", "fdv_usd": "2000000"}}}),
                    "/ohlcv/hour": gt_ohlcv([0.004])})
        self.assertIn("ATH: ~$4M (GT)", self.line(OTHER))
        for d in (bot._GT_CACHE, bot._ATH_NOTE, bot._DS_CACHE): d.clear()
        bot._GT_BACKOFF_UNTIL = 0.0
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_ATH, "pools?page": gt_pools(),
                    f"/tokens/{OTHER}": R(200, {"data": {"attributes": {}}}), "/ohlcv/hour": gt_ohlcv([0.004])})
        # missing GT supply => DS fallback
        self.assertIn("ATH: ~$40k (DS)", self.line(OTHER))

    def test_shared_rate_limit_and_warn_once(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX_EMPTY, "geckoterminal": R(500)})
        with self.assertLogs("bot", level="INFO") as L:
            for i in range(25):
                bot._GT_CACHE.clear(); bot._DS_CACHE.clear(); bot.coin_info(PUMP)
        self.assertEqual(sum("pump.fun API blocked" in x for x in L.output), 1)
        self.assertLessEqual(sum("geckoterminal.com" in u for u in self.calls), bot.GT_MAX_PER_MIN)
        self.assertTrue(any("rate_limited" in x or "local rate limit" in x for x in L.output))
        self.route({"pump.fun": R(200, {"symbol": "F", "usd_market_cap": 1, "ath_market_cap": 2})})
        with self.assertLogs("bot", level="INFO") as L2:
            bot.coin_info(PUMP)
        self.assertTrue(any("recovered" in x for x in L2.output)); self.assertFalse(bot._PF["blocked"])


if __name__ == "__main__":
    unittest.main()
