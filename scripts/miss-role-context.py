#!/usr/bin/env python3
"""SessionStart hook: inject the Miss Claude dev-workflow rails + this session's
repo identity into Claude's context.

Why: the behavioural rails (roles, the exact YES ... approval phrases, the stoplight)
live in the mission-dashboard repo's own CLAUDE.md, so a dev mission developing ANY
OTHER repo never showed them to Claude at all — the console banner is printed before
Claude starts and is human-visible only. This hook closes that gap: at session start
it prints a compact role block, which Claude Code adds to the model's context
(SessionStart stdout is injected as context, like CLAUDE.md).

It ALSO prints the session's recorded identity — repo root/id, worktree, feature
branch, integration branch + the checkout that holds it, preview port — read from the
MISS_* environment the console launcher exported from mission.json (see
scripts/mission-env.py). SessionStart fires again after /clear, so this block is what
a Claude with no conversational memory gets to know which repo/branch it is in.

Wired by console-hooks-dev.settings.json (local dev/integrator consoles) and
miss-rails.settings.json (remote dev consoles; shipped by scripts/ship-rails.sh).

Self-quieting: if the working directory's repo already documents the workflow (its
CLAUDE.md mentions the approval phrases — i.e. the mission-dashboard repo itself),
only the identity block is printed, so Claude-Miss sessions don't carry the rules twice.

Exit 0 always: a context nudge must never break a session.
"""

import os
import subprocess
import sys


def repo_documents_rails(cwd):
    """True when <cwd>/CLAUDE.md already carries the approval-phrase workflow."""
    try:
        with open(os.path.join(cwd, "CLAUDE.md"), encoding="utf-8", errors="replace") as fh:
            return "YES COMMIT" in fh.read()
    except OSError:
        return False


def git_branch(cwd):
    """The branch actually checked out in <cwd>, or None."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


IDENTITY = """\
== SESSION IDENTITY (recorded by the dashboard; re-injected after /clear) ==
Role:                  {role}
Repo (main checkout):  {repo}{repo_id}
Working directory:     {cwd}
Branch here:           {branch}
Integration branch:    {base}{integration_wt}{preview}
Everything git-related in this session refers to THAT repo and THAT branch. Do not
`git worktree add` anything — you are already in your checkout. Do not act on any
other repository from here.
"""

FEATURE = """\
== MISS CLAUDE DEV RAILS — you are a FEATURE WORKER ==

Rules (hard-enforced by a PreToolUse hook; do not fight it):
- Edit code ONLY inside this worktree. Never touch the primary checkout, other
  worktrees, or {protected}. Never push, merge, deploy, or restart services.
