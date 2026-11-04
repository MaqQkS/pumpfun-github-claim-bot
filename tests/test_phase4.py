"""Phase 4 reply-to-alert: mocked Telegram updates + real historical alert texts (tests/fixtures/real_alerts.json)."""
import html
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

import alert_map as am
import telegram_cmds as tc
from research.adapter import ResearchResult
from research.prompt import ClaimFacts
from tests.test_phase3 import BALA, GROUP, STRANGER, FakeAdapter, Resp, svc, wait_for, FakeHTTP, DEX

FIX = json.loads((Path(__file__).parent / "fixtures" / "real_alerts.json").read_text())
BOT_ID = 8000000001
SINGLE_MINT = "FKaWiTCDr1VfkCyMaCsMcs1bugJH4b8fzH53JPxppump"
MULTI = ["FdmgJeQwjCsC4o2tmtPLMHNoCxKMwVbgn48tPuz5pump", "BHDHo2tJqtCBx83kVc6vJkPSj6QEWJHYjw6ypEukpump",
         "6u8sChd6c3RccZZdcyGzG6RkrxXAMC39732Wqe4Dpump"]
OTHER = "9r33aA9jgEEhaSRzFVr7xa43MMaK7bJRcpjr5RyaAUnZ"


def u16(s):
    return len(s.encode("utf-16-le")) // 2


def tg_render(h):
    """Telegram-side rendering of our HTML: plain text + entities with UTF-16 offsets (what getUpdates returns)."""
    text, ents, pos = "", [], 0
    for m in re.finditer(r'<code>(.*?)</code>|<a href="([^"]*)">(.*?)</a>|([^<]+)', h, re.S):
        if m.group(1) is not None:
            t = html.unescape(m.group(1)); ents.append({"type": "code", "offset": u16(text), "length": u16(t)})
        elif m.group(2) is not None:
            t = html.unescape(m.group(3)); ents.append({"type": "text_link", "offset": u16(text), "length": u16(t), "url": html.unescape(m.group(2))})
        else:
            t = html.unescape(m.group(4))
        text += t
    return text, ents


def alert_msg(key="single", mid=500, chat=GROUP, frm=None, **extra):
    text, ents = tg_render(FIX[key])
    m = {"message_id": mid, "chat": {"id": chat}, "from": frm or {"id": BOT_ID, "is_bot": True, "username": "GitClaimed_bot"},
         "text": text, "entities": ents}
    m.update(extra)
    return m


def cmd_msg(text, rep, chat=GROUP, user=BALA):
    return {"message_id": 900, "chat": {"id": chat}, "from": {"id": user}, "text": text, "reply_to_message": rep}


def amap(path=None):
    return am.AlertMap(path or Path(tempfile.mkdtemp()) / "a.sqlite3")


class TestParse(unittest.TestCase):
    def test_utf16_offsets_with_emoji(self):
        text, ents = tg_render(FIX["single"])
        code = [e for e in ents if e["type"] == "code"][0]
        self.assertEqual(am.entity_text(text, code), SINGLE_MINT)
        self.assertNotEqual(text.index(SINGLE_MINT), code["offset"])        # emoji makes UTF-16 != str index
        bad = text[code["offset"]:code["offset"] + code["length"]]
        self.assertNotEqual(bad, SINGLE_MINT)

    def test_single_alert(self):
        p = am.parse_alert_message(*tg_render(FIX["single"]))
        self.assertTrue(p["is_alert"]); self.assertEqual(p["mints"], [SINGLE_MINT]); self.assertTrue(p["sig"].startswith("4XAtu3"))

    def test_multi_alert(self):
        p = am.parse_alert_message(*tg_render(FIX["multi"]))
        self.assertEqual(p["mints"], MULTI)

    def test_not_first_code_block(self):
        # a code entity that is not on a CA line (e.g. inside other text) is ignored
        h = FIX["single"].replace("👤 Dev:", f"👤 note <code>{OTHER}</code> Dev:")
        self.assertEqual(am.parse_alert_message(*tg_render(h))["mints"], [SINGLE_MINT])

    def test_base58_validation_case(self):
        self.assertTrue(am.b58_32(SINGLE_MINT))
        self.assertFalse(am.b58_32(SINGLE_MINT.lower()) and SINGLE_MINT.lower() == SINGLE_MINT)
        for bad in ("0OIl" * 9, "abc", SINGLE_MINT + "zz", ""):
            self.assertFalse(am.b58_32(bad))

    def test_invalid_ca_in_alert(self):
        h = FIX["single"].replace(SINGLE_MINT, "NotAValidMint0OIl")
        r = am.resolve_reply(cmd_msg("/re", alert_msg(text=tg_render(h)[0], entities=tg_render(h)[1])), BOT_ID, amap())
        self.assertIsNone(r.mint); self.assertIn("No token CA", r.error)


