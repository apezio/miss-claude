"""Per-session cgroup: the pane joins one, and app.kill_session ends the whole subtree.

This is the fix for the process leak. Dev/preview servers are started `nohup npx next
dev … &` and nohup exists to ignore the SIGHUP tmux sends a dying pane's process group,
so they survived the console and sat in claude-console.service's cgroup for weeks.
Signals and process-tree walks can always be dodged by daemonizing; a cgroup cannot.

Two halves, tested here:
  * scripts/console-cgroup.sh — sourced by the pane, moves $$ into
    `<unit cgroup>/<tmux session>`. Must FAIL OPEN: no cgroup => a normal, unprotected
    console, never a console that refuses to start.
  * app._session_cgroup / _kill_console_cgroup — one write to cgroup.kill, then rmdir.

stdlib only:  python3 -m unittest tests/test_console_cgroup.py
"""
import importlib.util
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HELPER = os.path.join(ROOT, "scripts", "console-cgroup.sh")


def load_app(**env):
    os.environ["MISSIONS_DIR"] = tempfile.mkdtemp()
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(
        "app_cgroup", os.path.join(ROOT, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HelperTest(unittest.TestCase):
    """The pane side, driven against a FAKE /sys/fs/cgroup so it needs no privileges."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # A fake unified hierarchy: <tmp>/sys/fs/cgroup/<rel>, plus the v2 marker file.
        self.rel = "/system.slice/claude-console.service"
        self.cgroot = os.path.join(self.tmp, "sys", "fs", "cgroup")
        self.unit = self.cgroot + self.rel
        os.makedirs(self.unit)
        open(os.path.join(self.cgroot, "cgroup.controllers"), "w").close()

    def run_helper(self, session="mission-demo", rel=None, cgroot=None, writable=True):
        """Source the helper in a shell whose /proc/self/cgroup and /sys/fs/cgroup are
        ours, and report what it did. `sed`/`mkdir` are real; only the paths are fake."""
        cgroot = self.cgroot if cgroot is None else cgroot
        rel = self.rel if rel is None else rel
        proc_cgroup = os.path.join(self.tmp, "proc-self-cgroup")
        with open(proc_cgroup, "w") as fh:
            fh.write("0::%s\n" % rel)
        # The helper reads two absolute paths; rewrite just those two to the fakes.
        with open(HELPER) as fh:
            src = fh.read()
        src = src.replace('/proc/self/cgroup', proc_cgroup)
        src = src.replace('/sys/fs/cgroup/cgroup.controllers',
                          os.path.join(cgroot, "cgroup.controllers"))
        src = src.replace('"/sys/fs/cgroup${rel}/${session}"',
                          '"%s${rel}/${session}"' % cgroot)
        shim = os.path.join(self.tmp, "helper.sh")
        with open(shim, "w") as fh:
            fh.write(src)
        if not writable:
            os.chmod(self.unit, 0o555)
        try:
            # TMUX unset deliberately: these tests must not be answered by whatever
            # pane the suite itself is running in.
            r = subprocess.run(
                ["bash", "-c",
                 '. "$1" && console_cgroup_join "$2"; echo "RC=$?"; '
                 'echo "CG=${MISS_CONSOLE_CGROUP:-}"',
                 "_", shim, session],
                capture_output=True, text=True, timeout=20,
                env={k: v for k, v in os.environ.items() if k != "TMUX"})
        finally:
            if not writable:
                os.chmod(self.unit, 0o755)
        return r

    def joined(self, r):
        for line in r.stdout.splitlines():
            if line.startswith("CG="):
                return line[3:]
        return ""

    def test_creates_the_cgroup_and_puts_the_shell_in_it(self):
        r = self.run_helper()
        cg = self.joined(r)
        self.assertEqual(cg, os.path.join(self.unit, "mission-demo"))
        self.assertTrue(os.path.isdir(cg))
        # The shell wrote its own pid into cgroup.procs — that is the whole mechanism.
        self.assertTrue(open(os.path.join(cg, "cgroup.procs")).read().strip().isdigit())

    def test_the_cgroup_is_named_for_the_tmux_session(self):
        """app.py derives the same path from the session name it is killing, so the two
        sides agree without any extra bookkeeping."""
        for session in ("mission-a", "local-0123456789ab", "remote-abcdef012345"):
            self.assertEqual(os.path.basename(self.joined(self.run_helper(session))),
                             session)

    # ---------------- fail open: every one of these must still start ----------------

    def test_fails_open_when_the_cgroup_cannot_be_created(self):
        """The Delegate=yes case that has not been applied yet: the unit's cgroup dir is
        still root-owned, so mkdir is refused."""
        r = self.run_helper(writable=False)
        self.assertIn("RC=0", r.stdout)
        self.assertEqual(self.joined(r), "")
        self.assertIn("Delegate=yes", r.stderr)      # says how to fix it

    def test_fails_open_when_there_is_no_cgroup2_hierarchy(self):
        r = self.run_helper(cgroot=os.path.join(self.tmp, "nope"))
        self.assertIn("RC=0", r.stdout)
        self.assertEqual(self.joined(r), "")

    def test_fails_open_on_an_unsafe_session_name(self):
        for session in (".", "..", "../escape", "has space", "semi;colon", "a/b"):
            r = self.run_helper(session)
            self.assertIn("RC=0", r.stdout)
            self.assertEqual(self.joined(r), "", session)

    def test_fails_open_when_the_session_name_cannot_be_determined(self):
        """No argument and no $TMUX to ask — e.g. a pane started outside tmux."""
        r = self.run_helper("")
        self.assertIn("RC=0", r.stdout)
        self.assertEqual(self.joined(r), "")

    def test_fails_open_when_proc_self_cgroup_has_no_v2_line(self):
        r = self.run_helper(rel="")
        self.assertIn("RC=0", r.stdout)
        self.assertEqual(self.joined(r), "")

    def test_reuses_a_leftover_cgroup_dir(self):
        """A previous session's rmdir can lose a race with a slow-dying process. The dir
        is empty by then and named for this session, so adopting it is right."""
        os.makedirs(os.path.join(self.unit, "mission-demo"))
        self.assertTrue(self.joined(self.run_helper()))


class KillTest(unittest.TestCase):
    """The dashboard side: resolve the cgroup, kill it, remove it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.unit = os.path.join(self.tmp, "system.slice", "claude-console.service")
        os.makedirs(self.unit)
        self.app = load_app(MISSION_CONSOLE_CGROUP=self.unit)

    def make(self, session):
        cg = os.path.join(self.unit, session)
        os.makedirs(cg)
        open(os.path.join(cg, "cgroup.kill"), "w").close()
        return cg

    def test_resolves_a_session_to_its_cgroup(self):
        self.assertEqual(self.app._session_cgroup("mission-demo"),
                         os.path.join(self.unit, "mission-demo"))

    def test_refuses_an_unsafe_session_name(self):
        """The name reaches a filesystem path, so it is validated the same way the pane
        side validates it — a traversal must never resolve."""
        for bad in ("", ".", "..", "../../etc", "a/b", "has space"):
            self.assertIsNone(self.app._session_cgroup(bad), bad)

    def rmdir_spy(self, fail=False):
        """A real cgroup dir is removable even though it lists virtual files; a temp dir
        holding a real cgroup.kill is not. So record the rmdir instead of expecting it
        to succeed against the fixture.

        `app.os` IS the os module, so this must be undone: patching it and walking away
        breaks every later test in the suite that removes a directory (it did — 93 of
        them). Hence the addCleanup rather than a bare assignment."""
        seen = []
        real = os.rmdir

        def spy(path):
            seen.append(path)
            if fail:
                raise OSError(16, "Device or resource busy")
        os.rmdir = spy
        self.addCleanup(lambda: setattr(os, "rmdir", real))
        return seen

    def test_kill_writes_1_to_cgroup_kill_and_removes_the_dir(self):
        cg = self.make("mission-demo")
        removed = self.rmdir_spy()
        self.assertTrue(self.app._kill_console_cgroup("mission-demo"))
        with open(os.path.join(cg, "cgroup.kill")) as fh:
            self.assertEqual(fh.read(), "1")
        self.assertEqual(removed, [cg])

    def test_kill_is_a_no_op_when_there_is_no_cgroup(self):
        """Every console started before this shipped, and every one that failed open."""
        self.assertFalse(self.app._kill_console_cgroup("mission-never-had-one"))

    def test_kill_survives_a_cgroup_it_cannot_remove(self):
        """rmdir fails while a process is still dying. The kill already happened, so this
        must not raise — the next open's mkdir -p adopts the leftover dir."""
        cg = self.make("mission-demo")
        self.rmdir_spy(fail=True)
        self.assertTrue(self.app._kill_console_cgroup("mission-demo"))
        with open(os.path.join(cg, "cgroup.kill")) as fh:
            self.assertEqual(fh.read(), "1")

    def test_kill_session_ends_the_cgroup_too(self):
        """The ✕ / idle-reaper / archive / rename path, end to end."""
        cg = self.make("mission-demo")
        removed = self.rmdir_spy()
        calls = []

        def fake_tmux(*args, capture=False):
            calls.append(args[0])
            if args[0] == "has-session":
                return (1, "") if "kill-session" in calls else (0, "")
            if args[0] == "list-sessions":
                return 0, "/sock\t99\t$1\tmission-demo\n"
            return 0, ""
        self.app._run_tmux = fake_tmux
        self.app.reap_console_procs = lambda tags=None: []   # the other, backstop half
        self.assertTrue(self.app.kill_session("demo"))
        with open(os.path.join(cg, "cgroup.kill")) as fh:
            self.assertEqual(fh.read(), "1")
        self.assertEqual(removed, [cg])


if __name__ == "__main__":
    unittest.main()
