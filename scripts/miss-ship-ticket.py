#!/usr/bin/env python3
"""miss-ship-ticket.py — write the scoped SHIP delegation for a committed feature branch.

    python3 scripts/miss-ship-ticket.py --approval "YES SHIP" \\
        --request "<the mission's request>" --tests "<checks run + results>" \\
        --review "<reviewer verdict + summary, or 'none (TINY)'>"

Run by the FEATURE WORKER after the operator typed YES SHIP. It integrates nothing.
It records — from git, never from the worker's say-so — exactly what was approved:
repo, branch, the branch's commit + tree, the integration branch and checkout, the
remote (if any) and the repo's release/deploy/verify commands (if defined), and writes
that as a ticket the guard hook reads. The `miss-integrator` SUBAGENT the worker then
spawns is the only party the hook grants integrator power to, and only for what the
ticket names: fast-forward THAT branch at THAT commit into THAT base, push THAT base,
run THOSE release/deploy/verify commands, each re-verified against git right before it
runs (drift => blocked => NEEDS_ATTENTION). The feature worker itself (no agent_type in
the hook input) keeps its feature-only rules — it cannot use the ticket.

Pre-checks (exit 1 + "BLOCKED: ..." and NO ticket): worktree not clean, branch not a
claude/* branch, nothing to integrate, branch behind base (=> YES REBASE), integration
checkout missing / on another branch / dirty (tracked files).

Release/deploy/verify come from a ship config — `~/.miss-claude/ship.json` (env
MISS_SHIP_CONFIG), a JSON object keyed by the repo's realpath:
  {"/path/to/repo": {"release_branch": "main",
      "release": ["git -C /path/to/repo push . working:main"],
      "deploy":  ["sudo systemctl restart mission-dashboard.service"],
      "verify":  ["curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:4200/"]}}
A repo with no entry ships as far as the established steps go — integrate, and push if
it has a remote — and stops there; nothing is invented. Miss Claude's own repo (the one
with mission-dashboard.service next to app.py) gets the entry above as a built-in
default when the config has none.

Ticket location: `~/.miss-claude/tickets/<repo_id>--<branch slug>.json` (env
MISS_TICKET_DIR) — outside every repo, same on a remote host (ship-rails ships this
script). The integrator's step log goes next to it (`<same>.log`); the ticket also
carries `mission_dir` so the log is mirrored into the mission when one exists.
Tickets expire after TTL_HOURS. Standard library only.
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
TTL_HOURS = 3
BRANCH_RE = re.compile(r"^claude/[A-Za-z0-9._-]+\Z")


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)
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


def ticket_dir():
    return os.environ.get("MISS_TICKET_DIR", "").strip() or os.path.expanduser("~/.miss-claude/tickets")


def ticket_path(repo_id, branch):
    return os.path.join(ticket_dir(), "%s--%s.json" % (repo_id, branch.replace("/", "_")))


def staging_checkout(repo, branch):
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
    """(release_branch, release_cmds, deploy_cmds, verify_cmds) for <repo>, or empties."""
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
        # Miss Claude itself: the established release + deploy + check, as CLAUDE.md
        # documents them (integrator-only steps, now delegated by the ticket).
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


def load_ticket(path):
    """The ticket dict if present, well-formed and not expired; else (None, why)."""
    try:
        with open(path) as fh:
            t = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "no ticket at %s (%s)" % (path, exc.__class__.__name__)
    need = ("approval", "repo", "branch", "commit", "base", "integration_worktree", "created")
    if not isinstance(t, dict) or any(not t.get(k) for k in need):
        return None, "malformed ticket %s" % path
    if t.get("approval") != APPROVAL:
        return None, "ticket %s lacks the %s approval" % (path, APPROVAL)
    if time.time() - float(t.get("created", 0)) > TTL_HOURS * 3600:
        return None, "ticket %s expired (older than %dh)" % (path, TTL_HOURS)
    return t, ""


def main():
    p = argparse.ArgumentParser(description="write the YES SHIP delegation ticket")
    p.add_argument("--approval", default="")
    p.add_argument("--request", default="")
    p.add_argument("--tests", default="")
    p.add_argument("--review", default="")
    p.add_argument("--show", action="store_true", help="print the current ticket and exit")
    a = p.parse_args()
    env = os.environ

    def blocked(msg):
        print("BLOCKED: " + msg)
        sys.exit(1)

    worktree = env.get("MISS_WORKTREE", "").strip() or os.getcwd()
    repo = env.get("MISS_REPO_ROOT", "").strip() or env.get("PRIMARY_REPO", "").strip() or repo_root_of(worktree)
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        blocked("cannot determine this mission's repo (MISS_REPO_ROOT/PRIMARY_REPO unset; %s is not a checkout)" % worktree)
    if os.path.realpath(repo_root_of(worktree) or "") != os.path.realpath(repo):
        blocked("%s is not a checkout of the declared repo %s" % (worktree, repo))
    rc, branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if rc or branch in ("", "HEAD"):
        blocked("the feature worktree is not on a branch")
    recorded = env.get("MISS_FEATURE_BRANCH", "").strip()
    if recorded and recorded != branch:
        blocked("worktree is on '%s' but the mission's recorded branch is '%s'" % (branch, recorded))
    if not BRANCH_RE.match(branch):
        blocked("only claude/* feature branches ship; this is '%s'" % branch)
    repo_id = env.get("MISS_REPO_ID", "").strip() or repo_id_of(repo)
    path = ticket_path(repo_id, branch)

    if a.show:
        t, why = load_ticket(path)
        print(json.dumps(t, indent=1) if t else "no valid ticket: " + why)
        sys.exit(0 if t else 1)
    if a.approval.strip() != APPROVAL:
        blocked('the operator must type exactly "%s" first; then pass --approval "%s"' % (APPROVAL, APPROVAL))

    base = env.get("MISS_INTEGRATION_BRANCH", "").strip() or env.get("BASE_BRANCH", "").strip()
    if not base:
        rc, _ = git(repo, "show-ref", "--verify", "--quiet", "refs/heads/working")
        base = "working" if rc == 0 else ""
    if not base:
        blocked("no integration branch recorded and no 'working' branch in %s" % repo)
    rc, _ = git(repo, "show-ref", "--verify", "--quiet", "refs/heads/" + base)
    if rc:
        blocked("integration branch '%s' does not exist in %s" % (base, repo))
    rc, dirty = git(worktree, "status", "--porcelain")
    if rc or dirty:
        blocked("the feature worktree has uncommitted changes — commit first (YES SHIP covers the commit)")
    rc, counts = git(repo, "rev-list", "--left-right", "--count", "%s...%s" % (branch, base))
    ahead, behind = (counts.split() + ["?", "?"])[:2] if not rc else ("?", "?")
    if ahead == "0":
        blocked("nothing to ship: %s has no commits beyond %s" % (branch, base))
    rc, _ = git(repo, "merge-base", "--is-ancestor", base, branch)
    if rc:
        blocked("%s is behind %s by %s commit(s) and cannot fast-forward — rebase first (YES REBASE), then YES SHIP again"
                % (branch, base, behind))
    iwt = env.get("MISS_INTEGRATION_WORKTREE", "").strip() or env.get("INTEGRATION_WORKTREE", "").strip() \
        or staging_checkout(repo, base)
    if not iwt or not os.path.isdir(iwt):
        blocked("no checkout has '%s' checked out — spawn an Integrator mission for this repo once, or check it out by hand" % base)
    if os.path.realpath(repo_root_of(iwt) or "") != os.path.realpath(repo):
        blocked("integration checkout %s is not a checkout of %s" % (iwt, repo))
    rc, ib = git(iwt, "rev-parse", "--abbrev-ref", "HEAD")
    if rc or ib != base:
        blocked("integration checkout %s is on '%s', not '%s' — operator must fix by hand" % (iwt, ib, base))
    rc, idirty = git(iwt, "status", "--porcelain", "--untracked-files=no")
    if rc or idirty:
        blocked("integration checkout %s has uncommitted changes to tracked files — operator must look" % iwt)

    rc, commit = git(worktree, "rev-parse", "HEAD")
    rc2, tree = git(worktree, "rev-parse", "HEAD^{tree}")
    if rc or rc2:
        blocked("cannot read the branch's commit")
    rc, remotes = git(repo, "remote")
    remote = "origin" if (not rc and "origin" in remotes.split()) else ((remotes.split() or [""])[0] if not rc else "")
    release_branch, release, deploy, verify = ship_config(repo, base)
    if release and not release_branch:
        blocked("ship config for %s has release commands but no release_branch" % repo)

    ticket = {
        "approval": APPROVAL, "created": time.time(), "id": "%s--%s@%s" % (repo_id, branch, commit[:12]),
        "mission": env.get("MISSION_NAME", "").strip(), "mission_dir": env.get("MISSION_DATA_DIR", "").strip(),
        "repo": os.path.realpath(repo), "repo_id": repo_id, "worktree": worktree,
        "branch": branch, "commit": commit, "tree": tree, "ahead": int(ahead),
        "base": base, "integration_worktree": os.path.realpath(iwt), "remote": remote,
        "release_branch": release_branch, "release": release, "deploy": deploy, "verify": verify,
        "request": a.request, "tests": a.tests, "review": a.review,
        "log": os.path.splitext(path)[0] + ".log",
    }
    os.makedirs(ticket_dir(), mode=0o700, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(ticket, fh, indent=1)
    steps = ["integrate: fast-forward %s -> %s in %s" % (branch, base, iwt)]
    steps.append(("push: %s -> %s/%s" % (base, remote, base)) if remote else "push: (no remote — skipped)")
    steps.append(("release: %s" % " ; ".join(release)) if release else "release: (not defined for this repo — stop after the last step above)")
    if deploy:
        steps.append("deploy: %s" % " ; ".join(deploy))
    if verify:
        steps.append("verify: %s" % " ; ".join(verify))
    print("TICKET: " + path)
    print("SHIP DELEGATION for the miss-integrator subagent (paste verbatim into its prompt):")
    print("  ticket:  %s" % path)
    print("  log:     %s" % ticket["log"])
    print("  repo:    %s  (id %s)" % (ticket["repo"], repo_id))
    print("  branch:  %s at %s (tree %s), %s commit(s) over %s" % (branch, commit[:12], tree[:12], ahead, base))
    print("  base:    %s in %s" % (base, ticket["integration_worktree"]))
    for s in steps:
        print("  " + s)
    print("  request: %s" % (a.request or "(none given)"))
    print("  tests:   %s" % (a.tests or "(none given)"))
    print("  review:  %s" % (a.review or "(none given)"))


if __name__ == "__main__":
    main()
