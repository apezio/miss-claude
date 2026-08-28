#!/usr/bin/env python3
"""Tests for the consoles' ONE-invocation resume flow (local + remote command strings).

    python3 -m unittest tests/test_console_launch.py -v      (from the repo root)

Standard library only, and it never talks to a real host: `ssh`/`scp`/`tmux`/`claude`
are stubs on PATH in a fake $HOME, and the stub `ssh` simply runs the command string
LOCALLY — so the remote one-liners console-launch.sh builds are actually executed the
way the far side's /bin/sh would run them, quoting (printf %q) and all.

Proves what the python-only helper test cannot: each console launches `claude` EXACTLY
once, with the right session flag for the transcripts on disk; that the remote dev
command keeps its `&&` chain, so a failed `cd` never reaches an unguarded
`--dangerously-skip-permissions`; that console-session.sh's
last resort still uses the mission's pinned conversation; and that scripts/claude-miss
does the same, through BOTH of its call sites (inside `script -c` and without it).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAUNCH = os.path.join(ROOT, "console-launch.sh")
SESSION = os.path.join(ROOT, "console-session.sh")
PINNED = "c52fc4e4-48c8-51e5-afe2-7251ebb46c58"

STUB_CLAUDE = "#!/bin/sh\necho \"CLAUDE-ARGS: $*\"\n"
# Like the real thing: `--continue` FAILS when the cwd's project dir has no transcript
# (that failure is what the old `--continue || claude` chain fell back from, at the cost
# of a second folder-trust dialog) — so a chain would show up here as two invocations.
STUB_CLAUDE_REAL = (
    "#!/bin/sh\necho \"CLAUDE-ARGS: $*\"\n"
    "case \" $* \" in *\" --continue \"*)\n"
    "  P=\"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/"
    "$(printf %s \"$(pwd -P)\" | tr -c 'A-Za-z0-9' '-')\"\n"
    "  for f in \"$P\"/*.jsonl; do [ -f \"$f\" ] && exit 0; done\n"
    "  echo 'No conversation found to continue'; exit 1 ;;\nesac\nexit 0\n")
STUB_CLAUDE_FAIL = "#!/bin/sh\necho \"CLAUDE-ARGS: $*\"\nexit 1\n"
# `ssh [-tt] <host> <cmd...>`: run it here instead. One arg = a command STRING (sh -c),
# several = a plain argv (that is how ship-rails.sh calls `ssh host python3 -`).
STUB_SSH = '#!/bin/bash\n[ "$1" = "-tt" ] && shift\nshift\nif [ $# -eq 1 ]; then exec sh -c "$1"; else exec "$@"; fi\n'
# `scp -q <files...> <host>:<dir>` -> copy into $HOME/<dir>.
STUB_SCP = ('#!/bin/bash\nargs=(); for a in "$@"; do [ "$a" = "-q" ] || args+=("$a"); done\n'
            'dest="${args[-1]#*:}"; unset "args[${#args[@]}-1]"\n'
            'mkdir -p "$HOME/$dest" && cp "${args[@]}" "$HOME/$dest"\n')
# tmux: record the session command, run it (that is what the pane does), never block.
STUB_TMUX = ('#!/bin/bash\ncase "$1" in\n'
             '  has-session) [ -f "$CAPTURE.started" ] && exit 0 || exit 1 ;;\n'
             '  attach-session) exit 0 ;;\n'
             '  ls) exit 1 ;;\n'
             '  new-session) for a in "$@"; do last="$a"; done\n'
             '    touch "$CAPTURE.started"; printf \'%s\\n\' "$last" >> "$CAPTURE"\n'
             '    sh -c "$last" </dev/null >> "$CAPTURE.out" 2>&1; exit 0 ;;\n'
             'esac\nexit 0\n')


# Resolve against the PATH the tests THEMSELVES run with, not the ambient one: a tool
# that lives only in /usr/local/bin would pass an ambient check and then fail inside.
TEST_PATH = "/usr/bin:/bin"


def have(*tools):
    return all(shutil.which(t, path=TEST_PATH) for t in tools)


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(path))


@unittest.skipUnless(have("bash", "sh", "uuidgen", "python3", "tr"),
                     "needs bash/sh/uuidgen/python3/tr")
class ConsoleLaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="miss-console-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.bin, os.path.join(self.home, ".local", "bin"),
                  os.path.join(self.home, "missions"), os.path.join(self.home, "work")):
            os.makedirs(d)
        self.write_stub(os.path.join(self.home, ".local", "bin", "claude"), STUB_CLAUDE)
        for name, src in (("ssh", STUB_SSH), ("scp", STUB_SCP), ("tmux", STUB_TMUX)):
            self.write_stub(os.path.join(self.bin, name), src)
        self.capture = os.path.join(self.tmp, "capture")

    def write_stub(self, path, src):
        with open(path, "w") as fh:
            fh.write(src)
        os.chmod(path, 0o755)

    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "PATH": self.bin + ":" + TEST_PATH,
            "MISSIONS_DIR": os.path.join(self.home, "missions"),
            "WORKTREES_DIR": os.path.join(self.home, "worktrees"),
            "TMUX_TMPDIR": os.path.join(self.tmp, "tmux"),
            "CAPTURE": self.capture,
        })
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.update(extra)
        return env

    # -- helpers ----------------------------------------------------------
    def transcript(self, cwd, sid):
        d = os.path.join(self.home, ".claude", "projects", slug(cwd))
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, sid + ".jsonl"), "w").close()

    def uuid5(self, name):
        return subprocess.run(["uuidgen", "--sha1", "--namespace", "@url", "--name", name],
                              stdout=subprocess.PIPE, text=True).stdout.strip()

    def launch(self, *args, **kw):
        """Run console-launch.sh; return the stub claude's argv lines."""
        for suffix in ("", ".started", ".out"):
            if os.path.exists(self.capture + suffix):
                os.unlink(self.capture + suffix)
        r = subprocess.run(["bash", LAUNCH, *args], env=self.env(**kw.get("env", {})),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=60)
        out = r.stdout
        if os.path.exists(self.capture + ".out"):
            with open(self.capture + ".out") as fh:
                out += fh.read()
        return [l for l in out.splitlines() if l.startswith("CLAUDE-ARGS:")], out

    def rerun_captured(self):
        """Re-run the LAST command tmux was handed, without going through the launcher.

        ship-rails.sh re-ships the guard bundle on every remote-dev launch, so this is
        the only way to see what the pane does when the shipped bundle misbehaves at
        RUNTIME (the stub ssh runs the command string here, like the far side's sh).
        """
        with open(self.capture) as fh:
            cmd = fh.read().splitlines()[-1]
        r = subprocess.run(["sh", "-c", cmd], env=self.env(), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=60)
        return [l for l in r.stdout.splitlines() if l.startswith("CLAUDE-ARGS:")], r.stdout

    def mission(self, name, meta):
        d = os.path.join(self.home, "missions", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "mission.json"), "w") as fh:
            json.dump(meta, fh)
        return d

    # -- 1) ad-hoc NAMED remote console -----------------------------------
    def test_remote_named_console_creates_then_resumes(self):
        work = os.path.join(self.home, "work")
        sid = self.uuid5("www|%s|my console" % work)
        calls, out = self.launch("remote", "www", work, "my console")
        self.assertEqual(calls, ["CLAUDE-ARGS: --session-id %s --dangerously-skip-permissions" % sid], out)
        self.transcript(work, sid)
        calls, out = self.launch("remote", "www", work, "my console")
        self.assertEqual(calls, ["CLAUDE-ARGS: --resume %s --dangerously-skip-permissions" % sid], out)

    # -- 2) ops mission whose console runs on a remote --------------------
    def test_remote_ops_mission_creates_then_resumes(self):
        work = os.path.join(self.home, "work")
        self.mission("opsm", {"mode": "ops",
                              "target": {"kind": "remote", "host": "db", "remote_dir": work}})
        sid = self.uuid5("opsm")
        calls, out = self.launch("opsm")
        self.assertEqual(calls, ["CLAUDE-ARGS: --session-id %s --dangerously-skip-permissions" % sid], out)
        self.transcript(work, sid)
        calls, out = self.launch("opsm")
        self.assertEqual(calls, ["CLAUDE-ARGS: --resume %s --dangerously-skip-permissions" % sid], out)

    # -- 3) remote DEV mission --------------------------------------------
    def dev_mission(self, name, worktree):
        repo = os.path.join(self.home, "repo")
        os.makedirs(repo, exist_ok=True)
        self.mission(name, {"mode": "dev",
                            "target": {"kind": "remote-repo", "host": "devbox", "remote_dir": repo},
                            "dev": {"role": "feature", "repo": repo, "repo_id": "repo-abcd1234",
                                    "worktree": worktree, "branch": "claude/" + name,
                                    "base_branch": "working", "host": "devbox",
                                    "preview_port": 4310}})

    def test_remote_dev_continue_only_with_history(self):
        wt = os.path.join(self.home, "wt")
        os.makedirs(wt, exist_ok=True)
        self.dev_mission("devm", wt)
        calls, out = self.launch("devm")
        self.assertEqual(len(calls), 1, out)
        self.assertNotIn("--continue", calls[0])
        self.assertIn("--settings %s/.miss-claude/miss-rails.settings.json" % self.home, calls[0])
        self.assertIn("--dangerously-skip-permissions", calls[0])
        # ...and once the remote worktree has any transcript, exactly one --continue.
        self.transcript(wt, "0f0e0d0c-0b0a-4009-8008-070605040302")
        calls, out = self.launch("devm")
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--continue", calls[0])

    def test_remote_dev_does_not_launch_when_cd_fails(self):
        """The && chain is load-bearing: no worktree => no claude at all.

        A `;` before claude would start it in the remote $HOME with an empty
        --settings, i.e. --dangerously-skip-permissions with no guard hook.
        """
        gone = os.path.join(self.home, "vanished-worktree")
        self.dev_mission("devgone", gone)
        calls, out = self.launch("devgone")
        self.assertEqual(calls, [], "claude must not launch after a failed cd:\n" + out)


