#!/usr/bin/env python3
"""The upstream-sync carve-out in prevent-misswork.py: one merge, everything verified.

    python3 -m unittest tests/test_upstream_sync_guard.py

A fork-sync mission IS a merge — it brings the upstream project's commits onto a
claude/<mission> branch. The feature-worker blocklist refuses `git merge` outright and
no approval phrase unlocks it, so the hook grants exactly that merge: one ref, on a
mission branch, from a remote literally named `upstream` whose URL is not origin's.

These tests exist because a carve-out that opens a merge path must fail closed on every
gate. Builds a throwaway repo (staging `working`, an `upstream` remote distinct from
`origin`, a real refs/remotes/upstream/* ref, a mission worktree that conflicts with it)
and runs the real hook as a subprocess. stdlib only.
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

TMP = REPO = WT = None
MISSION = "claude/sync"
MERGE = "git merge upstream/main"


def git(cwd, *a):
    r = subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
    if r.returncode:
        raise AssertionError("git %s failed in %s:\n%s%s" % (" ".join(a), cwd, r.stdout, r.stderr))
    return r.stdout.strip()


def git_try(cwd, *a):
    """Same, for commands expected to fail (the conflicting merge)."""
    return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                   GIT_COMMITTER_NAME="t",
                                   GIT_COMMITTER_EMAIL="t@t")).returncode


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def setUpModule():
    """A repo shaped like a real fork: staging `working`, two distinct remotes, upstream
    refs that have moved on, and a mission worktree whose commit conflicts with them."""
    global TMP, REPO, WT
    TMP = os.path.realpath(tempfile.mkdtemp(prefix="upstreamsync-"))
    REPO = os.path.join(TMP, "repo")
    os.makedirs(REPO)
    git(REPO, "init", "-q", "-b", "working")
    write(os.path.join(REPO, "shared.txt"), "base\n")
    git(REPO, "add", "shared.txt")
    git(REPO, "commit", "-qm", "init")
    base = git(REPO, "rev-parse", "HEAD")
    git(REPO, "remote", "add", "origin", os.path.join(TMP, "origin.git"))
    git(REPO, "remote", "add", "upstream", os.path.join(TMP, "upstream.git"))
    git(REPO, "update-ref", "refs/remotes/origin/main", base)
    # what upstream has and the fork does not, on two branches (for the octopus case)
    git(REPO, "checkout", "-q", "-b", "sim", base)
    write(os.path.join(REPO, "shared.txt"), "upstream\n")
    git(REPO, "commit", "-qam", "upstream moves on")
    git(REPO, "update-ref", "refs/remotes/upstream/main", git(REPO, "rev-parse", "HEAD"))
    git(REPO, "update-ref", "refs/remotes/upstream/next", git(REPO, "rev-parse", "HEAD"))
    git(REPO, "checkout", "-q", "working")
    git(REPO, "branch", "-qD", "sim")
    # a LOCAL branch that merely looks like an upstream ref
    git(REPO, "branch", "upstream/main-ish", base)
    WT = os.path.join(TMP, "wt", "sync")
    git(REPO, "worktree", "add", "-q", WT, "-b", MISSION, "working")
    write(os.path.join(WT, "shared.txt"), "mission\n")
    git(WT, "commit", "-qam", "mission work")
    git(WT, "branch", "sidequest")          # a non-claude/ branch to stand on
    git(WT, "branch", "main")               # ...and a protected one


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def run_hook(command, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MISS_") and k not in ("PRIMARY_REPO", "BASE_BRANCH")}
    env.update({"CLAUDE_MISS_ROLE": "feature", "PRIMARY_REPO": REPO,
                "WORKTREES_DIR": os.path.join(TMP, "wt"), "BASE_BRANCH": "working"})
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd or WT}
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True,
                       text=True, env=env)
    return r.returncode, r.stderr


class Base(unittest.TestCase):
    def setUp(self):
        # every test starts on the mission branch, clean, with no merge in progress
        git_try(WT, "merge", "--abort")
        if git(WT, "rev-parse", "--abbrev-ref", "HEAD") != MISSION:
            git(WT, "checkout", "-q", MISSION)

    def allowed(self, *commands):
        for c in commands:
            rc, err = run_hook(c)
            self.assertEqual(rc, 0, "expected ALLOWED: %r\n%s" % (c, err))

    def blocked(self, *commands):
        for c in commands:
            rc, _ = run_hook(c)
            self.assertEqual(rc, 2, "expected BLOCKED: %r" % (c,))


class TheSanctionedMerge(Base):
    def test_merging_the_upstream_ref_is_allowed(self):
        self.allowed(MERGE, "git merge upstream/next")

    def test_recording_flags_are_allowed(self):
        for flag in ("--no-ff", "--ff", "--ff-only", "--no-commit", "--no-edit",
                     "--log", "--stat", "-q", "--quiet", "-v"):
            self.allowed("git merge %s upstream/main" % flag)


class OnlyFromAMissionBranch(Base):
    def test_a_non_mission_branch_gets_nothing(self):
        git(WT, "checkout", "-q", "sidequest")
        self.addCleanup(git, WT, "checkout", "-q", MISSION)
        self.blocked(MERGE)

    def test_a_protected_branch_gets_nothing(self):
        git(WT, "checkout", "-q", "main")
        self.addCleanup(git, WT, "checkout", "-q", MISSION)
        self.blocked(MERGE)

    def test_staging_gets_nothing(self):
        rc, _ = run_hook(MERGE, cwd=REPO)      # the main checkout, on `working`
        self.assertEqual(rc, 2, "expected BLOCKED on staging")

    def test_a_detached_head_gets_nothing(self):
        git(WT, "checkout", "-q", "--detach")
        self.addCleanup(git, WT, "checkout", "-q", MISSION)
        self.blocked(MERGE)


class OnlyARealUpstreamRef(Base):
    def test_nothing_else_may_be_merged(self):
        self.blocked("git merge working", "git merge main", "git merge origin/main",
                     "git merge sidequest", "git merge HEAD~1", "git merge FETCH_HEAD",
                     "git merge %s" % git(REPO, "rev-parse", "refs/remotes/upstream/main"))

    def test_a_local_branch_that_looks_like_an_upstream_ref_is_refused(self):
        self.blocked("git merge upstream/main-ish")

    def test_a_ref_that_does_not_exist_is_refused(self):
        self.blocked("git merge upstream/nope")

    def test_upstream_must_be_a_different_remote_from_origin(self):
        same = git(REPO, "remote", "get-url", "origin")
        was = git(REPO, "remote", "get-url", "upstream")
        git(REPO, "remote", "set-url", "upstream", same)
        self.addCleanup(git, REPO, "remote", "set-url", "upstream", was)
        self.blocked(MERGE)

    def test_never_an_octopus(self):
        self.blocked("git merge upstream/main upstream/next",
                     "git merge upstream/main working")


class ResolvingOnlyAMergeAlreadyAllowed(Base):
    def test_abort_and_friends_need_a_merge_in_progress(self):
        self.blocked("git merge --abort", "git merge --quit", "git merge --continue")
        self.assertNotEqual(git_try(WT, "merge", "upstream/main"), 0)   # conflicts
        self.addCleanup(git_try, WT, "merge", "--abort")
        self.allowed("git merge --abort", "git merge --quit", "git merge --continue")

    def test_they_may_not_carry_anything_else(self):
        self.assertNotEqual(git_try(WT, "merge", "upstream/main"), 0)
        self.addCleanup(git_try, WT, "merge", "--abort")
        self.blocked("git merge --abort upstream/main", "git merge --abort --no-ff",
                     "git merge --abort working")


class NothingWiderThanThatMerge(Base):
    def test_strategy_message_and_signing_flags_are_refused(self):
        self.blocked("git merge -s ours upstream/main",
                     "git merge --strategy=ours upstream/main",
                     "git merge -X theirs upstream/main",
                     "git merge -m 'sync' upstream/main",
                     "git merge -S upstream/main",
                     "git merge --squash upstream/main",
                     "git merge --allow-unrelated-histories upstream/main")

    def test_nothing_may_ride_along(self):
        self.blocked("%s && git push origin working" % MERGE,
                     "%s; git push origin %s" % (MERGE, MISSION),
                     "%s > /tmp/out" % MERGE,
                     "bash -c '%s && git push origin main'" % MERGE,
                     "sudo %s" % MERGE,
                     "sudo -u alice %s" % MERGE)

    def test_other_checkouts_are_still_out_of_reach(self):
        self.blocked("git -C %s merge upstream/main" % REPO,
                     "cd %s && %s" % (REPO, MERGE))

    def test_everything_else_a_feature_worker_may_not_do_is_unchanged(self):
        self.blocked("git push origin %s" % MISSION, "git push upstream HEAD",
                     "git merge --ff-only sidequest", "git worktree add ../x",
                     "git branch -D sidequest")
        # ...and what it always could do, it still can: the carve-out widens nothing
        # and narrows nothing.
        self.allowed("git rebase working", "git checkout -b something",
                     "git add shared.txt", "git status", "git log --oneline",
                     "git merge-base HEAD working")


if __name__ == "__main__":
    unittest.main()
