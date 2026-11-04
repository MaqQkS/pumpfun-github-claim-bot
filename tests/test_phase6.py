"""Phase 6: automatic research after alerts (OFF by default), durable Helius accounting, bounded catch-up.
Mocked Telegram + stub Queen (no paid calls)."""
import json
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

import alert_map as am
import auto_research as ar
import direct_data as dd
import telegram_cmds as tc
from research.ledger import Ledger
from research.prompt import ClaimFacts
from research.tests.test_adapter import Base, ok_body, FFM, TRACE, MARSX
from tests.test_phase3 import BALA, GROUP, Resp, FakeAdapter, wait_for, FakeHTTP, DEX, TestAlertsUnchanged

ROOT = Path(__file__).resolve().parents[1]
SIG = "675ePaJEeYQ9zsvHvW62QH26RbxaiU8yUdV9weW1WXe3Jepg92Ti2XA26c81uBqzisaQdDdmEvCkaK5tHfYXJXBi"
PILOT = ar.AutoConfig(enabled=True, chats=frozenset({GROUP}), max_per_day=3, daily_usd=0.15, daily_helius=1000, max_queue=2)
FACTS = {"mint": FFM, "symbol": "$FFM", "github_login": "NubsCarson", "github_display": "nubs", "claim_sol": 5.52,
         "claim_kind": "FIRST claim ever", "signer_tags": ["automated signer"], "tx_signature": SIG}


def ev(**kw):
    d = dict(sig=SIG, kind="FIRST claim ever", mint=FFM, mints=[FFM], share_sol=5.52, origin="live", tags=(), facts=FACTS)
    d.update(kw); return ar.AlertEvent(**d)


def tmp(name):
    return Path(tempfile.mkdtemp()) / name


class TestConfigAndEligibility(unittest.TestCase):
    def test_disabled_by_default(self):
        for env in ({}, {"AUTO_RESEARCH_ENABLED": "true"}):
            c = ar.AutoConfig.from_env(env=env)
            self.assertEqual((c.chats, c.max_per_day, c.daily_usd, c.daily_helius, c.catchup_enabled), (frozenset(), 0, 0.0, 0, False))
        from research.config import _parse_env_file
        c = ar.AutoConfig.from_env(env=_parse_env_file(ROOT / "research/research.env.example"))
        self.assertFalse(c.enabled); self.assertEqual(c.max_per_day, 0); self.assertFalse(c.catchup_enabled)
        # enabled flag alone never researches: no chats, zero budget
        self.assertIsNotNone(ar.decide(ev(), ar.AutoConfig(enabled=True), GROUP, {GROUP: 1}))

    def test_eligible(self):
        self.assertIsNone(ar.decide(ev(), PILOT, GROUP, {GROUP: 10}))

    def test_ineligible_reasons(self):
        cases = [(dict(), dict(cfg=ar.AutoConfig()), "disabled"),
                 (dict(origin="catchup"), {}, "origin=catchup"), (dict(origin="replay"), {}, "origin=replay"),
                 ({}, dict(chat=BALA), "not opted in"), ({}, dict(delivered={}), "not delivered"),
                 (dict(kind="REPEAT claim"), {}, "FIRST only"), (dict(kind="FIRST claim on this token (unverified)"), {}, "unverified"),
                 (dict(mint=None), {}, "unresolved"), (dict(mint="NotAMint0OIl"), {}, "unresolved"),
                 (dict(tags=("token ambiguous (3 fee configs)",)), {}, "ambiguous"), (dict(share_sol=0.29), {}, "below min"),
                 ({}, dict(cfg=ar.AutoConfig(enabled=True, chats=frozenset({GROUP}))), "budget is zero")]
        for e_kw, o, want in cases:
            why = ar.decide(ev(**e_kw), o.get("cfg", PILOT), o.get("chat", GROUP), o.get("delivered", {GROUP: 10}))
            self.assertIn(want, why or "", (e_kw, o))


