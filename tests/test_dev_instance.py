"""scripts/dev-instance keeps its tmux away from the live console server.

    python3 -m unittest tests/test_dev_instance.py
"""
import os, socket, subprocess, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "dev-instance")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TmuxIsolation(unittest.TestCase):
    def test_instance_never_inherits_the_live_tmux(self):
        port = str(free_port())
        env = dict(os.environ, PORT=port, TMUX="/home/x/.tmux-console/tmux-1/default,1,0",
                   TMUX_TMPDIR="/home/x/.tmux-console", MISSION_NAME="caller",
                   MISSION_DATA_DIR="/home/x/missions/caller", CLAUDE_MISS_ROLE="feature",
                   MISS_WORKTREE="/home/x/wt", MISS_BARK_CONFIG="/tmp/bark-off.env")
        env.pop("MISSION_TMUX", None)
        state = "/tmp/miss-dev-instance-%d/%s" % (os.getuid(), port)
        r = subprocess.run([SCRIPT, "start"], env=env, capture_output=True, text=True, timeout=60)
        try:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with open(os.path.join(state, "pid")) as fh:
                pid = fh.read().strip()
            with open("/proc/%s/environ" % pid, "rb") as fh:
                penv = dict(kv.split("=", 1) for kv in fh.read().decode().split("\0") if "=" in kv)
            self.assertNotIn("TMUX", penv)
            self.assertEqual(penv["TMUX_TMPDIR"], os.path.join(state, "tmux"))
            with open(penv["MISSION_TMUX"]) as fh:     # console-launch.sh's plain tmux
                self.assertIn('-S "%s/tmux/tmux-%d/default"' % (state, os.getuid()), fh.read())
            for k in ("MISSION_NAME", "MISSION_DATA_DIR", "CLAUDE_MISS_ROLE", "MISS_WORKTREE"):
                self.assertNotIn(k, penv)               # the caller's identity stays behind
            self.assertEqual(penv["MISS_BARK_CONFIG"], "/tmp/bark-off.env")
        finally:
            env.pop("PORT")                             # the argument alone picks it
            subprocess.run([SCRIPT, "stop", port], env=env, capture_output=True, timeout=60)
        self.assertFalse(os.path.exists(state))

    def test_stop_refuses_a_port_with_no_instance_and_the_live_ports(self):
        env = dict(os.environ)
        env.pop("PORT", None)
        for port, why in ((str(free_port()), "no dev instance"), ("4200", "LIVE")):
            r = subprocess.run([SCRIPT, "stop", port], env=env, capture_output=True,
                               text=True, timeout=30)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn(why, r.stderr)
        r = subprocess.run([SCRIPT, "stop", "4211"], env=dict(env, PORT="4212"),
                           capture_output=True, text=True, timeout=30)
        self.assertIn("disagree", r.stderr)


if __name__ == "__main__":
    unittest.main()
