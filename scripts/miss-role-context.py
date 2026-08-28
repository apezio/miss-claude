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
When the work is done and verified, ask for YES SHIP (see SHIP below); only if that
path is unavailable, commit after YES COMMIT and say "ready for integrator".
"""

WORKFLOW = """\
== MISS CLAUDE WORKFLOW — pick a level, then implement -> review -> fix -> re-review ==
This session has three specialist subagents (defined per session via `claude
--agents`, see scripts/miss-agents.py): miss-implementer, miss-reviewer,
miss-architect. Every one runs in a FRESH, ISOLATED context — it knows only what you
put in its prompt — and is bound by the same hard guard as you. YOU are the
orchestrator: gather context, classify, delegate, judge, report. Do not write the
feature code in your own context.

First, understand the request and read just enough of the repo to write a precise
task statement (goal, acceptance criteria, files likely involved, what is OUT of
scope). Then CLASSIFY it yourself — the operator never picks a mode or a command —
into one of three levels, and say which one you chose and why in one line:
  TINY    — mechanical, one or two files, no behaviour change or a trivially
            verifiable one (typo, rename, comment, config value, log line, a
            one-line obvious fix).
  NORMAL  — an ordinary feature or bug fix: real behaviour changes, more than a
            couple of files, or anything a second pair of eyes would catch.
  COMPLEX — high-risk: broad refactors; auth/security; billing; migrations or data
            integrity; concurrency; networking; release/deployment tooling;
            destructive operations; or changes spanning several subsystems.
When torn between TINY and NORMAL, take the cheaper one — but escalate when
correctness risk warrants it. Cost matters: the loop below is worth its tokens on
NORMAL work and wasted on TINY work.

Then run the level's workflow automatically (never ask permission for it):
TINY:    Agent(miss-implementer) with the task; run the relevant tests/checks. No
         reviewer. ESCALATE to NORMAL — automatically, no asking — if a test fails,
         the implementer reports uncertainty, the diff comes back unusually large,
         or you see reviewer-worthy risk you did not expect.
NORMAL:  Agent(miss-implementer) with the task; then Agent(miss-reviewer). If
         CHANGES_REQUIRED: Agent(miss-implementer) with the task + the findings to
         fix, then a NEW Agent(miss-reviewer) round on the resulting diff. At most
         {rounds} review rounds in total; a new subagent per round, never reuse one.
COMPLEX: Agent(miss-architect) FIRST with the task statement; use its plan (put any
         open question it raises to the operator before building). Then the NORMAL
         loop, with the plan included in the implementer's brief and the plan's
         "risks to check" included in the reviewer's brief. The architect is
         exceptional — COMPLEX only, never routine.

The reviewer's brief is ALWAYS fresh context: the task statement + acceptance
criteria, the repo rules that matter (CLAUDE.md conventions, stdlib-only etc.), and
the diff scope (`git diff` in {worktree}) + which code to read around it — NEVER the
implementer's report or reasoning, so the review stays independent.

Finally, verify what you can yourself (syntax check, tests) and report to the
operator: a first line of the form
  Workflow: NORMAL — multi-file behavioural change, independent review used.
(level in caps, then a one-clause reason; mention an escalation if one happened),
then what changed, the final verdict, any findings you or the implementer disagreed
with, and the one safe next step (usually: ask for YES COMMIT). Put that same
Workflow line in the mission's LOG entry / summary.
The subagents cannot commit, and neither can you without YES COMMIT; nothing in
this changes the approval phrases, the worktree/integrator/release rules, or the
guard. If the subagents are missing, do the work yourself as before.

"""

SHIP = """\
== SHIP — one approval, then the integrator comes to you ==
The operator never switches to an integrator console for ordinary work. When the
work is done and verified, ask for ONE phrase in THIS conversation:
  NEEDS APPROVAL? YES SHIP — commit this branch and hand it to the integrator to
  integrate, push, and (where the repo defines them) release, deploy and verify.
Once the operator has typed exactly YES SHIP:
1. Commit your own changes (YES SHIP covers the commit: `git add <explicit paths>`
   + `git commit`; never `git add -A`).
2. Write the delegation ticket — it records repo/branch/commit/tree/base and the
   repo's shipping steps from git, and refuses if anything is not ready:
     python3 {scripts}/miss-ship-ticket.py --approval "YES SHIP" \\
         --request "<the request, in plain words>" \\
         --tests "<the checks you ran + results>" \\
         --review "<the reviewer's final VERDICT + one-line summary, or 'none (TINY)'>"
   (write those in plain words — not literal git command lines — your own guard reads
   the command you run). A "BLOCKED: ..." answer names the one thing to fix (often
   YES REBASE); report it and stop.
3. Agent(miss-integrator) with the ticket path and the SHIP DELEGATION block the
   script printed, verbatim, and nothing else — not your reasoning. That subagent
   is a separate role: the guard grants integrator power ONLY to its tool calls and
   ONLY for that ticket (that branch at that commit into that base, that remote,
   those release/deploy/verify commands), re-checking git before each step. You
   yourself still cannot merge, push, release or deploy — do not try.
4. Report to the operator, first line one of:
     SHIPPED         — <what is where now: base / remote / released / deployed / verified>
     BLOCKED         — <reason>; next: the one thing it names
     NEEDS ATTENTION — <what changed, what did not>; next: a human looks first
   Append the integrator's RESULT line and its step summary to the mission LOG.
   Its full step log is the ticket's `.log` file (mirrored to <mission>/ship.log).
If the miss-integrator subagent is not available in this session, say so and fall
back to the manual handoff: "ready for integrator" — the commit is safe on the
branch and nothing is lost.
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
            worktree=env("MISS_WORKTREE", "").strip() or cwd, rounds=MAX_ROUNDS,
            scripts=os.path.dirname(os.path.abspath(__file__))))
        sys.stdout.write("\n" + SHIP.format(scripts=os.path.dirname(os.path.abspath(__file__))))
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
