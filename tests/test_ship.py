#!/usr/bin/env python3
"""Tests for the YES SHIP delegation: feature worker -> miss-integrator SUBAGENT.

    python3 -m unittest tests/test_ship.py -v      (from the repo root)

Standard library only. Builds a throwaway repo (staging `working`, a bare `origin`,
the integration checkout at the repo root, a feature worktree on claude/<name>), a
temp ticket dir and a temp ship config whose release/deploy/verify commands are
harmless local commands, then drives the guard hook with the exact JSON Claude Code
sends — WITHOUT `agent_type` for the feature worker's own calls, WITH
`agent_type: miss-integrator` for the subagent's.

Proves: (1) feature workers get the miss-integrator agent and may spawn it;
(2) the feature worker itself still cannot do integrator-only things, ticket or no
ticket; (3) the scoped integrate -> push -> release -> deploy -> verify sequence is
allowed and actually works; (4) everything outside the ticket stays blocked (other
branches/refs, force, ref surgery, other sudo/systemctl, guard edits, no/expired
ticket); (5) state drift or a failed pre-check stops shipping before anything runs.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, ".claude", "hooks", "prevent-misswork.py")
TICKET = os.path.join(ROOT, "scripts", "miss-ship-ticket.py")
AGENTS = os.path.join(ROOT, "scripts", "miss-agents.py")
ROLE_CTX = os.path.join(ROOT, "scripts", "miss-role-context.py")
GIT_ID = ["-c", "user.email=t@example", "-c", "user.name=t"]
SHIP = "miss-integrator"


def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", cwd, *GIT_ID, *args],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and r.returncode != 0:
        raise AssertionError("git %s failed in %s:\n%s" % (" ".join(args), cwd, r.stdout))
    return r.stdout.strip()


class Ship(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="miss-ship-")
        self.tickets = os.path.join(self.tmp, "tickets")
        self.mission = os.path.join(self.tmp, "missions", "m1")
        os.makedirs(self.mission)
        # repo on `working`, with a bare origin, integration checkout = repo root
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
        self.deploy_cmd = "sudo -n true && touch %s || touch %s" % (self.deployed, self.deployed)
        self.verify_cmd = "test -f %s" % self.deployed
        with open(self.cfg, "w") as fh:
            json.dump({os.path.realpath(self.repo): {
                "release_branch": "main", "release": [self.release_cmd],
                "deploy": [self.deploy_cmd], "verify": [self.verify_cmd]}}, fh)
        self.repo_id = "repo-testid"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers -----------------------------------------------------------------
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MISS_", "MISSION_", "CLAUDE_MISS_"))}
        env.update({
            "CLAUDE_MISS_ROLE": "feature", "PRIMARY_REPO": self.repo, "BASE_BRANCH": "working",
            "WORKTREES_DIR": os.path.join(self.tmp, "worktrees"),
            "MISS_REPO_ROOT": self.repo, "MISS_REPO_ID": self.repo_id, "MISS_WORKTREE": self.wt,
            "MISS_FEATURE_BRANCH": self.branch, "MISS_INTEGRATION_BRANCH": "working",
            "MISS_INTEGRATION_WORKTREE": self.repo, "MISS_TICKET_DIR": self.tickets,
            "MISS_SHIP_CONFIG": self.cfg, "MISSION_NAME": "m1", "MISSION_DATA_DIR": self.mission,
        })
        env.update(extra)
        return env

    def hook(self, command=None, cwd=None, agent=None, tool="Bash", file_path=None):
        ev = {"tool_name": tool, "cwd": cwd or self.wt,
              "tool_input": {"command": command} if tool == "Bash" else {"file_path": file_path}}
        if agent:
            ev["agent_id"] = "abc123"
            ev["agent_type"] = agent
        r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True,
                           text=True, env=self.env())
        return r.returncode, r.stderr

    def allowed(self, command, cwd=None, agent=SHIP):
        rc, err = self.hook(command, cwd, agent)
        self.assertEqual(rc, 0, "expected ALLOWED: %s\n%s" % (command, err))

    def blocked(self, command, needle="", cwd=None, agent=SHIP):
        rc, err = self.hook(command, cwd, agent)
        self.assertEqual(rc, 2, "expected BLOCKED: %s" % command)
        if needle:
            self.assertIn(needle, err, command)
        return err

    def ticket(self, approval="YES SHIP", **extra):
        r = subprocess.run([sys.executable, TICKET, "--approval", approval, "--request", "make x two",
                            "--tests", "unit OK", "--review", "APPROVE"], cwd=self.wt,
                           env=self.env(**extra), capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def ticket_path(self):
        return os.path.join(self.tickets, "%s--%s.json" % (self.repo_id, self.branch.replace("/", "_")))

    def run_allowed(self, command, cwd=None):
        """Guard-check as the subagent, then really run it (as the subagent would)."""
        self.allowed(command, cwd)
        r = subprocess.run(command, shell=True, cwd=cwd or self.wt, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, command + "\n" + r.stdout + r.stderr)

    # --- 1. feature workers can spawn the integrator subagent ---------------------
    def test_feature_worker_gets_the_integrator_agent_and_may_spawn_it(self):
        env = self.env()
        agents = json.loads(subprocess.run([sys.executable, AGENTS], capture_output=True,
                                           text=True, env=env).stdout)
        self.assertIn(SHIP, agents)
        self.assertIn("Agent", agents[SHIP]["tools"])          # may use miss-reviewer itself
        self.assertIn("YES SHIP", agents[SHIP]["prompt"])
        self.assertIn("RESULT: SHIPPED", agents[SHIP]["prompt"])
        # Spawning is an Agent tool call: the guard has nothing to say about it.
        rc, _ = self.hook(tool="Agent")
        self.assertEqual(rc, 0)
        # And the rails tell the worker the one-phrase flow, not a second console.
        env["MISS_AGENTS_ATTACHED"] = "1"
        out = subprocess.run([sys.executable, ROLE_CTX], cwd=self.wt, capture_output=True,
                             text=True, env=env).stdout
        self.assertIn("== SHIP", out)
        self.assertIn("Agent(miss-integrator)", out)
        self.assertIn(TICKET, out)
        for word in ("SHIPPED", "BLOCKED", "NEEDS ATTENTION", "ready for integrator"):
            self.assertIn(word, out)
        # The integrator role does NOT get a ship agent (it has the real powers already)
        # and gets no subagents of its own.
        env["CLAUDE_MISS_ROLE"] = "integrator"
        agents = json.loads(subprocess.run([sys.executable, AGENTS], capture_output=True,
                                           text=True, env=env).stdout)
        self.assertEqual(agents, {})

    # --- 2. the feature worker itself still cannot integrate ------------------------
    def test_feature_worker_cannot_do_integrator_actions_even_with_a_ticket(self):
        rc, out = self.ticket()
        self.assertEqual(rc, 0, out)
        for cmd in ("git merge --ff-only %s" % self.branch, "git push origin working",
                    self.release_cmd, self.deploy_cmd, "sudo systemctl restart x.service"):
            self.blocked(cmd, "feature worker", cwd=self.repo, agent=None)
            self.blocked(cmd, "feature worker", cwd=self.wt, agent=None)
        # A subagent that is NOT the integrator is a feature worker too.
        self.blocked("git merge --ff-only %s" % self.branch, "feature worker",
                     cwd=self.repo, agent="miss-implementer")

    # --- 3. the scoped shipment succeeds -------------------------------------------
    def test_scoped_integrate_push_release_deploy_verify_succeeds(self):
        rc, out = self.ticket()
        self.assertEqual(rc, 0, out)
        self.assertIn("SHIP DELEGATION", out)
        self.assertIn(self.commit[:12], out)
        self.assertIn("release: " + self.release_cmd, out)
        self.assertIn("push: working -> origin/working", out)
        t = json.load(open(self.ticket_path()))
        self.assertEqual((t["branch"], t["commit"], t["base"], t["remote"], t["release_branch"]),
                         (self.branch, self.commit, "working", "origin", "main"))
        self.assertEqual(t["tree"], git(self.wt, "rev-parse", "HEAD^{tree}"))
        # read-only + fetch are fine; the log may be written; app code may not
        self.allowed("git log working..%s" % self.branch)
        self.allowed("git diff working...%s" % self.branch)
        self.allowed("git fetch origin")
        self.allowed("git merge-base --is-ancestor working %s" % self.branch)   # not `merge`
        self.allowed("printf '%%s\\n' 'step1 verify: OK' >> %s" % t["log"])      # its own log
        self.allowed("echo 'step1 OK' >> %s" % os.path.join(self.mission, "ship.log"))
        self.blocked("echo x >> %s/app.py" % self.repo, "outside the YES SHIP delegation")
        self.blocked("git push origin working >> %s" % t["log"], "mix a mutating command")
        rc, _ = self.hook(tool="Write", file_path=t["log"], agent=SHIP)
        self.assertEqual(rc, 0)
        rc, _ = self.hook(tool="Write", file_path=os.path.join(self.mission, "ship.log"), agent=SHIP)
        self.assertEqual(rc, 0)
        # integrate (only in the integration checkout), then push, release, deploy, verify
        self.blocked("git merge --ff-only %s" % self.branch, "outside the integration checkout", cwd=self.wt)
        self.run_allowed("cd %s && git merge --ff-only %s" % (self.repo, self.branch), cwd=self.repo)
        self.assertEqual(git(self.repo, "rev-parse", "working"), self.commit)
        self.run_allowed("git -C %s push origin working" % self.repo)
        self.assertEqual(git(self.origin, "rev-parse", "working"), self.commit)
        self.run_allowed(self.release_cmd)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.commit)
        self.run_allowed("git -C %s push origin main" % self.repo)
        self.run_allowed(self.deploy_cmd)
        self.run_allowed(self.verify_cmd)
        self.assertTrue(os.path.exists(self.deployed))
        # nothing about this needed CLAUDE_MISS_ROLE=integrator anywhere
        self.assertEqual(self.env()["CLAUDE_MISS_ROLE"], "feature")

    def test_repo_without_release_config_stops_after_push(self):
        os.unlink(self.cfg)
        rc, out = self.ticket()
        self.assertEqual(rc, 0, out)
        self.assertIn("release: (not defined", out)
        t = json.load(open(self.ticket_path()))
        self.assertEqual((t["release"], t["deploy"], t["verify"]), ([], [], []))
        self.blocked(self.release_cmd, "outside the YES SHIP delegation")
        self.blocked(self.deploy_cmd, "outside the YES SHIP delegation")

    # --- 4. unrelated operations stay blocked --------------------------------------
    def test_unrelated_operations_remain_blocked_for_the_subagent(self):
        rc, out = self.ticket()
        self.assertEqual(rc, 0, out)
        git(self.repo, "branch", "claude/other", "working")
        R = self.repo
        outside = "outside the YES SHIP delegation"
        for cmd in ("git merge --ff-only claude/other", "git merge %s" % self.branch,
                    "git merge --no-ff %s" % self.branch, "git merge --ff-only %s --no-verify" % self.branch,
                    "git push origin claude/other", "git push origin working:main",
                    "git push --force origin working", "git push -f origin working",
                    "git push origin +working", "git push origin :working", "git push other working",
                    "git push . working:main", "git reset --hard HEAD~1", "git checkout main",
                    "git switch main", "git branch -f main working", "git tag v1", "git rebase working",
                    "git remote set-url origin /x", "git worktree add /x", "git update-ref refs/heads/main HEAD",
                    "git commit -am x", "git stash", "git clean -fd",
                    "sudo systemctl restart other.service", "systemctl restart x", "sudo rm -rf /",
                    "scripts/make-release.sh --push", "rm -rf %s" % R, "echo x > %s/app.py" % R,
                    "git merge --ff-only %s && git push origin main" % self.branch,
                    "git -C %s merge --ff-only %s; git push origin working:main" % (R, self.branch)):
            self.blocked(cmd, outside, cwd=R)
        # guard/rails and app code are off-limits to edit; the ship log is not
        for path in (HOOK, os.path.join(ROOT, "scripts", "miss-agents.py"),
                     os.path.join(ROOT, "miss-rails.settings.json"),
                     os.path.join(R, "app.py"), os.path.join(self.wt, "app.py"), "/etc/sudoers"):
            rc, err = self.hook(tool="Edit", file_path=path, agent=SHIP)
            self.assertEqual(rc, 2, path)
        # blocks are recorded in the step log for later inspection
        self.assertTrue(os.path.exists(os.path.join(self.mission, "ship.log")))
        self.assertIn("guard:", open(os.path.join(self.mission, "ship.log")).read())

    def test_no_ticket_or_expired_ticket_means_no_power(self):
        self.blocked("git merge --ff-only %s" % self.branch, "no valid delegation", cwd=self.repo)
        self.blocked("git push origin working", "no valid delegation")
        rc, err = self.hook(tool="Write", file_path=os.path.join(self.mission, "ship.log"), agent=SHIP)
        self.assertEqual(rc, 2)
        self.allowed("git log -3")                       # read-only still fine
        rc, out = self.ticket()
        self.assertEqual(rc, 0, out)
        t = json.load(open(self.ticket_path()))
        t["created"] = time.time() - 4 * 3600
        json.dump(t, open(self.ticket_path(), "w"))
        self.blocked("git merge --ff-only %s" % self.branch, "expired", cwd=self.repo)
        # The phrase must be exact, and a wrong one writes nothing.
        os.unlink(self.ticket_path())
        for phrase in ("yes ship", "YES INTEGRATE", ""):
            rc, out = self.ticket(approval=phrase)
            self.assertEqual(rc, 1, phrase)
            self.assertIn("BLOCKED", out)
        self.assertFalse(os.path.exists(self.ticket_path()))

    # --- 5. drift / failed gates stop shipping safely -------------------------------
    def test_state_drift_stops_before_each_step(self):
        rc, out = self.ticket()
        self.assertEqual(rc, 0, out)
        # branch moved after approval => the merge is refused
        with open(os.path.join(self.wt, "app.py"), "a") as fh:
            fh.write("y = 3\n")
        git(self.wt, "commit", "-q", "-am", "sneaky extra")
        self.blocked("cd %s && git merge --ff-only %s" % (self.repo, self.branch),
                     "state drift", cwd=self.repo)
        self.assertNotEqual(git(self.repo, "rev-parse", "working"), self.commit)
        # working not at the approved commit => push / release / deploy refused
        self.blocked("git -C %s push origin working" % self.repo, "state drift")
        self.blocked(self.release_cmd, "state drift")
        self.blocked(self.deploy_cmd, "state drift")     # main is not at the commit either
        self.assertFalse(os.path.exists(self.deployed))
        log = open(os.path.join(self.mission, "ship.log")).read()
        self.assertIn("state drift", log)

    def test_failed_pre_checks_write_no_ticket(self):
        # behind staging
        with open(os.path.join(self.repo, "other.txt"), "w") as fh:
            fh.write("moved on\n")
        git(self.repo, "add", "other.txt")
        git(self.repo, "commit", "-q", "-m", "staging moved")
        rc, out = self.ticket()
        self.assertEqual(rc, 1)
        self.assertIn("YES REBASE", out)
        self.assertFalse(os.path.exists(self.ticket_path()))
        git(self.wt, "rebase", "-q", "working")
        # uncommitted work
        with open(os.path.join(self.wt, "app.py"), "a") as fh:
            fh.write("z = 4\n")
        rc, out = self.ticket()
        self.assertEqual(rc, 1)
        self.assertIn("uncommitted", out)
        git(self.wt, "checkout", "-q", "--", "app.py")
        # integration checkout with a modified tracked file
        with open(os.path.join(self.repo, "app.py"), "a") as fh:
            fh.write("# operator\n")
        rc, out = self.ticket()
        self.assertEqual(rc, 1)
        self.assertIn("integration checkout", out)
        git(self.repo, "checkout", "-q", "--", "app.py")
        # stray untracked files there do not matter
        with open(os.path.join(self.repo, "local.env.bak"), "w") as fh:
            fh.write("stray\n")
        rc, out = self.ticket()
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.ticket_path()))


if __name__ == "__main__":
    unittest.main()