class TestJobStore(unittest.TestCase):
    def test_dedupe_and_restart(self):
        p = tmp("j.sqlite3"); now = [1000.0]
        s = ar.JobStore(p, now=lambda: now[0])
        a = s.claim(SIG, FFM, GROUP, 10, {}); self.assertIsNotNone(a)
        self.assertIsNone(s.claim(SIG, FFM, GROUP, 10, {}))                 # duplicate event
        self.assertIsNone(ar.JobStore(p).claim(SIG, FFM, GROUP, 10, {}))    # after restart
        self.assertIsNotNone(s.claim(SIG, FFM, -42, 11, {}))                # different chat is a different identity
        b = s.claim("S2", FFM, GROUP, 12, {}); c = s.claim("S3", FFM, GROUP, 13, {})
        s.set(a, "running"); now[0] += 700
        d = s.claim("S4", FFM, GROUP, 14, {})
        fresh = ar.JobStore(p, now=lambda: now[0]).recover(600)
        self.assertEqual(s.get(a)["state"], "failed_unknown"); self.assertEqual(s.get(b)["state"], "expired")
        self.assertEqual([r["id"] for r in fresh], [d])


class TestLedger(Base):
    def test_auto_caps_separate_and_global(self):
        a = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"))
        lim = {"daily_usd": 0.15, "max_per_day": 2, "daily_helius": 100000}
        self.assertTrue(a.research(f"/re {FFM}", source="auto", auto=lim).ok)
        self.assertTrue(a.research(f"/re {TRACE}", source="auto", auto=lim).ok)
        r = a.research(f"/re {MARSX}", source="auto", auto=lim); self.assertEqual(r.error, "budget"); self.assertIn("auto_runs", r.text)
        self.assertTrue(a.research(f"/re {MARSX}").ok)                      # manual unaffected by auto cap
        self.assertEqual({x["source"] for x in a.ledger.rows(10)}, {"auto", "manual"})
        self.assertEqual(a.research(f"/re {SIG[:44]}", source="auto", auto=None).error, "budget")   # no limits => refused

    def test_auto_usd_and_global_cap(self):
        a = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"), budget_daily_usd=0.06)
        lim = {"daily_usd": 1.0, "max_per_day": 9, "daily_helius": 100000}
        self.assertTrue(a.research(f"/re {FFM}", source="auto", auto=lim).ok)        # 0.022
        r = a.research(f"/re {TRACE}", source="auto", auto=lim); self.assertIn("daily_budget", r.text)   # global wins
        b = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"))
        r = b.research(f"/re {FFM}", source="auto", auto={"daily_usd": 0.04, "max_per_day": 9, "daily_helius": 100000})
        self.assertIn("auto_daily_usd", r.text); self.assertEqual(self.stub.calls, 0)

    def test_migration_adds_source(self):
        p = Path(self.tmp) / "old.sqlite3"
        c = sqlite3.connect(p); c.executescript(__import__("research.ledger", fromlist=["SCHEMA"]).SCHEMA)
        c.execute("INSERT INTO requests(ts,day,status,reserved_usd) VALUES(1,'2026-10-31','ok',0.02)"); c.commit(); c.close()
        self.assertEqual(Ledger(str(p)).rows(1)[0]["source"], "manual")


