#!/usr/bin/env python3
"""Tests for the YES SHIP path: ONE approval, one deterministic script.

    python3 -m unittest tests/test_ship.py -v      (from the repo root)

Standard library only. Builds a throwaway repo (staging `working`, a bare `origin`, the
integration checkout at the repo root, a feature worktree on claude/<name>), a temp
state dir and a temp ship config whose release/deploy/verify commands are harmless
local commands, then really runs scripts/miss-ship.py against it.

Proves: (1) the whole integrate -> push -> release -> deploy -> verify path runs off a
single YES SHIP and actually lands, publishing the RELEASE branch while leaving the
local integration branch unpublished; (2) no approval, or a wrong one, ships nothing;
(3) a repo with no ship config stops after the established steps instead of inventing
any; (4) the pre-checks block with nothing changed (dirty worktree, nothing to ship,
branch behind staging, integration checkout dirty/on another branch); (5) re-running is
idempotent and resumes rather than repeating a deploy; (6) the branch moving off the
approved commit stops the run; (7) an ordinary shipment cannot create <remote>/working —
publishing staging needs an explicit "publish_base": true; and (8) the guard still
refuses hand-run shipping verbs from a feature worker while allowing the script itself.
"""

import hashlib
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


def repo_id_of(repo):
    """Mirrors miss-ship.py's repo_id_of() (itself mirroring app.py's), so tests can
    predict the canonical integration-worktree path without importing the script."""
    real = os.path.realpath(repo)
    return "%s-%s" % (os.path.basename(real), hashlib.sha1(real.encode()).hexdigest()[:8])


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
        self.origin_working_at_setup = git(self.origin, "rev-parse", "working")
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
        # staging + deploy branch are at the approved commit, and the RELEASE branch
        # is what got published — staging is local, so origin/working has not moved
        before_working = self.origin_working_at_setup
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "working"), before_working)
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
        # integrated locally, but nothing published, main untouched, nothing deployed
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "working"),
                         self.origin_working_at_setup)
        self.assertNotEqual(git(self.repo, "rev-parse", "main"), self.commit)
        self.assertEqual(self.deploy_count(), 0)

    def test_no_remote_means_no_push_and_still_ships(self):
        git(self.repo, "remote", "remove", "origin")
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertIn("no remote", out)
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)

    def test_push_false_skips_the_remote_but_still_releases_and_deploys(self):
        """"push": false suppresses ONLY the pushes to the repo's own remote. The
        release/deploy commands still run — including a release that pushes somewhere
        of its own — and the result line says the remote was NOT pushed."""
        before_working = git(self.origin, "rev-parse", "working")
        self.write_cfg({os.path.realpath(self.repo): {
            "release_branch": "main", "release": [self.release_cmd],
            "deploy": [self.deploy_cmd], "verify": [self.verify_cmd], "push": False}})
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertIn('"push": false', out)
        self.assertIn("NOT pushed to origin", out)
        # staging + release branch moved locally; the remote did not
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "working"), before_working)
        self.assertNotEqual(git(self.origin, "rev-parse", "working"), self.commit)
        # release/deploy/verify still ran
        self.assertEqual(self.deploy_count(), 1)

    def test_push_defaults_to_true_and_only_an_explicit_false_disables_it(self):
        """A missing or non-false "push" must keep pushing the release branch — a typo
        must not silently stop the remote from being updated."""
        for value in ({}, {"push": None}, {"push": "no"}, {"push": 0}, {"push": True}):
            self.tearDown()
            self.setUp()          # fresh repo/origin/state, and fresh command strings
            entry = {"release_branch": "main", "release": [self.release_cmd],
                     "deploy": [self.deploy_cmd], "verify": [self.verify_cmd]}
            entry.update(value)
            self.write_cfg({os.path.realpath(self.repo): entry})
            rc, out = self.ship()
            self.assertEqual(rc, 0, "%s -> %s" % (value, out))
            self.assertEqual(git(self.origin, "rev-parse", "main"), self.commit,
                             "push should still have happened for %s" % (value,))


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
        self.assertIn("YES SHIP you already have covers the rebase", out)
        # it does NOT invent an approval stage of its own
        for phrase in ("YES INTEGRATE", "YES DEPLOY", "YES RELEASE", "YES PUSH", "YES REBASE"):
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


