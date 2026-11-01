"""Open-group access (RESEARCH_OPEN_GROUP_CHAT_IDS) + persistent paid-/re quotas. Mocks only, zero paid calls."""
import tempfile
import time
import types
import unittest
from pathlib import Path

import alert_map as am
import open_quota as oq
import telegram_cmds as tc
from research.adapter import ResearchResult
from tests.test_phase3 import BALA, GROUP, STRANGER, OTHER_CHAT, FFM, FakeAdapter, FakeHTTP, Resp, DEX, P, wait_for, bot

JED, NEWBIE = 7001, 7002
SIG = "675ePaJEeYQ9zsvHvW62QH26RbxaiU8yUdV9weW1WXe3Jepg92Ti2XA26c81uBqzisaQdDdmEvCkaK5tHfYXJXBi"
MINTS = ["9r33aA9jgEEhaSRzFVr7xa43MMaK7bJRcpjr5RyaAUnZ", "DMfgsDZJjVqDor9j8jNZRiNaL2ZZTPzvnn9hX1GDpump",
         "HMBnTdwPz2gY6pCJRcmQqKT8uSzT5mVxcGFtRDvmpump", "2K8rxRJVf8Sih7C5c55ZbhoKFThQrpw6v3zULHARpump",
         "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU", "84MvU5XiLxD3yEVDbKdYd6avpjkkFmR3Wf8fbCKywQX"]
OPEN = dict(open_groups=frozenset({GROUP}))


class Adapter(FakeAdapter):
    def __init__(self, cost=0.03, cached=False, deduped=False, ok=True):
        super().__init__()
        self.cost, self.cached, self.deduped, self.ok = cost, cached, deduped, ok
        self.s = types.SimpleNamespace(worst_case_usd={"grok-4.3": 0.20, "grok-4.7": 0.15}, standard_model="grok-4.3")

    def research(self, text, facts=None, context=None, **k):
        self.calls.append((text, facts))
        return ResearchResult(self.ok, None, "NARRATIVE: n\nVERDICT: v\nDATA:\n- d", None if self.ok else "queen_unavailable",
                              cached=self.cached, deduped=self.deduped, usage={"actual_usd": self.cost}, ledger_id=1)


def make(adapter=None, qpath=None, **cfg):
    sent = []
    a = adapter or Adapter()
    s = tc.CommandService(tc.CmdConfig(enabled=True, **{**OPEN, **cfg}), lambda c, t: sent.append((c, t)),
                          tc.BotContext(lambda: {"ws_connected": True, "subscribers": 2}, lambda m: None, lambda m: []),
                          adapter_factory=lambda: a, http=FakeHTTP({"dexscreener": Resp(200, DEX)}), rpc_url="https://rpc.invalid/?api-key=x")
    s._quota = oq.OpenGroupQuota(qpath or str(Path(tempfile.mkdtemp()) / "q.sqlite3"), user_max=s.cfg.open_user_max,
                                 group_max=s.cfg.open_group_max, group_usd=s.cfg.open_group_usd)
    s.context_fn = None
    return s, sent, a


def re_(s, sent, user, mint, chat=GROUP, ctype="supergroup", wait=True):
    n = len(sent)
    ok = s.handle(chat, user, P(f"/re {mint}"), chat_type=ctype)
    if wait and ok:
        wait_for(lambda: any(("AI take" in t or "limit" in t or "paused" in t or "failed" in t) for _, t in sent[n:]), 5)
        wait_for(lambda: s.queue_state() == {"running": False, "waiting": 0, "max": s.cfg.max_queue}, 5)
    return ok, [t for _, t in sent[n:]]