@unittest.skipUnless(have("bash", "python3"), "needs bash/python3")
class ConsoleSessionTest(unittest.TestCase):
    """console-session.sh: the local ops console's pre-resolved two-step."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="miss-session-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.tmp, "mission")
        self.cwd = os.path.join(self.tmp, "shared-dir")
        for d in (os.path.join(self.home, ".local", "bin"), self.data, self.cwd):
            os.makedirs(d)
        self.claude = os.path.join(self.home, ".local", "bin", "claude")
        self.stub(STUB_CLAUDE)

    def stub(self, src):
        with open(self.claude, "w") as fh:
            fh.write(src)
        os.chmod(self.claude, 0o755)

    def transcript(self, sid):
        d = os.path.join(self.home, ".claude", "projects", slug(self.cwd))
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, sid + ".jsonl"), "w").close()

    def run_session(self, sid=None, extra_path="", live=None):
        if live is not None:
            with open(os.path.join(self.data, ".console-session"), "w") as fh:
                json.dump({"session_id": live}, fh)
        env = dict(os.environ)
        env.update({"HOME": self.home, "MISSION_DATA_DIR": self.data, "MISSION_NAME": "m",
                    "PATH": (extra_path + ":" if extra_path else "") + TEST_PATH})
        env.pop("CLAUDE_CONFIG_DIR", None)
        if sid:
            env["MISSION_SESSION_ID"] = sid
        else:
            env.pop("MISSION_SESSION_ID", None)
        r = subprocess.run(["bash", SESSION], cwd=self.cwd, env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=60)
        return [l for l in r.stdout.splitlines() if l.startswith("CLAUDE-ARGS:")], r.stdout

    def test_pinned_id_created_then_resumed(self):
        calls, out = self.run_session(PINNED)
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--session-id " + PINNED, calls[0])
        self.transcript(PINNED)
        calls, out = self.run_session(PINNED)
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--resume " + PINNED, calls[0])

    def test_live_id_wins_but_falls_back_to_the_pinned_one(self):
        live = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
        self.transcript(live)
        calls, out = self.run_session(PINNED, live=live)
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--resume " + live, calls[0])
        # A live id whose transcript is gone (rotated) => the pinned one, resumed if it exists.
        self.transcript(PINNED)
        calls, out = self.run_session(PINNED, live="deadbeef-dead-4eef-8eef-deadbeefdead")
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--resume " + PINNED, calls[0])

    def test_unpinned_mission_continues_only_with_history(self):
        calls, out = self.run_session()
        self.assertEqual(len(calls), 1, out)
        self.assertNotIn("--continue", calls[0])
        self.assertNotIn("--resume", calls[0])
        self.transcript(PINNED)
        calls, out = self.run_session()
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--continue", calls[0])

    def test_last_resort_still_creates_the_pinned_conversation(self):
        self.stub(STUB_CLAUDE_FAIL)      # claude refuses for some other reason
        calls, out = self.run_session(PINNED)
        self.assertEqual(len(calls), 2, out)
        self.assertIn("--session-id " + PINNED, calls[1])

    def broken_python3(self):
        """A PATH entry whose python3 always fails (helper unavailable)."""
        shim = os.path.join(self.tmp, "shim")
        os.makedirs(shim, exist_ok=True)
        p = os.path.join(shim, "python3")
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\nexit 127\n")
        os.chmod(p, 0o755)
        return shim

    def test_floor_when_the_helper_is_unavailable(self):
        """No python3 => the pure-shell floor still pins the mission's own uuid."""
        calls, out = self.run_session(PINNED, extra_path=self.broken_python3())
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--session-id " + PINNED, calls[0])

    def test_floor_resumes_an_existing_conversation(self):
        """...and it RESUMES when the transcript is there — creating it would error out."""
        self.transcript(PINNED)
        calls, out = self.run_session(PINNED, extra_path=self.broken_python3())
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--resume " + PINNED, calls[0])

    def test_last_resort_resumes_rather_than_recreates(self):
        self.transcript(PINNED)
        self.stub(STUB_CLAUDE_FAIL)
        calls, out = self.run_session(PINNED)
        self.assertEqual(len(calls), 2, out)
        self.assertIn("--resume " + PINNED, calls[1])

