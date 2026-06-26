# CLAUDE.md

# Miss Claude — Claude session instructions

High-signal "start here" file for the **Miss Claude** code (a.k.a. the Mission Dashboard).
Workflow detail lives in `docs/WORKFLOW_ROLES.txt`; product detail in `README.md`. Trust the
source code over the docs when they disagree.

## Project overview

Miss Claude is a tiny, **dependency-free** web UI for running ops "missions" on a single Linux
host — a mission is a directory of markdown files the app views/edits in the browser, plus a
live Claude console (ttyd) per mission. **Python 3 standard library only** (system `python3`, 3.9):
no pip, no venv, no Node, no DB, no internet. Keep it that way — adding a dependency needs explicit
approval.

Served by two systemd units:

- **`mission-dashboard.service`** — the HTTP app, `app.py`, port **4200** (binds localhost by
  default; `MISSION_TOKEN` unset = no app auth). Pure stdlib, **no auto-reloader** — code edits are not live
  until the service is restarted.
- **`claude-console.service`** — the ttyd "Claude console" bridge, port **4201**, runs
  `console-launch.sh` → `console-session.sh` → `claude` per mission inside a tmux session
  (`mission-<name>`) on the shared socket `TMUX_TMPDIR=~/.tmux-console`. A `KillMode=process`
  drop-in keeps tmux/Claude alive across a service restart.

"Application code" = `app.py`, the `console-*.sh` / `install.sh` scripts, the `*.service` units, and
`scripts/`. Missions themselves live under `~/missions/<name>/` (data, **not** in this repo).

## Everyday commands

```bash
python3 -c "import ast; ast.parse(open('app.py').read())"   # syntax check before "deploy"
MISSIONS_DIR=$(mktemp -d) MISSION_PORT=4209 MISSION_HOST=127.0.0.1 python3 app.py   # throwaway test instance
sudo systemctl restart mission-dashboard.service            # "deploy": load app.py changes (INTEGRATOR-only)
```

There is no build and no test framework. Verify by syntax-checking, by running a throwaway instance
on a spare port + temp `MISSIONS_DIR`, and by `curl` against it. **Never** test by restarting the
live service from a feature worktree.

---

## Standing rules

### Roles & branch workflow (read first)

The operator runs **many sessions at once** and is **not a git expert**. Talk in plain English, keep
git detail short, and always end with the one safe next step. Full detail: `docs/WORKFLOW_ROLES.txt`.

**Plain labels:** **feature worker** · **integrator** · **staging** = `working` · **deploy branch** =
`master`. Your role is set by the launch wrapper (`CLAUDE_MISS_ROLE`): `scripts/claude-miss` →
**feature worker**; `scripts/claude-miss-integrator` → **integrator**. If unset, assume **feature
worker**.

**Branch model:** feature workers edit in their own worktree `~/missclaude-worktrees/<slug>`
on `claude/<slug>` (off `working`). The integrator fast-forwards finished branches into **staging**
(`working`); promoting **staging → deploy branch** (`master`) and deploying (service restart) are
separate steps.

**Session start — always open with this, then wait:**

```
STATUS:
GREEN/YELLOW/RED — one sentence (role, clean/dirty, current-with-working/behind).

WHAT CHANGED:
Plain-English summary (skip on a fresh session).

SAFE NEXT STEP:
Exactly what to tell which Claude next.

NEEDS APPROVAL?
The exact phrase needed, if any.
```

Stoplight: **GREEN** safe to continue · **YELLOW** needs an approval phrase before continuing ·
**RED** stop and ask the integrator/operator.

**Approval phrases — the operator must type these EXACTLY. Vague approval ("ok", "do it", "sounds
good", "continue", "fix it") is NEVER enough; ask for the exact phrase.**

| Phrase | Who | Unlocks |
|---|---|---|
| `YES COMMIT` | feature worker | commit its own changes |
| `YES REBASE` | feature worker | rebase its branch onto current `working` |
| `YES INTEGRATE` | integrator | fast-forward a reviewed feature branch into `working` |
| `YES PUSH WORKING` | integrator | push `working` to origin (if a remote exists) |
| `YES RELEASE` | integrator | move `master` forward to `working` |
| `YES DEPLOY` | integrator | restart the service to load the released code |

**Feature worker** — may: edit code in **its own worktree only**, run/syntax-check the app, summarize,
say "ready for integrator", and (after `YES COMMIT`) `git add` by explicit path + `git commit`; rebase
onto `working` only after `YES REBASE`. **Must not:** update `working`, touch `master`, push, deploy /
restart services, edit/clean up other worktrees, or make git changes outside its own branch.

**Integrator** — may: review branches, fast-forward (`--ff-only`) a reviewed branch into `working`
after `YES INTEGRATE`, move `master` after `YES RELEASE`, deploy (restart the service) after
`YES DEPLOY`. **Must not:** write feature code, force-push, or do non-ff merges. Confirm a branch is
clean, based on current `working`, and its changed files are expected before integrating.

A `PreToolUse` hook (`.claude/hooks/prevent-misswork.py`) **hard-blocks** the forbidden actions per
role (and the full dangerous set on `main`/`master`). The hook can't read the chat, so the approval
phrases above are enforced by **you** — never act on a vague approval. **Never `git add .`/`-A`** —
stage by explicit path.

### Committing, checkpoints & "deploy"

- **Do not commit unless the user explicitly approves** (`YES COMMIT`). Include the exact files to
  stage and the exact commit message when you ask.
- **Checkpoints are different — no approval needed.** After meaningful edits run
  `missclaude-checkpoint ["msg"]` (a `scripts/` helper, the *one* sanctioned `git add -A`): it saves
  everything in the current worktree as a WIP commit. Inspect `git status` first. It refuses to
  checkpoint a non-placeholder ttyd credential into the repo.
- **"Deploy" = restart the service.** `sudo systemctl restart mission-dashboard.service` reloads
  `app.py` (no auto-reloader). Integrator-only, after `YES DEPLOY`. `console-*.sh` changes apply to
  the next console session with no restart. Restarting `claude-console.service` ends live console
  sessions — avoid it unless changing ttyd itself.
- Never commit secrets (the ttyd Basic-Auth credential in `claude-console.service`, any
  `MISSION_TOKEN`), `__pycache__/`, or `*.pyc`.

### Before editing & no blind retries

- `git status` first; read the relevant source before editing (don't assume docs are current); keep
  changes scoped — no unrequested features/refactors, don't touch unrelated files.
- Syntax-check after meaningful edits; prefer a throwaway instance over touching the live service.
- If a command/restart/test **fails, do not just retry**: state the exact action, exact error, likely
  cause, and what will change first.

### House style

- Match the surrounding code: stdlib-only, explicit, readable; the markdown renderer and HTTP handler
  are hand-rolled — keep them dependency-free. Preserve route shapes, env-var names, and the
  no-auth-by-default assumption unless explicitly asked.
- The dashboard is (optionally) token-gated admin tooling; the console runs Claude with
  `--dangerously-skip-permissions` on purpose. Don't "harden" that away without being asked.