class TestAccess(unittest.TestCase):
    def test_1_any_user_help_in_open_group(self):
        s, sent, a = make()
        for i, u in enumerate((JED, NEWBIE, STRANGER)):
            self.assertTrue(s.handle(GROUP, u, P("/h"), chat_type="supergroup"))
            self.assertTrue(wait_for(lambda: sum("Available" in t for _, t in sent) == i + 1))
        self.assertEqual(a.calls, [])

    def test_2_3_any_new_user_can_research_without_whitelist(self):
        s, sent, a = make()
        self.assertNotIn(NEWBIE, s.cfg.testers)
        ok, out = re_(s, sent, NEWBIE, FFM)
        self.assertTrue(ok); self.assertTrue(any("AI take" in t for t in out)); self.assertEqual(len(a.calls), 1)
        self.assertEqual(s.quota().today(GROUP, NEWBIE), {"user": 1, "group": 1, "usd": 0.03})

    def test_all_operational_aliases_and_unavailable_messages(self):
        s, sent, a = make()
        for c in ("/help", "/st", "/status", f"/ho {FFM}", f"/holders {FFM}", f"/liq {FFM}", f"/liquidity {FFM}", f"/gh {FFM}", f"/github {FFM}"):
            n = len(sent); self.assertTrue(s.handle(GROUP, JED, P(c), chat_type="group"), c)
            self.assertTrue(wait_for(lambda: len(sent) > n), c)
        for c in (f"/dev {FFM}", f"/wa {FFM}", f"/nar {FFM}", f"/rk {FFM}", f"/bu {FFM}", f"/sn {FFM}", f"/soc {FFM}", f"/za {FFM}", f"/d re {FFM}"):
            n = len(sent); self.assertTrue(s.handle(GROUP, JED, P(c), chat_type="group"), c)
            self.assertTrue(wait_for(lambda: len(sent) > n), c)
            self.assertTrue(any(w in sent[-1][1] for w in ("coming soon", "disabled", "only for")), (c, sent[-1][1]))
        time.sleep(0.3); self.assertEqual(a.calls, [])                                    # 9: free/unavailable -> zero LLM
        self.assertEqual(s.quota().today(GROUP, JED)["group"], 0)

    def test_5_unauthorized_group_denied(self):
        s, sent, a = make()
        for chat in (OTHER_CHAT, -100123456):
            self.assertFalse(s.handle(chat, JED, P("/h"), chat_type="supergroup"))
            self.assertFalse(s.handle(chat, JED, P(f"/re {FFM}"), chat_type="supergroup"))
        time.sleep(0.2); self.assertEqual(sent, []); self.assertEqual(a.calls, [])

    def test_6_dms_keep_allowlist_and_open_id_never_opens_a_dm(self):
        s, sent, a = make()
        self.assertFalse(s.handle(JED, JED, P("/h"), chat_type="private"))
        self.assertTrue(s.handle(BALA, BALA, P("/h"), chat_type="private"))               # tester DM unchanged
        self.assertFalse(s.handle(GROUP, JED, P("/h"), chat_type="private"))              # forged/odd type: not opened
        self.assertFalse(s.handle(GROUP, JED, P("/h"), chat_type=None))
        self.assertFalse(s.handle(GROUP, JED, P("/h"), chat_type="channel"))
        s2, _, _ = make(open_groups=frozenset())
        self.assertFalse(s2.handle(GROUP, JED, P("/h"), chat_type="supergroup"))          # default: not open
        self.assertTrue(s2.handle(GROUP, BALA, P("/h"), chat_type="supergroup"))          # tester path unchanged
        cfg = tc.CmdConfig.from_env(env={"RESEARCH_OPEN_GROUP_CHAT_IDS": f"{GROUP},{BALA}"})
        self.assertEqual(cfg.open_groups, frozenset({GROUP}))                             # positive (user) ids dropped
        self.assertEqual(tc.CmdConfig.from_env(env={}).open_groups, frozenset())
        d = tc.CmdConfig.from_env(env={})
        self.assertEqual((d.open_user_max, d.open_group_max, d.open_group_usd), (3, 10, 0.25))

    def test_tester_in_open_group_not_quota_limited(self):
        s, sent, a = make(open_user_max=0)
        ok, out = re_(s, sent, BALA, FFM); self.assertTrue(any("AI take" in t for t in out))
        self.assertEqual(s.quota().today(GROUP, BALA)["group"], 0)


