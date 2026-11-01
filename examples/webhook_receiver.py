"""Isolated EXAMPLE receiver for local tests only (stdlib, no GitClaim imports). Not for deployment as-is.

    WEBHOOK_SECRET=<same secret> python3 examples/webhook_receiver.py 8799

Shows the steps any receiver (Rust/TS/Python) must do: read RAW body, check headers, timestamp tolerance,
constant-time HMAC compare, dedupe by event id, ack fast with 2xx.
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOLERANCE_S = 300
MAX_BODY = 256 * 1024


def check(secret: bytes, h, body: bytes, now=None):
    eid, ts, ver, sig = (h.get("X-GitClaim-Event-ID"), h.get("X-GitClaim-Timestamp"),
                         h.get("X-GitClaim-Version"), h.get("X-GitClaim-Signature"))
    if not (eid and ts and ver and sig) or ver != "1":
        return 400, "bad headers"
    try:
        if abs((now or time.time()) - int(ts)) > TOLERANCE_S:
            return 401, "stale timestamp"
    except ValueError:
        return 400, "bad timestamp"
    want = "v1=" + hmac.new(secret, f"{ts}.{eid}.{ver}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(want.encode(), s.strip().encode()) for s in sig.split(",")):
        return 401, "bad signature"
    try:
        ev = json.loads(body)
    except ValueError:
        return 400, "bad json"
    if ev.get("event_id") != eid:
        return 400, "event id mismatch"
    return 200, ev


class Receiver:
    def __init__(self, secret: str, port: int = 0, fail_plan=None):
        self.secret = secret.encode()
        self.seen, self.events, self.log = set(), [], []
        self.fail_plan = list(fail_plan or [])          # test hook: e.g. [500, 429] -> first two requests fail
        self.lock = threading.Lock()
        rx = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._reply(413, "too large")
                body = self.rfile.read(n)
                with rx.lock:
                    if rx.fail_plan:
                        code = rx.fail_plan.pop(0); rx.log.append(code)
                        return self._reply(code, "planned failure", {"Retry-After": "1"} if code == 429 else None)
                code, ev = check(rx.secret, self.headers, body)
                with rx.lock:
                    if code != 200:
                        rx.log.append(code); return self._reply(code, ev)
                    if ev["event_id"] in rx.seen:
                        rx.log.append("dup"); return self._reply(200, "duplicate ignored")
                    rx.seen.add(ev["event_id"]); rx.events.append(ev); rx.log.append(200)
                self._reply(200, "ok")                  # ack fast; process asynchronously in a real receiver

            def _reply(self, code, msg, hdrs=None):
                self.send_response(code)
                for k, v in (hdrs or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(str(msg).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/gitclaim"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start(); return self

    def stop(self):
        self.server.shutdown(); self.server.server_close()


if __name__ == "__main__":
    s = os.environ.get("WEBHOOK_SECRET") or sys.exit("set WEBHOOK_SECRET")
    r = Receiver(s, int(sys.argv[1]) if len(sys.argv) > 1 else 8799).start()
    print(f"listening on {r.url} (local test only)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        r.stop()
