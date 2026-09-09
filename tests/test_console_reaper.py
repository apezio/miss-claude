"""Stranded-console-process reaper (app.stranded_console_procs / reap_console_procs).

A mission console's children escape tmux whenever they land in their own process group
(any backgrounded dev/preview server does): `tmux kill-session` SIGHUPs the PANE's
process group only, so those survive, reparent to PID 1, and stay in
claude-console.service's cgroup forever — the PID leak.

The reaper's discriminator is the `TMUX=<socket>,<server-pid>,<session-id>` variable
tmux itself puts in every pane's environment and every descendant therefore inherits
(verified to reach playwright's chrome and its crashpad handler). A process whose tag
names a pane that is NOT currently live is stranded by definition; a process with no
tag, or a tag naming a live pane, is never touched.

The whole point is that it can never hit a live mission, so most of this file is about
what it must REFUSE to kill — above all when tmux cannot be queried at all.

stdlib only:  python3 -m unittest tests/test_console_reaper.py
"""
import importlib.util
import os
import signal
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

SOCK = "/home/op/.tmux-console/tmux-1000/default"   # the shared console socket
OTHER = "/home/op/.tmux-console/tmux-1000/misstest"  # someone else's tmux server
LIVE_SERVER = 3007958        # the tmux server that is running now
DEAD_SERVER = 158818         # the one a `tmux kill-server` took out


def live(sid, sock=SOCK):
    return (sock, LIVE_SERVER, sid)


def dead(sid, sock=SOCK):
    return (sock, DEAD_SERVER, sid)


