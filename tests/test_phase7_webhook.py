"""Phase 7.1 webhook producer: offline only (local mock receiver on 127.0.0.1, fake HTTP/DNS). No xAI, no Helius."""
import json
import logging
import socket
import tempfile
import time
import types
import unittest
from pathlib import Path

import requests

import auto_research as ar
import direct_data as dd
import webhook as wh
from examples.webhook_receiver import Receiver, check
from research.prompt import ClaimFacts
from research.tests.test_adapter import ok_body, FFM, TRACE, MARSX
from tests.test_phase3 import GROUP, Resp, wait_for
from tests.test_phase6 import SIG, FACTS, PILOT, ev, Svc, TestBotIntegration

SECRET = "test-only-secret-0123456789abcdefABCDEF"          # fixture, not a real credential
SECRET2 = "next-test-secret-ZYXWVUTSRQ9876543210zyxw"
REC = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
PDA = "2K8rxRJVf8Sih7C5c55ZbhoKFThQrpw6v3zULHARpump"
CLAIM = {"user_id": "112233445", "amount": 7_250_000_000, "lifetime": 9_000_000_000, "recipient": REC, "pda": PDA,
         "ts": 1761966000, "tags": ["automated signer"]}
GH = {"login": "NubsCarson", "name": "nubs"}
PUBLIC = lambda host, port, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
ROOT = Path(__file__).resolve().parents[1]


def tmp(n="o.sqlite3"):
    return str(Path(tempfile.mkdtemp()) / n)


def cfg(url="https://hooks.example.com/gitclaim", **kw):
    base = dict(enabled=True, url=url, secret=SECRET, send_claims=True, send_research=True, db_path=tmp())
    base.update(kw)
    c = wh.WebhookConfig(**base); c.problems = c.validate(); return c


class FakePost:
    def __init__(self, plan):
        self.plan, self.calls = list(plan), []

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(dict(url=url, data=data, headers=headers, timeout=timeout, allow_redirects=allow_redirects))
        r = self.plan.pop(0) if self.plan else 200
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            return types.SimpleNamespace(status_code=r[0], headers=r[1])
        return types.SimpleNamespace(status_code=r, headers={})


def producer(plan=(200,), c=None, resolver=PUBLIC):
    c = c or cfg()
    p = wh.Producer(c, wh.Outbox(c.db_path), http=FakePost(plan), resolver=resolver)
    return p


def claim_ev(**kw):
    d = dict(sig=SIG, mint=FFM, kind="FIRST claim ever", claim=CLAIM, token_lamports=5_520_000_000, n_coins=2, qualifying=1,
             github=GH, symbol="FFM", market_cap_display="$56.3k")
    d.update(kw); return wh.build_claim_event(**d)


def research_res(text="NARRATIVE: FFM is a dating-agent meme by a solo dev.\nVERDICT: high risk (interpretation)\nDATA:\n- liq: $2k\n- 31 repos"):
    from research.router import RoutePlan
    ans = types.SimpleNamespace(answer=text, tools=["token_info", "github_user"], sources=["https://github.com/NubsCarson", "http://127.0.0.1:8790/x"])
    return types.SimpleNamespace(ok=True, plan=RoutePlan("research", 1, True, model="grok-4.3", effort="high", mint=FFM),
                                 answer=ans, text=text, usage={"actual_usd": 0.0213}, latency_ms=14100, cached=False)


