"""Phase 4: reply-to-alert token resolution.

1. AlertMap: (chat_id, message_id) -> mints, claim tx, verified claim metadata, recorded when GitClaim SENDS an alert
   (alert text itself is untouched; we only keep Telegram's returned message_id). SQLite (WAL, one transaction per
   write = atomic), separate from state.json, survives restarts, retention 30 days / 20k rows.
2. parse_alert_message(): fallback for older alerts with no mapping. Uses Telegram text + entities (UTF-16 offsets)
   and GitClaim's known alert layout, never "the first code block". Result is a mint ONLY, never trusted facts.
3. resolve_reply(): only a genuine, non-forwarded message from OUR bot in the SAME chat is accepted.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58I = {c: i for i, c in enumerate(B58)}
RETENTION_S = 30 * 86400
MAX_ROWS = 20000
ALERT_HEADS = ("🆕 FIRST CLAIM", "🔁 REPEAT CLAIM")
REPLY_COMMANDS = {"research", "holders", "github", "liquidity"}


def b58_32(s: str | None) -> bool:
    """True if s is base58 that decodes to exactly 32 bytes (a Solana pubkey). Case is significant."""
    if not s or not (32 <= len(s) <= 44):
        return False
    n = 0
    for ch in s:
        v = _B58I.get(ch)
        if v is None:
            return False
        n = n * 58 + v
    lead = len(s) - len(s.lstrip("1"))
    return lead + (n.bit_length() + 7) // 8 == 32


# ---- persistence -----------------------------------------------------------------------------
class AlertMap:
    def __init__(self, path: str | Path, now=time.time):
        self.path, self.now = str(path), now
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS alerts (chat INTEGER, msg INTEGER, ts REAL, mints TEXT, sig TEXT,"
                      " meta TEXT, PRIMARY KEY (chat, msg))")
            c.execute("CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts)")

    def _conn(self):
        return sqlite3.connect(self.path, timeout=5)

    def record(self, chat: int, msg_id: int, mints: list[str], sig: str | None, meta: dict | None = None) -> bool:
        mints = [m for m in dict.fromkeys(mints or []) if b58_32(m)]
        try:
            with self._lock, self._conn() as c:     # `with conn` = one atomic transaction
                c.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?)",
                          (int(chat), int(msg_id), self.now(), json.dumps(mints), sig, json.dumps(meta or {}, default=str)))
            return True
        except Exception:
            return False

    def get(self, chat: int, msg_id: int) -> dict | None:
        try:
            with self._lock, self._conn() as c:
                row = c.execute("SELECT ts, mints, sig, meta FROM alerts WHERE chat=? AND msg=? AND ts>=?",
                                (int(chat), int(msg_id), self.now() - RETENTION_S)).fetchone()
        except Exception:
            return None
        if not row:
            return None
        return {"ts": row[0], "mints": json.loads(row[1]), "sig": row[2], "meta": json.loads(row[3] or "{}")}

    def find_by_sig(self, sig: str) -> dict:
        """{chat_id: message_id} of alerts GitClaim delivered for this claim tx."""
        try:
            with self._lock, self._conn() as c:
                return {r[0]: r[1] for r in c.execute("SELECT chat, msg FROM alerts WHERE sig=?", (sig,))}
        except Exception:
            return {}

    def prune(self) -> int:
        try:
            with self._lock, self._conn() as c:
                n = c.execute("DELETE FROM alerts WHERE ts < ?", (self.now() - RETENTION_S,)).rowcount
                n += c.execute("DELETE FROM alerts WHERE rowid NOT IN (SELECT rowid FROM alerts ORDER BY ts DESC LIMIT ?)",
                               (MAX_ROWS,)).rowcount
            return n
        except Exception:
            return 0

    def count(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]


# ---- fallback parser ---------------------------------------------------------------------------
def entity_text(text: str, ent: dict) -> str:
    """Telegram offsets/lengths are in UTF-16 code units (emoji = 2)."""
    u = text.encode("utf-16-le")
    o, n = int(ent.get("offset", 0)) * 2, int(ent.get("length", 0)) * 2
    return u[o:o + n].decode("utf-16-le", "replace")


def _line_of(text: str, ent: dict) -> str:
    u = text.encode("utf-16-le")
    start = len(u[: int(ent["offset"]) * 2].decode("utf-16-le", "replace"))
    end = start + len(entity_text(text, ent))
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    return text[a: b if b >= 0 else len(text)]


def parse_alert_message(text: str | None, entities: list | None) -> dict:
    """-> {"is_alert": bool, "mints": [...], "sig": str|None}. Mints come from `code` entities in the two places the
    alert layout puts a CA: a token-block line that is exactly the CA, or an '• $SYM … <code>CA</code>' extra line."""
    text, entities = text or "", entities or []
    head = text.lstrip().split("\n", 1)[0]
    is_alert = head.startswith(ALERT_HEADS) and "💸 Claim:" in text
    mints, sig = [], None
    if not is_alert:
        return {"is_alert": False, "mints": [], "sig": None}
    for e in entities:
        t = e.get("type")
        if t == "code":
            v = entity_text(text, e)
            line = _line_of(text, e).strip()
            if b58_32(v) and (line == v or (line.startswith("• $") and line.endswith(v))):
                mints.append(v)
        elif t == "text_link" and sig is None:
            url = e.get("url") or ""
            if url.startswith("https://solscan.io/tx/"):
                cand = url[len("https://solscan.io/tx/"):]
                if 64 <= len(cand) <= 90 and all(ch in _B58I for ch in cand):
                    sig = cand
    return {"is_alert": True, "mints": list(dict.fromkeys(mints)), "sig": sig}


# ---- reply resolution --------------------------------------------------------------------------
@dataclass
class Resolution:
    mint: str | None = None
    source: str | None = None           # "stored" | "parsed"
    sig: str | None = None
    meta: dict = field(default_factory=dict)
    error: str | None = None            # user-facing reason when no single mint
    choices: list = field(default_factory=list)


FORWARD_KEYS = ("forward_origin", "forward_from", "forward_from_chat", "forward_date", "forward_sender_name",
                "forward_signature", "forward_from_message_id", "is_automatic_forward")


def resolve_reply(msg: dict, bot_id: int | None, amap: AlertMap | None) -> Resolution | None:
    """None when the command is not a reply at all (normal path). Otherwise a Resolution (mint or error)."""
    rep = (msg or {}).get("reply_to_message")
    if not rep:
        return None
    chat = (msg.get("chat") or {}).get("id")
    frm = rep.get("from") or {}
    if (bot_id is None or frm.get("id") != bot_id or not frm.get("is_bot") or (rep.get("chat") or {}).get("id") != chat
            or any(k in rep for k in FORWARD_KEYS) or rep.get("via_bot") or rep.get("sender_chat")):
        return Resolution(error="That reply isn't to a GitClaim alert. Reply to an alert, or send a CA: <code>/re &lt;CA&gt;</code>.")
    rec = amap.get(chat, rep.get("message_id")) if amap and rep.get("message_id") is not None else None
    if rec and rec["mints"]:
        if len(rec["mints"]) > 1:
            return Resolution(source="stored", sig=rec["sig"], choices=rec["mints"], error="multi")
        return Resolution(mint=rec["mints"][0], source="stored", sig=rec["sig"], meta=rec["meta"])
    p = parse_alert_message(rep.get("text"), rep.get("entities"))
    if not p["is_alert"]:
        return Resolution(error="That message isn't a GitClaim claim alert. Send a CA: <code>/re &lt;CA&gt;</code>.")
    if not p["mints"]:
        return Resolution(error="No token CA found in that alert (mint not resolved). Send a CA: <code>/re &lt;CA&gt;</code>.")
    if len(p["mints"]) > 1:
        return Resolution(source="parsed", sig=p["sig"], choices=p["mints"], error="multi")
    return Resolution(mint=p["mints"][0], source="parsed", sig=p["sig"])