class TestResolve(unittest.TestCase):
    def test_not_a_reply(self):
        self.assertIsNone(am.resolve_reply({"chat": {"id": GROUP}, "text": "/re"}, BOT_ID, amap()))

    def test_stored_mapping_wins_and_persists(self):
        path = Path(tempfile.mkdtemp()) / "a.sqlite3"
        a = amap(path)
        a.record(GROUP, 500, [SINGLE_MINT], "SIG", {"mints": [SINGLE_MINT], "facts": {"mint": SINGLE_MINT, "github_login": "svm"}})
        b = am.AlertMap(path)                                                # "restart"
        r = am.resolve_reply(cmd_msg("/re", alert_msg()), BOT_ID, b)
        self.assertEqual((r.mint, r.source), (SINGLE_MINT, "stored")); self.assertEqual(r.meta["facts"]["github_login"], "svm")

    def test_retention(self):
        now = [1000.0]
        a = am.AlertMap(Path(tempfile.mkdtemp()) / "a.sqlite3", now=lambda: now[0])
        a.record(GROUP, 1, [SINGLE_MINT], None)
        now[0] += am.RETENTION_S + 1
        self.assertIsNone(a.get(GROUP, 1)); self.assertEqual(a.prune(), 1); self.assertEqual(a.count(), 0)

    def test_older_alert_parsed(self):
        r = am.resolve_reply(cmd_msg("/re", alert_msg()), BOT_ID, amap())
        self.assertEqual((r.mint, r.source), (SINGLE_MINT, "parsed")); self.assertEqual(r.meta, {})

    def test_reply_to_user_message(self):
        rep = alert_msg(frm={"id": STRANGER, "is_bot": False})
        r = am.resolve_reply(cmd_msg("/re", rep), BOT_ID, amap())
        self.assertIsNone(r.mint); self.assertIn("isn't to a GitClaim alert", r.error)

    def test_forwarded_and_spoofed(self):
        for extra in ({"forward_origin": {"type": "user"}}, {"forward_date": 1}, {"is_automatic_forward": True},
                      {"via_bot": {"id": 1}}, {"chat": {"id": -100123}}):
            r = am.resolve_reply(cmd_msg("/re", alert_msg(**extra)), BOT_ID, amap())
            self.assertIsNone(r.mint, extra)
        other_bot = alert_msg(frm={"id": 777, "is_bot": True})
        self.assertIsNone(am.resolve_reply(cmd_msg("/re", other_bot), BOT_ID, amap()).mint)
        self.assertIsNone(am.resolve_reply(cmd_msg("/re", alert_msg()), None, amap()).mint)   # bot id unknown -> reject

    def test_bot_message_that_is_not_an_alert(self):
        rep = {"message_id": 3, "chat": {"id": GROUP}, "from": {"id": BOT_ID, "is_bot": True}, "text": f"💧 liq {SINGLE_MINT}",
               "entities": [{"type": "code", "offset": 7, "length": 44}]}
        self.assertIn("isn't a GitClaim claim alert", am.resolve_reply(cmd_msg("/re", rep), BOT_ID, amap()).error)

    def test_multi_uses_headline_mint(self):
        rep = alert_msg("multi", date=int(time.time()) - 3 * 3600)
        rep["entities"].reverse()  # selection follows the layout, not entity order
        r = am.resolve_reply(cmd_msg("/re", rep), BOT_ID, amap())
        self.assertIsNone(r.error); self.assertEqual(r.mint, MULTI[0])
        self.assertEqual(r.meta, {})  # parsing never creates verified claim facts

    def test_multi_stored_primary_survives_restart(self):
        for meta in ({"primary_mint": MULTI[0]}, {"facts": {"mint": MULTI[0]}}, {}):
            with self.subTest(meta=meta):
                a = amap()
                a.record(GROUP, 500, list(reversed(MULTI)), "SIG", meta)
                r = am.resolve_reply(cmd_msg("/liq", alert_msg("multi")), BOT_ID, am.AlertMap(a.path))
                self.assertEqual((r.mint, r.source, r.meta), (MULTI[0], "stored", meta))

    def test_ambiguous_alert_without_headline_still_asks(self):
        h = FIX["multi"].replace(" · $Q", "\n$Q")
        text, ents = tg_render(h)
        rep = alert_msg("multi", text=text, entities=ents)
        for stored in (False, True):
            a = amap()
            if stored:
                a.record(GROUP, 500, MULTI, "SIG")
            r = am.resolve_reply(cmd_msg("/liq", rep), BOT_ID, a)
            self.assertEqual(r.error, "multi"); self.assertEqual(r.choices, MULTI)

    def test_screenshot_epyh_headline(self):
        primary = "Gfw1Kbpoe3kuLwGCcPcrnLttrbEtyYDmWmVKJHGCpump"
        secondary = "HJrTsHqBwYB9Ck2jACcar84xqrvDGKRMLYYGdMVEpump"
        h = (f"🆕 FIRST CLAIM · $EPYH\n<code>{primary}</code>\n\n💸 Claim: 1.04 SOL by lalune"
             f"\n\n➕ Also first claims:\n• $EPYH 0.36 SOL · <code>{secondary}</code>")
        text, ents = tg_render(h)
        rep = alert_msg(text=text, entities=ents, date=int(time.time()) - 3 * 3600)
        for stored in (False, True):
            a = amap()
            if stored:
                a.record(GROUP, 500, [primary, secondary], "SIG")
            r = am.resolve_reply(cmd_msg("/liq", rep), BOT_ID, a)
            self.assertEqual(r.mint, primary); self.assertIsNone(r.error)