class AutoBootstrapIntegrationCheckout(ShipBase):
    """The precheck no longer blocks when nothing has `working` checked out — it
    provisions the canonical integration worktree itself (same layout app.py's
    ensure_integration_worktree() would use) and ships straight through it. An
    existing valid checkout (ShipBase's default: MISS_INTEGRATION_WORKTREE=self.repo,
    already on `working`) is covered by ShipPath's tests above and is untouched here."""

    def worktree_for(self, repo, branch):
        out = git(repo, "worktree", "list", "--porcelain")
        wt = ""
        for line in out.splitlines():
            if line.startswith("worktree "):
                wt = line[len("worktree "):]
            elif line == "branch refs/heads/" + branch:
                return wt
        return ""

    def canonical_dest(self):
        return os.path.join(self.tmp, "worktrees", ".integration",
                            repo_id_of(self.repo) + "--working")

    def free_working(self):
        """Move the main checkout off `working` without touching `main` (release
        pushes to `main`, and git refuses to push into a checked-out branch)."""
        git(self.repo, "checkout", "-q", "-b", "scratch", "working")

    # 2. no `working` checkout anywhere -> one is created and used
    def test_creates_integration_worktree_when_none_exists(self):
        self.free_working()
        rc, out = self.ship(MISS_INTEGRATION_WORKTREE="")
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertIn("bootstrapped integration checkout", out)
        iwt = self.worktree_for(self.repo, "working")
        self.assertEqual(os.path.realpath(iwt), os.path.realpath(self.canonical_dest()))
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.commit)
        self.assertEqual(self.deploy_count(), 1)

    # 3. rerun after auto-creation -> reused, not recreated or duplicated
    def test_rerun_after_auto_creation_reuses_the_same_checkout(self):
        self.free_working()
        rc, out = self.ship(MISS_INTEGRATION_WORKTREE="")
        self.assertEqual(rc, 0, out)
        iwt_first = self.worktree_for(self.repo, "working")
        wt_count = len(git(self.repo, "worktree", "list").splitlines())

        with open(os.path.join(self.wt, "app.py"), "w") as fh:
            fh.write("x = 3\n")
        git(self.wt, "commit", "-q", "-am", "feature 2")

        rc, out = self.ship(MISS_INTEGRATION_WORKTREE="")
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertNotIn("bootstrapped integration checkout", out, "should reuse, not recreate")
        self.assertEqual(self.worktree_for(self.repo, "working"), iwt_first)
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), wt_count,
                         "no duplicate worktree was registered")
        self.assertEqual(self.deploy_count(), 2)

    # 4a. unsafe: the canonical path is occupied by something that isn't a checkout
    def test_blocks_when_the_canonical_path_holds_something_else(self):
        self.free_working()
        dest = self.canonical_dest()
        os.makedirs(dest)
        with open(os.path.join(dest, "not_a_repo.txt"), "w") as fh:
            fh.write("stray\n")
        rc, out = self.ship(MISS_INTEGRATION_WORKTREE="")
        self.assertEqual(rc, 1, out)
        self.assertIn("RESULT: BLOCKED", out)
        self.assertIn("could not be created automatically", out)
        self.assertNotEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(self.deploy_count(), 0)
        self.assertTrue(os.path.isfile(os.path.join(dest, "not_a_repo.txt")), "left untouched")

    # 4b. unsafe: the canonical path is a real checkout, but of the wrong branch
    def test_blocks_when_the_canonical_path_is_the_wrong_branch(self):
        self.free_working()
        git(self.repo, "branch", "decoy", "main")
        dest = self.canonical_dest()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        git(self.repo, "worktree", "add", "-q", dest, "decoy")
        rc, out = self.ship(MISS_INTEGRATION_WORKTREE="")
        self.assertEqual(rc, 1, out)
        self.assertIn("RESULT: BLOCKED", out)
        self.assertIn("not a checkout of", out)
        self.assertNotEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(self.deploy_count(), 0)
        # the conflicting checkout is reported, not merged into or removed
        self.assertEqual(git(dest, "rev-parse", "--abbrev-ref", "HEAD"), "decoy")


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


