"""Start/stop lifecycle log lines for the bot (no secrets)."""
import os, platform, signal, subprocess, sys, threading, time

START = time.time()
REASON = ["normal"]


def commit(dirpath):
    try:
        return subprocess.run(["git", "-C", dirpath, "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def startup_line(dirpath, auto_on, catchup_on, testers, chats, cmds_on):
    return (f"LIFECYCLE start commit={commit(dirpath)} host={platform.node()} os={platform.system()} pid={os.getpid()} "
            f"py={platform.python_version()} commands={'on' if cmds_on else 'off'} testers={testers} chats={chats} "
            f"auto_research={'ON' if auto_on else 'off'} catchup={'ON' if catchup_on else 'off'}")


def stop_line(reason=None):
    up = int(time.time() - START)
    return f"LIFECYCLE stop reason={reason or REASON[0]} pid={os.getpid()} uptime={up // 3600}h{up % 3600 // 60:02d}m{up % 60:02d}s"


def install_signals(log):
    """SIGINT/SIGTERM (and SIGBREAK on Windows) -> log a stop line and exit."""
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum, _frame):
        REASON[0] = signal.Signals(signum).name
        log.info(stop_line()); logged[0] = True
        os._exit(0)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try: signal.signal(getattr(signal, name), handler)
            except (ValueError, OSError): pass


logged = [False]
