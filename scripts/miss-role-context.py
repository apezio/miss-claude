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

MAX_ROUNDS = 3   # keep in step with scripts/miss-agents.py


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
- The operator runs many sessions and is not a git expert: keep git talk plain,
  and always end with the one safe next step.

Approval phrases — the operator must type these EXACTLY (vague approval like
"ok"/"do it"/"go ahead" is NEVER enough; ask for the exact phrase):
- YES COMMIT  -> you may `git add <explicit paths>` + `git commit` your own changes
  (never `git add .`/`-A`; state files + message when you ask).
- YES REBASE  -> you may rebase this branch onto current {base}.

Open every session with a STATUS block: GREEN/YELLOW/RED, one sentence (role,
clean/dirty, current-with-{base}/behind), then WHAT CHANGED, then SAFE NEXT STEP.
When the work is committed and ready, tell the operator: "ready for integrator".
"""

WORKFLOW = """\
== MISS CLAUDE WORKFLOW — implement -> review -> fix -> re-review ==
This session has three specialist subagents (defined per session via `claude
--agents`, see scripts/miss-agents.py): miss-implementer, miss-reviewer,
miss-architect. Every one runs in a FRESH, ISOLATED context — it knows only what you
put in its prompt — and is bound by the same hard guard as you. YOU are the
orchestrator: gather context, delegate, judge, report. Do not write the feature
code in your own context.

For a normal feature request or bug fix (anything beyond a trivial one-file edit
with no behaviour change), do this automatically — do not ask permission for it:
1. Understand the request; read just enough of the repo to write a precise task
   statement (goal, acceptance criteria, files likely involved, what is OUT of scope).
2. Architect ONLY IF NEEDED: the task is unclear, touches several components, or
   needs a design decision -> Agent(miss-architect) with the task statement; use its
   plan. Skip it for well-understood changes.
3. Agent(miss-implementer) with the task statement (+ plan). Its report is data.
4. Agent(miss-reviewer) with ONLY the task statement and the diff scope
   (`git diff` in {worktree}) — never the implementer's report or reasoning, so the
   review is independent. It returns VERDICT + FINDINGS.
5. If CHANGES_REQUIRED: Agent(miss-implementer) with the task + the findings to fix,
   then a NEW Agent(miss-reviewer) round on the resulting diff. Repeat up to
   {rounds} review rounds in total. Always a new subagent per round — never reuse one.
6. Verify what you can yourself (syntax check, tests), then report to the operator:
   what changed, the final verdict, any findings you or the implementer disagreed
   with, and the one safe next step (usually: ask for YES COMMIT).
The subagents cannot commit, and neither can you without YES COMMIT; nothing in this
loop changes the approval phrases, the worktree/integrator/release rules, or the
guard.
"""

INTEGRATOR = """\
== MISS CLAUDE DEV RAILS — you are the INTEGRATOR ==

You review finished claude/* branches of THIS repo and integrate them into {base}
in THIS checkout. You do NOT write feature code (hard-enforced by a PreToolUse hook).
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
changed files are expected.{reviewer} Keep git talk plain; always end with the one safe next
step.
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
    if role == "feature" and env("MISS_AGENTS_ATTACHED", "").strip():
        # The workflow travels with EVERY feature session that has the specialists
        # attached (the launchers set MISS_AGENTS_ATTACHED after `--agents`), including
        # this repo's own (its CLAUDE.md carries the rails but not the subagent loop).
        # A session launched without them is never told to use agents it lacks.
        sys.stdout.write("\n" + WORKFLOW.format(
            worktree=env("MISS_WORKTREE", "").strip() or cwd, rounds=MAX_ROUNDS))
    if repo_documents_rails(cwd):
        return
    names = [base] + [b for b in ("main", "master") if b != base]
    protected = ", ".join(names[:-1]) + (", or " if len(names) > 2 else " or ") + names[-1]
    if role == "integrator":
        reviewer = ""
        if env("MISS_AGENTS_ATTACHED", "").strip():
            reviewer = (" For a real review of a branch's diff vs %s, delegate to\n"
                        "the miss-reviewer subagent (fresh, isolated context) with the branch\n"
                        "name and the mission's goal — it returns VERDICT + concrete findings;\n"
                        "you weigh them and never fix code yourself." % base)
        sys.stdout.write("\n" + INTEGRATOR.format(base=base, reviewer=reviewer))
    else:
        sys.stdout.write("\n" + FEATURE.format(
            base=base, protected=protected,
            port_hint=(" (%s)" % port) if port else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