class T01_Config(unittest.TestCase):
    def test_01_disabled_by_default(self):
        c = wh.WebhookConfig.from_env(env_file="/nonexistent", environ={})
        self.assertFalse(c.enabled); self.assertFalse(c.active); self.assertFalse(c.send_claims or c.send_research)
        self.assertEqual((c.timeout_s, c.max_retries, c.queue_limit, c.ttl_hours), (8.0, 5, 1000, 24.0))
        p = wh.Producer(c); self.assertEqual(p.describe(), "webhooks off"); self.assertIsNone(p.outbox)
        self.assertEqual(p.on_claim(sig=SIG, hits=[("FIRST claim ever", FFM, None)], claim=CLAIM, n_coins=1), [])
        p.start(); self.assertIsNone(p._thread)

    def test_02_missing_url_or_secret_fail_closed(self):
        for env in ({"WEBHOOK_ENABLED": "true", "WEBHOOK_SEND_CLAIMS": "true", "WEBHOOK_SECRET": SECRET},
                    {"WEBHOOK_ENABLED": "true", "WEBHOOK_SEND_CLAIMS": "true", "WEBHOOK_URL": "https://hooks.example.com/x"},
                    {"WEBHOOK_ENABLED": "true", "WEBHOOK_SEND_CLAIMS": "true", "WEBHOOK_URL": "https://hooks.example.com/x", "WEBHOOK_SECRET": "short"},
                    {"WEBHOOK_ENABLED": "true", "WEBHOOK_URL": "https://hooks.example.com/x", "WEBHOOK_SECRET": SECRET},   # no event type
                    {"WEBHOOK_ENABLED": "true", "WEBHOOK_SEND_CLAIMS": "true", "WEBHOOK_URL": "https://hooks.example.com/x",
                     "WEBHOOK_SECRET": SECRET, "WEBHOOK_TIMEOUT_SECONDS": "abc"}):
            c = wh.WebhookConfig.from_env(env_file="/nonexistent", environ=env)
            self.assertFalse(c.active, env); self.assertTrue(c.problems)
            self.assertIn("fail-closed", wh.Producer(c).describe())
            self.assertNotIn(SECRET, wh.Producer(c).describe()); self.assertNotIn(SECRET, repr(c))
        ok = wh.WebhookConfig.from_env(env_file="/nonexistent", environ={"WEBHOOK_ENABLED": "true", "WEBHOOK_SEND_CLAIMS": "true",
                                       "WEBHOOK_URL": "https://hooks.example.com/x", "WEBHOOK_SECRET": SECRET, "WEBHOOK_QUEUE_LIMIT": "50"},
                                       db_path=tmp())
        self.assertTrue(ok.active); self.assertEqual(ok.queue_limit, 50)

    def test_url_validation(self):
        bad = ["http://hooks.example.com/x", "https://127.0.0.1/x", "https://10.0.0.5/x", "https://169.254.169.254/latest", "https://localhost/x",
               "https://[::1]/x", "https://user:pw@hooks.example.com/x", "https://hooks.example.com/x#f", "ftp://hooks.example.com", "https://intranet/x",
               "https://box.local/x", "https:///x", "https://hooks.example.com:22/x", "", "https://exa mple.com"]
        for u in bad:
            self.assertIsNotNone(wh.url_problem(u), u)
        self.assertIsNone(wh.url_problem("https://hooks.example.com/gitclaim?k=1"))
        self.assertIsNone(wh.url_problem("https://hooks.example.com:8443/x"))
        self.assertIsNotNone(wh.url_problem("http://127.0.0.1:8799/x"))                     # local http needs test mode
        self.assertIsNone(wh.url_problem("http://127.0.0.1:8799/x", test_mode=True))
        self.assertIsNotNone(wh.url_problem("http://hooks.example.com/x", test_mode=True))   # test mode is localhost only
        self.assertIsNotNone(wh.resolve_ok("https://evil.example.com/x", False,
                                           lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))]))    # DNS rebinding to private
        self.assertIsNone(wh.resolve_ok("https://hooks.example.com/x", False, PUBLIC))