class TestQuotas(unittest.TestCase):
    def test_7_per_user_limit(self):
        s, sent, a = make(open_group_usd=5.0)
        for m in MINTS[:3]:
            self.assertTrue(any("AI take" in t for t in re_(s, sent, JED, m)[1]))
        ok, out = re_(s, sent, JED, MINTS[3])
        self.assertTrue(any("you have used 3 of 3" in t for t in out), out); self.assertEqual(len(a.calls), 3)
        self.assertTrue(any("AI take" in t for t in re_(s, sent, NEWBIE, MINTS[3])[1]))   # other user still fine

    def test_7_per_group_limit(self):
        s, sent, a = make(open_group_max=2, open_group_usd=5.0)
        re_(s, sent, JED, MINTS[0]); re_(s, sent, NEWBIE, MINTS[1])
        ok, out = re_(s, sent, STRANGER, MINTS[2])
        self.assertTrue(any("this group has used 2 of 2" in t for t in out), out); self.assertEqual(len(a.calls), 2)

    def test_usd_cap_reserves_worst_case_then_reconciles(self):
        s, sent, a = make(open_group_usd=0.25)
        n = 0
        for i, m in enumerate(MINTS[:6]):
            out = re_(s, sent, [JED, NEWBIE, STRANGER, 7003, 7004, 7005][i], m)[1]
            n += any("AI take" in t for t in out)
        self.assertEqual(n, 2)                         # 0.03 + 0.20 worst <= 0.25 ok; 0.06 + 0.20 > 0.25 -> refused
        self.assertTrue(any("research budget is $0.06 of $0.25" in t for _, t in sent))
        q = oq.OpenGroupQuota(str(Path(tempfile.mkdtemp()) / "x.sqlite3"), user_max=3, group_max=10, group_usd=0.25)
        r1, _ = q.reserve(GROUP, JED, FFM, 0.20)
        self.assertEqual(q.reserve(GROUP, NEWBIE, FFM, 0.20), (None, q.reserve(GROUP, NEWBIE, FFM, 0.20)[1]))   # concurrent: blocked
        q.finish(r1, None); self.assertAlmostEqual(q.today(GROUP)["usd"], 0.20)        # unknown cost keeps worst case

    def test_8_limits_survive_restart(self):
        path = str(Path(tempfile.mkdtemp()) / "q.sqlite3")
        s, sent, a = make(qpath=path, open_group_usd=5.0)
        for m in MINTS[:3]:
            re_(s, sent, JED, m)
        s2, sent2, a2 = make(qpath=path, open_group_usd=5.0)                           # "restart": new service, same file
        self.assertTrue(any("you have used 3 of 3" in t for t in re_(s2, sent2, JED, MINTS[3])[1])); self.assertEqual(a2.calls, [])
        q = oq.OpenGroupQuota(path, user_max=3, group_max=10, group_usd=5, clock=lambda: time.time() + 86400)
        self.assertEqual(q.today(GROUP, JED)["user"], 0)                                # next UTC day resets

    def test_cache_hit_and_dedup_not_counted(self):
        for kw in ({"cached": True}, {"deduped": True}):
            s, sent, a = make(adapter=Adapter(**kw))
            for _ in range(4):
                self.assertTrue(any("AI take" in t for t in re_(s, sent, JED, FFM)[1]))
            self.assertEqual(s.quota().today(GROUP, JED), {"user": 0, "group": 0, "usd": 0}, kw)

    def test_failure_and_budget_refusal(self):
        s, sent, a = make(adapter=Adapter(ok=False))
        re_(s, sent, JED, FFM)
        self.assertEqual(s.quota().today(GROUP, JED)["user"], 1)                         # a Queen call was attempted: counted
        s, sent, a = make()
        s._budget_precheck = lambda *a, **k: "daily_budget"
        out = re_(s, sent, JED, FFM)[1]
        self.assertTrue(any("paused" in t for t in out)); self.assertEqual(s.quota().today(GROUP, JED)["user"], 0)

    def test_quota_store_unavailable_fails_closed(self):
        s, sent, a = make()
        s._quota.reserve = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))
        out = re_(s, sent, JED, FFM)[1]
        self.assertTrue(any("usage tracker is unavailable" in t for t in out)); self.assertEqual(a.calls, [])


class TestBotPath(unittest.TestCase):
    def setUp(self):
        self.orig = (bot.CMDS, bot.ALERTS, bot.BOT_ID[0])
        self.addCleanup(self.restore)
        self.s, self.sent, self.a = make()
        bot.CMDS = self.s; bot._SEEN_UPDATES.clear()
        bot.ALERTS = am.AlertMap(str(Path(tempfile.mkdtemp()) / "a.sqlite3")); bot.BOT_ID[0] = 424242

    def restore(self):
        bot.CMDS, bot.ALERTS, bot.BOT_ID[0] = self.orig

    def upd(self, uid, chat, ctype, user, text, reply=None):
        m = {"message_id": uid, "date": int(time.time()), "chat": {"id": chat, "type": ctype}, "from": {"id": user, "is_bot": False}, "text": text}
        if reply: m["reply_to_message"] = reply
        bot.tg_handle_update({"update_id": 880000 + uid, "message": m})

    def test_4_reply_re_to_alert_by_new_user(self):
        subs = list(bot.state["subscribers"])
        bot.ALERTS.record(GROUP, 50, [FFM], SIG, {})
        self.upd(1, GROUP, "supergroup", NEWBIE, "/re", reply={"message_id": 50, "date": int(time.time()), "chat": {"id": GROUP, "type": "supergroup"},
                                                               "from": {"id": 424242, "is_bot": True, "username": "GitClaimed_bot"}, "text": "alert"})
        self.assertTrue(wait_for(lambda: self.a.calls, 5))
        self.assertIn(FFM, self.a.calls[0][0]); self.assertEqual(bot.state["subscribers"], subs)

    def test_bot_passes_chat_type(self):
        self.upd(2, GROUP, "supergroup", JED, "/h"); self.assertTrue(wait_for(lambda: any("Available" in t for _, t in self.sent)))
        n = len(self.sent)
        self.upd(3, JED, "private", JED, "/h"); self.upd(4, OTHER_CHAT, "supergroup", JED, "/h"); time.sleep(0.3)
        self.assertEqual(len(self.sent), n)


if __name__ == "__main__":
    unittest.main()
