#!/usr/bin/env python3
"""miss-ship.py — the whole normal ship path for ONE approved feature branch.

    python3 scripts/miss-ship.py --approval "YES SHIP" \\
        --request "<the request, in plain words>" --tests "<checks run + results>"

Run by the FEATURE WORKER itself, once, after the operator has typed exactly
YES SHIP. That single approval covers the whole established path for THIS repo,
THIS branch, THIS commit:

    integrate -> push -> release -> deploy -> verify

There is no ticket, no subagent and no second console: the operator approves the
shipment, not each of its stages. What makes that safe is that this script is
deterministic — it decides nothing. Every step is read out of git or out of the
repo's recorded ship config, re-verified immediately before it runs, and the run
stops the moment reality stops matching what was approved.

Scope (nothing here is inferred from the chat):
  repo/branch/base   the mission's recorded identity (MISS_* env, from mission.json)
  commit             whatever `branch` points at when this script starts; if it moves
                     mid-run the run stops (NEEDS_ATTENTION) rather than shipping
                     something the operator did not approve
  release/deploy     ONLY the command strings the repo already has in the ship config
  verify             likewise; a repo with no config ships as far as integrate (+ push
                     if it has a remote) and stops. Nothing is ever invented.

Idempotent + resumable. Each step is skipped when git already shows it done (the
integration branch contains the commit; the remote ref is at it; the release branch is
at it), and the step git cannot see (deploy) is recorded in a small state file keyed by
repo_id + branch + commit. So a run that dies after the push can simply be run again:
it resumes at the first incomplete step and never duplicates a commit, push, release or
deployment. A different commit starts a fresh shipment.

Blocked before anything changes (exit 1, "BLOCKED: ..."): uncommitted changes, not a
claude/* branch, nothing to ship, branch behind the integration branch (needs a rebase
+ re-verify, then YES SHIP again), integration checkout missing / on another branch /
dirty. Once something HAS changed, a later failure is NEEDS_ATTENTION (exit 2) — never
a silent retry, never a second approval prompt.

Ship config — `~/.miss-claude/ship.json` (env MISS_SHIP_CONFIG), keyed by repo realpath:
  {"/path/to/repo": {"release_branch": "main",
      "release": ["git -C /path/to/repo push . working:main"],
      "deploy":  ["sudo systemctl restart mission-dashboard.service"],
      "verify":  ["curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:4200/"]}}
Miss Claude's own repo (mission-dashboard.service next to app.py) carries that entry as
a built-in default.

State + log: `~/.miss-claude/ship/<repo_id>--<branch>.json` (env MISS_SHIP_STATE_DIR)
and `<same>.log`, mirrored to <mission>/ship.log — outside every repo, and the same on a
remote host (scripts/ship-rails.sh ships this file). Standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

APPROVAL = "YES SHIP"
BRANCH_RE = re.compile(r"^claude/[A-Za-z0-9._-]+\Z")
CMD_TIMEOUT = 900


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return 1, str(exc)
    return r.returncode, (r.stdout or "").strip() + (("\n" + r.stderr.strip()) if r.returncode else "")


def repo_root_of(path):
    rc, out = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if rc:
        return ""
    common = os.path.realpath(out.splitlines()[0])
    return common[:-len("/.git")] if common.endswith("/.git") else os.path.dirname(common)


def repo_id_of(repo):
    """Same shape as app.py repo_id_of: <basename>-<8 hex of the realpath>."""
    real = os.path.realpath(repo)
    return "%s-%s" % (os.path.basename(real), hashlib.sha1(real.encode()).hexdigest()[:8])


def state_dir():
    return os.environ.get("MISS_SHIP_STATE_DIR", "").strip() or os.path.expanduser("~/.miss-claude/ship")


def staging_checkout(repo, branch):
    """The checkout that has <branch> checked out, per `git worktree list`."""
    rc, out = git(repo, "worktree", "list", "--porcelain")
    if rc:
        return ""
    wt = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt = line[len("worktree "):]
        elif line == "branch refs/heads/" + branch:
            return wt
    return ""


def ship_config(repo, base):
    """(release_branch, release, deploy, verify) for <repo> — established steps only."""
    path = os.environ.get("MISS_SHIP_CONFIG", "").strip() or os.path.expanduser("~/.miss-claude/ship.json")
    entry = None
    try:
        with open(path) as fh:
            cfg = json.load(fh)
        if isinstance(cfg, dict):
            entry = cfg.get(os.path.realpath(repo)) or cfg.get(repo)
    except (OSError, ValueError):
        pass
    if entry is None and os.path.isfile(os.path.join(repo, "mission-dashboard.service")) \
            and os.path.isfile(os.path.join(repo, "app.py")):
        # Miss Claude itself: the release + deploy + check CLAUDE.md already documents.
        entry = {
            "release_branch": "main",
            "release": ["git -C %s push . %s:main" % (repo, base)],
            "deploy": ["sudo systemctl restart mission-dashboard.service"],
            "verify": ["sleep 2; curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:4200/"],
        }
    if not isinstance(entry, dict):
        return "", [], [], []
    lst = lambda k: [c for c in (entry.get(k) or []) if isinstance(c, str) and c.strip()]  # noqa: E731
    rb = entry.get("release_branch") if isinstance(entry.get("release_branch"), str) else ""
    return rb, lst("release"), lst("deploy"), lst("verify")


class Ship(object):
    """One shipment. Nothing mutates until run() gets past its pre-checks."""

    def __init__(self, args, env):
        self.a = args
        self.env = env
        self.log_paths = []
        self.state = {"done": []}
        self.changed = False          # has anything outside this process changed yet?
        self.lines = []               # the step summary, for the final report

    # -- reporting ------------------------------------------------------------
    def log(self, text):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for p in self.log_paths:
            try:
                with open(p, "a") as fh:
                    fh.write("%s %s\n" % (stamp, text.replace("\n", " | ")))
            except OSError:
                pass

    def say(self, text):
        print(text)
        self.log(text)

    def blocked(self, msg):
        """Nothing has changed (and now nothing will): stop, exit 1."""
        self.say("RESULT: BLOCKED — %s" % msg)
        sys.exit(1)

    def attention(self, msg):
        """Something changed and something did not: stop, exit 2."""
        self.say("RESULT: NEEDS_ATTENTION — %s %s" % (msg, self.done_so_far()))
        sys.exit(2)

    def fail(self, msg):
        """Whichever of the two applies, given how far this run got."""
        (self.attention if self.changed else self.blocked)(msg)

    def done_so_far(self):
        done = [l.strip() for l in self.lines if " OK" in l or "already done" in l]
        return ("Completed: " + "; ".join(done) + ".") if done else "Nothing had shipped yet."

    def step(self, name, text):
        line = "%-10s %s" % (name + ":", text)
        self.lines.append(line)
        self.say("STEP " + line)

    # -- git helpers ----------------------------------------------------------
    def rev(self, ref, cwd=None):
        rc, out = git(cwd or self.repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        return out.strip() if rc == 0 else ""

    def contains(self, ref):
        """True when <ref> already contains the approved commit."""
        rc, _ = git(self.repo, "merge-base", "--is-ancestor", self.commit, ref)
        return rc == 0

    def remote_rev(self, ref):
        """What the remote has for <ref>, or None when it cannot be reached."""
        rc, out = git(self.repo, "ls-remote", self.remote, "refs/heads/" + ref)
        return (out.split() or [""])[0] if rc == 0 else None

    def still_approved(self):
        """The branch must still be exactly what the operator approved."""
        if self.rev(self.branch) != self.commit:
            self.attention("%s moved off the approved commit %s mid-ship; stopped."
                           % (self.branch, self.commit[:12]))

    def run_cmd(self, cmd, cwd, what):
        """Run one established command string. Returns its combined output."""
        if self.a.dry_run:
            self.say("  (dry run) %s" % cmd)
            return ""
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                               text=True, timeout=CMD_TIMEOUT)
        except Exception as exc:
            self.fail("%s failed to run (%s): %s" % (what, exc.__class__.__name__, cmd))
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        self.log("  $ %s -> rc=%d %s" % (cmd, r.returncode, out[:2000]))
        if r.returncode != 0:
            self.fail("%s failed (exit %d): %s — %s" % (what, r.returncode, cmd, out[:400]))
        return out

    # -- state ----------------------------------------------------------------
    def load_state(self):
        """This branch's ship state, but only if it is for THIS commit."""
        try:
            with open(self.state_path) as fh:
                s = json.load(fh)
        except (OSError, ValueError):
            s = {}
        if not isinstance(s, dict) or s.get("commit") != self.commit:
            s = {"commit": self.commit, "branch": self.branch, "repo": self.repo,
                 "base": self.base, "started": time.time(), "done": []}
        if not isinstance(s.get("done"), list):
            s["done"] = []
        return s

    def save_state(self):
        try:
            os.makedirs(state_dir(), mode=0o700, exist_ok=True)
            self.state["updated"] = time.time()
            with open(self.state_path, "w") as fh:
                json.dump(self.state, fh, indent=1)
        except OSError:
            pass

    def mark(self, name):
        if name not in self.state["done"]:
            self.state["done"].append(name)
        self.save_state()

    # -- resolve / pre-check --------------------------------------------------
    def resolve(self):
        """Who am I shipping for? Recorded identity only — never a guess."""
        env = self.env
        self.worktree = env.get("MISS_WORKTREE", "").strip() or os.getcwd()
        self.repo = env.get("MISS_REPO_ROOT", "").strip() or env.get("PRIMARY_REPO", "").strip() \
            or repo_root_of(self.worktree)
        if not self.repo or not os.path.isdir(os.path.join(self.repo, ".git")):
            self.blocked("cannot determine this mission's repo (MISS_REPO_ROOT/PRIMARY_REPO unset; "
                         "%s is not a checkout)" % self.worktree)
        if os.path.realpath(repo_root_of(self.worktree) or "") != os.path.realpath(self.repo):
            self.blocked("%s is not a checkout of the declared repo %s" % (self.worktree, self.repo))
        rc, branch = git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD")
        if rc or branch in ("", "HEAD"):
            self.blocked("the feature worktree is not on a branch")
        recorded = env.get("MISS_FEATURE_BRANCH", "").strip()
        if recorded and recorded != branch:
            self.blocked("worktree is on '%s' but the mission's recorded branch is '%s'"
                         % (branch, recorded))
        if not BRANCH_RE.match(branch):
            self.blocked("only claude/* feature branches ship; this is '%s'" % branch)
        self.branch = branch
        self.repo_id = env.get("MISS_REPO_ID", "").strip() or repo_id_of(self.repo)
        self.state_path = os.path.join(state_dir(),
                                       "%s--%s.json" % (self.repo_id, branch.replace("/", "_")))
        self.log_paths = [os.path.splitext(self.state_path)[0] + ".log"]
        mission = env.get("MISSION_DATA_DIR", "").strip()
        if mission and os.path.isdir(mission):
            self.log_paths.append(os.path.join(mission, "ship.log"))
        try:
            os.makedirs(state_dir(), mode=0o700, exist_ok=True)
        except OSError:
            pass

        base = env.get("MISS_INTEGRATION_BRANCH", "").strip() or env.get("BASE_BRANCH", "").strip()
        if not base:
            rc, _ = git(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/working")
            base = "working" if rc == 0 else ""
        if not base:
            self.blocked("no integration branch recorded and no 'working' branch in %s" % self.repo)
        rc, _ = git(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/" + base)
        if rc:
            self.blocked("integration branch '%s' does not exist in %s" % (base, self.repo))
        self.base = base

        self.commit = self.rev("HEAD", cwd=self.worktree)
        self.tree = self.rev("HEAD^{tree}", cwd=self.worktree)
        if not self.commit:
            self.blocked("cannot read the branch's commit")
        rc, remotes = git(self.repo, "remote")
        names = remotes.split() if rc == 0 else []
        self.remote = "origin" if "origin" in names else (names[0] if names else "")
        self.release_branch, self.release, self.deploy, self.verify = ship_config(self.repo, self.base)
        if self.release and not self.release_branch:
            self.blocked("ship config for %s has release commands but no release_branch" % self.repo)

    def precheck(self):
        """Everything that must hold before ANY step runs. Blocks; changes nothing."""
        rc, dirty = git(self.worktree, "status", "--porcelain")
        if rc or dirty:
            self.blocked("the feature worktree has uncommitted changes — commit them first "
                         "(YES SHIP covers the commit), then run this again")
        self.iwt = self.env.get("MISS_INTEGRATION_WORKTREE", "").strip() \
            or self.env.get("INTEGRATION_WORKTREE", "").strip() \
            or staging_checkout(self.repo, self.base)
        rc, counts = git(self.repo, "rev-list", "--left-right", "--count",
                         "%s...%s" % (self.branch, self.base))
        ahead, behind = (counts.split() + ["?", "?"])[:2] if rc == 0 else ("?", "?")
        # "The base already contains this commit" means one of two things, and they are
        # NOT the same: a shipment of ours that got as far as the merge (resume it), or a
        # branch that simply has nothing on it (there is nothing to ship, and a stale
        # state file must not be able to turn that into a release/deploy of someone
        # else's work). The state file, keyed by this exact commit, is what tells them
        # apart — no state, no resume.
        self.integrated = self.contains(self.base)
        if self.integrated and self.state["done"]:
            return
        if ahead == "0":
            self.blocked("nothing to ship: %s has no commits beyond %s" % (self.branch, self.base))
        if self.integrated:
            return
        rc, _ = git(self.repo, "merge-base", "--is-ancestor", self.base, self.branch)
        if rc:
            self.blocked("%s is behind %s by %s commit(s) and cannot fast-forward. Rebase it onto %s, "
                         "re-run your checks, then ask for YES SHIP again — nothing was changed."
                         % (self.branch, self.base, behind, self.base))
        if not self.iwt or not os.path.isdir(self.iwt):
            self.blocked("no checkout has '%s' checked out — spawn an Integrator mission for this repo "
                         "once, or check it out by hand" % self.base)
        if os.path.realpath(repo_root_of(self.iwt) or "") != os.path.realpath(self.repo):
            self.blocked("integration checkout %s is not a checkout of %s" % (self.iwt, self.repo))
        rc, ib = git(self.iwt, "rev-parse", "--abbrev-ref", "HEAD")
        if rc or ib != self.base:
            self.blocked("integration checkout %s is on '%s', not '%s' — the operator must fix that "
                         "by hand" % (self.iwt, ib, self.base))
        rc, idirty = git(self.iwt, "status", "--porcelain", "--untracked-files=no")
        if rc or idirty:
            self.blocked("integration checkout %s has uncommitted changes to tracked files — the "
                         "operator must look" % self.iwt)
        if self.rev(self.base) != self.rev("HEAD", cwd=self.iwt):
            self.blocked("%s is not what is checked out in %s" % (self.base, self.iwt))

    # -- the steps ------------------------------------------------------------
    def do_integrate(self):
        if self.integrated:
            self.step("integrate", "already done — %s contains %s" % (self.base, self.commit[:12]))
            return
        self.still_approved()
        out = self.run_cmd("git merge --ff-only %s" % self.branch, self.iwt, "integrate")
        if not self.a.dry_run and not self.contains(self.base):
            self.fail("the fast-forward did not land: %s does not contain %s — %s"
                      % (self.base, self.commit[:12], out[:200]))
        self.changed = True
        self.mark("integrate")
        self.step("integrate", "OK — %s fast-forwarded to %s in %s"
                  % (self.base, self.commit[:12], self.iwt))

    def do_push(self, ref, label):
        if not self.remote:
            self.step(label, "skipped — this repo has no remote")
            return
        if self.remote_rev(ref) == self.commit:
            self.step(label, "already done — %s/%s is at %s" % (self.remote, ref, self.commit[:12]))
            return
        self.still_approved()
        if self.rev(ref) != self.commit:
            self.fail("%s is not at the approved commit %s; not pushing" % (ref, self.commit[:12]))
        self.run_cmd("git push %s %s" % (self.remote, ref), self.repo, "push of " + ref)
        self.changed = True
        self.mark(label)
        self.step(label, "OK — %s -> %s/%s" % (ref, self.remote, ref))

    def do_release(self):
        if not self.release:
            self.step("release", "not defined for this repo — shipping ends here")
            return
        if self.rev(self.release_branch) == self.commit:
            self.step("release", "already done — %s is at %s" % (self.release_branch, self.commit[:12]))
            return
        self.still_approved()
        if self.rev(self.base) != self.commit:
            self.fail("%s is not at the approved commit %s before release"
                      % (self.base, self.commit[:12]))
        for cmd in self.release:
            self.run_cmd(cmd, self.repo, "release")
        if not self.a.dry_run and self.rev(self.release_branch) != self.commit:
            self.fail("release ran but %s is not at %s" % (self.release_branch, self.commit[:12]))
        self.changed = True
        self.mark("release")
        self.step("release", "OK — %s at %s" % (self.release_branch, self.commit[:12]))

    def do_deploy(self):
        if not self.deploy:
            self.step("deploy", "not defined for this repo")
            return
        if "deploy" in self.state["done"]:
            self.step("deploy", "already done for %s" % self.commit[:12])
            return
        self.still_approved()
        ref = self.release_branch if self.release else self.base
        if self.rev(ref) != self.commit:
            self.fail("%s is not at the approved commit %s before deploy" % (ref, self.commit[:12]))
        for cmd in self.deploy:
            self.run_cmd(cmd, self.repo, "deploy")
        self.changed = True
        self.mark("deploy")
        self.step("deploy", "OK — %s" % "; ".join(self.deploy))

    def do_verify(self):
        """Read-only confirmation: always re-run, never 'already done'."""
        if not self.verify:
            self.step("verify", "not defined for this repo")
            return
        out = ""
        for cmd in self.verify:
            out = self.run_cmd(cmd, self.repo, "verify") or out
        self.mark("verify")
        self.step("verify", "OK%s" % ((" — " + out.splitlines()[-1][:120]) if out else ""))

    def run(self):
        self.resolve()
        if self.a.show:
            print(json.dumps(self.load_state(), indent=1))
            return 0
        if self.a.approval.strip() != APPROVAL:
            self.blocked('the operator must type exactly "%s" first; then pass --approval "%s"'
                         % (APPROVAL, APPROVAL))
        self.state = self.load_state()      # before precheck: it tells a resume apart
        self.precheck()                     # from a branch with nothing on it
        self.state.update({"tree": self.tree, "request": self.a.request, "tests": self.a.tests})
        self.save_state()
        self.say("SHIP %s at %s -> %s (repo %s)" % (self.branch, self.commit[:12], self.base, self.repo))
        self.do_integrate()
        self.do_push(self.base, "push")
        self.do_release()
        if self.release and self.release_branch:
            self.do_push(self.release_branch, "push-rel")
        self.do_deploy()
        self.do_verify()
        where = ["%s at %s" % (self.base, self.commit[:12])]
        if self.remote:
            where.append("pushed to " + self.remote)
        if self.release:
            where.append("released to " + self.release_branch)
        if self.deploy:
            where.append("deployed")
        if self.verify:
            where.append("verified")
        self.say("RESULT: SHIPPED — " + ", ".join(where) + ".")
        return 0


def main():
    p = argparse.ArgumentParser(description="run the approved YES SHIP path for this branch")
    p.add_argument("--approval", default="")
    p.add_argument("--request", default="")
    p.add_argument("--tests", default="")
    p.add_argument("--dry-run", action="store_true", help="print the steps without running them")
    p.add_argument("--show", action="store_true", help="print this branch's ship state and exit")
    a = p.parse_args()
    sys.exit(Ship(a, os.environ).run())


if __name__ == "__main__":
    main()