def load_app(missions_dir):
    os.environ["MISSIONS_DIR"] = missions_dir
    spec = importlib.util.spec_from_file_location(
        "app_reaper", os.path.join(HERE, "..", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReaperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = load_app(self.tmp)
        self.signals = []
        self.tmux_ok = True
        self.server_gone = False

        # Live: sessions $4 and $8 on the running server.
        def fake_tmux(*args, capture=False):
            if not self.tmux_ok:
                return 1, ""
            if args[0] == "list-sessions":
                return 0, "".join(
                    "%s\t%d\t$%d\tmission-m%d\n" % (SOCK, LIVE_SERVER, sid, sid)
                    for sid in (4, 8))
            return 0, ""

        self.app._run_tmux = fake_tmux
        self.app._tmux_server_gone = lambda: self.server_gone
        self.app._signal_pid = lambda pid, sig: self.signals.append((pid, sig))
        # No real waiting between TERM and KILL.
        self.app._reap_settle = lambda: None

        #        pid,     ppid,  tag,                       mission,        cmd
        self.table = [
            # --- live mission: pane, claude, playwright, chrome, crashpad -----
            (3013158, 3007958, live(4), "get-started", "bash console-session-wt.sh"),
            (3013206, 3013158, live(4), "get-started", "claude --dangerously-skip-permissions"),
            (3017431, 3013206, live(4), "get-started", "chrome --headless"),
            (3017433, 1,       live(4), "get-started", "chrome_crashpad_handler"),
            (3017519, 3017431, None,             None,          "chrome --type=renderer"),
            (3049806, 1,       live(8), "trial-annual", "claude -p (detached helper)"),
            # --- untagged infrastructure: must never be touched ---------------
            (931820,  1,       None, None, "ttyd --port 4201"),
            (3007958, 1,       None, None, "tmux new-session -d -s mission-x"),
            # --- stranded: dead server's panes --------------------------------
            (190760,  1,       dead(2),  "page-age-fix",  "npm exec next dev -p 3105"),
            (190775,  190760,  dead(2),  "page-age-fix",  "node next dev -p 3105"),
            (190788,  190775,  None,              None,            "next-server (v16.2.6)"),
            (1836330, 1,       dead(81), "grab-upstream", "node vite.js"),
            # --- stranded: live server, but its session was killed ------------
            (2900001, 1,       live(3),  "old-mission",   "npm exec next dev -p 24893"),
            # --- ANOTHER tmux server entirely: never ours to touch -------------
            (2950001, 1, (OTHER, 7654, 1), None, "sleep 4000 (operator\'s own tmux)"),
        ]
        self.app._console_proc_table = lambda: list(self.table)

    # ---------------- what counts as stranded ----------------

    def test_only_processes_whose_pane_is_gone_are_stranded(self):
        got = {p.pid for p in self.app.stranded_console_procs()}
        self.assertEqual(got, {190760, 190775, 1836330, 2900001})

    def test_a_live_missions_browser_and_crashpad_are_never_stranded(self):
        """chrome + its PPID=1 crashpad inherit the LIVE tag, so they are spared —
        the SPEC's hard constraint: never touch a mission whose console is alive."""
        got = {p.pid for p in self.app.stranded_console_procs()}
        for pid in (3013158, 3013206, 3017431, 3017433, 3049806):
            self.assertNotIn(pid, got)

    def test_another_tmux_servers_processes_are_out_of_scope(self):
        """A `tmux -L <name>` server — the operator\'s own, or the throwaway the guard
        hook allows tests to use — is not the dashboard\'s business. Its tag names a
        different SOCKET, so it is neither live nor reapable."""
        got = {p.pid for p in self.app.stranded_console_procs()}
        self.assertNotIn(2950001, got)
        self.app.reap_console_procs()
        self.assertNotIn(2950001, {pid for pid, _ in self.signals})

    def test_untagged_processes_are_never_stranded(self):
        """ttyd and the tmux server have no inherited TMUX tag — out of scope entirely."""
        got = {p.pid for p in self.app.stranded_console_procs()}
        self.assertNotIn(931820, got)
        self.assertNotIn(3007958, got)

    def test_fails_closed_when_tmux_cannot_be_queried(self):
        """A wedged or busy tmux server must NOT make every live mission look stranded.
        No answer => no reaping, ever."""
        self.tmux_ok = False          # rc != 0, and NOT the "no server" message
        self.assertIsNone(self.app.live_console_tags())
        self.assertEqual(self.app.stranded_console_procs(), [])
        self.assertEqual(self.app.reap_console_procs(), [])
        self.assertEqual(self.signals, [])

    def test_a_confirmed_dead_server_is_told_apart_from_a_wedged_one(self):
        """`tmux kill-server` (the 2026-09-07 outage) really does leave nothing live —
        an empty set, not the None a wedged server gets."""
        self.tmux_ok = False
        self.server_gone = True
        self.assertEqual(self.app.live_console_tags(), set())

    def test_an_unscoped_sweep_waits_until_it_can_name_its_own_socket(self):
        """With no server at all there is no way to tell OUR console socket from the
        operator\'s own tmux, and the reaper refuses to guess: a blind sweep does
        nothing until a console reopens. The strays keep until the next tick."""
        self.tmux_ok = False
        self.server_gone = True
        self.assertEqual(self.app.console_sockets(set()), set())
        self.assertEqual(self.app.stranded_console_procs(), [])
        self.assertEqual(self.app.reap_console_procs(), [])
        self.assertEqual(self.signals, [])

    def test_killing_the_last_session_still_reaps_its_strays(self):
        """The server exits with its last session, so the ✕ path\'s scoped reap must not
        be defeated by there being no server left to ask — it carries the tag (and so the
        socket) it captured before the kill."""
        self.tmux_ok = False
        self.server_gone = True
        reaped = {p.pid for p in self.app.reap_console_procs(tags={live(4)})}
        self.assertEqual(reaped, {3013158, 3013206, 3017431, 3017433, 3017519})

    # ---------------- what it actually kills ----------------

    def test_reap_kills_stranded_trees_including_untagged_descendants(self):
        """A chrome renderer / next-server child has no readable env of its own; it is
        reached as a descendant of its stranded root."""
        reaped = {p.pid for p in self.app.reap_console_procs()}
        self.assertEqual(reaped, {190760, 190775, 190788, 1836330, 2900001})
        termed = {pid for pid, sig in self.signals if sig == signal.SIGTERM}
        self.assertEqual(termed, reaped)

    def test_reap_never_signals_a_live_mission_or_infrastructure(self):
        self.app.reap_console_procs()
        hit = {pid for pid, _ in self.signals}
        for pid in (3013158, 3013206, 3017431, 3017433, 3017519, 3049806,
                    931820, 3007958, 1):
            self.assertNotIn(pid, hit)

    def test_survivors_are_sigkilled(self):
        gone = {190775, 190788, 1836330, 2900001}   # everything but 190760 dies on TERM
        self.app._proc_starttime = lambda pid: None if pid in self.dead else 4242

        def signal_pid(pid, sig):
            self.signals.append((pid, sig))
            if sig == signal.SIGTERM and pid in gone:
                self.dead.add(pid)
        self.dead = set()
        self.app._signal_pid = signal_pid
        self.app.reap_console_procs()
        killed = {pid for pid, sig in self.signals if sig == signal.SIGKILL}
        self.assertEqual(killed, {190760})

    def test_a_recycled_pid_is_not_sigkilled(self):
        """Two seconds pass between TERM and KILL. If the pid was freed and handed to a
        new process in that window, its start time differs and the KILL is withheld —
        the innocent occupant must not be shot."""
        recycled = 1836330
        self.app._proc_starttime = (
            lambda pid, seen=set(): 999999 if (pid == recycled and pid in seen)
            else (seen.add(pid) or 4242))
        self.app.reap_console_procs()
        killed = {pid for pid, sig in self.signals if sig == signal.SIGKILL}
        self.assertNotIn(recycled, killed)
        self.assertIn(190760, killed)      # everything else still gets its KILL

    def test_reap_can_be_scoped_to_one_session(self):
        """The ✕ / idle-sweep path reaps only the session it just killed, so a
        concurrent mission is untouched even if it has strays of its own."""
        reaped = {p.pid for p in self.app.reap_console_procs(tags={dead(2)})}
        self.assertEqual(reaped, {190760, 190775, 190788})

    def test_scoped_reap_still_refuses_a_live_tag(self):
        """Belt and braces: a caller that passes a LIVE pane's tag reaps nothing."""
        self.assertEqual(self.app.reap_console_procs(tags={live(4)}), [])
        self.assertEqual(self.signals, [])

    def test_reaper_is_off_when_disabled(self):
        self.app.CONSOLE_REAP = False
        self.assertEqual(self.app.reap_console_procs(), [])
        self.assertEqual(self.signals, [])


class TagParsingTest(unittest.TestCase):
    """_proc_tmux_tag reads the real /proc format tmux writes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = load_app(self.tmp)

    def parse(self, environ):
        return self.app._tmux_tag_from_environ(environ)

    def test_reads_server_pid_and_session_id(self):
        env = b"PATH=/bin\x00TMUX=/home/op/.tmux-console/tmux-1000/default,158818,53\x00X=1\x00"
        self.assertEqual(self.parse(env), (SOCK, 158818, 53))

    def test_ignores_a_missing_or_malformed_tmux_var(self):
        self.assertIsNone(self.parse(b"PATH=/bin\x00"))
        self.assertIsNone(self.parse(b"TMUX=\x00"))
        self.assertIsNone(self.parse(b"TMUX=/sock,notapid,3\x00"))
        self.assertIsNone(self.parse(b"TMUX=/sock,158818\x00"))

    def test_does_not_match_tmux_lookalike_variables(self):
        """TMUX_TMPDIR / TMUX_PANE are set everywhere and say nothing about a pane."""
        self.assertIsNone(self.parse(b"TMUX_TMPDIR=/home/op/.tmux-console,1,2\x00"))
        self.assertIsNone(self.parse(b"TMUX_PANE=%53\x00"))


if __name__ == "__main__":
    unittest.main()
