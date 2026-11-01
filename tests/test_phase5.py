"""Phase 5 reliability: command coverage, reply cases, concurrency, Queen failure modes, cost, scrubbing, testers."""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import alert_map as am
import telegram_cmds as tc
from research import load_settings
from research.adapter import ResearchAdapter, ResearchResult
from research.ledger import Ledger
from research.router import RoutePlan
from research.prompt import ClaimFacts
from research.tests.test_adapter import Base, ok_body, FFM, TRACE, MARSX
from tests.test_phase3 import BALA, GROUP, STRANGER, FakeAdapter, Resp, svc, wait_for, FakeHTTP, DEX, holder_http
from tests.test_phase4 import BOT_ID, SINGLE_MINT, MULTI, OTHER, FIX, alert_msg, cmd_msg, amap, tg_render

ROOT = Path(__file__).resolve().parents[1]
TIER0 = ["/help", "/h", "/status", "/st", f"/holders {FFM}", f"/ho {FFM}", "/github NubsCarson", "/gh NubsCarson",
         f"/liquidity {FFM}", f"/liq {FFM}"]
UNAVAILABLE = ["narrative", "nar", "developer", "dev", "wallets", "wa", "risk", "rk", "bundles", "bu", "snipers", "sn",
               "socials", "soc", "zauth", "za", "claims", "cl"]


def send(s, text, chat=GROUP, user=BALA, reply=None):
    return s.handle(chat, user, tc.parse_command(text, "GitClaimed_bot"), None, reply)


def gh_http():
    user = {"login": "NubsCarson", "name": "nubs", "id": 1, "type": "User", "created_at": "2024-01-01T00:00:00Z",
            "followers": 91, "public_repos": 31, "html_url": "https://github.com/NubsCarson"}
    return FakeHTTP({"dexscreener": Resp(200, DEX), "/users/NubsCarson/repos": Resp(200, []), "/users/NubsCarson": Resp(200, user)})


class TestCommands(unittest.TestCase):
    def test_tier0_aliases_and_d_make_zero_llm_calls(self):
        for cmd in TIER0 + ["/d " + c.lstrip("/") for c in TIER0]:
            ad = FakeAdapter(); s, sent = svc(adapter=ad, http=gh_http())
            s.ctx.status = lambda: {"ws_connected": True, "subscribers": 2}
            self.assertTrue(send(s, cmd), cmd)
            self.assertTrue(wait_for(lambda: sent), cmd)
            time.sleep(0.02)
            self.assertEqual(ad.calls, [], cmd); self.assertEqual(s.llm_calls, 0, cmd)
            self.assertNotIn("Command failed", sent[-1][1], cmd)

    def test_unavailable_commands_never_silent(self):
        for name in UNAVAILABLE + ["d re " + FFM, "d dev " + FFM, "d wa " + FFM, "d", "d nar " + FFM, "nosuchcmd"]:
            ad = FakeAdapter(); s, sent = svc(adapter=ad)
            self.assertTrue(send(s, f"/{name} {FFM}" if " " not in name and name != "d" else f"/{name}"), name)
            self.assertTrue(wait_for(lambda: sent), name)
            txt = sent[0][1]
            self.assertTrue(any(k in txt for k in ("coming soon", "disabled", "Unknown command", "/d is only", "Use <code>/d")), (name, txt))
            self.assertEqual(ad.calls, [], name)

    def test_research_route_is_43_low_no_escalation(self):
        st = load_settings(env_file="/nonexistent", environ={})
        self.assertEqual((st.standard_model, st.standard_effort, st.deep_enabled), ("grok-4.3", "high", False))
        self.assertEqual(st.worst_case_usd["grok-4.3"], 0.20); self.assertEqual(st.timeout_standard_s, 190.0)
        from research import route
        for t in (f"/re {FFM}", f"/research {FFM} please go deep and thorough " * 3, f"/d re {FFM}"):
            p = route(t, st)
            self.assertNotEqual(p.model, "grok-4.7", t)
        self.assertTrue(route(f"/d re {FFM}", st).error)

    def test_research_output_contract(self):
        facts = ClaimFacts(mint=FFM, symbol="$FFM", github_login="NubsCarson", claim_sol=5.52, tx_signature="675ePaJEeYQ9zsvHvW62QH26RbxaiU8yUdV9weW1WXe3Jepg92Ti2XA26c81uBqzisaQdDdmEvCkaK5tHfYXJXBi")
        ans = (f"NARRATIVE: about {FFM.lower()} see https://dexscreener.com/solana/{FFM.lower()} and https://Example.com/AbC\n"
               "VERDICT: high risk\nDATA:\n- liq: $2k")
        res = ResearchResult(True, RoutePlan("research", 1, True, model="grok-4.3", effort="low", mint=FFM), ans,
                             usage={"actual_usd": 0.0201}, latency_ms=12400)
        out = tc.format_research(FFM, res, facts)
        self.assertNotIn(FFM.lower(), out); self.assertIn(f"https://dexscreener.com/solana/{FFM}", out)
        self.assertIn("https://Example.com/AbC", out)
        self.assertIn("(interpretation)", out); self.assertIn("⚠️ high risk", out)
        self.assertLess(out.index("AI take"), out.index("GitClaim facts")); self.assertLess(out.index("GitClaim facts"), out.index("Key data"))
        self.assertTrue(out.rstrip().endswith("🤖 grok-4.3 · $0.0201 · 12.4s"))

    def test_scrub_ai_text(self):
        bad = ("NARRATIVE: see /Users/balachinthala/queen-harness/.env and C:\\Users\\x\\a.txt, mail me@site.io, "
               "HELIUS_API_KEYS=abc TELEGRAM_BOT_TOKEN, xai-" + "a" * 40 + " ghp_" + "b" * 36 + " 123456789:" + "c" * 35 +
               f" https://rpc.x/?api-key=zzz mint {FFM} https://dexscreener.com/solana/{FFM}\nVERDICT: low")
        res = ResearchResult(True, None, bad, usage={"actual_usd": 0.02})
        out = tc.format_research(FFM, res, ClaimFacts(mint=FFM))
        for leak in ("/Users/", "C:\\Users", "me@site.io", "HELIUS_API_KEYS", "TELEGRAM_BOT_TOKEN", "xai-aaaa", "ghp_bbb", "123456789:", "zzz"):
            self.assertNotIn(leak, out, leak)
        self.assertIn(FFM, out); self.assertIn(f"https://dexscreener.com/solana/{FFM}", out)

    def test_help_mentions_reply_and_fits_mobile(self):
        h = tc.help_text()
        self.assertIn("reply to a GitClaim alert", h); self.assertLess(len(h), 1200)