class TestHeliusAccounting(Base):
    def setUp(self):
        super().setUp(); self.addCleanup(lambda: dd.HELIUS_SINK.__setitem__(0, None)); dd._cache.clear()

    def test_ho_persisted_enforced_and_survives_restart(self):
        a = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"), helius_daily_credits=10, helius_worst_case=0)
        from tests.test_phase3 import holder_http
        s = tc.CommandService(tc.CmdConfig(enabled=True), lambda c, t: None, adapter_factory=lambda: a, http=holder_http(),
                              rpc_url="https://rpc.invalid/?api-key=x", durable_helius=True)
        out = s._cmd_holders((FFM,)); self.assertIn("holders", out.lower())
        rows = [r for r in a.ledger.rows(20) if r["source"] == "direct"]
        self.assertEqual(sum(r["helius_measured"] for r in rows), 4); self.assertTrue(all(r["actual_usd"] is None and r["reserved_usd"] == 0 for r in rows))
        b = Ledger(a.s.ledger_path)                                           # restart
        self.assertEqual(b.totals()["helius_today"], 4); self.assertEqual(b.totals()["usd_total"], 0)
        dd._cache.clear(); s._cmd_holders((FFM,))                              # 8 used
        dd._cache.clear(); self.assertIn("Holder lookups paused", s._cmd_holders((FFM,)))   # 8+4 > 10
        dd._cache.clear()
        s2 = tc.CommandService(tc.CmdConfig(enabled=True), lambda c, t: None, adapter_factory=lambda: type(a)(a.s), durable_helius=True,
                               http=holder_http(), rpc_url="x")
        self.assertIn("Holder lookups paused", s2._cmd_holders((FFM,)))      # limit enforced after restart

    def test_no_double_count_with_queen_meter(self):
        a = self.make(lambda n: (200, ok_body(), 0, "grok-4.3"))
        self.assertTrue(a.research(f"/re {FFM}").ok)
        q = [r for r in a.ledger.rows(5) if r["source"] == "manual"][0]["helius_measured"]
        self.assertEqual(a.ledger.totals()["helius_total"], q)                # Queen meter only; no direct rows added by adapter


class Svc(Base):
    """CommandService + real adapter on a stub Queen, auto reports captured."""
    def build(self, behavior, max_queue=2, send_ok=True, **kw):
        a = self.make(behavior, **kw)
        self.sent, self.replies = [], []
        s = tc.CommandService(tc.CmdConfig(enabled=True), lambda c, t: self.sent.append((c, t)), adapter_factory=lambda: a,
                              http=FakeHTTP({"dexscreener": Resp(200, DEX)}), rpc_url="https://rpc.invalid/?api-key=x",
                              send_reply=lambda c, t, r: (self.replies.append((c, t, r)), send_ok)[1], auto_max_queue=max_queue)
        self.alerts = am.AlertMap(Path(self.tmp) / "a.sqlite3"); self.store = ar.JobStore(Path(self.tmp) / "j.sqlite3")
        self.auto = ar.AutoResearch(PILOT, self.store, s, self.alerts)
        return a, s

    def deliver(self, sig=SIG, mint=FFM, chats=(GROUP, BALA), mid=10):
        for i, c in enumerate(chats):
            self.alerts.record(c, mid + i, [mint], sig, {})


