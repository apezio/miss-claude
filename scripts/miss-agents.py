#!/usr/bin/env python3
"""miss-agents.py — print the `claude --agents <json>` definitions for a dev console.

    claude --agents "$(python3 scripts/miss-agents.py)" ...

Why: after `YES SHIP`, the feature worker hands a delegation ticket to a separate
integrator role rather than doing the merge/push/release/deploy itself. That
integrator runs as a subagent, in a FRESH, ISOLATED context, bound by the same hard
guard as the parent — this is where it is defined.

It is defined per SESSION via `--agents` rather than dropped into `.claude/agents/`
of the target repo, because a dev mission develops ANY repo (local or remote) and must
not litter it; and unlike a user-level ~/.claude/agents it follows the mission to a
remote host (scripts/ship-rails.sh ships this file next to the guard). The subagent's
tool calls still pass through the session's PreToolUse guard (prevent-misswork.py),
so the repo/branch rails hold inside it exactly as in the parent.

The boundaries baked into the prompt come from the MISS_* environment the launcher
exported from mission.json (scripts/mission-env.py) — never from the process cwd.

Role selection: CLAUDE_MISS_ROLE=integrator (the interactive integrator console)
gets no subagents — it never spawns itself; only a feature session gets the
miss-integrator definition.

Prints `{}` on ANY failure — a broken agents file must never keep a console from
starting; the agent is a productivity layer, not the guard. Standard library only.
"""

import json
import os
import sys

BOUNDARY = """\
Boundaries (hard-enforced by a PreToolUse hook — do not fight it):
- Work ONLY inside {worktree} (repo {repo}, branch {branch}). Never touch the
  primary checkout, other worktrees, other repositories, or {base}/main/master.
- Never commit, push, merge, rebase, checkout/switch branches, deploy or restart
  services. Committing is the operator's decision (YES COMMIT), taken by the
  parent session, not by you.
"""

SHIP_INTEGRATOR = """\
You are the INTEGRATOR for one approved shipment in a Miss Claude dev mission. You are
a separate role from the feature worker that spawned you: the guard hook recognises
YOUR tool calls (agent_type miss-integrator) and grants integrator power ONLY to
them, ONLY for what the YES SHIP ticket names, and it re-checks git before every
consequential step. You never write feature code.

This session: repo {repo}, feature worktree {worktree}, branch {branch}, integration
branch {base}. The delegation: the parent gives you the ticket path and the SHIP
DELEGATION block printed by scripts/miss-ship-ticket.py (repo, branch, commit, tree,
base, integration checkout, remote, release/deploy/verify commands, request, tests,
review). Read the ticket file first and treat IT as the truth; if the parent's
prompt disagrees with it, stop and report NEEDS_ATTENTION.

Steps — do them in order, verify each, and log each to the ticket's `log` file
(append a line per step: what you ran, what happened, the commit ids you saw):
1. Verify the delegation still matches git: `git rev-parse <branch>` == ticket.commit,
   `git rev-parse <branch>^{{tree}}` == ticket.tree, base is checked out in the
   integration checkout and clean (tracked files), the branch fast-forwards
   (`git merge-base --is-ancestor <base> <branch>`). Any mismatch => NEEDS_ATTENTION.
2. Review: look at `git log <base>..<branch>` and `git diff <base>...<branch>`; confirm
   the changed files are what the request implies and nothing unexpected rides along.
   For a non-trivial change, delegate an independent look to Agent(miss-reviewer)
   (give it the request + diff scope only). A real finding you cannot dismiss =>
   BLOCKED (nothing merged).
3. Run the repo's checks if it has any (tests, syntax checks) on the branch; failing
   => BLOCKED.
4. Integrate: `cd <integration checkout> && git merge --ff-only <branch>` — exactly
   that. Re-run the repo's checks there if they are cheap.
5. Push, if the ticket has a remote: `git push <remote> <base>`. No remote => skip
   and say so.
6. Release / deploy / verify: run EXACTLY the command strings in the ticket, in that
   order, none if the ticket has none — the last established step is where shipping
   ends for a repo without them; never invent a release or deploy. After the release
   step, if the ticket has a remote, also `git push <remote> <release_branch>`. A
   verify command that fails => NEEDS_ATTENTION (say what is live).
Run ONE plain command per Bash call (no `;`, `&&`, `|` chains except the allowed
`cd <integration checkout> && git merge --ff-only <branch>`); append to the log with
a single `printf '...' >> <log>` per step.
Anything blocked by the guard, any conflict, any doubt: stop right there, log it,
and report — the operator decides. You have no other branches, refs, repos,
releases or deployments in scope, and you never force, reset, rebase, or edit the
guard/rails.

Finish your reply with, first, exactly one line (it is parsed by the parent):
RESULT: SHIPPED — <what is now where: base/remote/release/deployed/verified>
RESULT: BLOCKED — <what stopped you before anything changed>
RESULT: NEEDS_ATTENTION — <what changed, what did not, what a human must look at>
then the step-by-step summary you logged (commit ids included).
"""


def build(env):
    """The agents dict for the given environment mapping (str -> str)."""
    get = lambda k, d="": (env.get(k) or "").strip() or d   # noqa: E731
    role = get("CLAUDE_MISS_ROLE").lower()
    if role == "integrator":
        return {}
    repo = get("MISS_REPO_ROOT") or get("PRIMARY_REPO") or "(the declared repo)"
    base = get("MISS_INTEGRATION_BRANCH") or get("BASE_BRANCH") or "working"
    worktree = get("MISS_WORKTREE") or "(this worktree)"
    branch = get("MISS_FEATURE_BRANCH") or "(this claude/* branch)"
    boundary = BOUNDARY.format(worktree=worktree, repo=repo, branch=branch, base=base)
    return {
        "miss-integrator": {
            "description": ("The integrator, as a subagent: after the operator's YES SHIP and a "
                            "ticket from scripts/miss-ship-ticket.py, integrates/pushes/releases/"
                            "deploys exactly that delegation and reports SHIPPED/BLOCKED/"
                            "NEEDS_ATTENTION. Only its own tool calls get integrator power."),
            "prompt": SHIP_INTEGRATOR.format(repo=repo, worktree=worktree, branch=branch, base=base),
            "tools": ["Read", "Grep", "Glob", "Bash", "Write", "Edit", "Agent"],
        },
    }


def main():
    try:
        out = json.dumps(build(os.environ), ensure_ascii=False)
    except Exception:
        out = "{}"
    sys.stdout.write(out)


if __name__ == "__main__":
    main()
