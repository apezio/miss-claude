# CLAUDE.md

# Miss Claude — Claude session instructions

High-signal "start here" file for the **Miss Claude** code (a.k.a. the Mission Dashboard).
Workflow detail lives in `docs/WORKFLOW_ROLES.txt`; product detail in `README.md`; the canvas is
mapped in `docs/CANVAS.md`. Trust the source code over the docs when they disagree.

## Project overview

Miss Claude is a tiny, **dependency-free** web UI for running ops "missions" on a single Linux
host — a mission is a directory of markdown files the app views/edits in the browser, plus a
live Claude console (ttyd) per mission. **Python 3 standard library only** (system `python3`, 3.9):
no pip, no venv, no Node, no DB, no internet. Keep it that way — adding a dependency needs explicit
approval.

Served by two systemd units:

- **`mission-dashboard.service`** — the HTTP app, `app.py`, port **4200** (`MISSION_TOKEN` unset =
  no app auth). Pure stdlib, **no auto-reloader** — code edits are not live until the service is
  restarted.
- **`claude-console.service`** — the ttyd "Claude console" bridge, port **4201**, runs
  `console-launch.sh` → `console-session.sh` → `claude` per mission inside a tmux session
  (`mission-<name>`) on the shared socket `TMUX_TMPDIR=~/.tmux-console`. The unit sets
  `KillMode=process`, which is what keeps tmux/Claude alive across a service restart.

**ONE tmux server holds every mission console — never `tmux kill-server`.** Inside a console pane
`$TMUX` names that server and overrides `TMUX_TMPDIR`, so a bare `tmux kill-server` ends every
running mission at once. For a throwaway test server use `tmux -L <name> …` (`-L`/`-S` do override
`$TMUX`). The guard hook blocks the unqualified form; to stop one mission's console, use the
dashboard's ✕.

**`setup.sh` generates `/etc/systemd/system/claude-console.service` in full** — user/paths, TLS,
and the whole ttyd command line (`--ssl*`, `--credential`, `--index`, the `ExecStartPre` index
rebuild). **Never add a drop-in that redefines `ExecStart`**: it silently wins over the generated
unit and a later `setup.sh` run appears to work while changing nothing. Change the console by
editing `setup.sh` and re-running it. Additive drop-ins (an extra `Environment=`) are fine.

**TLS**: both services serve https off one certificate from a local CA in `~/.miss-claude/tls/`
(`scripts/make-certs.sh`; `setup.sh` runs it). In `app.py` it's opt-in via `MISSION_TLS_CERT` —
unset = plain http, so throwaway test instances are unaffected. **The two services must match**:
the dashboard iframes ttyd, and a browser silently blocks an http iframe inside an https page.

"Application code" = `app.py`, the `console-*.sh` / `install.sh` / `setup.sh` scripts, the
`*.service` units, and `scripts/`. Missions themselves live under `~/missions/<name>/` (data,
**not** in this repo).

## Everyday commands

```bash
scripts/status                 # branch, dirty, vs working, both live services
scripts/check                  # THE verification: syntax-check all tracked *.py, then the
                               #   full unittest suite (`scripts/check quick` = syntax only)
scripts/dev-instance start     # throwaway dashboard from THIS checkout on :4209
                               #   (temp MISSIONS_DIR with a 'probe' mission), then:
scripts/dev-instance get /m/probe    # curl it (body to stdout, non-2xx = exit 1)
scripts/dev-instance stop      # …and logs [N] | status | url
scripts/outline [file] [filter]      # line-numbered map of app.py's top-level defs
sudo systemctl restart mission-dashboard.service   # "deploy": load app.py changes
```

Prefer these helpers over hand-built equivalents. Verify with `scripts/check` plus a
`scripts/dev-instance`; **never** test by restarting the live service from a feature worktree.

---

## Standing rules

### Roles & branch workflow (read first)

The operator may run **many sessions at once** and is **not necessarily a git expert**. Talk in
plain English, keep it short, and end with the one safe next step. Full detail:
`docs/WORKFLOW_ROLES.txt`.

**Plain labels:** **feature worker** · **integrator** · **staging** = `working` · **deploy branch** =
`main`. Your role is set by the launch wrapper (`CLAUDE_MISS_ROLE`): `scripts/claude-miss` →
**feature worker**; `scripts/claude-miss-integrator` → **integrator**. If unset, assume **feature
worker**.

**Branch model:** feature workers edit in their own worktree `~/missclaude-worktrees/<slug>` on
`claude/<slug>` (off `working`). A finished branch is fast-forwarded into **staging** (`working`),
then **staging → deploy branch** (`main`), then deployed (service restart). For normal work those
are **not** separate approvals: one `YES SHIP` runs all of them via `scripts/miss-ship.py`.