class TestAutoFlow(Svc):
    def test_eligible_job_runs_once_and_replies_to_alert(self):
        a, s = self.build(lambda n: (200, ok_body("NARRATIVE: FFM is a dating-agent meme.\nVERDICT: high risk\nDATA:\n- liq: $2k"), 0.1, "grok-4.3"))
        self.deliver()
        self.assertEqual(self.auto.on_alert(ev()), [(GROUP, "queued")])
        self.assertTrue(wait_for(lambda: self.replies, 5))
        chat, text, reply_to = self.replies[0]
        self.assertEqual((chat, reply_to), (GROUP, 10)); self.assertTrue(text.startswith("🧠 <b>Queen Auto Research</b>"))
        self.assertIn(FFM, text); self.assertIn("Not a trading recommendation", text); self.assertIn("grok-4.3", text)
        self.assertTrue(wait_for(lambda: self.store.states().get("completed") == 1))
        self.assertEqual(self.auto.on_alert(ev())[0][1], "already triggered (persistent dedupe)")
        self.assertEqual(self.stub.calls, 1); self.assertEqual(self.sent, [])
        self.assertEqual([r["model"] for r in a.ledger.rows(5) if r["source"] == "auto"], ["grok-4.3"])
        self.assertNotIn("grok-4.7", "".join(self.stub.questions))

    def test_not_delivered_no_research(self):
        a, s = self.build(lambda n: (200, ok_body(), 0, "grok-4.3"))
        self.deliver(chats=(BALA,))
        self.assertIn("not delivered", self.auto.on_alert(ev())[0][1]); time.sleep(0.2); self.assertEqual(self.stub.calls, 0)

    def test_outside_test_group_never(self):
        a, s = self.build(lambda n: (200, ok_body(), 0, "grok-4.3"))
        self.deliver(chats=(BALA, -100777))
        self.auto.cfg = ar.AutoConfig(**{**PILOT.__dict__, "chats": frozenset({GROUP})})
        self.assertEqual([c for c, _ in self.auto.on_alert(ev())], [GROUP]); time.sleep(0.2)
        self.assertEqual(self.replies, []); self.assertEqual(self.stub.calls, 0)

    def test_multi_token_primary_only(self):
        a, s = self.build(lambda n: (200, ok_body(), 0, "grok-4.3"))
        self.deliver()
        self.auto.on_alert(ev(mints=[FFM, TRACE, MARSX]))
        self.assertTrue(wait_for(lambda: self.replies, 5)); time.sleep(0.2)
        self.assertEqual(self.stub.calls, 1); self.assertIn(FFM, self.stub.questions[0]); self.assertNotIn(TRACE, self.stub.questions[0])

    def test_manual_has_priority_and_queue_bounded(self):
        gate = threading.Event()
        a, s = self.build(lambda n: (200, ok_body(), 0.0, "grok-4.3"), max_queue=1)
        order = []
        real = a.research
        def research(text, facts=None, context=None, source="manual", auto=None):
            order.append((source, text.split()[-1])); gate.wait(5)
            return real(text, facts, context, source, auto)
        a.research = research
        s.handle(GROUP, BALA, tc.parse_command(f"/re {MARSX}", None))               # occupies the slot
        self.assertTrue(wait_for(lambda: order))
        self.deliver(sig="S1", mint=FFM); self.deliver(sig="S2", mint=TRACE, mid=20)
        self.assertEqual(self.auto.on_alert(ev(sig="S1"))[0][1], "queued")
        self.assertIn("auto queue full", self.auto.on_alert(ev(sig="S2", mint=TRACE, mints=[TRACE]))[0][1])
        s.handle(GROUP, BALA, tc.parse_command(f"/re {SIG[:44]}", None))             # manual arrives after the auto job
        gate.set()
        self.assertTrue(wait_for(lambda: len(order) == 3, 8))
        self.assertEqual([o[0] for o in order], ["manual", "manual", "auto"])

    def test_queen_failures_no_retry(self):
        for name, beh, kw in (("offline", None, {}), ("malformed", lambda n: (200, b"{bad", 0, None), {}),
                              ("timeout", lambda n: (200, ok_body(), 2, "grok-4.3"), {"timeout_standard_s": 0.3})):
            self.setUp()                                                        # fresh ledger / job store per case
            if beh is None:
                a, s = self.build(lambda n: (200, ok_body(), 0, "grok-4.3")); self.stub.close()
            else:
                a, s = self.build(beh, **kw)
            self.deliver()
            self.auto.on_alert(ev())
            self.assertTrue(wait_for(lambda: self.store.states().get("failed") == 1, 20), (name, self.store.states()))
            self.assertEqual(self.replies, [], name)
            if name != "offline":
                self.assertEqual(self.stub.calls, 1, name)
            if name == "timeout":
                self.assertGreaterEqual(a.ledger.rows(1)[0]["actual_usd"], 0.05)

    def test_failed_delivery_not_retried(self):
        a, s = self.build(lambda n: (200, ok_body(), 0, "grok-4.3"), send_ok=False)
        self.deliver(); self.auto.on_alert(ev())
        self.assertTrue(wait_for(lambda: self.store.states().get("undelivered") == 1, 5))
        time.sleep(0.3); self.assertEqual(self.stub.calls, 1); self.assertEqual(len(self.replies), 1)
        self.assertEqual(len([r for r in a.ledger.rows(9) if r["source"] == "auto"]), 1)

    def test_budget_exhausted_skips(self):
        a, s = self.build(lambda n: (200, ok_body(), 0, "grok-4.3"), budget_total_usd=0.03)
        self.deliver()
        self.assertEqual(self.auto.on_alert(ev())[0][1], "global xAI budget"); self.assertEqual(self.stub.calls, 0)

    def test_restart_running_job_not_retried(self):
        a, s = self.build(lambda n: (200, ok_body(), 0, "grok-4.3"))
        jid = self.store.claim(SIG, FFM, GROUP, 10, {"facts": FACTS}); self.store.set(jid, "running")
        ar.AutoResearch(PILOT, ar.JobStore(self.store.path), s, self.alerts).recover()
        time.sleep(0.3)
        self.assertEqual(self.store.get(jid)["state"], "failed_unknown"); self.assertEqual(self.stub.calls, 0)
        self.deliver(); self.assertIn("already triggered", self.auto.on_alert(ev())[0][1])


