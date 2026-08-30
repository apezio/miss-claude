#!/usr/bin/env python3
"""Cross-repo regression tests for Miss Claude's dev-mission orchestration.

    python3 -m unittest tests/test_cross_repo.py -v      (from the repo root)

Standard library only, like the app. Builds two throwaway git repos with DIFFERENT
layouts — A stages on `working` (the Miss Claude convention), B lives on `master`
with a src/ tree and no `working` at all — plus temp MISSIONS_DIR / WORKTREES_DIR,
and exercises the pieces that carry a mission's repo identity end to end:

  app.py            repo_root_of / repo_id_of / integration worktree resolution,
                    create_worktree (incl. refusing a foreign leftover worktree),
                    mission_target (legacy inference reads the worktree's OWN repo),
                    dev_identity, merged_dev_missions grouping, the /spawn route
                    (feature + integrator roles) over real HTTP
  mission-env.py    mission.json -> MISS_* env, eval'd by bash like the launcher
  prevent-misswork  cross-repo git is blocked for both roles; same-repo work is not
  claude-miss       dry-run: enters its worktree, never creates one, refuses a
                    repo mismatch and a cwd that is not the declared worktree;
                    with a stub `claude`: launches guarded, with no extra agents
  miss-role-context the SHIP block: one YES SHIP, one script, no subagent
  claude-miss-integrator
                    dry-run: resolves the repo from a worktree's cwd (not the
                    Miss Claude default), honours INTEGRATION_WORKTREE, refuses a
                    checkout of the wrong repo, and tolerates a dirty checkout
"""

import http.client
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, ".claude", "hooks", "prevent-misswork.py")
MISSION_ENV = os.path.join(ROOT, "scripts", "mission-env.py")
CLAUDE_MISS = os.path.join(ROOT, "scripts", "claude-miss")
INTEGRATOR = os.path.join(ROOT, "scripts", "claude-miss-integrator")
ROLE_CTX = os.path.join(ROOT, "scripts", "miss-role-context.py")

GIT_ID = ["-c", "user.email=t@example", "-c", "user.name=t"]


def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", cwd, *GIT_ID, *args],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and r.returncode != 0:
        raise AssertionError("git %s failed in %s:\n%s" % (" ".join(args), cwd, r.stdout))
    return r.stdout.strip()


def make_repo(path, branch, files):
    os.makedirs(path)
    git(path, "init", "-q")
    git(path, "symbolic-ref", "HEAD", "refs/heads/" + branch)
    for rel, body in files.items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(body)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return os.path.realpath(path)


class Env:
    """The two repos + dirs every test shares (built once; cheap to keep)."""
    tmp = None

    @classmethod
    def build(cls):
        cls.tmp = tempfile.mkdtemp(prefix="miss-xrepo-")
        cls.missions = os.path.join(cls.tmp, "missions")
        cls.worktrees = os.path.join(cls.tmp, "worktrees")
        os.makedirs(cls.missions)
        os.makedirs(cls.worktrees)
        # Repo A: main + working (checked out), flat layout like Miss Claude.
        cls.A = make_repo(os.path.join(cls.tmp, "repo-a"), "main", {"app.py": "print(1)\n"})
        git(cls.A, "switch", "-q", "-c", "working")
        # Repo B: master only, src/ layout, a different name AND a different default branch.
        cls.B = make_repo(os.path.join(cls.tmp, "proj", "frontend"), "master",
                          {"src/index.js": "export default 1\n", "package.json": "{}\n"})
        # Repo C: same basename as B (a second `frontend`) to prove ids don't collide.
        cls.C = make_repo(os.path.join(cls.tmp, "other", "frontend"), "master", {"x": "1\n"})
        os.environ["MISSIONS_DIR"] = cls.missions
        os.environ["WORKTREES_DIR"] = cls.worktrees
        os.environ["PRIMARY_REPO"] = cls.A          # the dashboard's DEFAULT repo
        os.environ["MISSION_BASE_BRANCH"] = "working"
        os.environ["MISSION_PORT"] = "0"
        os.environ.pop("MISSION_TOKEN", None)
        sys.path.insert(0, ROOT)
        cls.app = importlib.import_module("app")

    @classmethod
    def destroy(cls):
        if cls.tmp and os.path.isdir(cls.tmp):
            shutil.rmtree(cls.tmp, ignore_errors=True)


def setUpModule():
    Env.build()


def tearDownModule():
    Env.destroy()


def run_hook(command, cwd, role, repo, extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MISS_")}
    env.update({"CLAUDE_MISS_ROLE": role, "PRIMARY_REPO": repo,
                "WORKTREES_DIR": Env.worktrees, "BASE_BRANCH": "working"})
    env.update(extra or {})
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True,
                       text=True, env=env)
    return r.returncode, r.stderr


