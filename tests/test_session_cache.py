"""session_running() is memoized (one /room.json asked ~37 times, one fork each), and
the app's own start/stop forgets the cached answer.

    python3 -m unittest tests/test_session_cache.py
"""
import importlib.util, os, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SessionCache(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("app_session_cache_test",
                                                      os.path.join(REPO, "app.py"))
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.calls, self.live = [], True
        self.app._run_tmux = lambda *a, capture=False: (
            self.calls.append(a) or (0 if self.live else 1), "")

    def test_repeat_asks_fork_once(self):
        for _ in range(5):
            self.assertTrue(self.app.session_running("m"))
        self.assertEqual(len(self.calls), 1)

    def test_expires(self):
        self.app.session_running("m")
        self.live = False
        t, hit = self.app._session_running_cache["m"]
        self.app._session_running_cache["m"] = (t - self.app.SESSION_RUNNING_TTL, hit)
        self.assertFalse(self.app.session_running("m"))

    def test_kill_forgets(self):
        self.app.session_running("m")
        self.live = False
        self.app._kill_tmux_session = lambda session: True
        self.app.kill_session("m")
        self.assertFalse(self.app.session_running("m"))


if __name__ == "__main__":
    unittest.main()
