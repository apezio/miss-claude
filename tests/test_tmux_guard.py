#!/usr/bin/env python3
"""`tmux kill-server` ends EVERY mission console at once — the guard refuses it.

Regression cover for the 2026-09-07 console-drop outage: an e2e test ran
`export TMUX_TMPDIR=<scratch>; ... ; tmux kill-server` twice in six minutes and took
every running mission down with it, because TMUX_TMPDIR does NOT isolate a tmux run
from inside a pane ($TMUX names the socket and wins). `-S`/`-L` DO win, so those forms
stay allowed — an isolated throwaway server is still available to tests.

stdlib only:  python3 -m unittest tests/test_tmux_guard.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, ".claude", "hooks", "prevent-misswork.py")
TMP = None
REPO = None      # main checkout, on master (its staging is `working`)
WT = None        # feature worktree on claude/x
INT = None       # integration checkout on working


def git(cwd, *a):
    subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


def setUpModule():
    global TMP, REPO, WT, INT
    TMP = tempfile.mkdtemp(prefix="guardtmux-")
    REPO = os.path.join(TMP, "repo")
    os.makedirs(REPO)
    git(REPO, "init", "-q", "-b", "master")
    open(os.path.join(REPO, "f"), "w").close()
    git(REPO, "add", "f")
    git(REPO, "commit", "-qm", "init")
    git(REPO, "branch", "working")
    WT = os.path.join(TMP, "wt", "x")
    git(REPO, "worktree", "add", "-q", WT, "-b", "claude/x", "working")
    INT = os.path.join(TMP, "wt", ".integration")
    git(REPO, "worktree", "add", "-q", INT, "working")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def run_hook(command, role, cwd):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MISS_") and k not in ("PRIMARY_REPO", "BASE_BRANCH")}
    env.update({"CLAUDE_MISS_ROLE": role, "PRIMARY_REPO": REPO,
                "WORKTREES_DIR": os.path.join(TMP, "wt"), "BASE_BRANCH": "working"})
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True,
                       text=True, env=env)
    return r.returncode, r.stderr


class Base(unittest.TestCase):
    role = "feature"

    def cwd(self):
        return WT

    def allowed(self, *commands):
        for c in commands:
            rc, err = run_hook(c, self.role, self.cwd())
            self.assertEqual(rc, 0, "expected ALLOWED (%s): %r\n%s" % (self.role, c, err))

    def blocked(self, *commands):
        for c in commands:
            rc, err = run_hook(c, self.role, self.cwd())
            self.assertEqual(rc, 2, "expected BLOCKED (%s): %r" % (self.role, c))


class KillServerBlocked(Base):
    def test_bare(self):
        self.blocked("tmux kill-server", "tmux kill-server 2>/dev/null")

    def test_the_actual_outage_command(self):
        # Exactly the shape that took the box down, twice, on 2026-09-07.
        self.blocked(
            'export TMUX_TMPDIR="$SCRATCH/e2e/tmux"\n'
            'tmux new-session -d -s mission-kid "cat > $SCRATCH/received.txt"\n'
            'tmux kill-server 2>/dev/null\n'
            'echo done'
        )

    def test_tmpdir_does_not_excuse_it(self):
        # The whole bug: TMUX_TMPDIR looks like isolation and is not.
        self.blocked(
            "TMUX_TMPDIR=/tmp/iso tmux kill-server",
            "env TMUX_TMPDIR=/tmp/iso tmux kill-server",
            "export TMUX_TMPDIR=/tmp/iso; tmux kill-server",
        )

    def test_hidden_behind_wrappers_and_chains(self):
        self.blocked(
            "sudo tmux kill-server",
            "bash -c 'tmux kill-server'",
            "timeout 5 tmux kill-server",
            "cd /tmp && tmux kill-server",
            "true; tmux kill-server; echo ok",
            "$(echo x) ; tmux kill-server",
        )

    def test_every_role(self):
        for role in ("feature", "integrator", ""):
            rc, _ = run_hook("tmux kill-server", role, self.cwd())
            self.assertEqual(rc, 2, "role %r must not kill the server" % role)

    def test_on_the_integration_checkout_too(self):
        rc, _ = run_hook("tmux kill-server", "integrator", INT)
        self.assertEqual(rc, 2)

    def test_on_a_protected_branch_too(self):
        rc, _ = run_hook("tmux kill-server", "feature", REPO)   # REPO is on master
        self.assertEqual(rc, 2)


class ExplicitSocketAllowed(Base):
    def test_dash_L_and_dash_S(self):
        # -S/-L override $TMUX, so these really are a throwaway server.
        self.allowed(
            "tmux -L misstest kill-server",
            "tmux -Lmisstest kill-server",
            "tmux -S /tmp/iso/sock kill-server",
            "tmux -S/tmp/iso/sock kill-server",
            "tmux -L misstest kill-server 2>/dev/null",
        )

    def test_full_isolated_e2e_shape(self):
        self.allowed(
            'tmux -L misstest new-session -d -s kid "cat > /tmp/got"\n'
            "tmux -L misstest kill-server"
        )


class UnrelatedTmuxAllowed(Base):
    def test_read_only_and_session_scoped(self):
        self.allowed(
            "tmux ls",
            "tmux list-sessions",
            "tmux has-session -t =mission-x",
            "tmux capture-pane -p -t %3",
            "tmux new-session -d -s mission-x 'sleep 1'",
            "tmux send-keys -t %3 Escape",
        )

    def test_mentions_are_data_not_execution(self):
        # The guard judges what runs, not the words. These must not be blocked.
        self.allowed(
            'grep -rn "kill-server" docs/',
            "cat docs/WORKFLOW_ROLES.txt",
            'echo "never run tmux kill-server"',
            'git commit -m "docs: warn against tmux kill-server"',
            "python3 - <<'EOF'\nprint('tmux kill-server')\nEOF",
        )


if __name__ == "__main__":
    unittest.main()
