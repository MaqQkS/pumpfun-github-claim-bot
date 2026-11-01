"""Phase 6 fixes: fail-closed research Helius budget, claim-aware cache identity, /liq bonding curve."""
import threading
import time
import types
import unittest

import direct_data as dd
import project_context as pc
import telegram_cmds as tc
from research.adapter import claim_identity
from research.prompt import ClaimFacts
from research.tests.test_adapter import Base, ok_body, FFM, TRACE
from tests.test_phase3 import BALA, GROUP, Resp, FakeHTTP, DEX, holder_http, wait_for
from tests.test_phase6 import Svc, ev, PILOT, SIG, FACTS


class Guarded(Base):
    def setUp(self):
        super().setUp(); dd._cache.clear()
        self.addCleanup(lambda: dd.HELIUS_GUARD.__setitem__(0, None)); self.addCleanup(dd._cache.clear)

    def service(self, a, http=None):
        return tc.CommandService(tc.CmdConfig(enabled=True), lambda c, t: None, adapter_factory=lambda: a,
                                 http=http or holder_http(), rpc_url="https://rpc.invalid/?api-key=x", durable_helius=True)


class TestHeliusFailClosed(Guarded):
    def test_reserved_before_call_and_refused_when_exhausted(self):
        a = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"), helius_daily_credits=6, helius_worst_case=0)
        h = holder_http(); s = self.service(a, h)
        self.assertIn("holders", s._cmd_holders((FFM,)).lower())
        posts = [c for c in h.calls if c[0] == "POST"]; self.assertEqual(len(posts), 4)
        rows = [r for r in a.ledger.rows(20) if r["source"] == "direct"]; self.assertEqual(sum(r["helius_measured"] for r in rows), 4)
        dd._cache.clear(); n0 = len(h.calls)
        self.assertIn("paused", s._cmd_holders((FFM,)))                                     # 4 + 4 > 6: pre-check refuses
        self.assertEqual([c for c in h.calls[n0:] if c[0] == "POST"], [])
        with self.assertRaises(dd.HeliusBudgetError):                                       # single call over the cap
            dd._rpc("https://rpc.invalid/", "getAsset", {"id": FFM}, h)
        self.assertEqual([c for c in h.calls[n0:] if c[0] == "POST"], [])

    def test_ledger_error_fails_closed(self):
        a = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"))
        s = self.service(a); h = holder_http(); s.http = h
        def boom(*x, **k): raise RuntimeError("db locked")
        a.ledger.reserve_direct_helius = boom; a.ledger.helius_allowed = boom
        self.assertIn("ledger unavailable", s._cmd_holders((FFM,)))
        with self.assertRaises(dd.HeliusBudgetError):
            dd._rpc("https://rpc.invalid/", "getTokenSupply", [FFM], h)
        self.assertEqual([c for c in h.calls if c[0] == "POST"], [])
        self.assertEqual(pc.metadata(FFM, h, "https://rpc.invalid/"), {})                  # metadata omitted, no call
        self.assertEqual([c for c in h.calls if c[0] == "POST"], [])

    def test_auto_sub_limit_applies_to_metadata(self):
        a = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"))
        self.service(a)
        h = FakeHTTP({}, {"getAsset": {"content": {"metadata": {"description": "x"}}}})
        with dd.helius_scope("auto", {"daily_helius": 5}):
            self.assertEqual(pc.metadata(FFM, h, "https://rpc.invalid/"), {})                # getAsset ~10 > 5: refused
        self.assertEqual([c for c in h.calls if c[0] == "POST"], [])
        with dd.helius_scope("auto", {"daily_helius": 50}):
            self.assertEqual(pc.metadata(FFM, h, "https://rpc.invalid/")["description"], "x")
        self.assertEqual({r["source"] for r in a.ledger.rows(5)}, {"direct_auto"})
        self.assertEqual(a.ledger.auto_totals()["helius_today"], 10); self.assertEqual(a.ledger.auto_totals()["runs_today"], 0)

    def test_no_unguarded_call_without_guard_install_is_unchanged(self):
        h = holder_http()
        dd._rpc("https://rpc.invalid/", "getTokenSupply", [FFM], h)                         # tests/offline: no guard -> allowed
        self.assertEqual(len([c for c in h.calls if c[0] == "POST"]), 1)


class TestAutoRejectedNoDirectCalls(Svc):
    def tearDown(self):
        dd.HELIUS_GUARD[0] = None; dd._cache.clear()

    def test_budget_rejected_job_makes_no_helius_calls(self):
        a, s = self.build(lambda n: (200, ok_body(), 0, "grok-4.3"))
        dd.HELIUS_GUARD[0] = s._helius_guard
        h = FakeHTTP({"dexscreener": Resp(200, DEX)}, {"getAsset": {"content": {}}}); s.http = h
        self.deliver()
        self.auto.cfg = PILOT
        self.auto._budget_reason = lambda: None                                             # pass the early check ...
        a.ledger.check = lambda **k: types.SimpleNamespace(ok=False, reason="auto_runs: 3 of 3 today")   # ... runtime refuses
        self.auto.on_alert(ev())
        self.assertTrue(wait_for(lambda: self.store.states().get("skipped") == 1, 5), self.store.states())
        self.assertEqual(h.calls, []); self.assertEqual(self.stub.calls, 0); self.assertEqual(self.replies, [])