- Stay on your claude/* branch (no checkout/switch away).
- Dev/preview servers: never start the repo's canonical dev server from this
  worktree. If you need one, bind it to YOUR port{port_hint} so it is unmistakably
  this worktree's; the canonical staging server runs from the integration checkout.

Approval phrases — the operator must type these EXACTLY (vague approval like
"ok"/"do it"/"go ahead" is NEVER enough; ask for the exact phrase):
- YES SHIP    -> ship this finished change: commit, integrate, and whatever release /
  deploy / verify steps this repo already defines. This is the ONLY phrase normal
  shipping needs — never ask for a second one for a later stage (see SHIP).
- YES COMMIT  -> commit mid-work, without shipping (`git add <explicit paths>` +
  `git commit`; never `git add .`/`-A`; state files + message when you ask).
- YES REBASE  -> rebase this branch onto current {base} (only when it has fallen
  behind — not a shipping step).

Every reply ends with the STATUS block below, the first of a session included.
"""

OUTPUT_STYLE = """\
== OUTPUT STYLE — short, plain, decision-first ==
Every reply to the operator ends with this STATUS block, and little else:
  STATUS:
  GREEN / YELLOW / RED / SHIPPED / BLOCKED
  WHAT MATTERS:
  1-3 short bullets, plain English.
  NEXT STEP:
  What the operator types or does next, or None.
  NEEDS APPROVAL:
  Only when a phrase is actually required.
GREEN continue · YELLOW needs an approval phrase · RED stop and ask · SHIPPED done ·
BLOCKED stopped, nothing changed. Aim at 5-10 lines, fewer for routine success.
Implementation detail, command output, diffs and file lists belong in the mission LOG —
put them in a reply only when something failed, there is real risk, the operator must
choose between options, or they asked. Brevity never removes safety: approval phrases
stay exact and required, RED still means stop, and any real risk or irreversible step
is said — in one short line.
"""

SHIP = """\
== SHIP — ONE approval covers the whole shipment ==
When the work is done and verified, ask for ONE phrase and nothing else:
  NEEDS APPROVAL? YES SHIP
That single approval covers this change end to end — commit, integrate, push, and
(only where this repo already defines them) release, deploy, verify. Do NOT ask for
YES COMMIT / YES INTEGRATE / YES PUSH / YES RELEASE / YES DEPLOY afterwards; the
operator approves the shipment, not its stages.
Once the operator has typed exactly YES SHIP:
1. Commit your work (`git add <explicit paths>` + `git commit`; never `git add -A`).
2. Run the ship script — it does every remaining step itself, in order, checking git
   before each one and skipping whatever is already done:
     python3 {ship} --approval "YES SHIP" \\
         --request "<the request, in plain words>" --tests "<checks you ran + results>"
3. Report its last line in your STATUS block, and stop:
     RESULT: SHIPPED          -> STATUS: SHIPPED, one line of what is live, NEXT STEP: None
     RESULT: BLOCKED          -> STATUS: BLOCKED, its reason; nothing changed
     RESULT: NEEDS_ATTENTION  -> STATUS: RED; say what shipped and what did not
   If it was interrupted, run the SAME command again — it resumes at the first
   incomplete step and never repeats a push, release or deployment.
Steps are the repo's own; the script never invents a release or a deploy, and a repo
that defines none simply stops after the last established step. You still cannot
integrate, push or deploy by hand — the guard blocks that, and the script is the path.
"""

INTEGRATOR = """\
== MISS CLAUDE DEV RAILS — you are the INTEGRATOR ==

You review finished claude/* branches of THIS repo and integrate them into {base}
in THIS checkout. Normal shipping does NOT come through you — a feature worker ships
its own branch after one YES SHIP. This console is for hand-driven and recovery work:
a branch that needs a human eye, a half-finished shipment, a release done by hand.
You do NOT write feature code (hard-enforced by a PreToolUse hook).
Fast-forward only; never force-push, never rebase, no non-ff merges. Never integrate
into or from another repository.

Approval phrases — the operator must type these EXACTLY (vague approval is never
enough):
- YES INTEGRATE    -> fast-forward (--ff-only) a reviewed claude/* branch into {base}.
- YES PUSH WORKING -> push {base} to origin (if a remote exists).
- YES RELEASE      -> move the deploy branch forward to {base}.
- YES DEPLOY       -> restart the service to load released code.

Public releases (a separate release checkout on GitHub): NEVER run git against that
checkout by hand — use `scripts/make-release.sh --dry-run` to preview and
`scripts/make-release.sh --push` to publish (the hook blocks hand-run git there).

Before integrating: confirm the branch is clean, based on current {base}, and its
changed files are expected.
"""


def main():
    cwd = os.getcwd()
    env = os.environ.get
    role = env("CLAUDE_MISS_ROLE", "").strip().lower()
    role = "integrator" if role == "integrator" else "feature"
    repo = env("MISS_REPO_ROOT", "").strip() or env("PRIMARY_REPO", "").strip() or "(unknown repo)"
    repo_id = env("MISS_REPO_ID", "").strip()
    base = env("MISS_INTEGRATION_BRANCH", "").strip() or env("BASE_BRANCH", "").strip() or "working"
    iwt = env("MISS_INTEGRATION_WORKTREE", "").strip() or env("INTEGRATION_WORKTREE", "").strip()
    port = env("MISS_PREVIEW_PORT", "").strip()
    # The branch is what git says is checked out HERE; the recorded feature branch is
    # the fallback (never a guess from the directory name).
    branch = git_branch(cwd) or env("MISS_FEATURE_BRANCH", "").strip() or "(unknown)"
    sys.stdout.write(IDENTITY.format(
        role=role, repo=repo, repo_id=(" (id %s)" % repo_id) if repo_id else "",
        cwd=cwd, branch=branch, base=base,
        integration_wt=("\nIntegration checkout:  %s" % iwt) if iwt else "",
        preview=("\nPreview/dev port:      %s (yours; leave the repo's default port alone)" % port)
        if port else "",
    ))
    if role == "feature":
        # SHIP travels with every feature session: the whole path is one script sitting
        # next to this one (locally in scripts/, on a remote in ~/.miss-claude/), so
        # there is nothing to attach at launch and nothing that can be missing.
        sys.stdout.write("\n" + SHIP.format(
            ship=os.path.join(os.path.dirname(os.path.abspath(__file__)), "miss-ship.py")))
    if repo_documents_rails(cwd):
        return
    names = [base] + [b for b in ("main", "master") if b != base]
    protected = ", ".join(names[:-1]) + (", or " if len(names) > 2 else " or ") + names[-1]
    if role == "integrator":
        sys.stdout.write("\n" + INTEGRATOR.format(base=base))
    else:
        sys.stdout.write("\n" + FEATURE.format(
            base=base, protected=protected,
            port_hint=(" (%s)" % port) if port else ""))
    # The reply format travels with both roles — but only where the repo's own CLAUDE.md
    # does not already carry it (repo_documents_rails returned above), so no session pays
    # for the same rules twice.
    sys.stdout.write("\n" + OUTPUT_STYLE)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
