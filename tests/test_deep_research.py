"""Deep research (/d re): mocked xAI Responses + Telegram only. No paid calls."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import telegram_cmds as tc
from research.adapter import ResearchAdapter, ResearchResult
from research.client import QueenAnswer, QueenTimeout, QueenUnavailable
from research.config import load_settings
from research.deep_client import (
    DeepClient, DeepConfigError, DeepUsage, parse_deep_response, resolve_xai_api_key, _tool_cost, _token_cost,
)
from research.config import Price
from research.prompt import ClaimFacts, DEEP_RESEARCH_TASK, build_question
from research.router import route

FFM = "So11111111111111111111111111111111111111112"
DEEP_BODY = """NARRATIVE: A cat-themed meme coin. Claimant builds tooling.
X_SENTIMENT: Mixed; @alice shilling, @bob skeptical.
KEY_POSTS:
- https://x.com/alice/status/1 | @alice | aping the CA
WEBSITE: Landing page promises a game.
GITHUB: https://github.com/example/repo looks empty (unverified age).
FUD:
- Claim: deployer dumped (unverified)
VERDICT: medium risk — thin X volume and empty repo.
DATA:
- holders: top10 42%
"""


class FakeDeep:
    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def ask(self, question, timeout_s):
        self.calls += 1
        return self.fn(question, timeout_s)

    def health(self, timeout_s=3.0):
        return True


class DeepClientUnit(unittest.TestCase):
    def test_parse_response_and_costs(self):
        obj = {
            "output_text": DEEP_BODY,
            "usage": {
                "input_tokens": 10000,
                "output_tokens": 2000,
                "input_tokens_details": {"cached_tokens": 1000},
                "output_tokens_details": {"reasoning_tokens": 1500},
                "server_side_tool_usage_details": {
                    "web_search_calls": 8,
                    "x_search_calls": 3,
                    "x_posts_fetched": 40,
                    "x_users_fetched": 2,
                },
            },
            "citations": ["https://x.com/alice/status/1"],
        }
        price = Price(2.0, 0.5, 6.0)
        ans, du = parse_deep_response(obj, price, "grok-4.7")
        self.assertIn("NARRATIVE", ans.answer)
        self.assertIn("x_search", ans.tools)
        self.assertIn("web_search", ans.tools)
        # tokens: (9000*2 + 1000*0.5 + 2000*6)/1e6 = 0.018+0.0005+0.012 = 0.0305
        self.assertAlmostEqual(du.token_usd, 0.0305, places=5)
        # tools: 8*0.005 + 40*0.005 + 2*0.01 = 0.04+0.2+0.02 = 0.26
        self.assertAlmostEqual(du.tool_usd, 0.26, places=5)
        self.assertAlmostEqual(du.actual_usd, 0.2905, places=5)
        m = du.as_meter("grok-4.7")
        self.assertEqual(m["x_posts_fetched"], 40)
        self.assertEqual(m["cost_source"], "xai_responses_tools")

    def test_resolve_key_from_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            path.write_text("LLM_API_KEY=sk-test-key-1234567890\nLLM_MODEL=grok-4.3\n", encoding="utf-8")
            self.assertEqual(resolve_xai_api_key(env_file=str(path), environ={}), "sk-test-key-1234567890")
            self.assertEqual(resolve_xai_api_key(api_key="explicit", env_file=str(path), environ={}), "explicit")
            self.assertIsNone(resolve_xai_api_key(env_file=str(Path(d) / "missing"), environ={}))


class DeepRouting(unittest.TestCase):
    def test_disabled_by_default(self):
        s = load_settings(env_file="/nonexistent", environ={})
        p = route(f"/d re {FFM}", s)
        self.assertEqual(p.error, "deep_disabled")
        self.assertFalse(p.uses_llm)

    def test_enabled_routes_to_4_7(self):
        s = load_settings(env_file="/nonexistent", environ={}, deep_enabled=True)
        p = route(f"/d re {FFM}", s)
        self.assertTrue(p.uses_llm)
        self.assertEqual(p.model, "grok-4.7")
        self.assertEqual(p.effort, "medium")
        self.assertEqual(p.timeout_s, 180.0)
        self.assertTrue(p.deep)

    def test_d_alone(self):
        s = load_settings(env_file="/nonexistent", environ={}, deep_enabled=True)
        self.assertEqual(route("/d", s).error, "deep_needs_command")

    def test_deep_prompt_includes_x_and_fud(self):
        q = build_question(DEEP_RESEARCH_TASK, ClaimFacts(mint=FFM, symbol="CAT"), None, "deep_research")
        self.assertIn("x_search", q)
        self.assertIn("FUD", q)
        self.assertIn("web_search", q)
        self.assertIn(FFM, q)


class DeepAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.meter = os.path.join(self.tmp, "meter.jsonl")
        Path(self.meter).write_text("", encoding="utf-8")

    def _settings(self, **kw):
        base = dict(
            env_file=os.path.join(self.tmp, "none.env"), environ={},
            ledger_path=os.path.join(self.tmp, "l.sqlite3"), meter_path=self.meter,
            deep_enabled=True, deep_backend="xai_native",
            worst_case_usd={"grok-4.3": 0.05, "grok-4.7": 0.40},
            deep_daily_usd=1.00, budget_daily_usd=5.0, budget_total_usd=5.0,
            retry_backoff_s=0.01,
        )
        base.update(kw)
        return load_settings(**base)

    def test_success_reserves_and_records_usage(self):
        du = DeepUsage(input_tokens=1000, output_tokens=500, token_usd=0.05, tool_usd=0.10,
                       web_search_calls=4, x_posts_fetched=20, x_search_calls=2)
        def fn(q, t):
            self.assertIn("x_search", q)
            return QueenAnswer(DEEP_BODY, by="grok-4.7", tools=["x_search", "web_search"]), 1, du
        a = ResearchAdapter(self._settings(), deep_client=FakeDeep(fn), sleep=lambda _x: None)
        r = a.research(f"/d re {FFM}", ClaimFacts(mint=FFM, symbol="CAT"))
        self.assertTrue(r.ok, r.text)
        self.assertAlmostEqual(r.usage["actual_usd"], 0.15, places=5)
        self.assertEqual(r.usage["x_posts_fetched"], 20)
        row = a.ledger.rows(1)[0]
        self.assertEqual(row["mode"], "deep")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["model"], "grok-4.7")

    def test_budget_refusal_no_call(self):
        called = []
        def fn(q, t):
            called.append(1)
            raise AssertionError("should not call")
        a = ResearchAdapter(self._settings(budget_daily_usd=0.10, worst_case_usd={"grok-4.7": 0.40}),
                            deep_client=FakeDeep(fn), sleep=lambda _x: None)
        r = a.research(f"/d re {FFM}")
        self.assertEqual(r.error, "budget")
        self.assertEqual(called, [])
        self.assertEqual(a.ledger.totals()["requests"], 0)

    def test_deep_daily_cap(self):
        du = DeepUsage(token_usd=0.35, tool_usd=0.0)
        def fn(q, t):
            return QueenAnswer(DEEP_BODY, by="grok-4.7"), 1, du
        a = ResearchAdapter(self._settings(deep_daily_usd=0.50, worst_case_usd={"grok-4.7": 0.40}),
                            deep_client=FakeDeep(fn), sleep=lambda _x: None)
        self.assertTrue(a.research(f"/d re {FFM}").ok)
        # second would reserve 0.40 but 0.35+0.40 > 0.50
        r2 = a.research(f"/d re {FFM}1111111111111111111111111111111")  # different mint, bypass cache
        # mint must be valid base58 length - use another valid-looking mint
        r2 = a.research("/d re TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        self.assertEqual(r2.error, "budget")
        self.assertIn("deep_daily", r2.text)

    def test_missing_key_not_sent(self):
        a = ResearchAdapter(self._settings(xai_api_key="", xai_env_file=os.path.join(self.tmp, "nope")),
                            sleep=lambda _x: None)
        # no injected deep_client and no key file
        r = a.research(f"/d re {FFM}")
        self.assertEqual(r.error, "deep_config")
        row = a.ledger.rows(1)[0]
        self.assertEqual(row["status"], "not_sent")

    def test_timeout_keeps_reservation(self):
        def fn(q, t):
            raise QueenTimeout("no deep answer within 1s")
        a = ResearchAdapter(self._settings(), deep_client=FakeDeep(fn), sleep=lambda _x: None)
        r = a.research(f"/d re {FFM}")
        self.assertEqual(r.error, "queen_timeout")
        row = a.ledger.rows(1)[0]
        self.assertEqual(row["status"], "error")
        self.assertAlmostEqual(row["actual_usd"], 0.40, places=2)

    def test_cache_keyed_like_re(self):
        calls = []
        du = DeepUsage(token_usd=0.01, tool_usd=0.0)
        def fn(q, t):
            calls.append(1)
            return QueenAnswer(DEEP_BODY, by="grok-4.7"), 1, du
        a = ResearchAdapter(self._settings(), deep_client=FakeDeep(fn), sleep=lambda _x: None)
        f = ClaimFacts(mint=FFM, github_login="dev", tx_signature="Sig1111111111111111111111111111111111111111111", claim_sol=1.0)
        self.assertTrue(a.research(f"/d re {FFM}", f).ok)
        r2 = a.research(f"/d re {FFM}", f)
        self.assertTrue(r2.cached)
        self.assertEqual(len(calls), 1)
        # different claim identity must miss cache
        f2 = ClaimFacts(mint=FFM, github_login="dev", tx_signature="Sig2222222222222222222222222222222222222222222", claim_sol=2.0)
        self.assertTrue(a.research(f"/d re {FFM}", f2).ok)
        self.assertEqual(len(calls), 2)

    def test_auto_never_deep(self):
        a = ResearchAdapter(self._settings(), deep_client=FakeDeep(lambda q, t: (_ for _ in ()).throw(AssertionError())),
                            sleep=lambda _x: None)
        r = a.research(f"/d re {FFM}", source="auto", auto={"daily_usd": 1, "max_per_day": 3, "daily_helius": 1000})
        self.assertEqual(r.error, "deep_disabled")


def _wait(pred, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class DeepTelegram(unittest.TestCase):
    BALA, GROUP, JED = 2038104261, -5178862838, 6681835233

    def setUp(self):
        self.cfg = tc.CmdConfig(enabled=True, testers=frozenset({self.BALA, self.JED}),
                                chats=frozenset({self.GROUP, self.BALA, self.JED}), max_queue=3)

    def go(self, text, *, user=None, chat=None, deep_enabled=False, adapter=None):
        sent = []
        user = self.BALA if user is None else user
        chat = self.GROUP if chat is None else chat
        ad = adapter
        with mock.patch("research.config.load_settings",
                        return_value=load_settings(env_file="/nonexistent", environ={}, deep_enabled=deep_enabled)):
            s = tc.CommandService(self.cfg, lambda c, t: sent.append((c, t)),
                                  adapter_factory=lambda: ad if ad is not None else mock.Mock())
            s.handle(chat, user, tc.parse_command(text, "GitClaimedBot"))
            _wait(lambda: sent, 2.0)
            time.sleep(0.05)
            return s, sent, ad

    def test_d_alone_disabled(self):
        _, sent, _ = self.go("/d", deep_enabled=False)
        self.assertTrue(any("disabled" in t.lower() for _, t in sent), sent)

    def test_d_re_disabled(self):
        _, sent, _ = self.go(f"/d re {FFM}", deep_enabled=False)
        self.assertTrue(any("disabled" in t.lower() for _, t in sent), sent)

    def test_d_dev_coming_soon_even_when_enabled(self):
        _, sent, _ = self.go(f"/d dev {FFM}", deep_enabled=True)
        self.assertTrue(any("coming soon" in t.lower() for _, t in sent), sent)

    def test_d_wa_coming_soon(self):
        _, sent, _ = self.go(f"/d wa {FFM}", deep_enabled=True)
        self.assertTrue(any("coming soon" in t.lower() for _, t in sent), sent)

    def test_allowlist_blocks_stranger(self):
        _, sent, _ = self.go(f"/d re {FFM}", user=999, chat=999, deep_enabled=True)
        self.assertFalse(any("Deep-researching" in t for _, t in sent), sent)

    def test_d_re_enqueues_when_enabled(self):
        du = DeepUsage(token_usd=0.02, tool_usd=0.01, x_posts_fetched=5, web_search_calls=2)

        class AD:
            def research(self, text, facts=None, context=None, source="manual", auto=None):
                self.last = text
                plan = route(text, load_settings(env_file="/nonexistent", environ={}, deep_enabled=True))
                return ResearchResult(True, plan, "ok", usage=du.as_meter("grok-4.7"), latency_ms=1200,
                                      answer=QueenAnswer(DEEP_BODY, by="grok-4.7", tools=["x_search", "web_search"]))

        ad = AD()
        _, sent, _ = self.go(f"/d re {FFM}", deep_enabled=True, adapter=ad)
        self.assertTrue(any("Deep-researching" in t for _, t in sent), sent)
        self.assertTrue(_wait(lambda: hasattr(ad, "last") and any("red flags" in t.lower() or "Narrative" in t for _, t in sent), 3.0), sent)
        self.assertTrue(ad.last.startswith("d re"), ad.last)

    def test_jed_can_use_when_enabled(self):
        class AD:
            def research(self, text, facts=None, context=None, source="manual", auto=None):
                plan = route(text, load_settings(env_file="/nonexistent", environ={}, deep_enabled=True))
                return ResearchResult(True, plan, "ok", usage={"actual_usd": 0.01}, latency_ms=100,
                                      answer=QueenAnswer(DEEP_BODY, by="grok-4.7"))
        ad = AD()
        _, sent, _ = self.go(f"/d re {FFM}", user=self.JED, chat=self.GROUP, deep_enabled=True, adapter=ad)
        self.assertTrue(any("Deep-researching" in t for _, t in sent), sent)

    def test_format_splits_long(self):
        long_body = DEEP_BODY + ("\n- Claim: filler (unverified)\n" * 200)
        ans = QueenAnswer(long_body, by="grok-4.7")
        plan = route(f"/d re {FFM}", load_settings(env_file="/nonexistent", environ={}, deep_enabled=True))
        res = ResearchResult(True, plan, "x", usage={"actual_usd": 0.5}, latency_ms=90000, answer=ans)
        text = tc.format_deep_research(FFM, res)
        parts = tc.split_message(text)
        self.assertGreaterEqual(len(parts), 1)
        self.assertTrue(all(len(p) <= tc.CHUNK for p in parts))


if __name__ == "__main__":
    unittest.main()