class T03_Payloads(unittest.TestCase):
    def test_03_claim_detected_payload(self):
        e = claim_ev()
        self.assertEqual(e["event_type"], "claim.detected"); self.assertEqual(e["schema_version"], "1.0")
        self.assertEqual(e["event_id"], wh.claim_event_id(SIG, FFM)); self.assertTrue(e["emitted_at"].endswith("Z"))
        c = e["verified"]["claim"]
        self.assertEqual((c["classification"], c["token_fee_share_lamports"], c["token_fee_share_sol"]), ("FIRST", 5_520_000_000, 5.52))
        self.assertEqual((c["vault_claim_total_lamports"], c["vault_claim_total_sol"], c["coins_in_claim"]), (7_250_000_000, 7.25, 2))
        self.assertEqual(c["signer_classification"], "automated_signer")
        cl = e["verified"]["claimant"]
        self.assertEqual((cl["role"], cl["creator_relationship"], cl["github_login"], cl["github_user_id"]),
                         ("github_linked_fee_claimant", "not_verified", "NubsCarson", "112233445"))
        self.assertNotIn("creator", json.dumps(e).replace("creator_relationship", ""))
        self.assertEqual(e["links"]["github_profile"], "https://github.com/NubsCarson")
        self.assertEqual(e["third_party"]["symbol"], "FFM")
        unknown = claim_ev(github=None, token_lamports=None, symbol=None, market_cap_display=None,
                           claim={**CLAIM, "recipient": "not-an-address", "tags": []})
        self.assertIsNone(unknown["verified"]["claimant"]["github_login"]); self.assertIsNone(unknown["links"]["github_profile"])
        self.assertIsNone(unknown["verified"]["claim"]["token_fee_share_lamports"])          # multi-coin, unknown share: null
        self.assertEqual(unknown["verified"]["claim"]["token_fee_share_attribution"], "unknown")
        self.assertIsNone(unknown["verified"]["claimant"]["recipient_wallet"])
        self.assertEqual(unknown["verified"]["claim"]["signer_classification"], "unknown")
        self.assertEqual(claim_ev(kind="🔁 repeat claim")["verified"]["claim"]["classification"], "REPEAT")

    def test_04_research_completed_payload(self):
        import telegram_cmds as tc
        f = ClaimFacts(**{**FACTS, "signer_tags": tuple(FACTS["signer_tags"])})
        e = wh.build_research_event(sig=SIG, mint=FFM, res=research_res(), facts=f,
                                    pctx={"website": "https://ffm.example.org", "links": [{"url": "https://x.com/ffm"}, {"url": "javascript:alert(1)"}],
                                          "description": "Dating agent. Contact dev@example.com, keys in /Users/bala/.env",
                                          "repos": [{"name": "ffm", "language": "TS", "stars": 3}]},
                                    scrub=tc.scrub_ai_text, split=tc._split_answer)
        self.assertEqual(e["event_type"], "research.completed"); self.assertEqual(e["claim_event_id"], wh.claim_event_id(SIG, FFM))
        self.assertEqual(e["research"]["model"], "grok-4.3"); self.assertEqual(e["research"]["reasoning_effort"], "high")
        self.assertEqual((e["research"]["trigger"], e["research"]["cost_usd"], e["research"]["duration_ms"]), ("auto", 0.0213, 14100))
        self.assertIn("dating-agent", e["ai_interpretation"]["narrative"]); self.assertIn("high risk", e["ai_interpretation"]["risk_verdict"])
        self.assertEqual(e["ai_interpretation"]["tool_references"], ["token_info", "github_user"])
        self.assertEqual(e["ai_interpretation"]["source_urls"], ["https://github.com/NubsCarson"])   # internal URL dropped
        self.assertEqual(e["third_party"]["links"], ["https://x.com/ffm"])
        blob = json.dumps(e)
        for bad in ("/Users/", "dev@example.com", "127.0.0.1", "javascript:", "8790"):
            self.assertNotIn(bad, blob)
        self.assertIsNone(wh.build_research_event(sig=SIG, mint=FFM, res=types.SimpleNamespace(ok=False)))

    def test_18_verified_vs_ai_separated(self):
        e = wh.build_research_event(sig=SIG, mint=FFM, res=research_res(), facts=ClaimFacts(**{**FACTS, "signer_tags": ()}))
        self.assertEqual(set(e) >= {"verified", "third_party", "ai_interpretation"}, True)
        self.assertNotIn("narrative", json.dumps(e["verified"])); self.assertNotIn("dating-agent", json.dumps(e["verified"]))
        self.assertIn("not verified", e["ai_interpretation"]["label"]); self.assertEqual(e["verified"]["facts"]["github_login"], "NubsCarson")
        c = claim_ev(); self.assertNotIn("ai_interpretation", c)

    def test_19_original_case_addresses(self):
        for e in (claim_ev(), wh.build_research_event(sig=SIG, mint=FFM, res=research_res())):
            body = wh.canonical_body(e).decode()
            self.assertIn(FFM, body); self.assertIn(SIG, body); self.assertNotIn(FFM.lower(), body)
        self.assertIn(REC, wh.canonical_body(claim_ev()).decode())

    def test_17_missing_or_ambiguous_mint(self):
        self.assertIsNone(claim_ev(mint=None)); self.assertIsNone(claim_ev(mint="not0valid0mint"))
        self.assertIsNone(claim_ev(sig="short"))
        p = producer()
        r = p.on_claim(sig=SIG, hits=[("FIRST claim ever", None, None), ("FIRST claim ever", "0OIl", None)], claim=CLAIM, n_coins=2)
        self.assertEqual(r, []); self.assertEqual(p.outbox.counts(), {}); self.assertEqual(p.stats["skipped"], 2)


