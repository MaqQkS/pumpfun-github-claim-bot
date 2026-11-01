"""Phase 3 tests: Telegram parsing/subscriptions, command service, direct handlers, URL/address case,
alert byte-identity vs main, non-blocking research, secret scan. Everything is mocked: no Telegram,
no Queen, no xAI, no Helius, no network."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault("HELIUS_API_KEY", "test-helius-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test-token")
os.environ["TELEGRAM_BOT_USERNAME"] = "GitClaimedBot"
os.environ.pop("RESEARCH_COMMANDS_ENABLED", None)

import direct_data as dd  # noqa: E402
import telegram_cmds as tc  # noqa: E402
from research.adapter import ResearchResult  # noqa: E402
from research.formatter import format_result, sanitize_urls  # noqa: E402
from research.client import QueenAnswer  # noqa: E402
from research.prompt import ClaimFacts  # noqa: E402
from research.router import RoutePlan  # noqa: E402

FFM = "9r33aA9jgEEhaSRzFVr7xa43MMaK7bJRcpjr5RyaAUnZ"
SIG = "675ePaJEeYQ9zsvHvW62QH26RbxaiU8yUdV9weW1WXe3Jepg92Ti2XA26c81uBqzisaQdDdmEvCkaK5tHfYXJXBi"
BALA, GROUP, STRANGER, OTHER_CHAT = 2038104261, -5178862838, 555, -100999
AUTH = "2sMrGNK8i36YRkF5WWCwnaUYuwDJhHe1g2xA8aPvhkjM"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bot = _load("bot_new", REPO / "bot.py")
bot.save_state = lambda s: None   # never touch a state.json in tests


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}
        self.ok = status < 400
        self.text = str(body)[:200]

    def json(self):
        return self._b


class FakeHTTP:
    """Routes GET/POST by URL substring / RPC method. Counts calls; flags any LLM-ish host."""
    def __init__(self, routes=None, rpc=None):
        self.routes, self.rpc, self.calls = routes or {}, rpc or {}, []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        for k, v in self.routes.items():
            if k in url:
                return v
        return Resp(404, {})

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, (json or {}).get("method")))
        return Resp(200, {"result": self.rpc[json["method"]]})


DEX = [{"baseToken": {"address": FFM, "symbol": "FFM", "name": "FFM"}, "quoteToken": {"symbol": "SOL"}, "dexId": "pumpswap",
        "pairAddress": "Hs9GZ2oVJjS1PEXqVWEKwUsPuNNH2vcN8z1BqTyNbmGu", "priceUsd": "0.00000251", "marketCap": 2510, "fdv": 2510,
        "liquidity": {"usd": 2500.4, "base": 5e8, "quote": 6.3}, "volume": {"h24": 120.5}, "priceChange": {"h24": -12.3}}]
OWN = ["Hs9GZ2oVJjS1PEXqVWEKwUsPuNNH2vcN8z1BqTyNbmGu"] + [f"Wa11et{i}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"[:44] for i in range(1, 12)]
RPC = {"getTokenSupply": {"value": {"uiAmountString": "1000000000"}},
       "getTokenLargestAccounts": {"value": [{"address": f"TokAcct{i}" + "x" * 30, "uiAmountString": str(a)} for i, a in
                                              enumerate([150e6, 28e6, 20e6, 15e6, 12e6, 11e6, 10e6, 9e6, 8e6, 7e6, 6e6, 5e6])]},
       }


def holder_http():
    h = FakeHTTP({"dexscreener": Resp(200, DEX)}, dict(RPC))
    infos = [{"data": {"parsed": {"info": {"owner": OWN[i]}}}} for i in range(12)]
    progs = [{"owner": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}] + [{"owner": "11111111111111111111111111111111"}] * 11
    seq = iter([infos, progs])
    orig = h.post

    def post(url, json=None, **kw):
        if json["method"] == "getMultipleAccounts":
            h.calls.append(("POST", url, "getMultipleAccounts"))
            return Resp(200, {"result": {"value": next(seq)}})
        return orig(url, json=json, **kw)
    h.post = post
    return h


class FakeAdapter:
    def __init__(self, result=None, gate=None):
        self.calls, self.result, self.gate = [], result, gate

    def research(self, text, facts=None, context=None):
        self.calls.append((text, facts))
        if self.gate:
            self.gate.wait(10)
        return self.result or ResearchResult(True, None, "NARRATIVE: ok & <fine>\nVERDICT: low risk\nDATA:\n- holders: top10 13%",
                                             usage={"actual_usd": 0.02})


def svc(enabled=True, adapter=None, http=None, max_queue=3, **cfg):
    sent = []
    s = tc.CommandService(tc.CmdConfig(enabled=enabled, max_queue=max_queue, **cfg), lambda c, t: sent.append((c, t)),
                          tc.BotContext(lambda: {"ws_connected": True, "subscribers": 2}, lambda m: None, lambda m: ["39929227"]),
                          adapter_factory=lambda: adapter or FakeAdapter(), http=http or FakeHTTP({"dexscreener": Resp(200, DEX)}),
                          rpc_url="https://rpc.invalid/?api-key=x")
    return s, sent


def wait_for(pred, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def P(t):
    return tc.parse_command(t, "GitClaimedBot")


# ------------------------------------------------------------------------------------------------
class TestParsing(unittest.TestCase):
    def test_aliases_route_to_same_handler(self):
        for full, short in [("research", "re"), ("liquidity", "liq"), ("holders", "ho"), ("github", "gh"), ("status", "st"),
                            ("help", "h"), ("narrative", "nar"), ("developer", "dev"), ("wallets", "wa"), ("risk", "rk"),
                            ("zauth", "za"), ("claims", "cl"), ("bundles", "bu"), ("snipers", "sn"), ("socials", "soc"),
                            ("compare", "cmp"), ("history", "hist"), ("changes", "chg"), ("funding", "fund"),
                            ("launches", "la"), ("launches", "ln"), ("scanrepo", "sr"), ("ask", "q"), ("sources", "src")]:
            self.assertEqual(P(f"/{full} x").name, full)
            self.assertEqual(P(f"/{short} x").name, full)

    def test_bot_mention(self):
        self.assertEqual(P(f"/re@GitClaimedBot {FFM}").name, "research")
        self.assertEqual(P(f"/re@gitclaimedbot {FFM}").args, (FFM,))
        self.assertIsNone(P(f"/re@OtherBot {FFM}"))
        self.assertIsNone(P("/stop@OtherBot"))
        self.assertIsNone(tc.parse_command("/start@SomeBot", None))

    def test_exact_matching(self):
        self.assertEqual(P("/stop").name, "stop")
        self.assertFalse(P("/stopwatch").known)
        self.assertNotEqual(P("/stopwatch").name, "stop")
        self.assertFalse(P("/rex").known)
        self.assertIsNone(P("hello /re"))
        self.assertIsNone(P("/"))

    def test_case_kept_in_args(self):
        self.assertEqual(P(f"/RE {FFM}").args, (FFM,))

    def test_deep_prefix(self):
        p = P(f"/d re {FFM}")
        self.assertEqual((p.name, p.deep, p.args), ("research", True, (FFM,)))
        self.assertTrue(P(f"/d@GitClaimedBot dev {FFM}").deep)
        self.assertEqual(P("/d").name, "d")


class TestSubscriptions(unittest.TestCase):
    def setUp(self):
        bot.state.update({"subscribers": [BALA, GROUP], "tg_offset": 0, "seen": [], "pairs": []})
        self.replies = []
        bot.tg_reply = lambda c, t: self.replies.append((c, t))
        bot._SEEN_UPDATES.clear()
        bot.CMDS = tc.CommandService(tc.CmdConfig(enabled=False), lambda c, t: self.replies.append((c, t)))
        self.n = 1000

    def upd(self, text, chat=STRANGER, user=STRANGER, kind="message"):
        self.n += 1
        bot.tg_handle_update({"update_id": self.n, kind: {"chat": {"id": chat}, "from": {"id": user}, "text": text}})

    def test_no_auto_subscribe(self):
        self.upd("hi there")
        self.upd("/help")
        self.upd("/re " + FFM)
        self.assertEqual(bot.state["subscribers"], [BALA, GROUP])
        self.assertEqual(self.replies, [])

    def test_explicit_start(self):
        self.upd("/start")
        self.assertIn(STRANGER, bot.state["subscribers"])
        self.upd("/start@GitClaimedBot", chat=OTHER_CHAT)
        self.assertIn(OTHER_CHAT, bot.state["subscribers"])

    def test_bot_added_to_group_does_not_subscribe(self):
        bot.tg_handle_update({"update_id": 5, "my_chat_member": {"chat": {"id": OTHER_CHAT}, "new_chat_member": {"status": "member"}}})
        self.assertNotIn(OTHER_CHAT, bot.state["subscribers"])
        bot.tg_handle_update({"update_id": 6, "my_chat_member": {"chat": {"id": GROUP}, "new_chat_member": {"status": "kicked"}}})
        self.assertNotIn(GROUP, bot.state["subscribers"])

    def test_stop_exact(self):
        self.upd("/stopwatch", chat=GROUP, user=BALA)
        self.assertIn(GROUP, bot.state["subscribers"])
        self.upd("/stop@OtherBot", chat=GROUP, user=BALA)
        self.assertIn(GROUP, bot.state["subscribers"])
        self.upd("/stop@GitClaimedBot", chat=GROUP, user=BALA)
        self.assertNotIn(GROUP, bot.state["subscribers"])

    def test_edited_message_ignored(self):
        self.upd("/start", kind="edited_message")
        self.upd("/stop", chat=BALA, user=BALA, kind="edited_message")
        self.assertEqual(bot.state["subscribers"], [BALA, GROUP])

    def test_duplicate_update_id(self):
        u = {"update_id": 77, "message": {"chat": {"id": STRANGER}, "from": {"id": STRANGER}, "text": "/start"}}
        bot.tg_handle_update(u); bot.tg_handle_update(dict(u))
        self.assertEqual(len(self.replies), 1)

    def test_existing_subscribers_preserved(self):
        self.upd("/help", chat=BALA, user=BALA)
        self.assertEqual(bot.state["subscribers"], [BALA, GROUP])


class TestAccess(unittest.TestCase):
    def test_kill_switch_default_off(self):
        self.assertFalse(tc.CmdConfig.from_env({}).enabled)
        c = tc.CmdConfig.from_env({"RESEARCH_COMMANDS_ENABLED": "true", "RESEARCH_TESTER_IDS": "1,2", "RESEARCH_CHAT_IDS": "-5,1"})
        self.assertEqual((c.enabled, c.testers, c.chats), (True, frozenset({1, 2}), frozenset({-5, 1})))
        d = tc.CmdConfig.from_env({})
        self.assertEqual(d.testers, frozenset({BALA}))
        self.assertEqual(d.chats, frozenset({GROUP, BALA}))
        s, sent = svc(enabled=False)
        self.assertFalse(s.handle(GROUP, BALA, P("/h")))
        time.sleep(0.05); self.assertEqual(sent, [])

    def test_env_file_switch(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("RESEARCH_COMMANDS_ENABLED=true\nRESEARCH_TESTER_IDS=2038104261,7\n")
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("RESEARCH_COMMANDS")}
        try:
            c = tc.CmdConfig.from_env(env_file=f.name)
            self.assertTrue(c.enabled); self.assertEqual(c.testers, frozenset({BALA, 7}))
            os.environ["RESEARCH_COMMANDS_ENABLED"] = "false"
            self.assertFalse(tc.CmdConfig.from_env(env_file=f.name).enabled)
        finally:
            os.environ.pop("RESEARCH_COMMANDS_ENABLED", None); os.environ.update(saved); os.unlink(f.name)

    def test_allowlist(self):
        s, sent = svc()
        self.assertFalse(s.handle(GROUP, STRANGER, P("/h")))          # non-tester in test group
        self.assertFalse(s.handle(OTHER_CHAT, BALA, P("/h")))         # tester in another chat
        self.assertFalse(s.handle(STRANGER, STRANGER, P("/h")))       # stranger DM
        time.sleep(0.05); self.assertEqual(sent, [])
        self.assertTrue(s.handle(GROUP, BALA, P("/h")))
        self.assertTrue(s.handle(BALA, BALA, P("/st")))
        self.assertTrue(wait_for(lambda: len(sent) == 2))
        self.assertEqual({c for c, _ in sent}, {GROUP, BALA})

    def test_replies_never_broadcast(self):
        bot.state["subscribers"] = [BALA, GROUP, 42]
        posted = []
        orig = bot._tg_session
        bot._tg_session = lambda: types.SimpleNamespace(post=lambda url, json=None, **k: posted.append(json) or Resp(200, {}))
        try:
            bot.tg_reply_html(GROUP, "x")
        finally:
            bot._tg_session = orig
        self.assertEqual([p["chat_id"] for p in posted], [GROUP])


class TestCommands(unittest.TestCase):
    def run_cmd(self, text, s=None, **kw):
        s, sent = (s, s.send.__self__) if False else (s or None, None) if s else svc(**kw)
        return s, sent

    def go(self, text, **kw):
        s, sent = svc(**kw)
        s.handle(GROUP, BALA, P(text))
        self.assertTrue(wait_for(lambda: sent), text)
        time.sleep(0.05)
        return s, sent

    def test_help(self):
        s, sent = self.go("/h")
        t = sent[0][1]
        self.assertIn("Available", t); self.assertIn("Coming soon", t)
        for c in ("/re", "/liq", "/ho", "/gh", "/st"):
            self.assertIn(c, t.split("Coming soon")[0])
        for c in ("/nar", "/dev", "/wa", "/rk", "/za", "/cl", "/bu", "/sn", "/soc", "/cmp", "/q"):
            self.assertIn(c, t.split("Coming soon")[1])

    def test_coming_soon(self):
        for c in ("/nar x", "/dev x", "/za x", "/cl", "/bu x", "/soc x", "/cmp a b", "/la x", "/ln x"):
            s, sent = self.go(c)
            self.assertIn("coming soon", sent[0][1])
            self.assertEqual(s.llm_calls, 0)

    def test_deep_disabled_no_llm(self):
        ad = FakeAdapter()
        for c in (f"/d re {FFM}", f"/d dev {FFM}", f"/d wa {FFM}", f"/d nar {FFM}", "/d"):
            s, sent = self.go(c, adapter=ad)
            self.assertRegex(sent[0][1], "disabled|only for re")
            self.assertEqual(s.llm_calls, 0)
        self.assertEqual(ad.calls, [])

    def test_deep_on_direct_runs_normally(self):
        s, sent = self.go(f"/d liq {FFM}")
        self.assertIn("doesn't apply", sent[0][1]); self.assertIn("liquidity", sent[0][1])

    def test_no_ai_commands_zero_llm(self):
        ad = FakeAdapter()
        h = holder_http()
        h.routes["api.github.com"] = Resp(200, {"login": "NubsCarson", "created_at": "2025-01-01T00:00:00Z", "followers": 91,
                                                  "public_repos": 31, "html_url": "https://github.com/NubsCarson", "type": "User"})
        dd._cache.clear()
        for c in (f"/liq {FFM}", f"/ho {FFM}", "/gh NubsCarson", f"/gh {FFM}", "/st", "/h", f"/d ho {FFM}"):
            s, sent = self.go(c, adapter=ad, http=h)
            self.assertEqual(s.llm_calls, 0, c)
        self.assertEqual(ad.calls, [])
        hosts = {re.sub(r"^https?://([^/?]+).*", r"\1", c[1]) for c in h.calls}
        self.assertTrue(hosts <= {"api.dexscreener.com", "rpc.invalid", "api.github.com"}, hosts)

    def test_liquidity_output(self):
        dd._cache.clear()
        s, sent = self.go(f"/liq {FFM}")
        t = sent[0][1]
        self.assertIn("<code>9r33…AUnZ</code>", t)
        self.assertTrue(t.startswith("💧 <b>$FFM</b> liquidity: <b>$2.5k</b> (6.3 SOL) on PumpSwap"), t)
        self.assertNotIn("Price", t); self.assertNotIn("FDV", t)
        self.assertLessEqual(len(t.split("\n")), 4)
        self.assertIn("https://dexscreener.com/solana/Hs9GZ2oVJjS1PEXqVWEKwUsPuNNH2vcN8z1BqTyNbmGu", t)

    def test_holders_output_labels_pool(self):
        dd._cache.clear()
        s, sent = self.go(f"/ho {FFM}", http=holder_http())
        t = sent[0][1]
        self.assertIn("PumpSwap pool", t)
        self.assertIn("Pool / curve 15.0%", t)
        self.assertIn("Top 10 wallets 12.6%", t)   # 28+20+15+12+11+10+9+8+7+6 = 126M / 1B

    def test_bad_ca(self):
        s, sent = self.go("/liq notamint")
        self.assertIn("doesn't look like", sent[0][1])
        s, sent = self.go("/re")
        self.assertIn("Send a CA", sent[0][1])
        self.assertEqual(s.llm_calls, 0)

    def test_github_by_ca_uses_claim_identity(self):
        dd._cache.clear()
        h = FakeHTTP({"api.github.com/user/39929227": Resp(200, {"login": "NubsCarson", "name": "nubs", "created_at": "2025-01-01T00:00:00Z",
                                                                 "followers": 91, "public_repos": 31, "html_url": "https://github.com/NubsCarson"})})
        s, sent = self.go(f"/gh {FFM}", http=h)
        self.assertIn("NubsCarson", sent[0][1]); self.assertIn("fee claims", sent[0][1])


class TestResearch(unittest.TestCase):
    def test_research_flow(self):
        ad = FakeAdapter()
        s, sent = svc(adapter=ad)
        s.handle(GROUP, BALA, P(f"/re {FFM}"))
        self.assertTrue(wait_for(lambda: len(sent) >= 2))
        self.assertIn("Researching", sent[0][1])
        self.assertEqual(ad.calls[0][0], f"re {FFM}")
        self.assertIn("&amp; &lt;fine&gt;", sent[1][1])       # escaped for HTML parse mode
        self.assertIn("🧠 <b>AI take</b>", sent[1][1])
        self.assertEqual(s.llm_calls, 1)

    def test_queue_positions_and_overflow(self):
        gate = threading.Event()
        ad = FakeAdapter(gate=gate)
        s, sent = svc(adapter=ad, max_queue=2)
        mints = [FFM[:-1] + c for c in "ABCDE"]
        for m in mints:
            s.handle(GROUP, BALA, P(f"/re {m}"))
            time.sleep(0.05)
        self.assertTrue(wait_for(lambda: len(sent) >= 5))
        texts = [t for _, t in sent]
        self.assertIn("Researching", texts[0])
        self.assertTrue(any("position 2" in t for t in texts))
        self.assertTrue(any("position 3" in t for t in texts))
        self.assertEqual(sum("busy" in t for t in texts), 2)
        gate.set()
        self.assertTrue(wait_for(lambda: len(ad.calls) == 3))
        self.assertTrue(wait_for(lambda: not s.queue_state()["running"]))

    def test_single_concurrency(self):
        active, peak = [0], [0]
        lock = threading.Lock()

        class Slow(FakeAdapter):
            def research(self, text, facts=None, context=None):
                with lock:
                    active[0] += 1; peak[0] = max(peak[0], active[0])
                time.sleep(0.1)
                with lock:
                    active[0] -= 1
                return super().research(text, facts)
        ad = Slow()
        s, sent = svc(adapter=ad)
        for c in "ABC":
            s.handle(GROUP, BALA, P(f"/re {FFM[:-1]}{c}"))
        self.assertTrue(wait_for(lambda: len(ad.calls) == 3))
        self.assertEqual(peak[0], 1)

    def test_duplicate_in_flight(self):
        gate = threading.Event()
        ad = FakeAdapter(gate=gate)
        s, sent = svc(adapter=ad)
        self.assertTrue(s.handle(GROUP, BALA, P(f"/re {FFM}")))
        self.assertFalse(s.handle(GROUP, BALA, P(f"/re {FFM}")))
        self.assertFalse(s.handle(GROUP, BALA, P(f"/research {FFM}")))
        gate.set()
        self.assertTrue(wait_for(lambda: not s.queue_state()["running"] and len(sent) >= 2))
        self.assertEqual(len(ad.calls), 1)

    def test_queen_failure_and_budget(self):
        for code, txt in [("queen_unavailable", "Research unavailable right now (queen_unavailable). Alerts are unaffected."),
                          ("budget", "Research paused: budget limit reached (daily)."),
                          ("queen_timeout", "Research unavailable right now (queen_timeout). Alerts are unaffected.")]:
            ad = FakeAdapter(ResearchResult(False, None, txt, code))
            s, sent = svc(adapter=ad)
            s.handle(GROUP, BALA, P(f"/re {FFM}"))
            self.assertTrue(wait_for(lambda: len(sent) >= 2))
            self.assertIn(txt.split("(")[0], sent[1][1])
            self.assertIn(FFM, sent[1][1])

    def test_adapter_exception_contained(self):
        class Boom(FakeAdapter):
            def research(self, *a):
                raise RuntimeError("x")
        s, sent = svc(adapter=Boom())
        s.handle(GROUP, BALA, P(f"/re {FFM}"))
        self.assertTrue(wait_for(lambda: any("failed" in t for _, t in sent)))

    def test_research_layout(self):
        facts = ClaimFacts(mint=FFM, symbol="$FFM", github_login="NubsCarson", github_display="nubs", claim_sol=5.52,
                           claim_total_sol=5.75, claim_coins=2, claim_kind="FIRST claim ever", tx_signature=SIG,
                           signer_tags=("automated signer",))
        ans = QueenAnswer(answer=("NARRATIVE: FFM is a meme coin; the claimant builds IQ inscription forks.\n"
                                  "VERDICT: High risk: $2.5k liquidity and 14 insiders.\nDATA:\n- holders: top10 13.2% | largest 2.8% | pools 85.5%\n"
                                  f"- liquidity: $2.5k https://dexscreener.com/solana/{FFM.lower()}\n- pair https://dexscreener.com/solana/abcdefghijkmnopqrstuvwxyz123456789\n"
                                  "```token\n{\"a\":1}\n```"), tools=["token_pairs"], sources=["https://x.invalid/abcdefghijkmnopqrstuvwxyz123456789"], incomplete=[])
        plan = RoutePlan("research", 1, True, model="grok-4.3", effort="low", mint=FFM)
        r = ResearchResult(True, plan, "ignored", usage={"actual_usd": 0.0212}, latency_ms=22600, answer=ans)
        t = tc.format_research(FFM, r, facts)
        order = [t.index(x) for x in ("🧠 <b>AI take", "✅ <b>GitClaim facts", "📊 <b>Key data", "🔗 ", "🤖 grok-4.3")]
        self.assertEqual(order, sorted(order))
        self.assertLess(len(t), 1500)
        self.assertIn("High risk", t); self.assertIn("5.52 SOL of 5.75 across 2 coins", t)
        self.assertIn(f"https://dexscreener.com/solana/{FFM}", t); self.assertNotIn(FFM.lower(), t)
        self.assertNotIn("abcdefghijkmnop", t); self.assertIn("link omitted", t)
        self.assertIn(f"https://solscan.io/tx/{SIG}", t)
        self.assertNotIn("token_pairs", t); self.assertNotIn("&quot;a&quot;", t)
        self.assertIn("$0.0212", t); self.assertIn("22.6s", t)

    def test_research_prompt_asks_for_narrative(self):
        from research.config import COMMANDS
        from research.prompt import build_question
        c = COMMANDS["research"]
        q = build_question(c.task, ClaimFacts(mint=FFM), c.limited, c.answer_format)
        for k in ("NARRATIVE:", "VERDICT:", "DATA:", "Narrative unclear", "Never invent", "github_repo"):
            self.assertIn(k, q)
        self.assertNotIn("NARRATIVE:", build_question(COMMANDS["developer"].task, ClaimFacts(mint=FFM)))

    def test_token_card_json_stripped(self):
        r = ResearchResult(True, None, 'C. AI\nsummary\n\n```token\n{"a":1}\n```\n\nLimitations\n- none')
        t = tc.format_research(FFM, r)
        self.assertNotIn('"a"', t); self.assertNotIn("&quot;a&quot;", t); self.assertIn("summary", t); self.assertIn("Limitations", t)

    def test_long_reply_split(self):
        long = "\n".join(f"<b>line {i}</b> " + "x" * 80 for i in range(200))
        parts = tc.split_message(long)
        self.assertGreaterEqual(len(parts), 4)
        self.assertTrue(all(len(t) <= tc.TG_LIMIT for t in parts))
        self.assertTrue(all(t.count("<b>") == t.count("</b>") for t in parts))
        self.assertEqual("\n".join(parts), long)

    def test_real_adapter_budget_exhausted_no_call(self):
        """Real Phase 2 adapter with a $0 budget: refused before Queen is contacted."""
        from research.adapter import ResearchAdapter
        from research.config import load_settings
        with tempfile.TemporaryDirectory() as d:
            st = load_settings(environ={}, ledger_path=f"{d}/l.sqlite3", meter_path=f"{d}/m.jsonl", budget_daily_usd=0.0,
                               standard_url="http://127.0.0.1:9")
            a = ResearchAdapter(st)
            s, sent = svc(adapter=a)
            s.handle(GROUP, BALA, P(f"/re {FFM}"))
            self.assertTrue(wait_for(lambda: len(sent) >= 2))
            self.assertIn("budget", sent[1][1])


class TestNonBlocking(unittest.TestCase):
    def test_slow_research_does_not_block_poll_or_alerts(self):
        gate = threading.Event()
        ad = FakeAdapter(gate=gate)
        sent = []
        bot.CMDS = tc.CommandService(tc.CmdConfig(enabled=True), lambda c, t: sent.append((c, t)),
                                     tc.BotContext(), adapter_factory=lambda: ad, http=FakeHTTP({"dexscreener": Resp(200, DEX)}))
        bot._SEEN_UPDATES.clear()
        t0 = time.time()
        bot.tg_handle_update({"update_id": 9001, "message": {"chat": {"id": GROUP}, "from": {"id": BALA}, "text": f"/re {FFM}"}})
        self.assertLess(time.time() - t0, 0.2)
        self.assertTrue(wait_for(lambda: ad.calls))           # research is now "running" (blocked on gate)
        # alert path while research is stuck
        posted = []
        import requests as real
        bot.requests = types.SimpleNamespace(post=lambda url, json=None, **k: posted.append(json) or Resp(200, {}), exceptions=real.exceptions)
        try:
            bot.state["subscribers"] = [BALA, GROUP]
            t1 = time.time()
            bot.tg_send_all("ALERT")
            self.assertLess(time.time() - t1, 0.5)
            self.assertEqual([p["chat_id"] for p in posted], [BALA, GROUP])
            # another command on the poll thread still answers
            bot.tg_handle_update({"update_id": 9002, "message": {"chat": {"id": GROUP}, "from": {"id": BALA}, "text": "/h"}})
            self.assertTrue(wait_for(lambda: any("Available" in t for _, t in sent)))
        finally:
            bot.requests = real
            gate.set()
        self.assertTrue(wait_for(lambda: any("AI take" in t for _, t in sent)))


class TestUrlAndCase(unittest.TestCase):
    def test_lowercased_dexscreener_restored_from_trusted(self):
        r = sanitize_urls(f"see https://dexscreener.com/solana/{FFM.lower()}.", [FFM])
        self.assertIn(f"https://dexscreener.com/solana/{FFM}.", r.text)
        self.assertEqual(len(r.restored), 1)

    def test_unknown_address_url_dropped(self):
        bad = "https://dexscreener.com/solana/hs9gz2ovjjs1pexqvwekwuspunnh2vcn8z1bqtynbmgu"
        r = sanitize_urls(f"pair {bad} here", [FFM])
        self.assertNotIn(bad, r.text)
        self.assertIn("link omitted (unverified)", r.text)
        r2 = sanitize_urls("https://solscan.io/account/Hs9GZ2oVJjS1PEXqVWEKwUsPuNNH2vcN8z1BqTyNbmGu", [FFM])
        self.assertEqual(r2.dropped, ["https://solscan.io/account/Hs9GZ2oVJjS1PEXqVWEKwUsPuNNH2vcN8z1BqTyNbmGu"])

    def test_exact_urls_kept(self):
        txt = f"https://solscan.io/tx/{SIG} and https://pump.fun/coin/{FFM} and https://ffm.example/about"
        r = sanitize_urls(txt, [FFM, SIG])
        self.assertEqual(r.text, txt)
        self.assertEqual((r.dropped, r.restored), ([], []))

    def test_format_result_sources_and_body(self):
        facts = ClaimFacts(mint=FFM, tx_signature=SIG, github_login="NubsCarson")
        ans = QueenAnswer(answer=f"Mint {FFM.lower()} https://dexscreener.com/solana/{FFM.lower()} tx https://solscan.io/tx/{SIG.lower()}",
                          tools=["token_pairs"], sources=[f"https://dexscreener.com/solana/{FFM.lower()}", "https://rugcheck.xyz/tokens/AbCdEfGhJkLmNpQrStUvWxYz123456789abc"],
                          incomplete=[]) if "incomplete" in QueenAnswer.__dataclass_fields__ else None
        if ans is None:
            self.skipTest("QueenAnswer shape changed")
        plan = RoutePlan("research", 1, True, model="grok-4.3", effort="low", mint=FFM)
        text, _ = format_result(facts=facts, answer=ans, plan=plan, usage=None, latency_ms=1000)
        self.assertNotIn(FFM.lower(), text)
        self.assertIn(f"https://dexscreener.com/solana/{FFM}", text)
        self.assertIn(f"https://solscan.io/tx/{SIG}", text)
        self.assertNotIn("rugcheck.xyz/tokens/AbCd", text)
        self.assertIn("link omitted (unverified)", text)

    def test_mint_case_preserved_through_command(self):
        dd._cache.clear()
        s, sent = svc()
        s.handle(GROUP, BALA, P(f"/liq {FFM}"))
        self.assertTrue(wait_for(lambda: sent))
        self.assertIn("9r33…AUnZ", sent[0][1])
        self.assertNotIn(FFM.lower()[:4], sent[0][1].replace("9r33", ""))
        self.assertFalse(dd.is_mint(FFM.lower().replace("l", "L")[:31]))
        self.assertFalse(dd.is_mint("0" * 40))


class TestAlertsUnchanged(unittest.TestCase):
    """Old main bot.py vs new bot.py: identical alert text and identical Telegram send payloads."""
    @classmethod
    def setUpClass(cls):
        src = subprocess.run(["git", "show", "main:bot.py"], cwd=REPO, capture_output=True, text=True, check=True).stdout
        cls.tmp = tempfile.TemporaryDirectory()
        p = Path(cls.tmp.name) / "bot_main.py"; p.write_text(src)
        cls.old = _load("bot_main", p)
        cls.old.save_state = lambda s: None

    def patch(self, m, posted):
        m.github_info = lambda uid: {"login": "NubsCarson", "name": "nubs", "created_at": "2025-01-01T00:00:00Z", "followers": 91, "public_repos": 31}
        m.coin_info = lambda mint: ({FFM: "FFM"}.get(mint, "ALT"), 56300.0, 63100.0)
        m.rpc = lambda method, params, tries=3: None
        m.vault_mints = lambda pda: [FFM, "2K8rxRJVf8Sih7C5c55ZbhoKFThQrpw6v3zULHARpump"]
        m._creator.clear()
        import requests as real
        m.requests = types.SimpleNamespace(post=lambda url, json=None, **k: posted.append((url.split("/bot")[0], json)) or Resp(200, {}), exceptions=real.exceptions)
        m.state.update({"subscribers": [BALA, GROUP], "seen": [], "pairs": [], "user_claims": {}})

    def tx(self, signer):
        return {"transaction": {"message": {"accountKeys": [signer, "Rec1pientxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"], "header": {"numRequiredSignatures": 1},
                                            "instructions": []}}, "meta": {"innerInstructions": [], "err": None}, "blockTime": 1761800000}

    def claim(self, **kw):
        c = dict(ts=1, user_id="39929227", platform=2, pda="Pda1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                 recipient="Rec1pientxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", amount=5_750_000_000, lifetime=5_750_000_000)
        c.update(kw); return c

    def test_format_and_send_identical(self):
        cases = [
            (self.claim(), [("FIRST claim ever", FFM, "k", None)], AUTH),
            (self.claim(lifetime=9e12), [("FIRST claim on this token", FFM, "k", 5_520_000_000), ("FIRST claim on this token", "2K8rxRJVf8Sih7C5c55ZbhoKFThQrpw6v3zULHARpump", "k2", 230_000_000)], "Rec1pientxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            (self.claim(), [("FIRST claim on this token (unverified)", None, "k", None)], "Third9artyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
        ]
        outs = []
        for m in (self.old, bot):
            posted, res = [], []
            self.patch(m, posted)
            for c, ev, signer in cases:
                m.get_tx = lambda sig, _s=signer: self.tx(_s)
                m.decode_claims = lambda t, _c=c: [dict(_c)]
                m.evaluate = lambda sig, t, c, _e=ev: list(_e)
                res.append(m.handle_sig(SIG + str(len(res)), send=True))
            outs.append((res, posted))
        self.assertEqual(outs[0], outs[1])
        self.assertTrue(all(r for r in outs[0][0]))
        self.assertEqual(len(outs[0][1]), 6)    # 3 alerts x 2 subscribers, HTML parse mode
        self.assertTrue(all(j["parse_mode"] == "HTML" for _, j in outs[0][1]))
        import inspect
        # Phase 4 only adds message_id capture lines (_remember_alert) and an optional meta arg; delivery code is otherwise identical
        def strip4(src):
            src = src.replace("def tg_send_all(text, meta=None):", "def tg_send_all(text):")
            return "\n".join(l for l in src.splitlines() if "_remember_alert" not in l)
        self.assertEqual(strip4(inspect.getsource(self.old.tg_send_all)), strip4(inspect.getsource(bot.tg_send_all)))
        self.assertEqual(inspect.getsource(self.old.format_alert), inspect.getsource(bot.format_alert))


class TestLogging(unittest.TestCase):
    def test_token_redacted_from_logs_and_exceptions(self):
        import io, logging
        buf = io.StringIO(); h = logging.StreamHandler(buf); h.addFilter(bot.REDACT)
        lg = logging.getLogger("redact-test"); lg.addHandler(h); lg.propagate = False
        tok = os.environ["TELEGRAM_BOT_TOKEN"]
        lg.warning("tg poll: HTTPSConnectionPool(host='api.telegram.org'): url: /bot%s/getUpdates?offset=1", tok)
        try:
            raise IOError(f"https://api.telegram.org/bot{tok}/sendMessage failed; key {os.environ['HELIUS_API_KEY']}")
        except IOError:
            lg.exception("send failed")
        out = buf.getvalue()
        self.assertNotIn(tok, out); self.assertNotIn(os.environ["HELIUS_API_KEY"], out)
        self.assertGreaterEqual(out.count("<redacted>"), 3)

    def test_ipv4_forced_and_poll_timeouts(self):
        import socket, urllib3.util.connection as c
        self.assertEqual(c.allowed_gai_family(), socket.AF_INET)
        src = (REPO / "bot.py").read_text()
        self.assertIn("timeout=(5, 35)", src); self.assertIn("sess.get(", src)

    def test_command_timing_logged(self):
        s, sent = svc()
        with self.assertLogs("bot.cmds", "INFO") as cm:
            s.handle(GROUP, BALA, P("/h"), time.time())
            self.assertTrue(wait_for(lambda: sent))
            time.sleep(0.05)
        self.assertTrue(any(re.search(r"cmd /help .*recv->reply \d+ms", m) for m in cm.output))


class TestSecrets(unittest.TestCase):
    PATTERNS = [r"api-key=[A-Za-z0-9-]{20,}", r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b", r"xai-[A-Za-z0-9]{20,}", r"ghp_[A-Za-z0-9]{20,}"]

    def files(self):
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO, capture_output=True, text=True).stdout.split()
        return [REPO / f for f in out if (REPO / f).is_file()]

    def secret_values(self):
        vals = []
        for envf in (Path.home() / "pumpfun-github-claim-bot" / ".env", Path.home() / "queen-harness" / ".env"):
            try:
                for line in envf.read_text().splitlines():
                    k, _, v = line.partition("=")
                    if re.search(r"KEY|TOKEN|SECRET|RPC_URL", k):
                        vals += [x.strip().strip("'\"") for x in re.split(r"[,\s]", v) if len(x.strip()) >= 16]
            except OSError:
                pass
        return vals

    def test_no_secrets_in_repo_files(self):
        vals = self.secret_values()
        bad = []
        for f in self.files():
            t = f.read_text(errors="ignore")
            if any(re.search(p, t) for p in self.PATTERNS) or any(v in t for v in vals):
                bad.append(str(f.relative_to(REPO)))
        self.assertEqual(bad, [], "possible secret in tracked/untracked files (values not printed)")


if __name__ == "__main__":
    unittest.main()
