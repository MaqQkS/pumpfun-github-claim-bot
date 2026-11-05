"""Windows supervisor for deep Queen (port of research/queen/start_queen.sh deep mode).

Runs node packages/harness/src/server.ts in QUEEN_DIR on 127.0.0.1:8791 with grok-4.7,
restarts on crash, writes queen-deep start/stop/crash lines to logs/lifecycle.log (CT).
Do NOT start in production until RESEARCH_DEEP_ENABLED is approved.
Native xAI x_search/web_search for /d re is handled by research/deep_client.py (xai_native),
not by this process. This deep Queen is the optional RESEARCH_DEEP_BACKEND=queen path and
keeps TOOLS_XSEARCH/NEWS off by default (OpenRouter key required for Queen's own X tools).
Run hidden:  .venv\Scripts\pythonw.exe scripts/supervise_queen_deep_win.py
Stop:        scripts/queen_win.ps1 deep stop
"""
import datetime, os, pathlib, subprocess, time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
LIFE = os.path.join(LOGS, "lifecycle.log")
STOP = os.path.join(LOGS, "QUEEN_DEEP_STOP")
LOCK = os.path.join(LOGS, "queen_deep_supervisor.pid")
HOME = os.path.expanduser("~")
QUEEN_DIR = os.environ.get("QUEEN_DIR", os.path.join(HOME, "queen-harness"))
NODE_DIR = os.environ.get("NODE_DIR", os.path.join(HOME, ".local", "node-v24.11.0-win-x64"))
NODE = os.path.join(NODE_DIR, "node.exe")
DATA = os.path.join(ROOT, "research", "data")
METER = os.path.join(ROOT, "research", "queen", "meter.mjs")
CT = ZoneInfo("America/Chicago")
TOOLS_DISABLE = ("chain_tvl_ranking,dex_pair_lookup,prediction_odds,protocol_tvl,sol_ticker_mint,"
                 "stablecoin_supply,top_protocols,unlock_schedule,yield_pools")


def life(msg):
    ts = datetime.datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S %Z")
    with open(LIFE, "a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")


def pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return str(pid) in out


def queen_env():
    env = dict(os.environ)
    env["PATH"] = NODE_DIR + os.pathsep + env.get("PATH", "")
    env["METER_OUT"] = os.path.join(DATA, "queen-meter-deep.jsonl")
    env["NODE_OPTIONS"] = "--import " + pathlib.Path(METER).as_uri()
    # Deep Queen: grok-4.7 medium. Keep Queen's paid OpenRouter X/news OFF unless an operator
    # explicitly sets TOOLS_XSEARCH=1 and TOOLS_XSEARCH_KEY (OpenRouter). Preferred /d re path is
    # research/deep_client.py (native xAI tools, same xAI key, no OpenRouter).
    env.update({
        "HOST": "127.0.0.1", "PORT": "8791", "LLM_MODEL": "grok-4.7",
        "LLM_EXTRA_BODY": '{"reasoning_effort":"medium"}',
        "TOOLS_DISABLE": TOOLS_DISABLE,
        "TOOLS_MAX_CALLS_PER_ANSWER": os.environ.get("TOOLS_MAX_CALLS_PER_ANSWER", "12"),
        "HARNESS_IN_FLIGHT_MAX": "1",
        "TOOLS_XSEARCH": os.environ.get("TOOLS_XSEARCH", "0"),
        "TOOLS_NEWS": os.environ.get("TOOLS_NEWS", "0"),
        "TOOLS_CTRADAR": "0", "TOOLS_HUB": "0",
        "HARNESS_ANSWER_MS": os.environ.get("HARNESS_ANSWER_MS", "240000"),
    })
    return env


def main():
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    if os.path.exists(LOCK):
        try:
            old = int(open(LOCK).read().strip())
            if old != os.getpid() and pid_alive(old):
                life(f"queen-deep supervisor already running (pid {old}); exiting")
                return
        except ValueError:
            pass
    open(LOCK, "w").write(str(os.getpid()))
    if os.path.exists(STOP):
        os.remove(STOP)
    env = queen_env()
    life(f"queen-deep supervisor start pid={os.getpid()}")
    delay, n = 5, 0
    while True:
        n += 1
        out = open(os.path.join(LOGS, "queen-deep.log"), "a", encoding="utf-8")
        p = subprocess.Popen([NODE, "packages/harness/src/server.ts"], cwd=QUEEN_DIR, env=env, stdout=out,
                             stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        life(f"queen-deep start #{n} pid={p.pid}")
        started = time.time()
        while p.poll() is None:
            if os.path.exists(STOP):
                life(f"queen-deep stop requested; stopping pid={p.pid}")
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    p.wait(10)
                except Exception:
                    p.kill(); p.wait()
                life(f"queen-deep stop pid={p.pid} exit={p.returncode} (requested)")
                life("queen-deep supervisor stop")
                os.remove(STOP); os.remove(LOCK)
                return
            time.sleep(1)
        out.close()
        ran = time.time() - started
        delay = 5 if ran > 300 else min(delay * 2, 300)
        life(f"queen-deep crash pid={p.pid} exit={p.returncode} after {int(ran)}s; restart in {delay}s")
        for _ in range(delay):
            if os.path.exists(STOP):
                life("queen-deep supervisor stop (while waiting to restart)"); os.remove(STOP); os.remove(LOCK); return
            time.sleep(1)
        life("queen-deep restart")


if __name__ == "__main__":
    main()