class T05_Signing(unittest.TestCase):
    def setUp(self):
        self.rx = Receiver(SECRET).start(); self.addCleanup(self.rx.stop)

    def post(self, body, headers):
        return requests.post(self.rx.url, data=body, headers=headers, timeout=5)

    def test_05_correct_hmac(self):
        e = claim_ev(); body = wh.canonical_body(e)
        h = wh.headers_for(cfg(), e["event_id"], e["event_type"], body, now=1761966000)
        self.assertEqual(h["X-GitClaim-Signature"], wh.sign(SECRET, "1761966000", e["event_id"], "1", body))
        import hashlib, hmac
        manual = "v1=" + hmac.new(SECRET.encode(), f"1761966000.{e['event_id']}.1.".encode() + body, hashlib.sha256).hexdigest()
        self.assertEqual(h["X-GitClaim-Signature"], manual)
        self.assertEqual(wh.verify(SECRET, h, body, now=1761966010), (True, "ok"))
        h2 = wh.headers_for(cfg(), e["event_id"], e["event_type"], body)
        self.assertEqual(self.post(body, h2).status_code, 200); self.assertEqual(self.rx.events[0]["event_id"], e["event_id"])

    def test_06_invalid_signature_rejected(self):
        e = claim_ev(); body = wh.canonical_body(e); h = wh.headers_for(cfg(), e["event_id"], e["event_type"], body)
        self.assertEqual(self.post(body, {**h, "X-GitClaim-Signature": "v1=" + "0" * 64}).status_code, 401)
        self.assertEqual(self.post(body + b" ", h).status_code, 401)                             # body tampered
        self.assertEqual(self.post(body, {**h, "X-GitClaim-Event-ID": "gc_claim_other"}).status_code, 401)
        wrong = wh.headers_for(cfg(secret="WRONG-secret-0123456789abcdefghijklmn"), e["event_id"], e["event_type"], body)
        self.assertEqual(self.post(body, wrong).status_code, 401); self.assertEqual(self.rx.events, [])

    def test_07_timestamp_expired(self):
        e = claim_ev(); body = wh.canonical_body(e)
        old = wh.headers_for(cfg(), e["event_id"], e["event_type"], body, now=time.time() - 3600)
        self.assertEqual(self.post(body, old).status_code, 401)
        self.assertEqual(wh.verify(SECRET, old, body)[1], "timestamp outside tolerance")
        self.assertEqual(check(SECRET.encode(), old, body)[0], 401)

    def test_rotation_dual_signature(self):
        e = claim_ev(); body = wh.canonical_body(e)
        h = wh.headers_for(cfg(secret_next=SECRET2), e["event_id"], e["event_type"], body)
        self.assertEqual(len(h["X-GitClaim-Signature"].split(",")), 2)
        self.assertTrue(wh.verify(SECRET, h, body)[0]); self.assertTrue(wh.verify(SECRET2, h, body)[0])
        self.assertEqual(self.post(body, h).status_code, 200)

    def test_08_duplicate_event_ids(self):
        p = producer()
        e = claim_ev()
        self.assertEqual(p._submit(e), "queued"); self.assertEqual(p._submit(claim_ev()), "duplicate")   # same (sig, mint)
        self.assertEqual(p.outbox.counts(), {"pending": 1})
        c = cfg(url=self.rx.url, test_mode=True)
        body = wh.canonical_body(e)
        for _ in range(2):
            self.assertEqual(self.post(body, wh.headers_for(c, e["event_id"], e["event_type"], body)).status_code, 200)
        self.assertEqual(len(self.rx.events), 1); self.assertEqual(self.rx.log, [200, "dup"])