**Every reply to the operator ends with this block — and, normally, little else:**

```
STATUS:
GREEN / YELLOW / RED / SHIPPED / BLOCKED

WHAT MATTERS:
1-3 short bullets maximum.

NEXT STEP:
Exactly what I need to type/do next, or `None`.

NEEDS APPROVAL:
Only list an approval phrase if one is actually required.
```

Stoplight: **GREEN** safe to continue · **YELLOW** needs an approval phrase before continuing ·
**RED** stop and ask the integrator/operator · **SHIPPED** done · **BLOCKED** stopped, nothing
changed.

**Approval phrases — the operator must type these EXACTLY. Vague approval ("ok", "do it", "sounds
good", "continue") is NEVER enough; ask for the exact phrase.**

| Phrase | Who | Unlocks |
|---|---|---|
| `YES SHIP` | feature worker | **the whole shipment**: commit → integrate (into local `working`) → release → push `main` → deploy → verify, run by `scripts/miss-ship.py` |
| `YES COMMIT` | feature worker | commit mid-work, without shipping |
| `YES INTEGRATE` | integrator | fast-forward a reviewed feature branch into `working` |
| `YES PUSH WORKING` | integrator | push `working` to origin by hand (a shipment never does) |
| `YES RELEASE` | integrator | move `main` forward to `working` |
| `YES DEPLOY` | integrator | restart the service to load the released code |

**`YES SHIP` is the only phrase a normal shipment needs** — never ask for the others as a follow-up
to it. If the branch is behind `working`, `YES SHIP` covers the rebase. If a step fails, report
`BLOCKED` / `NEEDS_ATTENTION` and stop. `miss-ship.py` decides nothing itself: scope comes from the
mission's recorded identity, and release/deploy/verify are only the commands the repo defines in
`~/.miss-claude/ship.json` (a repo with no entry stops after integrating). It is idempotent and
resumable — re-run the same command after an interruption.

**Feature worker** — may: edit code in **its own worktree only**, run/syntax-check the app,
summarize; after `YES COMMIT` (or as part of `YES SHIP`) `git add` by explicit path + `git commit`;
after `YES SHIP`, run `scripts/miss-ship.py`. **Must not:** update `working`, touch `main`, push,
deploy / restart the live service by hand, edit other worktrees, or make git changes outside its
own branch.

**Integrator** — the hand-driven / recovery console. May: review branches, fast-forward
(`--ff-only`) a reviewed branch into `working` after `YES INTEGRATE`, move `main` after
`YES RELEASE`, deploy after `YES DEPLOY`. **Must not:** write feature code, force-push, or do
non-ff merges.

A `PreToolUse` hook (`.claude/hooks/prevent-misswork.py`) **hard-blocks** the forbidden actions per
role (and the full dangerous set on `main`/`master`), including git aimed at a repo other than the
session's declared one. The hook can't read the chat, so the approval phrases are enforced by
**you** — never act on a vague approval. **Never `git add .`/`-A`** — stage by explicit path.

### Committing, checkpoints & "deploy"

- **Do not commit unless the operator explicitly approves** (`YES COMMIT`, or the `YES SHIP` that
  covers it). Include the exact files and commit message when you ask.
- **Checkpoints need no approval:** `missclaude-checkpoint ["msg"]` (a `scripts/` helper, the *one*
  sanctioned `git add -A`) saves the current worktree as a WIP commit. Inspect `git status` first.
- **"Deploy" = restart the service** (`mission-dashboard.service`). `console-*.sh` changes apply to
  the next console session with no restart. Restarting `claude-console.service` does **not** end
  live sessions (`KillMode=process`); browsers reconnect to the same tmux sessions.
- Never commit secrets (the ttyd Basic-Auth credential in `claude-console.service`, any
  `MISSION_TOKEN`), `__pycache__/`, or `*.pyc`.

### Before editing & no blind retries

- `git status` first; read the relevant source before editing; keep changes scoped — no
  unrequested features/refactors, don't touch unrelated files.
- Run `scripts/check` after meaningful edits; prefer a dev instance over touching the live service.
- If a command/restart/test **fails, do not just retry**: state the exact action, exact error,
  likely cause, and what will change first.

### House style

- Match the surrounding code: stdlib-only, explicit, readable; the markdown renderer and HTTP
  handler are hand-rolled — keep them dependency-free. Preserve route shapes, env-var names, and
  the no-auth-by-default assumption unless explicitly asked.
- The dashboard is (optionally) token-gated admin tooling; the console runs Claude with
  `--dangerously-skip-permissions` on purpose. Don't "harden" that away without being asked.
