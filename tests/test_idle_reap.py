"""Idle reaper (app.idle_sessions / reap_idle_sessions): only an unattached
`mission-<name>` session of an existing mission, idle past IDLE_REAP_AFTER on BOTH
activity and last-attach, is stopped — and only through kill_session's exact-name path."""
import importlib.util, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load_app(missions_dir):
    os.environ["MISSIONS_DIR"] = missions_dir
    os.environ["MISSION_IDLE_REAP"] = "3600"
    spec = importlib.util.spec_from_file_location("app_idle", os.path.join(HERE, "..", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class IdleReapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for m in ("old", "fresh", "watched", "recent-attach"):
            os.makedirs(os.path.join(self.tmp, m))
        self.app = load_app(self.tmp)
        self.now = 1_000_000
        stale = self.now - 2 * 3600
        rows = [
            ("mission-old", stale, stale, 0),            # eligible
            ("mission-fresh", self.now - 10, stale, 0),   # recent output
            ("mission-watched", stale, stale, 1),         # someone attached
            ("mission-recent-attach", stale, self.now - 5, 0),  # just detached
            ("mission-gone", stale, stale, 0),            # no mission dir
            ("remote-abcdef123456", stale, stale, 0),     # ad-hoc console
            ("mission-../x", stale, stale, 0),            # unsafe name
        ]
        out = "".join("%s\t%d\t%d\t%d\n" % r for r in rows)
        self.calls = []

        def fake_tmux(*args, capture=False):
            self.calls.append(args)
            if args[0] == "list-sessions":
                return 0, out
            if args[0] == "has-session":
                return 0, ""
            return 0, ""
        self.app._run_tmux = fake_tmux

    def test_only_idle_unattached_existing_missions(self):
        self.assertEqual(self.app.idle_sessions(now=self.now), ["old"])

    def test_disabled(self):
        self.app.IDLE_REAP_AFTER = 0
        self.assertEqual(self.app.idle_sessions(now=self.now), [])

    def test_reap_goes_through_kill_session_exact_name(self):
        killed = []
        self.app.kill_session = lambda name: killed.append(name) or True
        self.app.time.time = lambda: self.now
        self.assertEqual(self.app.reap_idle_sessions(), ["old"])
        self.assertEqual(killed, ["old"])

    def test_kill_session_targets_exact_session(self):
        self.app.time.sleep = lambda s: None
        self.app.kill_session("old")
        kills = [c for c in self.calls if c[0] == "kill-session"]
        self.assertEqual(kills, [("kill-session", "-t", "=mission-old")])

    def test_no_tmux_server(self):
        self.app._run_tmux = lambda *a, **k: (1, "")
        self.assertEqual(self.app.idle_sessions(now=self.now), [])


if __name__ == "__main__":
    unittest.main()
