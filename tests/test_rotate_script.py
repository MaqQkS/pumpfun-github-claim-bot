"""Rotation script fails closed. Mock curl (reads the URL config from stdin like real curl -K -); fake tokens only."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rotate_telegram_token.sh"
OLD = "111111111:" + "A" * 35
NEW = "222222222:" + "B" * 35
MOCK = """#!/bin/bash
cfg=$(cat)
case "$cfg" in *bot%s/*) printf '%%s' "$MOCK_OLD";; *bot%s/*) printf '%%s' "$MOCK_NEW";; *) printf 404;; esac
""" % (OLD, NEW)


@unittest.skipUnless(__import__("shutil").which("bash"), "bash scripts are macOS-only")
class TestRotate(unittest.TestCase):
    def run_script(self, old_code, new_code, token=NEW):
        d = Path(tempfile.mkdtemp())
        (d / ".env").write_text(f"export HELIUS_API_KEY=keep-me\nexport TELEGRAM_BOT_TOKEN={OLD}\n"); os.chmod(d / ".env", 0o600)
        (d / "bot.log").write_text("old line\n")
        curl = d / "mockcurl"; curl.write_text(MOCK); curl.chmod(0o755)
        start = d / "start.sh"
        start.write_text("#!/bin/bash\necho started >> restarts.txt\necho '2026 INFO tg poll started (offset 1, 2 subscribers)' >> bot.log\necho 'INFO ws connected (helius)' >> bot.log\necho 'INFO bot username @x' >> bot.log\n")
        start.chmod(0o755)
        env = dict(os.environ, ROTATE_DIR=str(d), ROTATE_CURL=str(curl), ROTATE_START="./start.sh", ROTATE_WAIT="0",
                   ROTATE_SKIP_VERIFY="1", MOCK_OLD=old_code, MOCK_NEW=new_code)
        p = subprocess.run(["bash", str(SCRIPT)], input=token + "\n", capture_output=True, text=True, env=env, timeout=30)
        return d, p

    def assertUntouched(self, d, p):
        self.assertNotEqual(p.returncode, 0); self.assertIn("ABORT", p.stdout); self.assertIn("Nothing was changed", p.stdout)
        self.assertIn(OLD, (d / ".env").read_text()); self.assertFalse((d / "restarts.txt").exists())
        self.assertEqual(list(d.glob(".env.bak*")), [])

    def test_old_token_still_valid_aborts(self):
        d, p = self.run_script("200", "200"); self.assertUntouched(d, p); self.assertIn("not revoked", p.stdout)

    def test_old_token_network_error_aborts(self):
        d, p = self.run_script("000", "200"); self.assertUntouched(d, p)

    def test_new_token_rejected_aborts(self):
        d, p = self.run_script("401", "401"); self.assertUntouched(d, p); self.assertIn("new token was not accepted", p.stdout)

    def test_bad_format_and_same_token_abort(self):
        d, p = self.run_script("401", "200", token="nope"); self.assertUntouched(d, p)
        d, p = self.run_script("401", "200", token=OLD); self.assertUntouched(d, p)

    def test_success_rotates_once_and_never_prints_tokens(self):
        d, p = self.run_script("401", "200")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        env = (d / ".env").read_text()
        self.assertIn(f"export TELEGRAM_BOT_TOKEN={NEW}", env); self.assertNotIn(OLD, env); self.assertIn("HELIUS_API_KEY=keep-me", env)
        self.assertEqual(stat.S_IMODE((d / ".env").stat().st_mode), 0o600)
        baks = list(d.glob(".env.bak-*")); self.assertEqual(len(baks), 1)
        self.assertEqual(stat.S_IMODE(baks[0].stat().st_mode), 0o600)
        self.assertEqual((d / "restarts.txt").read_text().count("started"), 1)
        for secret in (OLD, NEW, OLD.split(":")[1], NEW.split(":")[1]):
            self.assertNotIn(secret, p.stdout + p.stderr)
        self.assertIn("polling: 1", p.stdout); self.assertIn("2 subscribers", p.stdout)
        self.assertIn("new-token hits 0, old-token hits 0", p.stdout)
        self.assertEqual(list(d.glob(".env.tmp*")), [])


if __name__ == "__main__":
    unittest.main()