# Tools scripts/claude-miss needs; symlinked into a private bin dir so a run can be
# given a PATH that deliberately does NOT contain script(1) (there is no other way to
# reach the `command -v script` else-branch, since script lives in /usr/bin).
MISS_TOOLS = ["bash", "sh", "git", "python3", "tr", "sed", "awk", "date", "mkdir",
              "dirname", "readlink", "id", "sleep", "pgrep", "cat", "printf", "ls",
              "basename"]


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "user.email=t@example", "-c", "user.name=t",
                    *args], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True)


@unittest.skipUnless(have("bash", "git", "python3", "tr", "script"),
                     "needs bash/git/python3/tr/script")
class ClaudeMissTest(unittest.TestCase):
    """scripts/claude-miss: one claude, --continue only when the worktree has history.

    Both call sites are exercised: the `script -q -c "<command string>"` form (run
    through the REAL script(1), so what it executes is what is asserted) and the
    plain one used when script(1) is absent.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="miss-claudemiss-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        self.repo = os.path.join(self.home, "repo")
        self.wt = os.path.join(self.home, "worktrees", "feat")
        os.makedirs(self.bin)
        os.makedirs(self.repo)
        # a private PATH: the tools claude-miss uses, and NO script(1)
        for tool in MISS_TOOLS:
            src = shutil.which(tool, path=TEST_PATH)
            if src:
                os.symlink(src, os.path.join(self.bin, tool))
        with open(os.path.join(self.bin, "claude"), "w") as fh:
            fh.write(STUB_CLAUDE_REAL)
        os.chmod(os.path.join(self.bin, "claude"), 0o755)
        git(self.repo, "init", "-q", "-b", "working")
        open(os.path.join(self.repo, "f.txt"), "w").close()
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-qm", "init")
        git(self.repo, "worktree", "add", "-q", self.wt, "-b", "claude/feat", "working")

    def env(self, with_script, **extra):
        env = dict(os.environ)
        path = self.bin + (":" + TEST_PATH if with_script else "")
        env.update({"HOME": self.home, "PATH": path, "PRIMARY_REPO": self.repo,
                    "WORKTREES_DIR": os.path.join(self.home, "worktrees"),
                    "BASE_BRANCH": "working", "MISS_WORKTREE": self.wt,
                    "CLAUDE_LOGS": os.path.join(self.tmp, "logs")})
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CLAUDE_MISS_DRYRUN", None)
        env.pop("CLAUDE_MISS_SETTINGS", None)
        env.update(extra)
        return env

    def transcript(self):
        d = os.path.join(self.home, ".claude", "projects", slug(self.wt))
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "11112222-3333-4444-5555-666677778888.jsonl"), "w").close()

    def run_miss(self, with_script, **extra):
        bash = shutil.which("bash", path=TEST_PATH)
        r = subprocess.run([bash, os.path.join(ROOT, "scripts", "claude-miss")],
                           cwd=self.wt, env=self.env(with_script, **extra),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=120)
        out = r.stdout.replace("\r", "")
        return [l for l in out.splitlines() if l.startswith("CLAUDE-ARGS:")], out, r.returncode

    def check_one_launch(self, with_script):
        calls, out, _ = self.run_miss(with_script)
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--settings ", calls[0])
        self.assertIn("--dangerously-skip-permissions", calls[0])
        self.assertNotIn("--continue", calls[0])
        self.transcript()
        calls, out, _ = self.run_miss(with_script)
        self.assertEqual(len(calls), 1, out)
        self.assertIn("--continue", calls[0])
        self.assertIn("--settings ", calls[0])

    def test_script_call_site(self):
        self.check_one_launch(with_script=True)

    def test_plain_call_site_when_script_is_absent(self):
        self.assertIsNone(shutil.which("script", path=self.bin),
                          "the private PATH must not contain script(1)")
        self.check_one_launch(with_script=False)

    def test_fail_closed_when_the_guard_is_missing(self):
        """A broken guard must stop the launch — before, not after, claude starts."""
        for with_script in (True, False):
            calls, out, rc = self.run_miss(
                with_script, MISSWORK_HOOK=os.path.join(self.tmp, "no-such-hook.py"))
            self.assertEqual(calls, [], out)
            self.assertEqual(rc, 1, out)
            self.assertIn("guard rails are missing or broken", out)



if __name__ == "__main__":
    unittest.main()
