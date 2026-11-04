"""Windows supervisor for GitClaimed: loads .env, runs bot.py, restarts on crash, writes logs/lifecycle.log (CT).

Run hidden:  .venv\Scripts\pythonw.exe scripts\supervise_win.py
Stop:        scripts\gitclaim.ps1 stop   (creates logs\STOP; the bot gets CTRL_BREAK and logs a clean stop line)
"""
import datetime, os, subprocess, sys, time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
LIFE = os.path.join(LOGS, "lifecycle.log")
STOP = os.path.join(LOGS, "STOP")
LOCK = os.path.join(LOGS, "supervisor.pid")
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
CT = ZoneInfo("America/Chicago")


def life(msg):
    ts = datetime.datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S %Z")
    with open(LIFE, "a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")


def load_env(path, env):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v


def pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def main():
    os.makedirs(LOGS, exist_ok=True)
    if os.path.exists(LOCK):
        try:
            old = int(open(LOCK).read().strip())
            if old != os.getpid() and pid_alive(old):
                life(f"supervisor already running (pid {old}); exiting")
                return
        except ValueError:
            pass
    open(LOCK, "w").write(str(os.getpid()))
    if os.path.exists(STOP):
        os.remove(STOP)
    env = dict(os.environ)
    load_env(os.path.join(ROOT, ".env"), env)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    life(f"supervisor start pid={os.getpid()}")
    delay, n = 5, 0
    while True:
        n += 1
        out = open(os.path.join(ROOT, "supervisor.log"), "a", encoding="utf-8")
        p = subprocess.Popen([PY, "bot.py"], cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        life(f"bot start #{n} pid={p.pid}")
        started = time.time()
        while p.poll() is None:
            if os.path.exists(STOP):
                life(f"stop requested; sending CTRL_BREAK to bot pid={p.pid}")
                try:
                    p.send_signal(subprocess.signal.CTRL_BREAK_EVENT)
                    p.wait(15)
                except Exception:
                    p.kill(); p.wait()
                life(f"bot stop pid={p.pid} exit={p.returncode} (requested)")
                life("supervisor stop")
                os.remove(STOP)
                os.remove(LOCK)
                return
            time.sleep(1)
        out.close()
        ran = time.time() - started
        delay = 5 if ran > 300 else min(delay * 2, 300)
        life(f"bot crash pid={p.pid} exit={p.returncode} after {int(ran)}s; restart in {delay}s")
        for _ in range(delay):
            if os.path.exists(STOP):
                life("supervisor stop (while waiting to restart)"); os.remove(STOP); os.remove(LOCK); return
            time.sleep(1)
        life("bot restart")


if __name__ == "__main__":
    main()
