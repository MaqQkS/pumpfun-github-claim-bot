"""ResearchAdapter: route -> budget gate -> cache/dedup -> Queen -> reconcile -> format. Never raises."""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from .client import QueenAnswer, QueenClient, QueenError
from .config import COMMANDS, Settings, load_settings
from .ledger import Ledger
from .meter import Meter
from .prompt import ClaimFacts, build_question
from .router import RoutePlan, route


@dataclass
class ResearchResult:
    ok: bool
    plan: RoutePlan | None = None
    text: str = ""                     # formatted A/B/C text (ok) or a user-facing reason
    error: str | None = None           # machine code: unavailable, deep_disabled, budget, queen_unavailable, ...
    cached: bool = False
    deduped: bool = False
    usage: dict | None = None
    latency_ms: int | None = None
    answer: QueenAnswer | None = None
    address_fixes: list = field(default_factory=list)
    unverified_addresses: list = field(default_factory=list)
    ledger_id: int | None = None


class ResearchAdapter:
    def __init__(self, settings: Settings | None = None, *, clients: dict | None = None, sleep=time.sleep):
        self.s = settings or load_settings()
        self.ledger = Ledger(self.s.ledger_path)
        self.meter = Meter(self.s.meter_path, self.s.prices)
        self._clients = clients or {}
        self._sleep = sleep
        self._gate = threading.BoundedSemaphore(max(1, self.s.max_in_flight))
        self._lock = threading.Lock()
        self._cache: dict = {}            # key -> (expires, ResearchResult)
        self._inflight: dict = {}         # key -> Future

    def client_for(self, url: str) -> QueenClient:
        if url not in self._clients:
            self._clients[url] = QueenClient(url, max_retries=self.s.max_retries, backoff_s=self.s.retry_backoff_s, sleep=self._sleep)
        return self._clients[url]

    def health(self) -> dict:
        out = {"standard": self.client_for(self.s.standard_url).health()}
        out["deep"] = self.client_for(self.s.deep_url).health() if self.s.deep_enabled else None
        out["budget"] = self.ledger.totals()
        return out

    # ---- public entry -------------------------------------------------------------------------
    def research(self, command_text: str, facts: ClaimFacts | None = None) -> ResearchResult:
        try:
            return self._research(command_text, facts)
        except Exception as e:  # last line of defense: never propagate into the caller
            return ResearchResult(False, None, "Research failed unexpectedly.", f"internal:{type(e).__name__}")

    def _research(self, text: str, facts: ClaimFacts | None) -> ResearchResult:
        plan = route(text, self.s)
        if plan.error:
            msg = {"unavailable": plan.note, "deep_disabled": plan.note, "missing_mint": "Send a token mint (CA).",
                   "unknown_command": "Unknown command.", "empty": "Empty command.",
                   "deep_needs_command": "Use /d with re, dev or wa."}.get(plan.error, plan.error)
            return ResearchResult(False, plan, msg or plan.error, plan.error)
        if not plan.uses_llm:
            # Tier 0 belongs to the future command layer (direct APIs). The adapter never calls an LLM for it.
            return ResearchResult(False, plan, plan.note or "Answered directly without AI (not part of the adapter).", "direct_command")
        if facts is None or facts.mint != plan.mint:
            facts = ClaimFacts(mint=plan.mint)
        key = (plan.command, "deep" if plan.deep else "standard", plan.model, plan.mint)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                r = hit[1]
                return ResearchResult(**{**r.__dict__, "cached": True})
            fut = self._inflight.get(key)
            owner = fut is None
            if owner:
                fut = Future()
                self._inflight[key] = fut
        if not owner:
            r = fut.result()
            return ResearchResult(**{**r.__dict__, "deduped": True})
        try:
            r = self._run(plan, facts)
            if r.ok:
                with self._lock:
                    self._cache[key] = (time.time() + self.s.cache_ttl_s, r)
            fut.set_result(r)
            return r
        except Exception as e:
            r = ResearchResult(False, plan, "Research failed unexpectedly.", f"internal:{type(e).__name__}")
            fut.set_result(r)
            return r
        finally:
            with self._lock:
                self._inflight.pop(key, None)

    def _run(self, plan: RoutePlan, facts: ClaimFacts) -> ResearchResult:
        from .formatter import format_result
        mode = "deep" if plan.deep else "standard"
        if not self._gate.acquire(timeout=1.0):
            return ResearchResult(False, plan, "Another research is running; try again shortly.", "busy_local")
        try:
            row, dec = self.ledger.reserve(settings=self.s, command=plan.command, mode=mode, model=plan.model,
                                           effort=plan.effort, mint=plan.mint)
            if row is None:
                return ResearchResult(False, plan, f"Research paused: budget limit reached ({dec.reason}).", "budget")
            task = COMMANDS[plan.command].task
            question = build_question(task, facts, COMMANDS[plan.command].limited, COMMANDS[plan.command].answer_format)
            mark = self.meter.mark()
            t0 = time.time()
            try:
                ans, _ = self.client_for(plan.url).ask(question, plan.timeout_s)
            except QueenError as e:
                lat = int((time.time() - t0) * 1000)
                usage = self._reconcile(mark)
                status = "error" if (e.reached_queen or (usage and usage.get("llm_calls"))) else "not_sent"
                if status == "error" and usage is not None:
                    # Queen may still have been mid-call when we hung up: never record less than the reservation
                    usage = {**usage, "actual_usd": max(usage.get("actual_usd") or 0.0, self.s.worst_case_usd.get(plan.model, 0.0)),
                             "cost_source": "reservation_kept_after_error"}
                self.ledger.finish(row, status=status, error=e.code, latency_ms=lat, usage=usage)
                return ResearchResult(False, plan, f"Research unavailable right now ({e.code}). Alerts are unaffected.",
                                      e.code, usage=usage, latency_ms=lat, ledger_id=row)
            lat = int((time.time() - t0) * 1000)
            usage = self._reconcile(mark)
            if usage and usage.get("models") and any(m != plan.model for m in usage["models"]):
                usage["model_mismatch"] = usage["models"]
            self.ledger.finish(row, status="ok", latency_ms=lat, tools=ans.tools, usage=usage,
                               est_usd=self.s.worst_case_usd.get(plan.model))
            text, rep = format_result(facts=facts, answer=ans, plan=plan, usage=usage, latency_ms=lat)
            return ResearchResult(True, plan, text, None, usage=usage, latency_ms=lat, answer=ans,
                                  address_fixes=rep.fixed, unverified_addresses=rep.unverified, ledger_id=row)
        finally:
            self._gate.release()

    def _reconcile(self, mark: int) -> dict | None:
        self._sleep(0.3)   # the meter writes usage just after the response body is read
        return self.meter.read_since(mark)
