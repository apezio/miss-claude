#!/usr/bin/env python3
"""The /upstream-pr carve-out in prevent-misswork.py: two commands, everything verified.

    python3 -m unittest tests/test_upstream_pr_guard.py

The skill ports ONE fork feature onto a branch based on upstream/main and opens a PR
against upstream. To finish unattended it needs exactly two things a feature worker is
otherwise refused — `git push origin refs/heads/pr/<x>:refs/heads/pr/<x>` and a
`git checkout` back to the branch it started on — so the hook grants those two and
nothing else. The state file under ~/.cache/upstream-pr only says WHICH branches are in
play; every condition that matters is re-derived from git at hook time.

These tests exist because a carve-out that opens a push path must fail closed on every
gate. Builds a throwaway repo (staging `working`, a fork branch, a pr/ branch cut
from the upstream/main ref, an origin distinct from upstream) and runs the real hook as
a subprocess. stdlib only.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, ".claude", "hooks", "prevent-misswork.py")
STATE_DIR = os.path.expanduser("~/.cache/upstream-pr")

TMP = REPO = WT = None
BASE_SHA = FORK_SHA = None
ORIG = "claude/feat"
PR = "pr/feat"


def git(cwd, *a):
    r = subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
    if r.returncode:
        raise AssertionError("git %s failed in %s:\n%s%s" % (" ".join(a), cwd, r.stdout, r.stderr))
    return r.stdout.strip()


def setUpModule():
    """A repo shaped like a real port: `working` staging, a fork branch carrying the
    feature, and pr/feat branched from the recorded upstream/main with the port on top."""
    global TMP, REPO, WT, BASE_SHA, FORK_SHA
    TMP = os.path.realpath(tempfile.mkdtemp(prefix="upstreampr-"))
    REPO = os.path.join(TMP, "repo")
    os.makedirs(REPO)
    git(REPO, "init", "-q", "-b", "working")
    open(os.path.join(REPO, "f"), "w").close()
    git(REPO, "add", "f")
    git(REPO, "commit", "-qm", "init")
    BASE_SHA = git(REPO, "rev-parse", "HEAD")
    # what the fork has and upstream does not
    git(REPO, "update-ref", "refs/remotes/upstream/main", BASE_SHA)
    git(REPO, "remote", "add", "origin", os.path.join(TMP, "origin.git"))
    git(REPO, "remote", "add", "upstream", os.path.join(TMP, "upstream.git"))
    WT = os.path.join(TMP, "wt", "port")
    git(REPO, "worktree", "add", "-q", WT, "-b", ORIG, "working")
    with open(os.path.join(WT, "fork-only.txt"), "w") as fh:
        fh.write("fork\n")
    git(WT, "add", "fork-only.txt")
    git(WT, "commit", "-qm", "fork feature")
    FORK_SHA = git(WT, "rev-parse", "HEAD")
    # the PR branch: off upstream/main, carrying the port but NOT the fork branch
    git(WT, "checkout", "-q", "-b", PR, BASE_SHA)
    with open(os.path.join(WT, "ported.txt"), "w") as fh:
        fh.write("ported\n")
    git(WT, "add", "ported.txt")
    git(WT, "commit", "-qm", "port the feature")
    # a branch that WOULD smuggle the fork branch upstream
    git(WT, "branch", "pr/bad", ORIG)


def tearDownModule():
    for name in os.listdir(STATE_DIR) if os.path.isdir(STATE_DIR) else []:
        if name.startswith("_" + TMP.strip("/").replace("/", "_")):
            os.remove(os.path.join(STATE_DIR, name))
    shutil.rmtree(TMP, ignore_errors=True)


def state_path(worktree=None):
    return os.path.join(STATE_DIR, (worktree or WT).replace("/", "_") + ".env")


def write_state(orig=None, sha=None, pr=None, mode=0o600, age=0):
    """The workflow's own state file. It grants nothing by itself — that is the point."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    path = state_path()
    with open(path, "w") as fh:
        fh.write("ORIG_BRANCH=%s\nORIG_SHA=%s\nPR_BRANCH=%s\n"
                 % (ORIG if orig is None else orig, FORK_SHA if sha is None else sha,
                    PR if pr is None else pr))
    os.chmod(path, mode)
    if age:
        os.utime(path, (time.time() - age, time.time() - age))
    return path


def clear_state():
    try:
        os.remove(state_path())
    except OSError:
        pass


def run_hook(command, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MISS_") and k not in ("PRIMARY_REPO", "BASE_BRANCH")}
    env.update({"CLAUDE_MISS_ROLE": "feature", "PRIMARY_REPO": REPO,
                "WORKTREES_DIR": os.path.join(TMP, "wt"), "BASE_BRANCH": "working"})
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd or WT}
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True,
                       text=True, env=env)
    return r.returncode, r.stderr


PUSH = "git push origin refs/heads/%s:refs/heads/%s" % (PR, PR)


