"""HTTP client for Queen's API (packages/harness/src/server.ts). Stdlib only.

  GET  /health  -> {"ok": true}
  POST /v1/ask  {"question": str, "history": [...]} -> {"answer","sources","followups","by","tools","ms"}
  Errors: 400 bad_question/bad_body, 404, 413, 429 busy (HARNESS_IN_FLIGHT_MAX reached).
Closing the connection (our timeout) aborts the answer inside Queen (res 'close' -> AbortController).
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class QueenError(Exception):
    code = "queen_error"
    reached_queen = True   # whether Queen may have spent anything

    def __init__(self, msg: str = "", status: int | None = None):
        super().__init__(msg or self.code)
        self.status = status


class QueenUnavailable(QueenError):
    code, reached_queen = "queen_unavailable", False


class QueenBusy(QueenError):
    code, reached_queen = "queen_busy", False       # 429: refused before any work


class QueenTimeout(QueenError):
    code = "queen_timeout"


class QueenHTTPError(QueenError):
    code = "queen_http_error"


class QueenBadResponse(QueenError):
    code = "queen_bad_response"


@dataclass
class QueenAnswer:
    answer: str
    sources: list = field(default_factory=list)
    followups: list = field(default_factory=list)
    by: str = ""
    tools: list = field(default_factory=list)
    ms: int | None = None
    incomplete: list = field(default_factory=list)   # fields that were missing or wrong-typed


def validate_answer(obj) -> QueenAnswer:
    if not isinstance(obj, dict):
        raise QueenBadResponse("response is not a JSON object")
    ans = obj.get("answer")
    if not isinstance(ans, str) or not ans.strip():
        raise QueenBadResponse("missing or empty 'answer'")
    bad = []

    def lst(k, typ=(str, dict)):
        v = obj.get(k)
        if not isinstance(v, list):
            bad.append(k)
            return []
        return [x for x in v if isinstance(x, typ)]
    ms = obj.get("ms")
    if not isinstance(ms, (int, float)):
        bad.append("ms")
        ms = None
    by = obj.get("by") if isinstance(obj.get("by"), str) else ""
    if not by:
        bad.append("by")
    return QueenAnswer(ans, lst("sources"), lst("followups"), by, [t for t in lst("tools", (str,))], int(ms) if ms is not None else None, bad)


RETRY_STATUS = {502, 503}


class QueenClient:
    def __init__(self, base_url: str, *, max_retries: int = 2, backoff_s: float = 2.0, sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self.max_retries = max(0, min(2, max_retries))
        self.backoff_s = backoff_s
        self._sleep = sleep

    def health(self, timeout_s: float = 3.0) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=timeout_s) as r:
                return r.status == 200 and json.loads(r.read() or b"{}").get("ok") is True
        except Exception:
            return False

    def _post_once(self, payload: bytes, timeout_s: float) -> QueenAnswer:
        req = urllib.request.Request(self.base + "/v1/ask", data=payload, method="POST",
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise QueenBusy("queen busy (429)", 429)
            if e.code in RETRY_STATUS:
                raise QueenUnavailable(f"queen HTTP {e.code}", e.code)
            raise QueenHTTPError(f"queen HTTP {e.code}", e.code)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise QueenTimeout(f"no answer within {timeout_s:.0f}s")
            raise QueenUnavailable(f"cannot reach queen: {type(e.reason).__name__}")
        except (socket.timeout, TimeoutError):
            raise QueenTimeout(f"no answer within {timeout_s:.0f}s")
        except (ConnectionError, OSError) as e:
            raise QueenUnavailable(f"connection failed: {type(e).__name__}")
        try:
            obj = json.loads(body.decode("utf8"))
        except (UnicodeDecodeError, ValueError):
            raise QueenBadResponse("malformed JSON from queen")
        return validate_answer(obj)

    def ask(self, question: str, timeout_s: float) -> tuple[QueenAnswer, int]:
        """Returns (answer, attempts). Retries only errors that happened before Queen did work.
        A timeout or any received answer (even a weak one) is never retried: that could double the spend."""
        payload = json.dumps({"question": question, "history": []}).encode()
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._post_once(payload, timeout_s), attempt
            except (QueenBusy, QueenUnavailable) as e:
                if attempt > self.max_retries:
                    raise
                self._sleep(self.backoff_s * attempt * (2 if isinstance(e, QueenBusy) else 1))