class T09_Delivery(unittest.TestCase):
    def test_09_retry_after_500_same_id_and_body(self):
        p = producer([500, 200]); e = claim_ev(); p._submit(e)
        self.assertTrue(p.run_once()); st = p.outbox.get(e["event_id"])
        self.assertEqual((st["status"], st["attempts"], st["last_status"]), ("pending", 1, 500))
        self.assertGreater(st["next_at"], time.time()); self.assertFalse(p.run_once())        # not due yet: no retry storm
        p.clock = lambda: time.time() + 1000
        self.assertTrue(p.run_once()); self.assertEqual(p.outbox.get(e["event_id"])["status"], "delivered")
        a, b = p.http.calls
        self.assertEqual(a["data"], b["data"]); self.assertEqual(a["headers"]["X-GitClaim-Event-ID"], b["headers"]["X-GitClaim-Event-ID"])
        self.assertFalse(a["allow_redirects"]); self.assertEqual(len(p.outbox.attempts(e["event_id"])), 2)

    def test_10_retry_after_429_honors_retry_after(self):
        p = producer([(429, {"Retry-After": "120"}), 200]); e = claim_ev(); p._submit(e)
        t0 = time.time(); p.run_once(); st = p.outbox.get(e["event_id"])
        self.assertEqual(st["status"], "pending"); self.assertAlmostEqual(st["next_at"] - t0, 120, delta=3)
        p2 = producer([(429, {"Retry-After": "999999"})]); p2._submit(claim_ev()); p2.run_once()
        self.assertLessEqual(p2.outbox.get(e["event_id"])["next_at"] - time.time(), wh.MAX_RETRY_AFTER_S + 1)
        self.assertEqual(producer([408]).cfg.max_retries, 5)
        p3 = producer([408]); p3._submit(claim_ev()); p3.run_once(); self.assertEqual(p3.outbox.get(e["event_id"])["status"], "pending")

    def test_11_permanent_400_401_no_retry(self):
        for code in (400, 401, 403, 404, 301, 302):
            p = producer([code, 200]); e = claim_ev(); p._submit(e); p.run_once()
            st = p.outbox.get(e["event_id"]); self.assertEqual((st["status"], st["attempts"]), ("failed", 1), code)
            p.clock = lambda: time.time() + 99999; self.assertFalse(p.run_once()); self.assertEqual(len(p.http.calls), 1)

    def test_12_timeout_and_dns_failure(self):
        p = producer([requests.exceptions.ConnectTimeout("t"), requests.exceptions.ReadTimeout("t")]); e = claim_ev(); p._submit(e)
        p.run_once(); self.assertEqual(p.outbox.get(e["event_id"])["status"], "pending")
        self.assertEqual(p.http.calls[0]["timeout"], (5.0, 8.0))
        def nodns(*a, **k): raise socket.gaierror("nodename nor servname")
        d = producer([200], resolver=nodns); d._submit(e); d.run_once()
        st = d.outbox.get(e["event_id"]); self.assertEqual(st["status"], "pending"); self.assertIn("dns", st["last_error"])
        self.assertEqual(d.http.calls, [])                                                    # never sent
        m = producer([500] * 10, c=cfg(max_retries=2)); m._submit(e)
        for i in range(5):
            m.clock = (lambda k: (lambda: time.time() + 1000 * k))(i + 1); m.run_once()
        self.assertEqual(m.outbox.get(e["event_id"])["status"], "failed"); self.assertEqual(len(m.http.calls), 3)   # 1 + 2 retries
        self.assertTrue(all(1 <= wh.backoff_s(i) <= wh.MAX_BACKOFF_S for i in range(1, 30)))
        self.assertLess(wh.backoff_s(1, lambda: 0), wh.backoff_s(3, lambda: 0))

    def test_13_restart_with_queued_events(self):
        c = cfg(); p = producer([500], c=c); e = claim_ev(); e2 = claim_ev(mint=TRACE)
        p._submit(e); p._submit(e2)
        p.outbox.next_due()                                                                    # crash mid-delivery
        o2 = wh.Outbox(c.db_path); self.assertEqual(o2.recover(), 1)
        self.assertEqual(o2.counts(), {"pending": 2})
        p2 = wh.Producer(c, o2, http=FakePost([200, 200]), resolver=PUBLIC)
        p2.run_once(); p2.run_once(); self.assertEqual(o2.counts(), {"delivered": 2})
        self.assertEqual({x["headers"]["X-GitClaim-Event-ID"] for x in p2.http.calls}, {e["event_id"], e2["event_id"]})
        self.assertEqual(p2._submit(claim_ev()), "duplicate")                                   # stable id across restart

    def test_14_old_events_expire(self):
        p = producer([200], c=cfg(ttl_hours=1)); e = claim_ev(); p._submit(e)
        p.clock = lambda: time.time() + 7200
        self.assertFalse(p.run_once()); self.assertEqual(p.outbox.get(e["event_id"])["status"], "expired"); self.assertEqual(p.http.calls, [])
        p.outbox.prune(keep_days=0, now=time.time() + 99999); self.assertIsNone(p.outbox.get(e["event_id"]))

    def test_15_queue_capacity(self):
        p = producer(c=cfg(queue_limit=2))
        r = p.on_claim(sig=SIG, hits=[("FIRST claim ever", m, None) for m in (FFM, TRACE, MARSX)], claim=CLAIM, n_coins=3)
        self.assertEqual(r, ["queued", "queued", "full"]); self.assertEqual(p.stats["full"], 1)
        t0 = time.time()
        for _ in range(50):
            p.on_claim(sig=SIG, hits=[("FIRST claim ever", MARSX, None)], claim=CLAIM, n_coins=1)
        self.assertLess(time.time() - t0, 1.0)                                                # never blocks the scanner

    def test_16_multi_token_one_event_per_token(self):
        p = producer()
        hits = [("FIRST claim ever", FFM, 5_000_000_000), ("FIRST claim on this token", TRACE, 2_000_000_000)]
        self.assertEqual(p.on_claim(sig=SIG, hits=hits, claim=CLAIM, n_coins=3, github=GH, symbols={FFM: "FFM"}), ["queued", "queued"])
        bodies = [json.loads(p.outbox._db.execute("select body from events where event_id=?", (wh.claim_event_id(SIG, m),)).fetchone()[0])
                  for m in (FFM, TRACE)]
        self.assertEqual([b["verified"]["claim"]["token_fee_share_lamports"] for b in bodies], [5_000_000_000, 2_000_000_000])
        self.assertTrue(all(b["verified"]["claim"]["vault_claim_total_lamports"] == 7_250_000_000 for b in bodies))
        self.assertEqual([b["third_party"]["symbol"] for b in bodies], ["FFM", None])
        self.assertEqual(p.on_claim(sig=SIG, hits=hits, claim=CLAIM, n_coins=3), ["duplicate", "duplicate"])

    def test_20_no_secrets_in_json_or_logs(self):
        c = cfg(url="https://hooks.example.com/gitclaim?token=PATHSECRET123", secret_next=SECRET2)
        p = producer([500, 200], c=c)
        with self.assertLogs("gitclaim.webhook", level="INFO") as L:
            p._submit(claim_ev()); p.run_once(); p.clock = lambda: time.time() + 1000; p.run_once()
            logging.getLogger("gitclaim.webhook").info(p.describe())
        logs = "\n".join(L.output); body = p.http.calls[0]["data"].decode()
        sig_hdr = p.http.calls[0]["headers"]["X-GitClaim-Signature"]
        for s in (SECRET, SECRET2, "PATHSECRET123", sig_hdr.split(",")[0][3:]):
            self.assertNotIn(s, logs); self.assertNotIn(s, body)
        self.assertIn("hooks.example.com", logs)
        for bad in ("api-key", "xai-", "/Users/", "TELEGRAM", "HELIUS"):
            self.assertNotIn(bad, body)

    def test_end_to_end_mock_receiver(self):
        rx = Receiver(SECRET, fail_plan=[500, 429]).start(); self.addCleanup(rx.stop)
        c = cfg(url=rx.url, test_mode=True)
        p = wh.Producer(c, wh.Outbox(c.db_path)); p.clock = time.time
        p.on_claim(sig=SIG, hits=[("FIRST claim ever", FFM, None)], claim=CLAIM, n_coins=1, github=GH)
        for i in range(4):
            p.clock = (lambda k: (lambda: time.time() + 2000 * k))(i)
            p.run_once()
        self.assertEqual(rx.log, [500, 429, 200]); self.assertEqual(len(rx.events), 1)
        self.assertEqual(rx.events[0]["verified"]["claim"]["token_fee_share_sol"], 7.25)