class Base(unittest.TestCase):
    def setUp(self):
        write_state()
        self.addCleanup(clear_state)
        # every test starts on the PR branch with a clean tree
        if git(WT, "rev-parse", "--abbrev-ref", "HEAD") != PR:
            git(WT, "checkout", "-q", PR)

    def allowed(self, *commands):
        for c in commands:
            rc, err = run_hook(c)
            self.assertEqual(rc, 0, "expected ALLOWED: %r\n%s" % (c, err))

    def blocked(self, *commands):
        for c in commands:
            rc, _ = run_hook(c)
            self.assertEqual(rc, 2, "expected BLOCKED: %r" % (c,))


class TheTwoSanctionedCommands(Base):
    def test_the_pr_push_is_allowed(self):
        self.allowed(PUSH)

    def test_returning_to_the_recorded_branch_is_allowed(self):
        self.allowed("git checkout %s" % ORIG)

    def test_they_are_still_blocked_for_a_worker_with_no_workflow_running(self):
        clear_state()
        self.blocked(PUSH, "git checkout %s" % ORIG)


class TheStateFileGrantsNothing(Base):
    """It says which branches are in play. Every gate is re-derived from git."""

    def test_a_stale_run_fails_closed(self):
        write_state(age=25 * 3600)
        self.blocked(PUSH)

    def test_a_group_or_world_writable_state_file_is_ignored(self):
        write_state(mode=0o666)
        self.blocked(PUSH)
        write_state(mode=0o620)
        self.blocked(PUSH)

    def test_a_state_file_naming_staging_or_a_protected_branch_buys_nothing(self):
        for pr in ("working", "main", "master"):
            write_state(pr=pr)
            self.blocked("git push origin refs/heads/%s:refs/heads/%s" % (pr, pr))
        for orig in ("working", "main", "master"):
            write_state(orig=orig)
            self.blocked("git checkout %s" % orig)

    def test_a_pr_branch_outside_the_pr_namespace_buys_nothing(self):
        write_state(pr=ORIG)
        self.blocked("git push origin refs/heads/%s:refs/heads/%s" % (ORIG, ORIG))

    def test_it_must_match_the_branch_actually_checked_out(self):
        git(WT, "checkout", "-q", ORIG)
        self.addCleanup(git, WT, "checkout", "-q", PR)
        self.blocked(PUSH)

    def test_a_moved_dev_branch_stops_it(self):
        write_state(sha=BASE_SHA)          # ORIG has moved on since it was recorded
        self.blocked(PUSH)

    def test_a_pr_branch_carrying_the_fork_branch_is_refused(self):
        git(WT, "checkout", "-q", "pr/bad")
        self.addCleanup(git, WT, "checkout", "-q", PR)
        write_state(pr="pr/bad")
        self.blocked("git push origin refs/heads/pr/bad:refs/heads/pr/bad")


class NothingWiderThanThoseTwo(Base):
    def test_only_the_exact_refspec(self):
        self.blocked(
            "git push origin %s" % PR,                       # short form
            "git push origin %s:%s" % (PR, PR),
            "git push origin refs/heads/%s:refs/heads/working" % PR,
            "git push origin refs/heads/working:refs/heads/%s" % PR,
            "git push origin refs/heads/%s:refs/heads/%s --set-upstream" % (PR, PR),
        )

    def test_no_force_no_delete_no_mirror(self):
        for flag in ("--force", "-f", "--force-with-lease", "--delete", "--mirror",
                     "--all", "--tags", "--no-verify"):
            self.blocked("git push %s origin refs/heads/%s:refs/heads/%s" % (flag, PR, PR))

    def test_not_to_upstream_and_not_under_sudo(self):
        self.blocked("git push upstream refs/heads/%s:refs/heads/%s" % (PR, PR),
                     "sudo %s" % PUSH,
                     "sudo -u alice %s" % PUSH)

    def test_nothing_may_ride_along(self):
        self.blocked("%s && git push origin working" % PUSH,
                     "%s; git merge --ff-only %s" % (PUSH, ORIG),
                     "%s | tee /dev/null && git reset --hard" % PUSH,
                     "%s > /tmp/out" % PUSH,
                     "bash -c '%s && git push origin main'" % PUSH)

    def test_the_checkout_is_only_the_recorded_branch_and_only_when_clean(self):
        self.blocked("git checkout working", "git checkout .")
        dirty = os.path.join(WT, "ported.txt")
        with open(dirty, "a") as fh:
            fh.write("uncommitted\n")
        self.addCleanup(git, WT, "checkout", "-q", "--", "ported.txt")
        self.blocked("git checkout %s" % ORIG)

    def test_other_repositories_are_still_out_of_reach(self):
        self.blocked("git -C %s push origin refs/heads/%s:refs/heads/%s" % (REPO, PR, PR),
                     "cd %s && %s" % (REPO, PUSH))

    def test_everything_else_a_feature_worker_may_not_do_is_unchanged(self):
        self.blocked("git push origin working", "git merge --ff-only %s" % ORIG,
                     "git push --force origin %s" % PR, "git worktree add ../x",
                     "git branch -D %s" % ORIG)
        # ...and what it always could do, it still can (the carve-out widens nothing
        # and narrows nothing): its own branch work stays allowed.
        self.allowed("git rebase working", "git checkout -b something",
                     "git add ported.txt", "git status", "git log --oneline")


if __name__ == "__main__":
    unittest.main()