def run_wrapper(script, cwd, env_extra, *args):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MISS_") and k not in ("PRIMARY_REPO", "BASE_BRANCH",
                                                        "INTEGRATION_WORKTREE", "MISSION_NAME")}
    env["CLAUDE_MISS_DRYRUN"] = "1"
    env["WORKTREES_DIR"] = Env.worktrees
    env["MISSIONS_DIR"] = Env.missions
    env["HOME"] = Env.tmp                      # so ~/mission-dashboard does NOT exist
    env.update(env_extra)
    r = subprocess.run(["bash", script, *args], cwd=cwd, capture_output=True, text=True,
                       env=env, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout + r.stderr


class RepoIdentity(unittest.TestCase):
    def test_worktree_resolves_to_its_repo(self):
        app = Env.app
        wt = os.path.join(Env.worktrees, "id-wt")
        git(Env.B, "worktree", "add", "-q", wt, "-b", "claude/id-wt", "master")
        self.assertEqual(app.repo_root_of(wt), Env.B)
        self.assertEqual(app.repo_root_of(os.path.join(wt, "src")), Env.B)
        self.assertTrue(app.same_repo(wt, Env.B))
        self.assertFalse(app.same_repo(wt, Env.A))
        self.assertIsNone(app.repo_root_of(Env.tmp))

    def test_ids_are_stable_and_distinct_for_same_basename(self):
        app = Env.app
        self.assertEqual(app.repo_id_of(Env.B), app.repo_id_of(Env.B))
        self.assertNotEqual(app.repo_id_of(Env.B), app.repo_id_of(Env.C))
        self.assertTrue(app.repo_id_of(Env.B).startswith("frontend-"))
        self.assertTrue(app.repo_id_of(Env.A).startswith("repo-a-"))

    def test_integration_worktree_lookup_and_creation(self):
        app = Env.app
        # A has working checked out in its main checkout: that IS the integration checkout.
        self.assertEqual(app.integration_worktree_of(Env.A, "working"), Env.A)
        self.assertEqual(app.ensure_integration_worktree(Env.A, "working"), (Env.A, None))
        # B's master is checked out in B itself.
        self.assertEqual(app.ensure_integration_worktree(Env.B, "master"), (Env.B, None))
        # A `working` branch that exists but is checked out NOWHERE gets a dedicated
        # checkout under WORKTREES_DIR/.integration keyed by repo id.
        git(Env.C, "branch", "working")
        self.assertIsNone(app.integration_worktree_of(Env.C, "working"))
        path, err = app.ensure_integration_worktree(Env.C, "working")
        self.assertIsNone(err)
        self.assertTrue(path.startswith(os.path.join(Env.worktrees, ".integration")))
        self.assertIn(app.repo_id_of(Env.C), path)
        self.assertEqual(app.worktree_branch(path), "working")
        self.assertEqual(app.repo_root_of(path), Env.C)
        # Idempotent, and a missing branch is a clean error, not a crash.
        self.assertEqual(app.ensure_integration_worktree(Env.C, "working"), (path, None))
        self.assertIsNotNone(app.ensure_integration_worktree(Env.C, "nope")[1])


class MissionsAcrossRepos(unittest.TestCase):
    def _mission(self, name, repo, base):
        app = Env.app
        self.assertIsNone(app.create_worktree(name, repo, base))
        os.makedirs(app.mission_path(name), exist_ok=True)
        meta = {"mode": "dev", "target": {"kind": "local-repo", "path": repo},
                "dev": dict(app.dev_meta(repo, base, worktree=os.path.join(Env.worktrees, name)),
                            preview_port=app.preview_port_for(name))}
        app.write_mission_meta(name, meta)
        return meta

    def test_two_missions_same_repo_and_two_repos_at_once(self):
        app = Env.app
        m1 = self._mission("a-one", Env.A, "working")
        m2 = self._mission("a-two", Env.A, "working")
        mb = self._mission("b-one", Env.B, "master")
        for name, meta, repo, base in (("a-one", m1, Env.A, "working"),
                                       ("a-two", m2, Env.A, "working"),
                                       ("b-one", mb, Env.B, "master")):
            d = meta["dev"]
            self.assertEqual(d["repo"], repo)
            self.assertEqual(d["repo_id"], app.repo_id_of(repo))
            self.assertEqual(d["branch"], "claude/" + name)
            self.assertEqual(d["base_branch"], base)
            self.assertEqual(d["integration_worktree"], repo)
            self.assertEqual(d["role"], "feature")
            self.assertEqual(git(os.path.join(Env.worktrees, name), "rev-parse", "--abbrev-ref", "HEAD"),
                             "claude/" + name)
            self.assertEqual(app.repo_root_of(os.path.join(Env.worktrees, name)), repo)
            # What every later reader sees, re-read from disk (no process state).
            ident = app.dev_identity(name)
            self.assertEqual((ident["repo"], ident["branch"], ident["base_branch"]),
                             (repo, "claude/" + name, base))
        self.assertNotEqual(m1["dev"]["preview_port"], m2["dev"]["preview_port"])
        self.assertEqual(m1["dev"]["preview_port"], app.preview_port_for("a-one"))
        # The badge names the repo for the non-default one and the branch for all.
        self.assertIn("frontend", app.dev_badge("b-one"))
        self.assertIn("claude/b-one", app.dev_badge("b-one"))
        self.assertIn("claude/a-one", app.dev_badge("a-one"))
        # Merged-detection groups per (repo, base): B's branch is judged against B's
        # master, A's against A's working — a commit on A's working must not affect B.
        groups = app._dev_missions_by_repo()
        self.assertEqual(groups[(Env.A, "working")], {"a-one": "a-one", "a-two": "a-two"})
        self.assertEqual(groups[(Env.B, "master")], {"b-one": "b-one"})
        merged = app.merged_dev_missions()
        self.assertTrue({"a-one", "a-two", "b-one"} <= merged)   # no commits yet => merged
        wt = os.path.join(Env.worktrees, "b-one")
        with open(os.path.join(wt, "src", "new.js"), "w") as fh:
            fh.write("1\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "work")
        merged = app.merged_dev_missions()
        self.assertNotIn("b-one", merged)
        self.assertIn("a-one", merged)

    def test_attach_refuses_a_foreign_worktree(self):
        app = Env.app
        self.assertIsNone(app.create_worktree("shared-name", Env.A, "working"))
        err = app.create_worktree("shared-name", Env.B, "master")
        self.assertIsNotNone(err)
        self.assertIn("different repository", err)
        self.assertEqual(app.repo_root_of(os.path.join(Env.worktrees, "shared-name")), Env.A)

    def test_legacy_mission_infers_repo_from_the_worktree_not_the_default(self):
        app = Env.app
        name = "legacy-b"
        git(Env.B, "worktree", "add", "-q", os.path.join(Env.worktrees, name),
            "-b", "claude/" + name, "master")
        os.makedirs(app.mission_path(name), exist_ok=True)   # no mission.json at all
        tgt = app.mission_target(name)
        self.assertEqual(tgt["mode"], "dev")
        self.assertEqual(tgt["dev"]["repo"], Env.B)          # not PRIMARY_REPO (= A)
        self.assertEqual(tgt["target"]["path"], Env.B)
        self.assertEqual(app.dev_identity(name)["branch"], "claude/" + name)


class MissionEnv(unittest.TestCase):
    def _env_for(self, meta):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir=Env.tmp) as fh:
            json.dump(meta, fh)
        script = ('eval "$(python3 %s %s)"; for k in MISS_MODE MISS_ROLE MISS_REPO_ROOT MISS_REPO_ID '
                  'MISS_WORKTREE MISS_FEATURE_BRANCH MISS_INTEGRATION_BRANCH MISS_INTEGRATION_WORKTREE '
                  'MISS_PREVIEW_PORT MISS_TARGET_KIND; do printf "%%s=%%s\\n" "$k" "${!k}"; done'
                  % (MISSION_ENV, fh.name))
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
        return dict(line.split("=", 1) for line in out.strip().splitlines())

    def test_roundtrip_through_bash_eval(self):
        app = Env.app
        meta = {"mode": "dev", "target": {"kind": "local-repo", "path": Env.B},
                "dev": dict(app.dev_meta(Env.B, "master", worktree=os.path.join(Env.worktrees, "b-one")),
                            preview_port=24123)}
        e = self._env_for(meta)
        self.assertEqual(e["MISS_MODE"], "dev")
        self.assertEqual(e["MISS_ROLE"], "feature")
        self.assertEqual(e["MISS_REPO_ROOT"], Env.B)
        self.assertEqual(e["MISS_REPO_ID"], app.repo_id_of(Env.B))
        self.assertEqual(e["MISS_FEATURE_BRANCH"], "claude/b-one")
        self.assertEqual(e["MISS_INTEGRATION_BRANCH"], "master")
        self.assertEqual(e["MISS_INTEGRATION_WORKTREE"], Env.B)
        self.assertEqual(e["MISS_PREVIEW_PORT"], "24123")

    def test_integrator_and_legacy_and_garbage(self):
        app = Env.app
        meta = {"mode": "dev", "target": {"kind": "local-repo", "path": Env.A},
                "dev": app.dev_meta(Env.A, "working", role="integrator", integration_worktree=Env.A)}
        e = self._env_for(meta)
        self.assertEqual((e["MISS_ROLE"], e["MISS_WORKTREE"], e["MISS_INTEGRATION_WORKTREE"]),
                         ("integrator", "", Env.A))
        # An old sidecar (no role/branch/repo_id) still yields a full identity.
        old = {"mode": "dev", "target": {"kind": "local-repo", "path": Env.A},
               "dev": {"repo": Env.A, "base_branch": "working",
                       "worktree": os.path.join(Env.worktrees, "old-one")}}
        e = self._env_for(old)
        self.assertEqual((e["MISS_ROLE"], e["MISS_FEATURE_BRANCH"]), ("feature", "claude/old-one"))
        # Hostile/odd values never reach the shell un-neutralised.
        bad = {"mode": "dev", "target": {"kind": "local-repo", "path": "/x"},
               "dev": {"repo": "/tmp/x'; echo pwned", "base_branch": "$(id)", "worktree": "/ok/wt"}}
        e = self._env_for(bad)
        self.assertEqual(e["MISS_REPO_ROOT"], "")
        self.assertEqual(e["MISS_INTEGRATION_BRANCH"], "")
        self.assertEqual(e["MISS_WORKTREE"], "/ok/wt")
        r = subprocess.run([sys.executable, MISSION_ENV, os.path.join(Env.tmp, "nope.json")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("MISS_MODE=''", r.stdout)


class GuardHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wtA = os.path.join(Env.worktrees, "hook-a")
        cls.wtB = os.path.join(Env.worktrees, "hook-b")
        git(Env.A, "worktree", "add", "-q", cls.wtA, "-b", "claude/hook-a", "working")
        git(Env.B, "worktree", "add", "-q", cls.wtB, "-b", "claude/hook-b", "master")

    def test_integrator_cannot_touch_another_repo(self):
        rc, err = run_hook("git -C %s merge --ff-only claude/hook-b" % Env.B, Env.A, "integrator", Env.A)
        self.assertEqual(rc, 2)
        self.assertIn("acts on " + Env.B, err)
        # ...even when its cwd wandered into the other repo.
        rc, _ = run_hook("git merge --ff-only claude/hook-b", Env.B, "integrator", Env.A)
        self.assertEqual(rc, 2)
        rc, _ = run_hook("git push origin master", self.wtB, "integrator", Env.A)
        self.assertEqual(rc, 2)

    def test_git_dash_C_forms_match_the_role_rules(self):
        """`git -C <dir> push` reads as `git push` to the role patterns (it used not to)."""
        for cmd in ("git -C %s push origin working" % Env.A, "git -c core.x=y push",
                    "git -C %s merge --ff-only claude/x" % Env.A):
            rc, out = run_hook(cmd, self.wtA, "feature", Env.A)
            self.assertEqual(rc, 2, cmd)
            self.assertIn("feature worker", out)
        rc, _ = run_hook("git -C %s rebase working" % Env.A, Env.A, "integrator", Env.A)
        self.assertEqual(rc, 2)

    def test_integrator_same_repo_still_works(self):
        rc, _ = run_hook("git merge --ff-only claude/hook-a", Env.A, "integrator", Env.A)
        self.assertEqual(rc, 0)
        rc, _ = run_hook("git -C %s merge --ff-only claude/hook-a" % Env.A, Env.tmp, "integrator", Env.A)
        self.assertEqual(rc, 0)
        # Read-only git on another repo is fine (reviewing is allowed).
        rc, _ = run_hook("git -C %s log --oneline -3" % Env.B, Env.A, "integrator", Env.A)
        self.assertEqual(rc, 0)
        rc, _ = run_hook("git -C %s diff working" % Env.B, Env.A, "integrator", Env.A)
        self.assertEqual(rc, 0)
        # The existing rails are intact: no non-ff merge, no rebase, no force-push.
        self.assertEqual(run_hook("git merge claude/hook-a", Env.A, "integrator", Env.A)[0], 2)
        self.assertEqual(run_hook("git rebase working", Env.A, "integrator", Env.A)[0], 2)
        self.assertEqual(run_hook("git push --force origin working", Env.A, "integrator", Env.A)[0], 2)

    def test_feature_worker_is_confined_to_its_repo(self):
        rc, _ = run_hook("git -C %s commit -am x" % self.wtB, self.wtA, "feature", Env.A)
        self.assertEqual(rc, 2)
        rc, _ = run_hook("git -C %s add -A" % Env.B, self.wtA, "feature", Env.A)
        self.assertEqual(rc, 2)
        rc, _ = run_hook("git commit -am x", self.wtA, "feature", Env.A)
        self.assertEqual(rc, 0)
        rc, _ = run_hook("git add app.py && git commit -m x", self.wtA, "feature", Env.A)
        self.assertEqual(rc, 0)
        # A worker on repo B (declared B) commits in its own B worktree — fine.
        rc, _ = run_hook("git commit -am x", self.wtB, "feature", Env.B)
        self.assertEqual(rc, 0)
        # Existing worker rails intact.
        self.assertEqual(run_hook("git push", self.wtA, "feature", Env.A)[0], 2)
        self.assertEqual(run_hook("git worktree add /tmp/x", self.wtA, "feature", Env.A)[0], 2)
        self.assertEqual(run_hook("git switch working", self.wtA, "feature", Env.A)[0], 2)
        self.assertEqual(run_hook("sudo systemctl restart foo", self.wtA, "feature", Env.A)[0], 0)

    def test_fetch_pull_and_cd_forms_are_cross_repo_mutations(self):
        for cmd in ("git -C %s fetch origin" % Env.B,
                    "git -C %s pull --ff-only" % Env.B,
                    "cd %s && git pull" % Env.B,
                    "cd %s; git fetch --all" % self.wtB,
                    "git -C %s remote add up https://x/y.git" % Env.B,
                    "git -C %s tag v1" % Env.B,
                    "git -C %s branch -D claude/hook-b" % Env.B,
                    "git -C %s worktree prune" % Env.B,
                    "git -C %s stash pop" % Env.B):
            self.assertEqual(run_hook(cmd, Env.A, "integrator", Env.A)[0], 2, cmd)
            self.assertEqual(run_hook(cmd, self.wtA, "feature", Env.A)[0], 2, cmd)
        # In the declared repo, fetch/pull are the integrator's business as before.
        self.assertEqual(run_hook("git fetch origin", Env.A, "integrator", Env.A)[0], 0)
        self.assertEqual(run_hook("git pull --ff-only", Env.A, "integrator", Env.A)[0], 0)

    def test_readonly_forms_are_allowed_in_foreign_repos(self):
        for cmd in ("git -C %s branch --list" % Env.B,
                    "git -C %s branch -a" % Env.B,
                    "git -C %s branch --merged master" % Env.B,
                    "git -C %s branch --show-current" % Env.B,
                    "git -C %s branch" % Env.B,
                    "git -C %s tag -l 'v*'" % Env.B,
                    "git -C %s tag" % Env.B,
                    "git -C %s worktree list --porcelain" % Env.B,
                    "git -C %s stash list" % Env.B,
                    "git -C %s remote -v" % Env.B,
                    "cd %s && git status && git log --oneline -3" % Env.B):
            self.assertEqual(run_hook(cmd, Env.A, "integrator", Env.A)[0], 0, cmd)
            self.assertEqual(run_hook(cmd, self.wtA, "feature", Env.A)[0], 0, cmd)
        # ...but the same subcommands' mutating forms are not.
        for cmd in ("git -C %s branch new-branch" % Env.B,
                    "git -C %s branch -m a b" % Env.B,
                    "git -C %s branch --list -D x" % Env.B,
                    "git -C %s tag -a v1 -m x" % Env.B,
                    "git -C %s worktree add /tmp/x" % Env.B,
                    "git -C %s stash" % Env.B):
            self.assertEqual(run_hook(cmd, Env.A, "integrator", Env.A)[0], 2, cmd)

    def test_release_repo_is_script_only(self):
        rel = os.path.join(Env.tmp, "release-checkout")
        git(Env.tmp, "init", "-q", rel)
        rel = os.path.realpath(rel)
        extra = {"RELEASE_DIR": rel}
        for role, cwd in (("integrator", Env.A), ("feature", self.wtA)):
            rc, err = run_hook("cd %s && git add -A && git commit -m x && git push" % rel,
                               cwd, role, Env.A, extra)
            self.assertEqual(rc, 2)
            self.assertIn("make-release.sh", err)
            rc, err = run_hook("git -C %s push origin HEAD" % rel, cwd, role, Env.A, extra)
            self.assertEqual((rc, "make-release.sh" in err), (2, True))
            # Looking is fine; the script itself is not a git command.
            self.assertEqual(run_hook("git -C %s log --oneline -3" % rel, cwd, role, Env.A, extra)[0], 0)
            self.assertEqual(run_hook("scripts/make-release.sh --dry-run", cwd, role, Env.A, extra)[0], 0)
        # RELEASE_DIR is also read from the declared repo's local.env.
        with open(os.path.join(Env.A, "local.env"), "w") as fh:
            fh.write('RELEASE_DIR="%s"\n' % rel)
        try:
            rc, err = run_hook("git -C %s commit -am x" % rel, Env.A, "integrator", Env.A)
            self.assertEqual((rc, "make-release.sh" in err), (2, True))
        finally:
            os.remove(os.path.join(Env.A, "local.env"))
        # A session declared FOR the release repo may work in it (the guard applies as usual).
        self.assertEqual(run_hook("git commit -am x", rel, "feature", rel, extra)[0], 0)

    def test_feature_write_guard_covers_integration_checkout(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MISS_")}
        env.update({"CLAUDE_MISS_ROLE": "feature", "PRIMARY_REPO": Env.B,
                    "WORKTREES_DIR": Env.worktrees, "MISS_INTEGRATION_WORKTREE": Env.C})
        for target, expect in ((os.path.join(Env.C, "x"), 2),        # integration checkout
                               (os.path.join(Env.B, "src", "y.js"), 2),   # primary checkout
                               (os.path.join(self.wtB, "src", "y.js"), 0),  # own worktree
                               (os.path.join(Env.tmp, "scratch.txt"), 0)):  # outside
            ev = {"tool_name": "Write", "tool_input": {"file_path": target, "content": ""},
                  "cwd": self.wtB}
            r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev),
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, expect, target)


class Wrappers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wtB = os.path.join(Env.worktrees, "wrap-b")
        git(Env.B, "worktree", "add", "-q", cls.wtB, "-b", "claude/wrap-b", "master")

    def _worktree_count(self):
        return len(os.listdir(Env.worktrees))

    def test_claude_miss_enters_its_worktree_and_never_creates_one(self):
        before = self._worktree_count()
        rc, out = run_wrapper(CLAUDE_MISS, self.wtB,
                              {"PRIMARY_REPO": Env.B, "BASE_BRANCH": "master",
                               "MISS_WORKTREE": self.wtB, "MISS_REPO_ID": "frontend-x",
                               "MISS_PREVIEW_PORT": "24999"})
        self.assertEqual(rc, 0, out)
        self.assertIn("repo:      " + Env.B, out)
        self.assertIn("claude/wrap-b", out)
        self.assertIn("port 24999", out)
        self.assertEqual(self._worktree_count(), before)
        # Legacy path (no MISS_WORKTREE): under WORKTREES_DIR => enter, still no create.
        rc, out = run_wrapper(CLAUDE_MISS, self.wtB, {"PRIMARY_REPO": Env.B, "BASE_BRANCH": "master"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._worktree_count(), before)

    def test_claude_miss_refuses_a_repo_mismatch_or_a_wrong_cwd(self):
        before = self._worktree_count()
        # Declared for A, but the worktree belongs to B: RED, no launch, no worktree.
        rc, out = run_wrapper(CLAUDE_MISS, self.wtB, {"PRIMARY_REPO": Env.A, "MISS_WORKTREE": self.wtB})
        self.assertNotEqual(rc, 0)
        self.assertIn("RED", out)
        # Started for a worktree but the cwd is somewhere else: refuse, never create.
        rc, out = run_wrapper(CLAUDE_MISS, Env.B, {"PRIMARY_REPO": Env.B, "MISS_WORKTREE": self.wtB})
        self.assertNotEqual(rc, 0)
        self.assertIn("not that worktree", out)
        # The classic Case B (no worktree, no TTY, no name) also refuses to create.
        rc, out = run_wrapper(CLAUDE_MISS, Env.B, {"PRIMARY_REPO": Env.B, "BASE_BRANCH": "master"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(self._worktree_count(), before)

    def test_integrator_resolves_repo_from_the_worktree_cwd(self):
        # No PRIMARY_REPO at all, cwd = a feature worktree of B: the repo is B (the
        # old code fell back to ~/mission-dashboard here). B stages on master.
        rc, out = run_wrapper(INTEGRATOR, self.wtB, {})
        self.assertEqual(rc, 0, out)
        self.assertIn("Repo: " + Env.B, out)
        self.assertIn("on staging 'master' (%s)" % Env.B, out)
        self.assertNotIn("mission-dashboard", out)

    def test_integrator_has_no_default_repo(self):
        # Outside every git repo with no PRIMARY_REPO: refuse, never guess a repo.
        rc, out = run_wrapper(INTEGRATOR, Env.tmp, {})
        self.assertNotEqual(rc, 0)
        self.assertIn("no default repo", out)
        self.assertNotIn("Repo: ", out)

    def test_integrator_uses_the_recorded_integration_checkout(self):
        app = Env.app
        git(Env.C, "branch", "working", check=False)
        iwt, err = app.ensure_integration_worktree(Env.C, "working")
        self.assertIsNone(err)
        rc, out = run_wrapper(INTEGRATOR, Env.tmp,
                              {"PRIMARY_REPO": Env.C, "BASE_BRANCH": "working",
                               "INTEGRATION_WORKTREE": iwt})
        self.assertEqual(rc, 0, out)
        self.assertIn("on staging 'working' (%s)" % iwt, out)
        # Without the recorded path it still finds the checkout holding `working`.
        rc, out = run_wrapper(INTEGRATOR, Env.tmp, {"PRIMARY_REPO": Env.C, "BASE_BRANCH": "working"})
        self.assertEqual(rc, 0, out)
        self.assertIn("(%s)" % iwt, out)
        # A checkout of the WRONG repo is refused even if it is on a `working` branch.
        rc, out = run_wrapper(INTEGRATOR, Env.tmp,
                              {"PRIMARY_REPO": Env.B, "BASE_BRANCH": "working",
                               "INTEGRATION_WORKTREE": iwt})
        self.assertNotEqual(rc, 0)
        self.assertIn("not a checkout of " + Env.B, out)

    def test_integrator_tolerates_a_dirty_checkout_and_reports_it(self):
        with open(os.path.join(Env.A, "dirty.txt"), "w") as fh:
            fh.write("x\n")
        try:
            rc, out = run_wrapper(INTEGRATOR, Env.A, {"PRIMARY_REPO": Env.A, "BASE_BRANCH": "working"})
            self.assertEqual(rc, 0, out)
            self.assertIn("YELLOW", out)
            self.assertIn("uncommitted", out)
        finally:
            os.remove(os.path.join(Env.A, "dirty.txt"))

    def test_role_context_reports_recorded_identity_after_clear(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MISS_")}
        env.update({"CLAUDE_MISS_ROLE": "feature", "MISS_REPO_ROOT": Env.B,
                    "MISS_REPO_ID": "frontend-abc", "MISS_INTEGRATION_BRANCH": "master",
                    "MISS_INTEGRATION_WORKTREE": Env.B, "MISS_PREVIEW_PORT": "24555"})
        out = subprocess.run([sys.executable, ROLE_CTX], cwd=self.wtB, capture_output=True,
                             text=True, env=env).stdout
        self.assertIn("Repo (main checkout):  " + Env.B, out)
        self.assertIn("Branch here:           claude/wrap-b", out)
        self.assertIn("Integration branch:    master", out)
        self.assertIn("24555", out)
        self.assertIn("Do not\n`git worktree add`", out)
        self.assertIn("FEATURE WORKER", out)
        env["CLAUDE_MISS_ROLE"] = "integrator"
        out = subprocess.run([sys.executable, ROLE_CTX], cwd=Env.B, capture_output=True,
                             text=True, env=env).stdout
        self.assertIn("INTEGRATOR", out)
        self.assertIn("Branch here:           master", out)

    def test_ship_block_travels_with_every_feature_session(self):
        """One approval, one script — and no session is told to use a subagent."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("MISS_")}
        env.update({"CLAUDE_MISS_ROLE": "feature", "MISS_REPO_ROOT": Env.B,
                    "MISS_WORKTREE": self.wtB, "MISS_INTEGRATION_BRANCH": "master"})
        out = subprocess.run([sys.executable, ROLE_CTX], cwd=self.wtB, capture_output=True,
                             text=True, env=env).stdout
        self.assertIn("== SHIP —", out)
        self.assertIn("YES SHIP", out)
        self.assertIn("miss-ship.py", out)
        self.assertIn("not its stages", out)
        # nothing about the retired delegation path survives in a session's context
        for gone in ("miss-integrator", "Agent(", "ticket", "ready for integrator"):
            self.assertNotIn(gone, out)
        self.assertLess(out.index("SESSION IDENTITY"), out.index("== SHIP —"))
        self.assertIn("FEATURE WORKER", out)         # the rails still follow
        # The dashboard repo itself keeps its rails in CLAUDE.md but still gets SHIP.
        out = subprocess.run([sys.executable, ROLE_CTX], cwd=ROOT, capture_output=True,
                             text=True, env=env).stdout
        self.assertIn("== SHIP —", out)
        self.assertNotIn("FEATURE WORKER", out)
        # The integrator console never gets the feature worker's ship instructions.
        env.update({"CLAUDE_MISS_ROLE": "integrator", "MISS_INTEGRATION_WORKTREE": Env.B})
        out = subprocess.run([sys.executable, ROLE_CTX], cwd=Env.B, capture_output=True,
                             text=True, env=env).stdout
        self.assertNotIn("== SHIP —", out)
        self.assertIn("INTEGRATOR", out)

    def test_role_context_carries_the_status_block_per_role_and_self_quiets(self):
        """The reply format travels with BOTH roles — and only where the repo lacks it."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("MISS_")}
        env.update({"MISS_REPO_ROOT": Env.B, "MISS_INTEGRATION_BRANCH": "master"})

        def ctx(role, cwd):
            env["CLAUDE_MISS_ROLE"] = role
            return subprocess.run([sys.executable, ROLE_CTX], cwd=cwd, capture_output=True,
                                  text=True, env=env).stdout

        for role, cwd in (("feature", self.wtB), ("integrator", Env.B)):
            out = ctx(role, cwd)
            self.assertIn("== OUTPUT STYLE", out)
            for head in ("STATUS:", "WHAT MATTERS:", "NEXT STEP:", "NEEDS APPROVAL:"):
                self.assertIn(head, out)
            for value in ("SHIPPED", "BLOCKED"):
                self.assertIn(value, out)
            # Brevity never softens the phrases, and every pointer at the block names it
            # the way the block itself does (no dangling "OUTPUT STYLE block").
            self.assertIn("EXACTLY", out)
            self.assertIn("STATUS block", out)
            self.assertNotIn("OUTPUT STYLE block", out)

        # Self-quieting: a repo whose own CLAUDE.md documents the rails gets the identity
        # (+ the workflow) only — and still nothing pointing at a block it was not given.
        quiet_repo = os.path.join(Env.tmp, "documented-repo")
        os.makedirs(quiet_repo, exist_ok=True)
        with open(os.path.join(quiet_repo, "CLAUDE.md"), "w") as fh:
            fh.write("commit only after YES COMMIT\n")
        quiet = ctx("feature", quiet_repo)
        self.assertIn("SESSION IDENTITY", quiet)
        self.assertNotIn("== OUTPUT STYLE", quiet)
        self.assertNotIn("FEATURE WORKER", quiet)
        self.assertNotIn("OUTPUT STYLE block", quiet)

    def test_claude_miss_launches_guarded_and_without_extra_agents(self):
        """A stub `claude` on PATH records its argv: --settings + the guard, and no
        --agents (the ship path is a script the worker runs, not a subagent)."""
        stub_dir = tempfile.mkdtemp(prefix="stub-")
        argv_file = os.path.join(stub_dir, "argv")
        with open(os.path.join(stub_dir, "claude"), "w") as fh:
            fh.write("#!/bin/bash\nprintf '%s\\0' \"$@\" > \"$STUB_OUT\"\n")
        os.chmod(os.path.join(stub_dir, "claude"), 0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MISS_")}
        env.update({"PATH": stub_dir + os.pathsep + env.get("PATH", ""), "STUB_OUT": argv_file,
                    "CLAUDE_MISS_ROLE": "feature", "PRIMARY_REPO": Env.B, "BASE_BRANCH": "master",
                    "WORKTREES_DIR": Env.worktrees, "MISS_WORKTREE": self.wtB,
                    "MISS_REPO_ROOT": Env.B, "MISS_FEATURE_BRANCH": "claude/wrap-b"})
        subprocess.run(["bash", CLAUDE_MISS], cwd=self.wtB, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
        with open(argv_file) as fh:
            argv = fh.read().split("\0")
        self.assertIn("--settings", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--agents", argv)
        shutil.rmtree(stub_dir, ignore_errors=True)

class SpawnRoute(unittest.TestCase):
    """The /spawn POST over real HTTP against the imported app, temp dirs."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Env.app.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, **fields):
        body = urllib.parse.urlencode(fields)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("POST", "/spawn", body,
                     {"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, resp.getheader("Location") or "", data

    def meta(self, name):
        with open(os.path.join(Env.missions, name, "mission.json")) as fh:
            return json.load(fh)

    def test_feature_missions_in_two_repos_record_full_identity(self):
        app = Env.app
        st, loc, _ = self.post(mode="dev", kind="local-repo", path=Env.B, name="sp-b")
        self.assertEqual((st, loc), (303, "/m/sp-b/dashboard"))
        d = self.meta("sp-b")["dev"]
        self.assertEqual(d["repo"], Env.B)
        self.assertEqual(d["base_branch"], "master")          # auto-detected, not `working`
        self.assertEqual(d["branch"], "claude/sp-b")
        self.assertEqual(d["repo_id"], app.repo_id_of(Env.B))
        self.assertEqual(d["integration_worktree"], Env.B)
        self.assertEqual(d["role"], "feature")
        self.assertEqual(d["preview_port"], app.preview_port_for("sp-b"))
        self.assertEqual(app.repo_root_of(d["worktree"]), Env.B)
        st, loc, _ = self.post(mode="dev", kind="local-repo", path=Env.A, name="sp-a")
        self.assertEqual(st, 303)
        d = self.meta("sp-a")["dev"]
        self.assertEqual((d["repo"], d["base_branch"]), (Env.A, "working"))
        # Same name again, other repo: refused (the worktree belongs to B).
        st, _, page = self.post(mode="dev", kind="local-repo", path=Env.A, name="sp-b")
        self.assertEqual(st, 200)
        self.assertIn("already exists", page)

    def test_integrator_mission_records_the_integration_checkout(self):
        app = Env.app
        st, loc, page = self.post(mode="dev", kind="local-repo", path=Env.B, role="integrator",
                                  name="int-b")
        self.assertEqual((st, loc), (303, "/m/int-b/dashboard"), page[:300])
        m = self.meta("int-b")
        d = m["dev"]
        self.assertEqual(d["role"], "integrator")
        self.assertEqual(d["repo"], Env.B)
        self.assertEqual(d["base_branch"], "master")
        self.assertEqual(d["integration_worktree"], Env.B)
        self.assertNotIn("worktree", d)
        self.assertFalse(os.path.exists(os.path.join(Env.worktrees, "int-b")))
        self.assertIn("integrator", app.dev_badge("int-b"))
        self.assertEqual(app.mission_location("int-b"), (None, Env.B))
        self.assertNotIn("int-b", app.merged_dev_missions())
        # Integrator for a repo whose staging branch is checked out nowhere: a
        # dedicated checkout is created and recorded.
        git(Env.C, "branch", "working", check=False)
        st, _, page = self.post(mode="dev", kind="local-repo", path=Env.C, role="integrator",
                                base="working", name="int-c")
        self.assertEqual(st, 303, page[:300])
        d = self.meta("int-c")["dev"]
        self.assertTrue(d["integration_worktree"].startswith(os.path.join(Env.worktrees, ".integration")))
        self.assertEqual(app.repo_root_of(d["integration_worktree"]), Env.C)
        # Remote integrators are refused up front, and a non-repo dir too.
        st, _, page = self.post(mode="dev", kind="remote-repo", host="nowhere", dir="/x",
                                role="integrator", name="int-r")
        self.assertEqual(st, 200)
        self.assertIn("not supported yet", page)
        st, _, page = self.post(mode="dev", kind="local-repo", path=Env.tmp, role="integrator",
                                name="int-x")
        self.assertIn("not a git repository", page)

    def test_identity_survives_a_restart(self):
        """A fresh interpreter (= a restarted dashboard) reads the same identity back
        from disk; the console launcher's env comes from the same file."""
        self.post(mode="dev", kind="local-repo", path=Env.B, name="persist-b")
        code = ("import app, json; d = app.dev_identity('persist-b'); "
                "print(json.dumps([d['repo'], d['branch'], d['base_branch'], d['integration_worktree']]))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                             text=True, env=dict(os.environ)).stdout
        self.assertEqual(json.loads(out), [Env.B, "claude/persist-b", "master", Env.B])
        script = ('eval "$(python3 %s %s)"; echo "$MISS_REPO_ROOT|$MISS_FEATURE_BRANCH|$MISS_INTEGRATION_BRANCH"'
                  % (MISSION_ENV, os.path.join(Env.missions, "persist-b", "mission.json")))
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "%s|claude/persist-b|master" % Env.B)


if __name__ == "__main__":
    unittest.main()