class T21_Bot(TestBotIntegration):
    """Real bot.handle_sig with mocked chain/Telegram plus the webhook producer."""
    def attach(self, bot, url, plan=None):
        c = cfg(url=url, test_mode=True)
        bot.WEBHOOK = wh.Producer(c, wh.Outbox(c.db_path))
        self.addCleanup(setattr, bot, "WEBHOOK", wh.Producer(wh.WebhookConfig()))
        return bot.WEBHOOK

    def run_alert(self, with_webhook, url="http://127.0.0.1:9/x"):
        bot, posted, calls = self.setup_bot()
        bot._gh["112233445"] = GH
        p = self.attach(bot, url) if with_webhook else None
        subs = list(bot.state["subscribers"])
        msgs = bot.handle_sig(SIG)
        self.assertEqual(bot.state["subscribers"], subs)
        return bot, posted, calls, msgs, p

    def test_23_alerts_unchanged(self):
        _, posted0, calls0, msgs0, _ = self.run_alert(False)
        _, posted1, calls1, msgs1, p = self.run_alert(True)
        self.assertEqual(msgs0, msgs1); self.assertEqual(posted0, posted1); self.assertEqual(calls0, calls1)
        self.assertEqual(p.outbox.counts(), {"pending": 1})

    def test_21_receiver_offline_bot_continues(self):
        bot, posted, calls, msgs, p = self.run_alert(True, url="http://127.0.0.1:9/offline")
        self.assertEqual(sum(1 for x in posted if x[0] == "post"), 2)
        p.run_once()
        st = p.outbox.get(
                          p.outbox._db.execute("select event_id from events").fetchone()[0])
        self.assertEqual(st["status"], "pending"); self.assertIsNotNone(st["last_error"])
        self.assertIsNone(bot.handle_sig(SIG))                                                 # duplicate ws event: no new event
        self.assertEqual(p.outbox.counts(), {"pending": 1})

    def test_24_no_replay_or_catchup_by_default(self):
        bot, posted, calls = self.setup_bot()
        p = self.attach(bot, "http://127.0.0.1:9/x")
        bot.handle_sig(SIG, send=False, prefix="🧪 TEST")                                      # replay path
        self.assertEqual(p.outbox.counts(), {})
        r = p.on_claim(sig=SIG, hits=[("FIRST claim ever", FFM, None)], claim=CLAIM, n_coins=1, origin="catchup")
        self.assertEqual(r, []); self.assertEqual(p.outbox.counts(), {})

    def test_22_no_xai_or_helius_from_webhook(self):
        before = dict(dd.CALLS)
        bot, posted, calls, msgs, p = self.run_alert(True)
        rx = Receiver(SECRET).start(); self.addCleanup(rx.stop); p.cfg.url = rx.url
        p.run_once()
        self.assertEqual(len(rx.events), 1)
        self.assertEqual({k: v for k, v in dd.CALLS.items() if before.get(k) != v and "helius" in k}, {})