def run(s, text, rep, user=BALA, chat=GROUP, a=None):
    p = tc.parse_command(text, "GitClaimed_bot")
    msg = cmd_msg(text, rep, chat, user)
    return s.handle(chat, user, p, None, am.resolve_reply(msg, BOT_ID, a or amap()))


class TestService(unittest.TestCase):
    def test_reply_re_uses_stored_facts(self):
        ad = FakeAdapter(); s, sent = svc(adapter=ad)
        a = amap(); a.record(GROUP, 500, [SINGLE_MINT], "SIG", {"facts": {"mint": SINGLE_MINT, "github_login": "svm-7f3a-account",
                                                                           "claim_sol": 2.07, "signer_tags": ["automated signer"]}})
        self.assertTrue(run(s, "/re", alert_msg(), a=a))
        self.assertTrue(wait_for(lambda: any("AI take" in t for _, t in sent)))
        text, facts = ad.calls[0]
        self.assertIn(SINGLE_MINT, text); self.assertEqual(facts.github_login, "svm-7f3a-account"); self.assertEqual(facts.signer_tags, ("automated signer",))
        out = [t for _, t in sent if "AI take" in t][0]
        self.assertIn("stored record", out)
        # verified facts and AI analysis stay in separate sections
        self.assertLess(out.index("AI take"), out.index("GitClaim facts")); self.assertIn("🐙 svm-7f3a-account", out)
        self.assertNotIn("svm-7f3a-account", out[out.index("AI take"):out.index("GitClaim facts")])

    def test_full_and_short_aliases(self):
        for cmd in ("/re", "/research", "/ho", "/holders", "/gh", "/github", "/liq", "/liquidity", "/re@GitClaimed_bot"):
            s, sent = svc(adapter=FakeAdapter())
            self.assertTrue(run(s, cmd, alert_msg()), cmd)
            self.assertTrue(wait_for(lambda: len(sent) >= 1), cmd)
            self.assertFalse(any("Send a CA" in t for _, t in sent), (cmd, sent))

    def test_explicit_ca_overrides_reply(self):
        ad = FakeAdapter(); s, sent = svc(adapter=ad)
        run(s, f"/re {OTHER}", alert_msg())
        self.assertTrue(wait_for(lambda: ad.calls)); self.assertIn(OTHER, ad.calls[0][0])

    def test_gh_explicit_login_overrides_reply(self):
        s, sent = svc()
        s._cmd_github = lambda args, rc=None: f"args={args}"
        run(s, "/gh NubsCarson", alert_msg())
        self.assertTrue(wait_for(lambda: sent)); self.assertIn("NubsCarson", sent[0][1])

    def test_gh_reuses_stored_github_ids(self):
        s, sent = svc(); s.ctx.github_ids_for = lambda m: self.fail("state lookup not needed")
        seen = []
        import direct_data as dd
        orig = dd.github_user; dd.github_user = lambda uid=None, http=None, login=None: (seen.append(uid), {"ok": False, "error": "x"})[1]
        try:
            a = amap(); a.record(GROUP, 500, [SINGLE_MINT], "S", {"github_ids": ["4242"], "facts": None})
            run(s, "/gh", alert_msg(), a=a); self.assertTrue(wait_for(lambda: sent))
        finally:
            dd.github_user = orig
        self.assertEqual(seen, ["4242"])

    def test_multi_token_reply_researches_primary(self):
        ad = FakeAdapter(); s, sent = svc(adapter=ad)
        run(s, "/re", alert_msg("multi"))
        self.assertTrue(wait_for(lambda: ad.calls)); self.assertIn(MULTI[0], ad.calls[0][0])

    def test_multi_reply_direct_commands_and_explicit_override(self):
        for command, handler in (("liq", "liquidity"), ("ho", "holders"), ("gh", "github")):
            for explicit in (False, True):
                with self.subTest(command=command, explicit=explicit):
                    s, sent = svc()
                    setattr(s, "_cmd_" + handler, lambda args, *rest: args[0])
                    run(s, "/" + command + (" " + MULTI[1] if explicit else ""), alert_msg("multi"))
                    self.assertTrue(wait_for(lambda: sent))
                    self.assertEqual(sent[0][1], MULTI[1] if explicit else MULTI[0])

    def test_rejected_reply_no_llm(self):
        ad = FakeAdapter(); s, sent = svc(adapter=ad)
        run(s, "/re", alert_msg(forward_origin={"type": "user"}))
        self.assertTrue(wait_for(lambda: sent)); self.assertEqual(ad.calls, []); self.assertIn("isn't to a GitClaim alert", sent[0][1])

    def test_any_user_can_research_reply(self):
        ad = FakeAdapter(); s, sent = svc(adapter=ad)
        self.assertTrue(run(s, "/re", alert_msg(), user=STRANGER))
        self.assertTrue(wait_for(lambda: any("AI take" in t for _, t in sent)))
        self.assertEqual(len(ad.calls), 1)

    def test_disabled_reply_ignored(self):
        ad = FakeAdapter(); s, sent = svc(adapter=ad, enabled=False)
        self.assertFalse(run(s, "/re", alert_msg(), user=STRANGER))
        self.assertEqual(sent, []); self.assertEqual(ad.calls, [])

    def test_parsed_older_alert_labels_missing_facts(self):
        ad = FakeAdapter(); s, sent = svc(adapter=ad)
        run(s, "/re", alert_msg())
        self.assertTrue(wait_for(lambda: any("AI take" in t for _, t in sent)))
        out = [t for _, t in sent if "AI take" in t][0]
        self.assertIn("Older alert", out); self.assertIn("unavailable (not verified)", out)
        self.assertIn("No GitHub claim seen", out)
        self.assertIsNone(ad.calls[0][1])                       # Telegram text never becomes facts

    def test_duplicate_reply_ignored(self):
        gate = threading.Event(); ad = FakeAdapter(gate=gate); s, sent = svc(adapter=ad)
        self.assertTrue(run(s, "/re", alert_msg())); self.assertFalse(run(s, "/re", alert_msg()))
        gate.set(); self.assertTrue(wait_for(lambda: len(ad.calls) == 1))

    def test_queue_overflow(self):
        gate = threading.Event(); ad = FakeAdapter(gate=gate); s, sent = svc(adapter=ad, max_queue=1)
        mints = [SINGLE_MINT, OTHER, MULTI[0]]
        for i, m in enumerate(mints):
            a = amap(); a.record(GROUP, 500 + i, [m], None, {})
            run(s, "/re", alert_msg(mid=500 + i), a=a)
            time.sleep(0.05)
        self.assertTrue(wait_for(lambda: any("queue full" in t for _, t in sent))); gate.set()

    def test_queen_down_and_budget(self):
        for err, txt in (("queen_unavailable", "Queen is offline"), ("budget", "budget")):
            ad = FakeAdapter(ResearchResult(False, None, f"Research paused: {err}", err)); s, sent = svc(adapter=ad)
            run(s, "/re", alert_msg())
            self.assertTrue(wait_for(lambda: any(txt in t for _, t in sent)), (err, sent))


