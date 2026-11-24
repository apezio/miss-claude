"""dev-run.sh started from inside a console keeps its tmux sessions on its own server.

Inside a console $TMUX names the production server and overrides TMUX_TMPDIR, so a
throwaway dev-run once put a mission's session on the real ~/.tmux-console server.
A fake ttyd stands in for the console bridge: it runs `tmux new-session` with exactly
the environment ttyd (and so console-launch.sh) would get.
"""
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_RUN = os.path.join(REPO, "dev-run.sh")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def has_session(sock, name):
    return subprocess.run(["tmux", "-S", sock, "has-session", "-t", name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class DevRunIgnoresInheritedTmuxTest(unittest.TestCase):
    def test_session_lands_on_tmux_tmpdir_server(self):
        tmp = tempfile.mkdtemp(prefix="mc-tx-")  # short: UNIX socket paths are ~104 bytes max
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = os.path.join(tmp, "home")
        tmux_dir = os.path.join(tmp, "tc")
        bin_dir = os.path.join(home, ".local", "bin")  # dev-run puts this first on PATH
        os.makedirs(bin_dir)
        os.makedirs(tmux_dir)
        for name, body in (("claude", "exit 0\n"),
                           ("ttyd", "tmux new-session -d -s probe 'sleep 60'\nexec sleep 60\n")):
            path = os.path.join(bin_dir, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)

        # The "real" server this console would be inside: a decoy, never the production one.
        decoy = os.path.join(tmp, "decoy.sock")
        subprocess.run(["tmux", "-S", decoy, "new-session", "-d", "-s", "decoy", "sleep 60"],
                       check=True)
        self.addCleanup(subprocess.run, ["tmux", "-S", decoy, "kill-server"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ours = os.path.join(tmux_dir, "tmux-%d" % os.getuid(), "default")
        self.addCleanup(subprocess.run, ["tmux", "-S", ours, "kill-server"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        env = dict(os.environ, HOME=home, TMUX_TMPDIR=tmux_dir, TMUX=decoy + ",1,0",
                   MISSIONS_DIR=os.path.join(tmp, "missions"),
                   MISSION_ROOM_DIR=os.path.join(tmp, "room"), MISSION_TLS="0",
                   MISSION_PORT=str(free_port()), CONSOLE_TTYD_PORT=str(free_port()))
        for k in ("MISSION_LOGIN", "MISSION_TOKEN", "MISSION_CONSOLE_RELAY"):
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
        while not has_session(ours, "probe") and time.time() < deadline:
            if has_session(decoy, "probe"):
                break
            time.sleep(0.1)
        self.assertFalse(has_session(decoy, "probe"), "session landed on the inherited $TMUX server")
        self.assertTrue(has_session(ours, "probe"), "no session on the TMUX_TMPDIR server")


if __name__ == "__main__":
    unittest.main()
