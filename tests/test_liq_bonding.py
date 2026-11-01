import unittest
import telegram_cmds as tc

PUMP = "DMfgsDZJjVqDor9j8jNZRiNaL2ZZTPzvnn9hX1GDpump"
OTHER = "9r33aA9jgEEhaSRzFVr7xa43MMaK7bJRcpjr5RyaAUnZ"


class TestLiqBonding(unittest.TestCase):
    def test_no_pair_pump_mint(self):
        out = tc.format_liquidity(PUMP, {"ok": True, "pairs": 0})
        self.assertIn(": on bonding curve", out); self.assertIn("DMfg…pump", out); self.assertNotIn("n/a", out)
        self.assertIn("$LUNAR</b>: on bonding curve", tc.format_liquidity(PUMP, {"ok": True, "pairs": 0, "symbol": "LUNAR"}))

    def test_pumpfun_dex_zero_liq(self):
        out = tc.format_liquidity(PUMP, {"ok": True, "pairs": 1, "symbol": "phylax", "dex": "pumpfun", "pair": "P1", "liq_usd": None, "mc": 4400, "vol24": 95800})
        self.assertIn("$phylax</b>: on bonding curve", out); self.assertIn("📈 MC", out); self.assertIn("dexscreener.com/solana/P1", out)

    def test_normal_pool_unchanged(self):
        out = tc.format_liquidity(OTHER, {"ok": True, "pairs": 1, "symbol": "FFM", "dex": "pumpswap", "pair": "P", "liq_usd": 2500,
                                          "liq_quote": 4.3, "quote": "SOL", "mc": 2400, "vol24": 8400})
        self.assertIn("$FFM</b> liquidity: <b>$2.5k</b> (4.3 SOL) on PumpSwap", out); self.assertNotIn("bonding", out)

    def test_non_pump_no_pair(self):
        out = tc.format_liquidity(OTHER, {"ok": True, "pairs": 0})
        self.assertIn("No DexScreener pool found", out); self.assertNotIn("bonding", out)


if __name__ == "__main__":
    unittest.main()