class TestBotIntegration(TestAlertsUnchanged):
    """Real bot.handle_sig with mocked chain + Telegram: alert first, unchanged payload, auto after, catch-up safe."""
    def test_format_and_send_identical(self):
        pass                                                                    # covered in test_phase3

    def setup_bot(self, cfg=PILOT, post_ok=True):
        import bot
        posted, mid = [], [100]
        def post(url, json=None, **k):
            posted.append(("post", json.get("chat_id"), json.get("text", "")[:30])); mid[0] += 1
            return Resp(200 if post_ok else 400, {"ok": post_ok, "result": {"message_id": mid[0]}})
        self.patch(bot, [])
        import requests as real
        bot.requests = types.SimpleNamespace(post=post, exceptions=real.exceptions)
        bot.save_state = lambda s: None
        bot.ALERTS = am.AlertMap(tmp("a.sqlite3"))
        calls = []
        class S:
            def enqueue_auto(self, job): calls.append(("enqueue", job.chat, job.mint, job.alert_msg_id)); posted.append(("enqueue",)); return True, "queued"
            def adapter(self):
                return types.SimpleNamespace(ledger=Ledger(str(tmp("l.sqlite3"))), s=__import__("research").load_settings(env_file="/x", environ={}, worst_case_usd={"grok-4.3": 0.05, "grok-4.7": 0.15}))
        bot.AUTO = ar.AutoResearch(cfg, ar.JobStore(tmp("j.sqlite3")), S(), bot.ALERTS)
        bot.get_tx = lambda sig: self.tx("2sMrGNK8i36YRkF5WWCwnaUYuwDJhHe1g2xA8aPvhkjM")
        bot.decode_claims = lambda t: [dict(self.claim())]
        bot.evaluate = lambda sig, t, c: [("FIRST claim ever", FFM, "k", None)]
        return bot, posted, calls

    def test_alert_before_research_and_subscribers_unchanged(self):
        bot, posted, calls = self.setup_bot()
        subs = list(bot.state["subscribers"])
        bot.handle_sig(SIG)
        kinds = [p[0] for p in posted]
        self.assertEqual(kinds, ["post", "post", "enqueue"])                  # both alerts sent before any research job
        self.assertEqual(calls, [("enqueue", GROUP, FFM, 102)])               # only the opted-in chat, reply to its alert
        self.assertEqual(bot.state["subscribers"], subs)
        bot.handle_sig(SIG); self.assertEqual(len(calls), 1)                  # duplicate ws event: seen + store dedupe

    def test_failed_alert_no_research_no_resend(self):
        bot, posted, calls = self.setup_bot(post_ok=False)
        bot.handle_sig(SIG)
        self.assertEqual(calls, []); self.assertEqual(sum(1 for p in posted if p[0] == "post"), 4)   # HTML + plain fallback x2, once

    def test_disabled_default_never_enqueues(self):
        bot, posted, calls = self.setup_bot(cfg=ar.AutoConfig())
        bot.handle_sig(SIG); self.assertEqual(calls, [])

    def test_catchup_and_replay_never_researched_and_deduped(self):
        bot, posted, calls = self.setup_bot(cfg=ar.AutoConfig(**{**PILOT.__dict__, "catchup_enabled": True, "catchup_max_sigs": 5}))
        now = time.time()
        sigs = [{"signature": "C2", "blockTime": now - 10}, {"signature": "C1", "blockTime": now - 20},
                {"signature": SIG, "blockTime": now - 30}, {"signature": "OLD", "blockTime": now - 99999}, {"signature": "ERR", "blockTime": now, "err": {"InstructionError": [0, 1]}}]
        bot.state["seen"] = [SIG]
        seen_order = []
        _orig = bot.rpc; self.addCleanup(setattr, bot, "rpc", _orig)
        bot.rpc = lambda m, p, tries=3: sigs if m == "getSignaturesForAddress" else None
        orig = bot._handle_sig
        bot._handle_sig = lambda sig, send=True, prefix="", origin="live": (seen_order.append((sig, origin)), orig(sig, send, prefix, origin))[1]
        n = bot.catchup_once()
        self.assertEqual(seen_order, [("C1", "catchup"), ("C2", "catchup")])   # oldest first, SIG deduped, old/err skipped
        self.assertEqual(n, 2); self.assertEqual(calls, [])                   # alerts recovered, never auto-researched
        self.assertEqual(bot.catchup_once(), 0)                                # second run: all seen
        self.assertIsNone(bot.handle_sig("C1"))                               # live duplicate of a recovered sig
        bot.handle_sig(SIG, send=False, prefix="🧪 TEST"); self.assertEqual(calls, [])   # replay path

    def test_catchup_off_by_default(self):
        bot, posted, calls = self.setup_bot()
        _orig = bot.rpc; self.addCleanup(setattr, bot, "rpc", _orig)
        bot.rpc = lambda *a, **k: self.fail("no RPC when catch-up is off")
        self.assertEqual(bot.catchup_once(), 0)


