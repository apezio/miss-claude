"""The console's ttyd socket and its folder are owner-only.

On a Mac every account is in 'staff', so a group-readable socket (or a folder others
can enter) gives another local account a shell as this user. dev-run.sh — which the
Mac LaunchAgent runs — must leave ~/.tmux-console at 700 and the socket at 600;
setup.sh's Linux unit does the same for its login install.
"""
import os
import pwd
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_RUN = os.path.join(REPO, "dev-run.sh")
SETUP = os.path.join(REPO, "setup.sh")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@unittest.skipUnless(shutil.which("ttyd") and shutil.which("tmux"), "needs ttyd + tmux")
class DevRunRelaySocketTest(unittest.TestCase):
    def test_folder_is_700_and_socket_owner_only(self):
        tmp = tempfile.mkdtemp(prefix="miss-sockmode-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = os.path.join(tmp, "home")
        tmux_dir = os.path.join(home, ".tmux-console")
        os.makedirs(tmux_dir)
        os.chmod(tmux_dir, 0o755)  # what the Mac had: the re-run must tighten it
        bin_dir = os.path.join(home, ".local", "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "claude"), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(bin_dir, "claude"), 0o755)
        sock = os.path.join(tmux_dir, "ttyd.sock")
        env = dict(os.environ, HOME=home, TMUX_TMPDIR=tmux_dir,
                   MISSIONS_DIR=os.path.join(tmp, "missions"),
                   MISSION_ROOM_DIR=os.path.join(tmp, "room"),
                   MISSION_PORT=str(free_port()), CONSOLE_TTYD_PORT=str(free_port()),
                   MISSION_CONSOLE_RELAY=sock)
        for k in ("TMUX", "MISSION_LOGIN", "MISSION_TOKEN", "MISSION_TLS"):
            env.pop(k, None)
        p = subprocess.Popen(["bash", DEV_RUN], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)

        def stop():
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            p.wait(timeout=10)
        self.addCleanup(stop)

        deadline = time.time() + 15
        while not os.path.exists(sock) and time.time() < deadline and p.poll() is None:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(sock), "ttyd never created its socket")
        # dev-run.sh chmods it after its 1 s startup check; the 700 folder covers the gap
        while mode(sock) & 0o077 and time.time() < deadline and p.poll() is None:
            time.sleep(0.1)
        self.assertTrue(stat.S_ISSOCK(os.stat(sock).st_mode))
        self.assertEqual(mode(tmux_dir), 0o700)
        self.assertEqual(mode(sock) & 0o077, 0, oct(mode(sock)))


@unittest.skipIf(sys.platform == "darwin", "setup.sh is Linux-only")
class SetupShConsoleModesTest(unittest.TestCase):
    """setup.sh (Linux): the folder is installed 700; with --login the unit chmods the socket."""

    def render(self, *flags):
        user = pwd.getpwuid(os.getuid()).pw_name
        r = subprocess.run(["bash", SETUP, "--dry-run", "--user", user, *flags],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=60)
        return r.stdout

    def test_login_install(self):
        out = self.render("--login")
        self.assertRegex(out, r"install -d -o \S+ -g \S+ -m 0700 \S+/\.tmux-console\n")
        self.assertRegex(out, r"ExecStartPost=-/bin/sh -c .*chmod 600 \S+/\.tmux-console/ttyd\.sock")

    def test_port_install_still_gets_the_700_folder(self):
        out = self.render()
        self.assertIn("-m 0700", out)
        self.assertNotIn("ExecStartPost=-/bin/sh", out)


if __name__ == "__main__":
    unittest.main()