class TestScannerUntouched(unittest.TestCase):
    def test_scanner_rpc_never_hits_guard(self):
        import pathlib
        from tests.test_phase3 import _load
        bot = _load("bot_scan_guard_check", pathlib.Path(__file__).resolve().parents[1] / "bot.py")   # fresh, unpatched copy
        hits = []
        dd.HELIUS_GUARD[0] = lambda *a: (hits.append(a), False)[1]                          # would refuse everything
        try:
            from unittest import mock
            sent = []
            fake = types.SimpleNamespace(post=lambda url, json=None, **k: (sent.append(json["method"]), Resp(200, {"result": {"ok": 1}}))[1])
            with mock.patch.object(bot, "requests", fake), mock.patch.object(bot.time, "sleep", lambda s: None):
                self.assertEqual(bot.rpc("getTransaction", ["SIG"], tries=1), {"ok": 1})
            self.assertEqual(sent, ["getTransaction"]); self.assertEqual(hits, [])
            import inspect
            self.assertNotIn("dd._rpc", inspect.getsource(bot)); self.assertNotIn("HELIUS_GUARD", inspect.getsource(bot.rpc))
        finally:
            dd.HELIUS_GUARD[0] = None


class TestCacheIdentity(Base):
    def test_same_claim_hits_new_claim_misses(self):
        a = self.make(lambda n: (200, ok_body(), 0.05, "grok-4.3"))
        f1 = ClaimFacts(mint=FFM, github_login="NubsCarson", claim_sol=5.52, tx_signature=SIG)
        f2 = ClaimFacts(mint=FFM, github_login="SomeoneElse", claim_sol=1.1, tx_signature="2" * 87)
        self.assertTrue(a.research(f"/re {FFM}", f1).ok); self.assertEqual(self.stub.calls, 1)
        self.assertTrue(a.research(f"/re {FFM}", f1).cached); self.assertEqual(self.stub.calls, 1)
        r = a.research(f"/re {FFM}", f2); self.assertFalse(r.cached); self.assertEqual(self.stub.calls, 2)
        self.assertIn("SomeoneElse", self.stub.questions[1])
        self.assertTrue(a.research(f"/re {FFM}").ok); self.assertEqual(self.stub.calls, 3)          # no facts = 'none'
        self.assertTrue(a.research(f"/re {FFM}").cached)

    def test_identical_inflight_still_coalesced(self):
        a = self.make(lambda n: (200, ok_body(), 0.5, "grok-4.3"))
        f = ClaimFacts(mint=FFM, github_login="NubsCarson", claim_sol=5.52, tx_signature=SIG)
        out = []
        ts = [threading.Thread(target=lambda: out.append(a.research(f"/re {FFM}", f))) for _ in range(3)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(self.stub.calls, 1); self.assertEqual(sum(r.deduped for r in out), 2)

    def test_identity_values(self):
        self.assertEqual(claim_identity(None), "none"); self.assertEqual(claim_identity(ClaimFacts(mint=FFM)), "none")
        a = claim_identity(ClaimFacts(mint=FFM, tx_signature=SIG)); b = claim_identity(ClaimFacts(mint=FFM, tx_signature=SIG + "x"))
        self.assertNotEqual(a, b)

    def test_auto_jobs_keyed_on_event(self):
        import auto_research as ar, tempfile
        st = ar.JobStore(tempfile.mkdtemp() + "/j.sqlite3")
        self.assertIsNotNone(st.claim(SIG, FFM, GROUP, 1, {})); self.assertIsNotNone(st.claim("NEWCLAIM", FFM, GROUP, 2, {}))
        self.assertIsNone(st.claim(SIG, FFM, GROUP, 1, {}))


class TestLiqBondingCurve(unittest.TestCase):
    def test_bonding_curve(self):
        for d in ({"ok": True, "pairs": 1, "symbol": "phylax", "dex": "pumpfun", "pair": "84MvU5XiLxD3yEVDbKdYd6avpjkkFmR3Wf8fbCKywQX",
                   "liq_usd": None, "mc": 4400, "vol24": 95800},
                  {"ok": True, "pairs": 1, "symbol": "X", "dex": "pumpswap", "pair": None, "liq_usd": None, "mc": 10, "vol24": 1}):
            out = tc.format_liquidity(FFM, d)
            self.assertIn(": on bonding curve", out); self.assertNotIn("n/a", out); self.assertIn("📈 MC", out)
        self.assertIn("dexscreener.com/solana/84MvU5", tc.format_liquidity(FFM, {**d, "pair": "84MvU5XiLxD3yEVDbKdYd6avpjkkFmR3Wf8fbCKywQX"}))
        normal = tc.format_liquidity(FFM, {"ok": True, "pairs": 1, "symbol": "FFM", "dex": "pumpswap", "pair": "P", "liq_usd": 2500,
                                           "liq_quote": 4.3, "quote": "SOL", "mc": 2400, "vol24": 8400})
        self.assertIn("liquidity: <b>$2.5k</b>", normal)


if __name__ == "__main__":
    unittest.main()