class TestReplyCases(unittest.TestCase):
    """A–J. Several are also in test_phase4; these drive the full service path."""
    def _svc(self, **kw):
        return svc(adapter=kw.pop("adapter", FakeAdapter()), http=kw.pop("http", holder_http()), **kw)

    def test_A_recent_alert_re(self):
        ad = FakeAdapter(); s, sent = self._svc(adapter=ad); a = amap()
        a.record(GROUP, 500, [SINGLE_MINT], "S", {"facts": {"mint": SINGLE_MINT, "github_login": "svm"}})
        send(s, "/re", reply=am.resolve_reply(cmd_msg("/re", alert_msg()), BOT_ID, a))
        self.assertTrue(wait_for(lambda: ad.calls)); self.assertEqual(ad.calls[0][1].github_login, "svm")

    def test_B_historical_holders(self):
        s, sent = self._svc()
        send(s, "/ho", reply=am.resolve_reply(cmd_msg("/ho", alert_msg()), BOT_ID, amap()))
        self.assertTrue(wait_for(lambda: sent)); self.assertNotIn("Send a CA", sent[0][1])

    def test_C_liquidity_full_name(self):
        s, sent = self._svc()
        send(s, "/liquidity", reply=am.resolve_reply(cmd_msg("/liquidity", alert_msg()), BOT_ID, amap()))
        self.assertTrue(wait_for(lambda: sent)); self.assertIn("liquidity", sent[0][1])

    def test_D_explicit_ca(self):
        ad = FakeAdapter(); s, sent = self._svc(adapter=ad)
        send(s, f"/re {OTHER}", reply=am.resolve_reply(cmd_msg(f"/re {OTHER}", alert_msg()), BOT_ID, amap()))
        self.assertTrue(wait_for(lambda: ad.calls)); self.assertIn(OTHER, ad.calls[0][0])

    def test_E_multi(self):
        s, sent = self._svc()
        send(s, "/liq", reply=am.resolve_reply(cmd_msg("/liq", alert_msg("multi")), BOT_ID, amap()))
        self.assertTrue(wait_for(lambda: sent)); self.assertIn("/liq " + MULTI[2], sent[0][1])

    def test_F_unresolved_mint(self):
        h = FIX["single"].replace(f"<code>{SINGLE_MINT}</code>", "(mint not resolved)").replace(SINGLE_MINT, "x")
        t, e = tg_render(h)
        s, sent = self._svc()
        send(s, "/re", reply=am.resolve_reply(cmd_msg("/re", alert_msg(text=t, entities=e)), BOT_ID, amap()))
        self.assertTrue(wait_for(lambda: sent)); self.assertIn("No token CA found", sent[0][1])

    def test_G_other_user_and_other_bot(self):
        for frm in ({"id": STRANGER, "is_bot": False}, {"id": 99, "is_bot": True}):
            ad = FakeAdapter(); s, sent = self._svc(adapter=ad)
            send(s, "/re", reply=am.resolve_reply(cmd_msg("/re", alert_msg(frm=frm)), BOT_ID, amap()))
            self.assertTrue(wait_for(lambda: sent)); self.assertEqual(ad.calls, [])

    def test_H_mention(self):
        ad = FakeAdapter(); s, sent = self._svc(adapter=ad)
        send(s, "/re@GitClaimed_bot", reply=am.resolve_reply(cmd_msg("/re@GitClaimed_bot", alert_msg()), BOT_ID, amap()))
        self.assertTrue(wait_for(lambda: ad.calls)); self.assertIn(SINGLE_MINT, ad.calls[0][0])

    def test_I_mapping_survives_restart(self):
        p = Path(tempfile.mkdtemp()) / "m.sqlite3"
        am.AlertMap(p).record(GROUP, 77, [SINGLE_MINT], "S", {})
        self.assertEqual(am.resolve_reply(cmd_msg("/re", alert_msg(mid=77)), BOT_ID, am.AlertMap(p)).source, "stored")

    def test_J_fallback_real_fixtures(self):
        self.assertEqual(am.parse_alert_message(*tg_render(FIX["single"]))["mints"], [SINGLE_MINT])
        self.assertEqual(am.parse_alert_message(*tg_render(FIX["multi"]))["mints"], MULTI)