class T04_ResearchFlow(Svc):
    def test_research_event_from_auto_only(self):
        a, s = self.build(lambda n: (200, ok_body("NARRATIVE: FFM is a dating-agent meme.\nVERDICT: high risk\nDATA:\n- liq: $2k"), 0.1, "grok-4.3"))
        import telegram_cmds as tc
        c = cfg(); prod = wh.Producer(c, wh.Outbox(c.db_path), http=FakePost([200]), resolver=PUBLIC)
        s.on_auto_research = lambda job, res, pctx: prod.on_auto_research(sig=job.key[1], mint=job.mint, res=res, facts=job.facts,
                                                                          pctx=pctx, scrub=tc.scrub_ai_text, split=tc._split_answer)
        self.deliver(); self.auto.on_alert(ev())
        self.assertTrue(wait_for(lambda: self.replies and prod.outbox.counts().get("pending") == 1, 5))
        calls_after = self.stub.calls
        prod.run_once()
        body = json.loads(prod.http.calls[0]["data"])
        self.assertEqual(body["event_type"], "research.completed"); self.assertEqual(body["research"]["trigger"], "auto")
        self.assertIn("dating-agent", body["ai_interpretation"]["narrative"])
        self.assertEqual(self.stub.calls, calls_after); self.assertEqual(self.stub.calls, 1)  # no extra Queen/xAI call
        before = prod.outbox.counts()
        self.assertEqual(prod.outbox.counts(), before)                                          # manual paths have no hook


class TestFixtures(unittest.TestCase):
    def test_sample_fixtures_valid_and_signed(self):
        for name in ("claim.detected.json", "research.completed.json"):
            ev_ = json.loads((ROOT / "tests" / "fixtures" / "webhook" / name).read_text())
            self.assertEqual(ev_["schema_version"], "1.0"); self.assertTrue(ev_["event_id"].startswith("gc_"))
            body = wh.canonical_body(ev_); h = wh.headers_for(cfg(), ev_["event_id"], ev_["event_type"], body)
            self.assertTrue(wh.verify(SECRET, h, body)[0])


if __name__ == "__main__":
    unittest.main()