class TestAutoReportFormat(unittest.TestCase):
    def test_examples(self):
        from research.adapter import ResearchResult
        from research.router import RoutePlan
        plan = RoutePlan("research", 1, True, model="grok-4.3", effort="low", mint=FFM)
        res = ResearchResult(True, plan, "NARRATIVE: The site says FFM is an AI agent for dating apps; the claimant forks inscription-NFT repos.\n"
                             "VERDICT: high risk: $2.5k liquidity, 14 insiders flagged (RugCheck)\nDATA:\n- holders: top10 13%\n- liquidity: $2.5k (pumpswap)",
                             usage={"actual_usd": 0.0201}, latency_ms=12400)
        f = tc.facts_from_meta({"facts": FACTS}, FFM)
        out = tc.format_auto_research(FFM, res, f, {"links": [("Website", "https://flirtfor.me/")]}, 10)
        self.assertTrue(out.startswith("🧠 <b>Queen Auto Research</b> · $FFM\n<code>" + FFM))
        self.assertIn("Follow-up to the claim alert above", out); self.assertIn("✅ <b>GitClaim facts</b>", out)
        self.assertTrue(out.rstrip().endswith("🤖 grok-4.3 · $0.0201 · 12.4s")); self.assertLess(len(out), 1800)
        print("\n" + out)


if __name__ == "__main__":
    unittest.main()