class TestConcurrency(unittest.TestCase):
    def test_free_commands_responsive_while_research_busy(self):
        gate = threading.Event(); ad = FakeAdapter(gate=gate); s, sent = svc(adapter=ad, http=holder_http())
        send(s, f"/re {FFM}")
        self.assertTrue(wait_for(lambda: ad.calls))
        t0 = time.time()
        for u, cmd in ((BALA, f"/liq {FFM}"), (BALA, "/h"), (BALA, "/st")):
            t1 = time.time(); self.assertTrue(send(s, cmd)); self.assertLess(time.time() - t1, 0.05)   # handle() never blocks
        self.assertTrue(wait_for(lambda: sum(1 for _, t in sent if "AI take" not in t) >= 4, 2))
        self.free_ms = (time.time() - t0) * 1000
        self.assertLess(self.free_ms, 1500); gate.set()

    def test_many_tokens_single_slot_bounded_queue(self):
        gate = threading.Event(); live, peak = [0], [0]; lock = threading.Lock()

        class A(FakeAdapter):
            def research(self, text, facts=None, context=None):
                with lock:
                    live[0] += 1; peak[0] = max(peak[0], live[0])
                gate.wait(5)
                with lock:
                    live[0] -= 1
                return super().research(text, facts, context)
        ad = A(); s, sent = svc(adapter=ad, max_queue=2)
        for m in (FFM, TRACE, MARSX, SINGLE_MINT, OTHER):
            send(s, f"/re {m}"); time.sleep(0.05)
        self.assertTrue(wait_for(lambda: any("queue full" in t for _, t in sent)))
        self.assertTrue(any("position" in t for _, t in sent))
        gate.set(); self.assertTrue(wait_for(lambda: len(ad.calls) == 3, 5)); self.assertEqual(peak[0], 1)


