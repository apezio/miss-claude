#!/usr/bin/env python3
"""Tests for the YES SHIP path: ONE approval, one deterministic script.

    python3 -m unittest tests/test_ship.py -v      (from the repo root)

Standard library only. Builds a throwaway repo (staging `working`, a bare `origin`, the
integration checkout at the repo root, a feature worktree on claude/<name>), a temp
state dir and a temp ship config whose release/deploy/verify commands are harmless
local commands, then really runs scripts/miss-ship.py against it.

Proves: (1) the whole integrate -> push -> release -> deploy -> verify path runs off a
single YES SHIP and actually lands; (2) no approval, or a wrong one, ships nothing;
(3) a repo with no ship config stops after the established steps instead of inventing
any; (4) the pre-checks block with nothing changed (dirty worktree, nothing to ship,
branch behind staging, integration checkout dirty/on another branch); (5) re-running is
idempotent and resumes rather than repeating a deploy; (6) the branch moving off the
approved commit stops the run; and (7) the guard still refuses hand-run shipping verbs
from a feature worker while allowing the script itself.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, ".claude", "hooks", "prevent-misswork.py")
SHIP = os.path.join(ROOT, "scripts", "miss-ship.py")
ROLE_CTX = os.path.join(ROOT, "scripts", "miss-role-context.py")
GIT_ID = ["-c", "user.email=t@example", "-c", "user.name=t"]


def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", cwd, *GIT_ID, *args],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and r.returncode != 0:
        raise AssertionError("git %s failed in %s:\n%s" % (" ".join(args), cwd, r.stdout))
    return r.stdout.strip()


class ShipBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(
            __import__("tempfile").mkdtemp(prefix="miss-ship-"))
        self.state = os.path.join(self.tmp, "shipstate")
        self.mission = os.path.join(self.tmp, "missions", "m1")
        os.makedirs(self.mission)
        # repo on `working`, with a bare origin; integration checkout = the repo root
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "working")
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write("x = 1\n")
        git(self.repo, "add", "app.py")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "branch", "main", "working")
        self.origin = os.path.join(self.tmp, "origin.git")
        git(self.tmp, "init", "-q", "--bare", self.origin)
        git(self.repo, "remote", "add", "origin", self.origin)
        git(self.repo, "push", "-q", "origin", "working", "main")
        # feature worktree with one commit
        self.branch = "claude/feat"
        self.wt = os.path.join(self.tmp, "worktrees", "feat")
        git(self.repo, "worktree", "add", "-q", self.wt, "-b", self.branch, "working")
        with open(os.path.join(self.wt, "app.py"), "w") as fh:
            fh.write("x = 2\n")
        git(self.wt, "commit", "-q", "-am", "feature")
        self.commit = git(self.wt, "rev-parse", "HEAD")
        # ship config: harmless local stand-ins for release / deploy / verify
        self.deployed = os.path.join(self.tmp, "deployed")
        self.cfg = os.path.join(self.tmp, "ship.json")
        self.release_cmd = "git -C %s push . working:main" % self.repo
        # appends, so a duplicate deployment is visible as a second line
        self.deploy_cmd = "echo deployed >> %s" % self.deployed
        self.verify_cmd = "test -s %s && echo 200" % self.deployed
        self.write_cfg({os.path.realpath(self.repo): {
            "release_branch": "main", "release": [self.release_cmd],
            "deploy": [self.deploy_cmd], "verify": [self.verify_cmd]}})
        self.repo_id = "repo-testid"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers -----------------------------------------------------------------
    def write_cfg(self, obj):
        with open(self.cfg, "w") as fh:
            json.dump(obj, fh)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MISS_", "MISSION_", "CLAUDE_MISS_"))}
        env.update({
            "CLAUDE_MISS_ROLE": "feature", "PRIMARY_REPO": self.repo, "BASE_BRANCH": "working",
            "WORKTREES_DIR": os.path.join(self.tmp, "worktrees"),
            "MISS_REPO_ROOT": self.repo, "MISS_REPO_ID": self.repo_id, "MISS_WORKTREE": self.wt,
            "MISS_FEATURE_BRANCH": self.branch, "MISS_INTEGRATION_BRANCH": "working",
            "MISS_INTEGRATION_WORKTREE": self.repo, "MISS_SHIP_STATE_DIR": self.state,
            "MISS_SHIP_CONFIG": self.cfg, "MISSION_NAME": "m1", "MISSION_DATA_DIR": self.mission,
            # git inside the script needs an identity for the merge commit it never makes,
            # but also for `git push` bookkeeping on some setups.
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example",
        })
        env.update(extra)
        return env

    def ship(self, approval="YES SHIP", *extra_args, **envextra):
        args = [sys.executable, SHIP, "--request", "make x two", "--tests", "unit OK"]
        if approval is not None:
            args += ["--approval", approval]
        args += list(extra_args)
        r = subprocess.run(args, cwd=self.wt, env=self.env(**envextra),
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def hook(self, command, cwd=None, tool="Bash", file_path=None):
        ev = {"tool_name": tool, "cwd": cwd or self.wt,
              "tool_input": {"command": command} if tool == "Bash" else {"file_path": file_path}}
        r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True,
                           text=True, env=self.env())
        return r.returncode, r.stderr

    def deploy_count(self):
        try:
            with open(self.deployed) as fh:
                return len([l for l in fh if l.strip()])
        except OSError:
            return 0


class ShipPath(ShipBase):
    # --- 1. the whole path, off one approval -------------------------------------
    def test_one_approval_runs_integrate_push_release_deploy_verify(self):
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        # staging + deploy branch + both remote refs are at the approved commit
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.commit)
        self.assertEqual(self.deploy_count(), 1)
        for step in ("integrate:", "push:", "release:", "deploy:", "verify:"):
            self.assertIn(step, out)
        # and it is logged where the operator can find it afterwards
        self.assertTrue(os.path.isfile(os.path.join(self.mission, "ship.log")))

    # --- 2. no approval, no shipment ---------------------------------------------
    def test_without_the_exact_approval_nothing_ships(self):
        for bad in (None, "", "ok", "yes ship", "YES  SHIP"):
            with self.subTest(approval=bad):
                rc, out = self.ship(bad)
                self.assertEqual(rc, 1, out)
                self.assertIn("RESULT: BLOCKED", out)
                self.assertEqual(git(self.repo, "rev-parse", "working"),
                                 git(self.repo, "rev-parse", "working~0"))
                self.assertNotEqual(git(self.repo, "rev-parse", "working"), self.commit)
                self.assertEqual(self.deploy_count(), 0)

    # --- 3. only established steps ------------------------------------------------
    def test_repo_without_ship_config_stops_after_the_established_steps(self):
        self.write_cfg({})            # no entry for this repo
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertIn("not defined for this repo", out)
        # integrated + pushed, but main untouched and nothing deployed
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "working"), self.commit)
        self.assertNotEqual(git(self.repo, "rev-parse", "main"), self.commit)
        self.assertEqual(self.deploy_count(), 0)

    def test_no_remote_means_no_push_and_still_ships(self):
        git(self.repo, "remote", "remove", "origin")
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertIn("no remote", out)
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)


class PreChecks(ShipBase):
    """Everything that must stop the run before anything at all has changed."""

    def assert_blocked(self, needle, **envextra):
        rc, out = self.ship(**envextra)
        self.assertEqual(rc, 1, out)
        self.assertIn("RESULT: BLOCKED", out)
        self.assertIn(needle, out)
        self.assertNotEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(self.deploy_count(), 0)

    def test_uncommitted_changes(self):
        with open(os.path.join(self.wt, "app.py"), "a") as fh:
            fh.write("# dirty\n")
        self.assert_blocked("uncommitted changes")

    def test_nothing_to_ship(self):
        git(self.repo, "worktree", "remove", "--force", self.wt)
        git(self.repo, "branch", "-D", self.branch)
        git(self.repo, "worktree", "add", "-q", self.wt, "-b", self.branch, "working")
        rc, out = self.ship()
        self.assertEqual(rc, 1, out)
        self.assertIn("nothing to ship", out)

    def test_branch_behind_staging_asks_for_a_rebase_not_another_phrase(self):
        # move staging on independently, so the branch no longer fast-forwards
        with open(os.path.join(self.repo, "other.txt"), "w") as fh:
            fh.write("moved\n")
        git(self.repo, "add", "other.txt")
        git(self.repo, "commit", "-q", "-m", "staging moved")
        rc, out = self.ship()
        self.assertEqual(rc, 1, out)
        self.assertIn("cannot fast-forward", out)
        self.assertIn("YES SHIP again", out)
        # it does NOT invent an approval stage of its own
        for phrase in ("YES INTEGRATE", "YES DEPLOY", "YES RELEASE", "YES PUSH"):
            self.assertNotIn(phrase, out)
        self.assertEqual(self.deploy_count(), 0)

    def test_integration_checkout_on_another_branch(self):
        git(self.repo, "checkout", "-q", "main")
        self.assert_blocked("not 'working'")

    def test_integration_checkout_dirty(self):
        with open(os.path.join(self.repo, "app.py"), "a") as fh:
            fh.write("# meddled\n")
        self.assert_blocked("uncommitted changes to tracked files")

    def test_only_claude_branches_ship(self):
        git(self.wt, "branch", "-m", self.branch, "hotfix")
        rc, out = self.ship(**{"MISS_FEATURE_BRANCH": "hotfix"})
        self.assertEqual(rc, 1, out)
        self.assertIn("only claude/* feature branches ship", out)


class ResumeAndDrift(ShipBase):
    def test_rerunning_a_finished_ship_repeats_nothing(self):
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.deploy_count(), 1)
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertIn("already done", out)
        self.assertEqual(self.deploy_count(), 1, "the deploy must not run twice")

    def test_resumes_at_the_first_incomplete_step(self):
        # first run: deploy fails, so the run stops after release with NEEDS_ATTENTION
        self.write_cfg({os.path.realpath(self.repo): {
            "release_branch": "main", "release": [self.release_cmd],
            "deploy": ["false"], "verify": [self.verify_cmd]}})
        rc, out = self.ship()
        self.assertEqual(rc, 2, out)
        self.assertIn("RESULT: NEEDS_ATTENTION", out)
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.commit)
        # fix the deploy and re-run the SAME command: integrate/push/release are skipped
        self.write_cfg({os.path.realpath(self.repo): {
            "release_branch": "main", "release": [self.release_cmd],
            "deploy": [self.deploy_cmd], "verify": [self.verify_cmd]}})
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertIn("integrate: already done", out)
        self.assertIn("release:   already done", out)
        self.assertEqual(self.deploy_count(), 1)

    def test_a_moved_branch_stops_the_run(self):
        """The approved commit is the scope; a new commit is not covered by it."""
        # integrate + push + release succeed, then the deploy command moves the branch
        # on under us — the run must stop rather than carry on with something else.
        moved = ("git -C %s -c user.email=t@example -c user.name=t commit -q --allow-empty "
                 "-m sneaky" % self.wt)
        self.write_cfg({os.path.realpath(self.repo): {
            "release_branch": "main", "release": [self.release_cmd, moved],
            "deploy": [self.deploy_cmd], "verify": [self.verify_cmd]}})
        rc, out = self.ship()
        self.assertEqual(rc, 2, out)
        self.assertIn("RESULT: NEEDS_ATTENTION", out)
        self.assertIn("moved off the approved commit", out)
        self.assertEqual(self.deploy_count(), 0, "nothing is deployed after drift")


class GuardStillHolds(ShipBase):
    """The worker's own hands stay tied; the script is the only route."""

    def allowed(self, command):
        rc, err = self.hook(command)
        self.assertEqual(rc, 0, "expected ALLOWED: %s\n%s" % (command, err))

    def blocked(self, command, needle=""):
        rc, err = self.hook(command)
        self.assertEqual(rc, 2, "expected BLOCKED: %s" % command)
        if needle:
            self.assertIn(needle, err, command)

    def test_hand_run_shipping_verbs_stay_blocked(self):
        self.blocked("git merge --ff-only %s" % self.branch)
        self.blocked("git push origin working")
        self.blocked("sudo systemctl restart mission-dashboard.service")
        self.blocked("git -C %s merge --ff-only %s" % (self.repo, self.branch))

    def test_the_ship_script_itself_is_allowed(self):
        self.allowed('python3 %s --approval "YES SHIP" --request "r" --tests "t"' % SHIP)
        self.allowed("python3 scripts/miss-ship.py --show")
        # ... including when its free-text arguments quote a blocked verb
        self.allowed('python3 %s --approval "YES SHIP" --request "make git push work" '
                     '--tests "sudo systemctl restart checked"' % SHIP)

    def test_nothing_may_ride_along_with_it(self):
        self.blocked("python3 %s --approval x && git push origin working" % SHIP)
        self.blocked("python3 %s --approval x; sudo systemctl restart foo" % SHIP)
        self.blocked("python3 %s --approval x | git push origin working" % SHIP)
        self.blocked("git push origin working # python3 miss-ship.py")

    def test_role_context_asks_for_one_phrase_and_no_stages(self):
        r = subprocess.run([sys.executable, ROLE_CTX], cwd=self.wt, capture_output=True,
                           text=True, env=self.env())
        out = r.stdout
        self.assertIn("YES SHIP", out)
        self.assertIn("miss-ship.py", out)
        self.assertIn("not its stages", out)
        # the old delegation machinery is gone from what a session is told
        for gone in ("miss-integrator", "Agent(", "ticket", "ready for integrator"):
            self.assertNotIn(gone, out)


if __name__ == "__main__":
    unittest.main()
