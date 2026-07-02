#!/usr/bin/env python3
"""SessionStart hook: inject the Miss Claude dev-workflow rails into Claude's context.

Why: the behavioural rails (roles, the exact YES ... approval phrases, the stoplight)
live in the mission-dashboard repo's own CLAUDE.md, so a dev mission developing ANY
OTHER repo never showed them to Claude at all — the console banner is printed before
Claude starts and is human-visible only. This hook closes that gap: at session start
it prints a compact role block, which Claude Code adds to the model's context
(SessionStart stdout is injected as context, like CLAUDE.md).

Wired by console-hooks-dev.settings.json (local dev/integrator consoles) and
miss-rails.settings.json (remote dev consoles; shipped by scripts/ship-rails.sh).
Everything is read from the environment the launch wrappers already export:
CLAUDE_MISS_ROLE, PRIMARY_REPO, BASE_BRANCH, WORKTREES_DIR.

Self-quieting: if the working directory's repo already documents the workflow (its
CLAUDE.md mentions the approval phrases — i.e. the mission-dashboard repo itself),
print nothing, so Claude-Miss sessions don't carry the rules twice.

Exit 0 always: a context nudge must never break a session.
"""

import os
import sys


def repo_documents_rails(cwd):
    """True when <cwd>/CLAUDE.md already carries the approval-phrase workflow."""
    try:
        with open(os.path.join(cwd, "CLAUDE.md"), encoding="utf-8", errors="replace") as fh:
            return "YES COMMIT" in fh.read()
    except OSError:
        return False


FEATURE = """\
== MISS CLAUDE DEV RAILS — you are a FEATURE WORKER ==
Worktree: {cwd} (branch {branch})
Repo under development: {repo} (staging branch: {base})

Rules (hard-enforced by a PreToolUse hook; do not fight it):
- Edit code ONLY inside this worktree. Never touch the primary checkout, other
  worktrees, or {protected}. Never push, merge, deploy, or restart services.
- Stay on your claude/* branch (no checkout/switch away).
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

INTEGRATOR = """\
== MISS CLAUDE DEV RAILS — you are the INTEGRATOR ==
Repo: {repo} (staging branch: {base})

You review finished claude/* branches and integrate them. You do NOT write feature
code (hard-enforced by a PreToolUse hook). Fast-forward only; never force-push,
never rebase, no non-ff merges.

Approval phrases — the operator must type these EXACTLY (vague approval is never
enough):
- YES INTEGRATE    -> fast-forward (--ff-only) a reviewed claude/* branch into {base}.
- YES PUSH WORKING -> push {base} to origin (if a remote exists).
- YES RELEASE      -> move the deploy branch forward to {base}.
- YES DEPLOY       -> restart the service to load released code.

Before integrating: confirm the branch is clean, based on current {base}, and its
changed files are expected. Keep git talk plain; always end with the one safe next
step.
"""


def main():
    cwd = os.getcwd()
    if repo_documents_rails(cwd):
        return
    role = os.environ.get("CLAUDE_MISS_ROLE", "").strip().lower()
    repo = os.environ.get("PRIMARY_REPO", "").strip() or "(unknown repo)"
    base = os.environ.get("BASE_BRANCH", "").strip() or "working"
    branch = "claude/" + os.path.basename(cwd)
    # e.g. "working, main, or master" / "main or master" — no duplicates when the
    # staging branch IS main/master.
    names = [base] + [b for b in ("main", "master") if b != base]
    protected = ", ".join(names[:-1]) + (", or " if len(names) > 2 else " or ") + names[-1]
    if role == "integrator":
        sys.stdout.write(INTEGRATOR.format(repo=repo, base=base))
    else:
        sys.stdout.write(FEATURE.format(cwd=cwd, branch=branch, repo=repo, base=base,
                                        protected=protected))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