class TestCoalesceReal(Base):
    def test_same_token_two_chats_one_paid_call(self):
        a = self.make(lambda n: (200, ok_body(), 0.3, "grok-4.3"))
        s, sent = svc(adapter=a, http=FakeHTTP({"dexscreener": Resp(200, DEX)}))
        s.cfg = tc.CmdConfig(enabled=True, testers=frozenset({BALA, 7}), chats=frozenset({GROUP, BALA}))
        send(s, f"/re {FFM}", chat=GROUP); send(s, f"/re {FFM}", chat=BALA, user=BALA); send(s, f"/re {FFM}", chat=GROUP, user=7)
        self.assertTrue(wait_for(lambda: sum("AI take" in t for _, t in sent) >= 2, 8), sent)
        self.assertEqual(self.stub.calls, 1)
        self.assertEqual(len([r for r in a.ledger.rows(10) if r["status"] == "ok"]), 1)   # no duplicate charge
        self.assertTrue(any("cached" in t for _, t in sent))

    def test_failures_and_ledger(self):
        cases = {"empty": (200, json.dumps({"answer": " ", "sources": [], "by": "m", "tools": [], "ms": 1}).encode(), 0, None),
                 "malformed": (200, b"{not json", 0, None), "http500": (500, b"{}", 0, None)}
        for name, beh in cases.items():
            a = self.make(lambda n, b=beh: b)
            r = a.research(f"/re {FFM}")
            self.assertFalse(r.ok, name); self.assertIn("Alerts are unaffected", r.text, name)
            self.assertEqual(self.stub.calls, 1, name)                                    # never retried
            row = a.ledger.rows(1)[0]
            self.assertEqual(row["status"], "error", name)
        a = self.make(lambda n: (200, ok_body(), 3, "grok-4.3"), timeout_standard_s=0.5)
        r = a.research(f"/re {FFM}"); row = a.ledger.rows(1)[0]
        self.assertEqual(r.error, "queen_timeout"); self.assertGreaterEqual(row["actual_usd"] or 0, 0.05)   # worst case charged
        self.assertEqual(self.stub.calls, 1)

    def test_ledger_persists_across_restart(self):
        a = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"))
        self.assertTrue(a.research(f"/re {FFM}").ok)
        t1 = a.ledger.totals()
        b = Ledger(a.s.ledger_path)
        self.assertAlmostEqual(b.totals()["usd_total"], t1["usd_total"]); self.assertEqual(b.totals()["requests"], 1)


class TestQueenSupervisor(unittest.TestCase):
    def test_supervisor_restarts_dummy_bounded(self):
        d = tempfile.mkdtemp()
        env = dict(os.environ, QUEEN_SUP_DATA=d, QUEEN_START_CMD="echo dummy-run; exit 3", QUEEN_RESTART_S="0", QUEEN_MAX_RESTARTS="3")
        p = subprocess.run(["zsh", str(ROOT / "research/queen/queen_supervisor.sh")], env=env, timeout=20, capture_output=True)
        self.assertEqual(p.returncode, 0)
        log = Path(d, "queen-supervisor.log").read_text()
        self.assertEqual(log.count("queen exited 3"), 3); self.assertEqual(Path(d, "queen-serve.log").read_text().count("dummy-run"), 3)

    def test_default_restart_delay_prevents_tight_loop(self):
        src = (ROOT / "research/queen/queen_supervisor.sh").read_text()
        self.assertIn("${QUEEN_RESTART_S:-10}", src)


class TestHelius(unittest.TestCase):
    def test_holders_rpc_error_is_clear(self):
        class Bad(FakeHTTP):
            def post(self, url, json=None, **kw):
                return Resp(429, {})
        s, sent = svc(http=Bad({"dexscreener": Resp(200, DEX)}))
        send(s, f"/ho {SINGLE_MINT}")
        self.assertTrue(wait_for(lambda: sent)); self.assertIn("Helius", sent[0][1]); self.assertNotIn("api-key", sent[0][1])


class TestTesters(unittest.TestCase):
    def test_adding_tester_is_config_only(self):
        st_before = json.dumps({"subscribers": [1, 2]})
        c = tc.CmdConfig.from_env(env={"RESEARCH_COMMANDS_ENABLED": "true", "RESEARCH_TESTER_IDS": "2038104261,123",
                                       "RESEARCH_CHAT_IDS": "-5178862838,2038104261"})
        self.assertEqual(c.testers, frozenset({2038104261, 123}))
        import inspect
        src = inspect.getsource(tc)
        self.assertNotIn("state.json", src); self.assertNotIn("subscribers\"].append", src)
        self.assertEqual(st_before, json.dumps({"subscribers": [1, 2]}))

    def test_user_and_chat_both_required(self):
        c = tc.CmdConfig(enabled=True, testers=frozenset({BALA, 123}), chats=frozenset({GROUP}))
        s = tc.CommandService(c, lambda *_: None)
        self.assertTrue(s.allowed(GROUP, 123)); self.assertFalse(s.allowed(-1009, 123)); self.assertFalse(s.allowed(GROUP, 555))
        self.assertFalse(tc.CommandService(tc.CmdConfig(enabled=False, testers=c.testers, chats=c.chats), lambda *_: None).allowed(GROUP, BALA))


if __name__ == "__main__":
    unittest.main()