class BaseBranchIsNotPublished(ShipBase):
    """`working` is the LOCAL integration branch: a shipment fast-forwards it and leaves
    it there. Publishing is what the release does, and the release branch is what goes to
    the remote. The regression this class exists for: an ordinary YES SHIP must not be
    able to create <remote>/working on a repo that never asked for one."""

    def drop_remote_working(self):
        """A remote that has never seen staging — the case a stray push would break."""
        git(self.origin, "update-ref", "-d", "refs/heads/working")
        git(self.repo, "fetch", "-q", "--prune", "origin")

    def remote_has(self, ref):
        return bool(git(self.repo, "ls-remote", self.origin, "refs/heads/" + ref))

    def cfg_with(self, **extra):
        entry = {"release_branch": "main", "release": [self.release_cmd],
                 "deploy": [self.deploy_cmd], "verify": [self.verify_cmd]}
        entry.update(extra)
        self.write_cfg({os.path.realpath(self.repo): entry})

    def test_ordinary_shipment_never_creates_remote_working(self):
        self.drop_remote_working()
        self.assertFalse(self.remote_has("working"), "precondition")
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertFalse(self.remote_has("working"),
                         "an ordinary shipment must not publish the integration branch")
        self.assertIn("is the local integration branch", out)
        self.assertIn("working kept local", out)
        # ...while everything else ships exactly as before
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.commit)
        self.assertEqual(self.deploy_count(), 1)

    def test_an_existing_remote_working_is_left_where_it_was(self):
        before = git(self.origin, "rev-parse", "working")
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertEqual(git(self.origin, "rev-parse", "working"), before,
                         "staging on the remote is not moved by a shipment either")
        self.assertNotEqual(before, self.commit)

    def test_publish_base_true_is_the_opt_in(self):
        self.drop_remote_working()
        self.cfg_with(publish_base=True)
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: SHIPPED", out)
        self.assertEqual(git(self.origin, "rev-parse", "working"), self.commit)
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.commit)
        self.assertIn("pushed working+main to origin", out)

    def test_only_an_explicit_true_publishes_the_base(self):
        """The mirror image of "push": a truthy-looking typo must not publish staging."""
        for value in ({}, {"publish_base": None}, {"publish_base": False},
                      {"publish_base": "yes"}, {"publish_base": 1}):
            self.tearDown()
            self.setUp()          # fresh repo/origin/state, and fresh command strings
            self.cfg_with(**value)
            self.drop_remote_working()
            rc, out = self.ship()
            self.assertEqual(rc, 0, "%s -> %s" % (value, out))
            self.assertFalse(self.remote_has("working"),
                             "%s must not publish the integration branch" % (value,))
            self.assertEqual(git(self.origin, "rev-parse", "main"), self.commit,
                             "the release is published regardless")

    def test_publish_base_still_obeys_push_false(self):
        """"push": false disables the repo's remote outright; the opt-in cannot re-enable it."""
        self.drop_remote_working()
        self.cfg_with(publish_base=True, push=False)
        rc, out = self.ship()
        self.assertEqual(rc, 0, out)
        self.assertIn("NOT pushed to origin", out)
        self.assertFalse(self.remote_has("working"))
        self.assertNotEqual(git(self.origin, "rev-parse", "main"), self.commit)
        # the local integrate + release + deploy still happened
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.commit)
        self.assertEqual(self.deploy_count(), 1)


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
        self.allowed("sudo systemctl restart mission-dashboard.service")  # no longer a hook rule
        self.blocked("git -C %s merge --ff-only %s" % (self.repo, self.branch))

    def test_the_ship_script_itself_is_allowed(self):
        self.allowed('python3 %s --approval "YES SHIP" --request "r" --tests "t"' % SHIP)
        self.allowed("python3 scripts/miss-ship.py --show")
        # ... including when its free-text arguments quote a blocked verb
        self.allowed('python3 %s --approval "YES SHIP" --request "make git push work" '
                     '--tests "sudo systemctl restart checked"' % SHIP)

    def test_nothing_may_ride_along_with_it(self):
        self.blocked("python3 %s --approval x && git push origin working" % SHIP)
        self.blocked("python3 %s --approval x; git push origin working" % SHIP)
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
