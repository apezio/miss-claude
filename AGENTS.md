# AGENTS.md

Miss Claude (the Mission Dashboard). Read **CLAUDE.md** first — it carries the project overview, the
multi-session role/branch workflow, and the standing rules. Full workflow reference:
`docs/WORKFLOW_ROLES.txt`.

Key constraints for any agent working here:

- **Python 3 standard library only.** No pip/venv/Node/DB/internet. Adding a dependency needs explicit
  approval.
- **No auto-reloader.** `app.py` changes are not live until `mission-dashboard.service` is restarted
  ("deploy", integrator-only after `YES DEPLOY`). Verify with a throwaway instance on a spare port +
  temp `MISSIONS_DIR`, not by restarting the live service.
- **Roles are enforced by a hook** (`.claude/hooks/prevent-misswork.py`, keyed off `CLAUDE_MISS_ROLE`).
  Feature workers edit only their own worktree and never push/merge/deploy; the integrator never writes
  feature code. The `YES …` approval phrases are behavioural — never act on vague approval.
- **Never `git add .` / `-A`** — stage by explicit path. Don't commit secrets or `__pycache__/`.
