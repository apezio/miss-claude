"""macOS process cleanup without /proc or cgroups (app.py's IS_DARWIN branches).

Real processes, no mocks of ps: a fake "pane" (a bash that is its own session/group
leader, as a tmux pane is) sources scripts/console-cgroup.sh, which records its
pid/pgid next to a fake tmux socket, and starts three kinds of background child:

  * nohup'd, same process group      -> killed (kill -- -PGID, and the walk)
  * own process group, still a child -> killed (descendant walk)
  * double fork + setsid daemon      -> ESCAPES: not a descendant, not in the group.
    That is the documented limit of the macOS path; the test pins it down.

Darwin only: Linux keeps the cgroup path (tests/test_console_cgroup.py).
stdlib only:  python3 -m unittest tests/test_mac_console_tree.py
"""
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "scripts", "console-cgroup.sh")


def load_app(missions_dir):
    os.environ["MISSIONS_DIR"] = missions_dir
    spec = importlib.util.spec_from_file_location(
        "app_mac_tree", os.path.join(HERE, "..", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); ps says whether it is really running.
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                       capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


SLEEP = "import time; time.sleep(120)"
OWN_GROUP = "import os, time; os.setpgid(0, 0); time.sleep(120)"
DAEMON = ("import os, sys, time\n"
          "if os.fork(): sys.exit(0)\n"
          "os.setsid()\n"
          "if os.fork(): sys.exit(0)\n"
          "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
          "time.sleep(120)\n")


@unittest.skipUnless(sys.platform == "darwin", "macOS-only fallback")
class MacConsoleTreeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = load_app(os.path.join(self.tmp, "missions"))
        self.app._reap_settle = lambda: time.sleep(0.3)
        self.sock = os.path.join(self.tmp, "tmux-501", "default")
        os.makedirs(os.path.dirname(self.sock))
        self.pids, self.procs = [], []

    def tearDown(self):
        for p in self.pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        for p in self.procs:
            p.wait(5)

    def popen(self, *a, **kw):
        p = subprocess.Popen(*a, **kw)
        self.procs.append(p)
        self.pids.append(p.pid)
        return p

    def start_pane(self):
        """A pane-like bash with a fake $TMUX; returns {name: pid} of what it started."""
        d = self.tmp
        script = (
            '. "$1"; console_cgroup_join mission-t\n'
            'nohup python3 -c "$2" >/dev/null 2>&1 & echo $! > "$5/same"\n'
            'python3 -c "$3" & echo $! > "$5/own"\n'
            'python3 -c "$4" "$5/daemon"\n'
            'echo ready > "$5/ready"; wait\n')
        env = dict(os.environ, TMUX="%s,4242,7" % self.sock, MISSION_NAME="t")
        pane = self.popen(
            ["/bin/bash", "-c", script, "_", HELPER, SLEEP, OWN_GROUP, DAEMON, d],
            env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            time.sleep(0.05)
        time.sleep(0.3)   # let the own-group child reach setpgid
        got = {"pane": pane.pid}
        for k in ("same", "own", "daemon"):
            with open(os.path.join(d, k)) as fh:
                got[k] = int(fh.read())
        self.pids += [got[k] for k in ("same", "own", "daemon")]
        return pane, got

    def test_join_records_the_panes_pid_and_pgid_next_to_the_socket(self):
        pane, got = self.start_pane()
        with open(self.sock + "-pgid/mission-t") as fh:
            self.assertEqual(fh.read().split(), [str(pane.pid), str(pane.pid)])

    def test_stopping_kills_the_group_and_descendants_but_a_daemon_escapes(self):
        pane, got = self.start_pane()
        snap = self.app._console_tree_snapshot("mission-t", self.sock)
        self.assertIsNotNone(snap)
        pgid, born, path = snap
        self.assertEqual(pgid, pane.pid)
        self.assertTrue({got["pane"], got["same"], got["own"]} <= set(born))
        self.assertNotIn(got["daemon"], born)
        self.assertNotIn(os.getpid(), born)
        # What kill-session does: SIGHUP the pane's group. nohup shrugs it off.
        os.killpg(pane.pid, signal.SIGHUP)
        pane.wait(5)
        self.assertTrue(alive(got["same"]))
        self.app._kill_console_tree(snap)
        time.sleep(0.2)
        self.assertFalse(alive(got["same"]), "nohup'd child in the pane's group survived")
        self.assertFalse(alive(got["own"]), "own-group descendant survived")
        self.assertTrue(alive(got["daemon"]), "documented escape: double-fork + setsid")
        self.assertFalse(os.path.exists(path))

    def test_a_stale_record_is_ignored(self):
        os.makedirs(self.sock + "-pgid")
        with open(self.sock + "-pgid/mission-t", "w") as fh:
            fh.write("999999 999999\n")
        self.assertIsNone(self.app._console_tree_snapshot("mission-t", self.sock))
        self.assertIsNone(self.app._console_tree_snapshot("../x", self.sock))

    def test_proc_table_reads_tags_from_ps(self):
        p = self.popen(
            [sys.executable, "-c", SLEEP],
            env=dict(os.environ, TMUX="/x/sock,11,3", MISSION_NAME="probe"))
        time.sleep(0.3)
        rows = {r[0]: r for r in self.app._console_proc_table()}
        self.assertIn(p.pid, rows)
        _pid, ppid, tag, mission, cmd = rows[p.pid]
        self.assertEqual((ppid, tag, mission), (os.getpid(), ("/x/sock", 11, 3), "probe"))
        self.assertIn("time.sleep(120)", cmd)
        self.assertNotIn("TMUX=", cmd)

    def test_starttime_is_ps_lstart(self):
        p = self.popen([sys.executable, "-c", SLEEP])
        st = self.app._proc_starttime(p.pid)
        self.assertTrue(st)
        self.assertEqual(st, self.app._proc_starttime(p.pid))
        p.kill()
        p.wait()
        self.assertIsNone(self.app._proc_starttime(p.pid))

    def test_live_cwd_comes_from_lsof(self):
        p = self.popen([sys.executable, "-c", SLEEP], cwd=self.tmp)
        self.app._running_claude_pid = lambda name: p.pid
        time.sleep(0.2)
        self.assertEqual(os.path.realpath(self.app._live_console_cwd("x")),
                         os.path.realpath(self.tmp))

    def test_notice_names_launchctl_kickstart(self):
        n = self.app._ttyd_down_notice()
        self.assertIn("launchctl kickstart -k gui/%d/local.miss-claude" % os.getuid(), n)
        self.assertNotIn("systemctl", n)


if __name__ == "__main__":
    unittest.main()
