import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lifecycle as lc


class TestLifecycle(unittest.TestCase):
    def test_start_line_has_summary_no_secrets(self):
        os.environ["TELEGRAM_BOT_TOKEN_TEST"] = "123456:SECRETSECRETSECRET"
        line = lc.startup_line(".", False, False, 2, 3, True)
        for part in ("LIFECYCLE start", "commit=", "host=", "pid=", "testers=2", "chats=3", "auto_research=off", "catchup=off"):
            self.assertIn(part, line)
        self.assertNotIn("SECRET", line)

    def test_stop_line_reason_and_uptime(self):
        lc.REASON[0] = "SIGTERM"
        line = lc.stop_line()
        self.assertIn("reason=SIGTERM", line); self.assertRegex(line, r"uptime=\d+h\d\dm\d\ds")
        self.assertIn("reason=exception:RuntimeError", lc.stop_line("exception:RuntimeError"))
        lc.REASON[0] = "normal"


if __name__ == "__main__":
    unittest.main()
