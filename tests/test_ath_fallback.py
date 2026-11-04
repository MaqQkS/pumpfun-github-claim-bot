"""ATH: pump.fun first, GeckoTerminal approximate fallback, labeled n/a, logging. Mocked HTTP only."""
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


class Base(unittest.TestCase):
    def setUp(self):
        for d in (bot._GT_CACHE, bot._ATH_NOTE, bot._creator): d.clear()
        bot._GT_CALLS.clear(); bot._PF.update(fail=0, fail_streak=0, blocked=False, last_warn=0.0)
        self.calls = []
        p = mock.patch.object(bot, "rpc", lambda *a, **k: None); p.start(); self.addCleanup(p.stop)

    def route(self, table):
        def get(url, **k):
            self.calls.append(url)
            for key, resp in sorted(table.items(), key=lambda kv: ("ohlcv" not in kv[0], "pools?page" not in kv[0])):
                if key in url:
                    if isinstance(resp, Exception): raise resp
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
        self.assertEqual(self.line(PUMP), "• MC: $29.2k | ATH: $31.1k")
        self.assertFalse(any("geckoterminal" in u for u in self.calls)); self.assertEqual(bot._creator[PUMP], "C")

    def test_403_cloudflare_falls_back_to_gt(self):
        self.route({"pump.fun": R(403, None, CF, {"server": "cloudflare"}), "dexscreener": DEX,
                    "pools?page": gt_pools(), "/ohlcv/hour": gt_ohlcv([1e-5, 3.11e-5, 2e-5])})
        with self.assertLogs("bot", level="WARNING") as L:
            self.assertEqual(self.line(PUMP), "• MC: $29.2k | ATH: ~$31.1k (GT)")
        self.assertTrue(any("blocked (HTTP 403" in x and "deployer" in x for x in L.output))
        self.assertNotIn(PUMP, bot._creator)                                   # deployer flag degrades (unknown)
        n = len(self.calls); self.line(PUMP)
        self.assertEqual(sum("geckoterminal" in u for u in self.calls[n:]), 0)  # cached per mint
        self.assertEqual(bot._PF["fail"], 2)

    def test_gt_ath_never_below_current_mc(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX, "pools?page": gt_pools(), "/ohlcv/hour": gt_ohlcv([1e-5])})
        self.assertEqual(self.line(PUMP), "• MC: $29.2k | ATH: ~$29.2k (GT)")

    def test_gt_fails_labeled_blocked(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX, "geckoterminal": R(500)})
        self.assertEqual(self.line(PUMP), "• MC: $29.2k | ATH: n/a (pump.fun blocked)")

    def test_not_indexed_and_generic(self):
        self.route({"pump.fun": R(404, None, "nf"), "dexscreener": R(200, []), "geckoterminal": R(200, {"data": []})})
        self.assertEqual(self.line(PUMP), "• MC: n/a | ATH: n/a (not indexed)")
        for d in (bot._GT_CACHE, bot._ATH_NOTE): d.clear()
        self.route({"pump.fun": R(500, None, "err"), "dexscreener": DEX, "geckoterminal": R(500)})
        self.assertEqual(self.line(PUMP), "• MC: $29.2k | ATH: n/a")

    def test_timeouts_never_raise(self):
        self.route({"pump.fun": real.exceptions.ReadTimeout("t"), "dexscreener": DEX, "geckoterminal": real.exceptions.ConnectTimeout("t")})
        self.assertEqual(self.line(PUMP), "• MC: $29.2k | ATH: n/a")
        self.route({"pump.fun": real.exceptions.ReadTimeout("t"), "dexscreener": DEX, "pools?page": gt_pools(), "/ohlcv/hour": gt_ohlcv([5e-5])})
        for d in (bot._GT_CACHE, bot._ATH_NOTE): d.clear()
        self.assertEqual(self.line(PUMP), "• MC: $29.2k | ATH: ~$50k (GT)")

    def test_non_pump_mint_uses_gt_supply_or_skips(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX, "pools?page": gt_pools(),
                    f"/tokens/{OTHER}": R(200, {"data": {"attributes": {"price_usd": "0.002", "fdv_usd": "2000000"}}}),
                    "/ohlcv/hour": gt_ohlcv([0.004])})
        self.assertEqual(self.line(OTHER), "• MC: $29.2k | ATH: ~$4M (GT)")             # 0.004 x (2M / 0.002 = 1B)
        for d in (bot._GT_CACHE, bot._ATH_NOTE): d.clear()
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX, "pools?page": gt_pools(),
                    f"/tokens/{OTHER}": R(200, {"data": {"attributes": {}}}), "/ohlcv/hour": gt_ohlcv([0.004])})
        self.assertEqual(self.line(OTHER), "• MC: $29.2k | ATH: n/a (pump.fun blocked)")

    def test_rate_limit_and_warn_once(self):
        self.route({"pump.fun": R(403, None, CF), "dexscreener": DEX, "geckoterminal": R(500)})
        import logging
        with self.assertLogs("bot", level="INFO") as L:
            for i in range(25):
                bot._GT_CACHE.clear(); bot.coin_info(PUMP)
        self.assertEqual(sum("pump.fun API blocked" in x for x in L.output), 1)       # no log spam
        self.assertLessEqual(sum("geckoterminal" in u for u in self.calls), bot.GT_MAX_PER_MIN)
        self.route({"pump.fun": R(200, {"symbol": "F", "usd_market_cap": 1, "ath_market_cap": 2})})
        with self.assertLogs("bot", level="INFO") as L2:
            bot.coin_info(PUMP)
        self.assertTrue(any("recovered" in x for x in L2.output)); self.assertFalse(bot._PF["blocked"])


if __name__ == "__main__":
    unittest.main()