class TestAlertUnchanged(unittest.TestCase):
    def test_metadata_records_primary_mint(self):
        import bot
        hits = [("FIRST claim ever", m, 1_000_000_000) for m in MULTI]
        meta = bot._alert_meta({"user_id": 123}, MULTI[0], "SIG", "FIRST claim ever", hits, FIX["multi"])
        self.assertEqual(meta["primary_mint"], MULTI[0])
        self.assertEqual(meta["mints"], MULTI)

    def test_send_text_unchanged_and_mapping_recorded(self):
        import bot
        posted = []

        class R:
            ok, status_code, text = True, 200, ""
            def json(self): return {"ok": True, "result": {"message_id": 4321}}
        orig_post, orig_alerts, subs = bot.requests.post, bot.ALERTS, list(bot.state["subscribers"])
        bot.requests.post = lambda url, json=None, timeout=None: (posted.append(json), R())[1]
        bot.ALERTS = amap(); bot.state["subscribers"][:] = [GROUP]
        try:
            bot.tg_send_all(FIX["single"], {"mints": [SINGLE_MINT], "sig": "S"})
            bot.tg_send_all(FIX["single"])
        finally:
            bot.requests.post = orig_post
            rec = bot.ALERTS.get(GROUP, 4321); bot.ALERTS = orig_alerts; bot.state["subscribers"][:] = subs
        self.assertEqual([p["text"] for p in posted], [FIX["single"], FIX["single"]])
        self.assertEqual(posted[0], posted[1])                  # byte-identical payload with or without mapping
        self.assertEqual(rec["mints"], [SINGLE_MINT])

    def test_alert_meta_from_alert_text(self):
        import bot
        m = bot._alert_meta({"user_id": 99}, SINGLE_MINT, "SIG", "FIRST claim ever", [("k", SINGLE_MINT, 1)], FIX["single"])
        self.assertEqual(m["facts"]["mint"], SINGLE_MINT); self.assertEqual(m["facts"]["github_login"], "svm-7f3a-account")
        self.assertEqual(m["github_ids"], ["99"]); self.assertEqual(m["mints"], [SINGLE_MINT])

    def test_record_failure_never_breaks_send(self):
        import bot
        class R:
            ok, status_code, text = True, 200, ""
            def json(self): raise ValueError
        orig, subs = bot.requests.post, list(bot.state["subscribers"])
        bot.requests.post = lambda *a, **k: R(); bot.state["subscribers"][:] = [GROUP]
        try:
            bot.tg_send_all("x", {"mints": [SINGLE_MINT]})
        finally:
            bot.requests.post = orig; bot.state["subscribers"][:] = subs


if __name__ == "__main__":
    unittest.main()
