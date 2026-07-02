Human generated content:

So this is claude cli wrapped in a webui.  You cant lose a session.  You cant get disconnected.  Claude updates files in the webui with actions taken / info gathered. 

What this helps fix:

* getting disconnected from ssh when running claude
* change from dialup to wifi? move to a new hotspot? restart computer?  Everything stays just where you left it, and claude keeps running.
* losing long claude sessions
* losing important context
* scrolling/reading through claude output to figure out what it did/didnt do
* not being able to find that ONE claude that had the THING i need.
* claude stopping working because laptop closed/computer went to sleep
* Spending days trying claude management software that is shitty, overcomplicated, and still didn't meet the minimum requirements of what I wanted -^
* being unable to copy/paste large text blocks out of claude

Claudes are Searchable by name + context.
Run claude on remote servers - fully resumable with context.
Optional full screen web page console.

Webui displays:
* session / weekly Claude usage limits
* color coded Context usages in every window/claude so you know when to /compact or /clear.
* Dashboard, Plan, Handoff, Log, Descisions, Artifcats files per claude instance.
* all / commands are unchanged.  Claude interface itself is unchanged.  /clear is normal, but this keeps important context in the files -^ which auto-update when claude does something significant.



AI generated content:

# Miss Claude

**Miss Claude** (the "Mission Dashboard") is a tiny, dependency-free web UI for running ops
**"missions"** on a single Linux host — with a *real* `claude` terminal embedded in every
mission page.

A mission is just a directory of plain markdown files. The app lets you view and edit those
files in a browser, and pins a live Claude Code console (the actual CLI, over a websocket) at
the top of each mission. The files can be edited in the web UI or directly on disk.

Built with the **Python 3 standard library only** — no pip, no venv, no Node, no database, no
internet access. The system `python3` (3.9+) is the sole hard dependency. (The in-browser
console additionally uses [`ttyd`](https://github.com/tsl0922/ttyd) + `tmux` + the `claude` CLI.)

---

## Why / what you get

- **Zero dependencies, zero build.** One `app.py` of pure stdlib. No package manager, no Node
  toolchain, no DB to run or back up. Clone it and run it.
- **Files as the source of truth.** Missions are folders of markdown. Nothing is locked inside a
  database — grep them, diff them, edit them with any tool, sync them however you like.
- **A real Claude console, in the browser.** Not a re-implemented chat box — it *is* the `claude`
  terminal (slash commands, plan mode, permission prompts, live screen updates all work), kept
  alive across reloads by `tmux`.
- **Per-mission persistence.** Each mission has its own resumable Claude session; close the tab
  and reopen later — even after a reboot — and you land back in the same conversation (the console
  relaunches with `claude --continue`). A reboot only drops the live tmux scrollback, not the
  conversation itself.
- **Localhost by default.** Binds `127.0.0.1` out of the box; you opt into wider exposure
  explicitly (`MISSION_HOST=0.0.0.0`). An optional shared token adds a thin auth layer.
- **Built-in multi-session guardrails (optional).** A small role/branch workflow lets several
  Claude sessions safely develop *the dashboard itself* at once, enforced by a `PreToolUse` hook.
  See [Development workflow](#development-workflow).

---

## What a mission looks like

```
~/missions/<mission-name>/
  DASHBOARD.md     # status, objective, current focus (+ the Claude instruction block)
  PLAN.md          # steps / open questions
  HOSTS.md         # hosts table for this mission
  LOG.md           # running log (newest on top)
  HANDOFF.md       # write before stopping: state / next / blockers
  DECISIONS.md     # durable decisions + rationale
  artifacts/       # any output files you drop here show up in the Artifacts tab
  scans/           # same — for scan output, dumps, etc.
  mission.json     # optional sidecar: how/where this mission's console runs (see Spawn)
```

Create missions from the web UI (the **+ Create mission** box), or just
`mkdir ~/missions/<name>` and add files by hand — both work.

---

## Spawn — pick a mode, then where it runs

Next to the create box, **+ Spawn** opens a small two-step wizard:

1. **What (mode):** **Mission** (the ops mission above — markdown docs + a console at the target),
   **Dev Mission** (also creates a `git worktree` on branch `claude/<name>` and runs the console as
   a feature worker), or **Console** (a stateless Claude session — no mission folder).
2. **Where:** only the choices valid for the mode are shown —
   - **Mission** → **Local dir** (any path) or **Remote dir** (`host` + `dir`).
   - **Dev Mission** → **Local repo** (a git repo on this box) or **Remote repo** (`host` + `dir`).
   - **Console** → **Local dir** or **Remote dir** (`host` + `dir`, same as the **Remote console** page).

A **Dev Mission can target any repo, local or remote** — not just Miss Claude itself — and the
worker rails travel with the console either way: the guard hook + role rules are **attached at
launch** (locally via `console-hooks-dev.settings.json`; remotely by copying the bundle to
`~/.miss-claude/` with `scripts/ship-rails.sh`), and the console **refuses to start if it can't
confirm the guard is in place** — it never runs Claude with permissions skipped and no guardrail. In
a repo without Miss Claude's own `CLAUDE.md`, a session-start hook tells Claude its role and the
exact `YES …` approval phrases. See [Development workflow](#development-workflow) for the roles.

Leave the **base branch blank to auto-detect** it: a `working` branch if the repo has one, otherwise
the repo's checked-out branch (a brand-new path is `git init`'d with the base as its initial branch,
staging any existing files into the first commit).

A spawned mission records its choice in `~/missions/<name>/mission.json`. Missions created the old
way (or by hand) work exactly as before: no `mission.json` means ops, unless a same-named
`~/missclaude-worktrees/<name>` worktree exists (then it's a dev mission on this repo).

---

## Architecture

```
browser ──> :4200 dashboard ──console <iframe>──> :4201 ttyd ──> console-launch.sh <mission>
                                                                  └─> tmux  mission-<slug>  (cwd = ~/missions/<mission>)
                                                                        └─> console-session.sh ──> claude
```

- **`app.py`** — the dashboard: a hand-rolled HTTP handler + markdown renderer on
  `http.server`. Lists missions, views/edits the markdown tabs, serves artifacts, and embeds the
  console iframe. No framework, no database.
- **[ttyd](https://github.com/tsl0922/ttyd)** bridges a real terminal to the browser over a
  websocket; **one** instance on port **4201** serves every mission via its `--url-arg` flag (the
  mission name arrives as `?arg=<mission>`).
- **tmux** is the persistence layer: the session `mission-<slug>` outlives ttyd and browser
  reloads, so reconnecting lands you back in the same live Claude with scrollback intact.
- **`console-launch.sh` → `console-session.sh`** validate the mission name, attach to (or create)
  the tmux session in the mission directory, and run `claude`. When Claude exits you drop to a
  login shell in the mission dir; type `claude` to restart it.

---

## Requirements

- A Linux host with the system **`python3` ≥ 3.9** (that alone runs the dashboard).
- For the in-browser console: **`ttyd`** (e.g. from EPEL on RHEL/Alma, or your distro), **`tmux`**,
  and the **`claude`** CLI on `PATH`.
- To run it as a service: **systemd**.

> The examples below use AlmaLinux/RHEL conventions (`dnf`, EPEL). Adapt the package commands to
> your distro.

---

## Quick start (no sudo, dev / test)

```bash
git clone https://github.com/apezio/miss-claude ~/mission-dashboard
MISSION_PORT=4200 python3 ~/mission-dashboard/app.py
# open http://127.0.0.1:4200/  (Ctrl-C to stop)
```

This won't survive a logout/reboot and has no console — use the steps below for a real install.

---

## Install (recommended: `setup.sh`)

One script does the whole install: it renders the systemd units with your user/paths, installs the
console prerequisites (`ttyd`, `tmux`), and enables both services. **Preview it first with
`--dry-run`** — that prints exactly what it will write and run, changing nothing.

```bash
git clone https://github.com/apezio/miss-claude ~/mission-dashboard
cd ~/mission-dashboard

# 1. See what it would do (no changes):
sudo bash setup.sh --dry-run

# 2. Run it for real (prompts once for the console password):
sudo bash setup.sh
```

Common options (`setup.sh --help` for the full list):

| Flag | Meaning |
|------|---------|
| `--user USER` | Account to run the services as (default: the invoking user). |
| `--label TEXT` | Short label shown in the UI header (default: the hostname). |
| `--token TOKEN` | Turn on app token auth (default: none). |
| `--no-console` | Skip the in-browser Claude console (dashboard only). |
| `--console-pass PW` | ttyd basic-auth password (otherwise prompted). |
| `--dry-run` | Print the plan and the exact unit files; change nothing. |

Run as root; anything not passed as a flag is prompted for. The dashboard ends up on
`http://<host>:4200/` and the console on `4201`; the first time the console iframe loads, the
browser asks once for the basic-auth password. The deployed units bind all interfaces so you can
reach the dashboard from another machine — restrict access however you normally do. (The console
runs Claude with `--dangerously-skip-permissions`.)

After install:

```bash
systemctl status mission-dashboard claude-console   # are they up?
sudo systemctl restart mission-dashboard            # after editing app.py (no auto-reloader)
journalctl -u mission-dashboard -f                  # live logs
tmux ls                                             # live mission sessions (mission-<name>)
```

To remove the console later: `sudo systemctl disable --now claude-console`.

### Manual install

Prefer to do it by hand? The repo also ships editable templates: the `*.service` files (referencing
a `youruser` placeholder) and `install.sh` (installs the dashboard unit). Edit the units to match
your host, then `sudo bash install.sh` for the dashboard and
`sudo cp claude-console.service /etc/systemd/system/` (+ `sudo dnf install -y ttyd tmux`) for the
console. `setup.sh` just automates exactly these steps.

### Remote consoles (optional side feature)

The dashboard's **🖥 Remote console** link can run Claude on *another* host over SSH, wrapped in a
local tmux session (nothing is installed on the remote beyond starting Claude). It's a self-contained
add-on; see the fenced `REMOTE CONSOLES` blocks in `app.py` and `console-launch.sh` to customize or
remove it.

---

## Configuration (environment variables)

| Variable        | Default              | Meaning                                              |
|-----------------|----------------------|------------------------------------------------------|
| `MISSION_PORT`  | `4200`               | TCP port to listen on.                               |
| `MISSION_HOST`  | `127.0.0.1`          | Bind address. Set `0.0.0.0` to listen on all interfaces. |
| `MISSIONS_DIR`  | `~/missions`         | Where mission directories live.                      |
| `MISSION_TOKEN` | _(unset)_            | If set, requests need `?token=...` (then a cookie).  |
| `MISSION_LABEL` | _(unset)_            | Optional short label shown beside the title (e.g. the host name). |
| `CONSOLE_TTYD_PORT` | `4201`           | Port of the ttyd console bridge the iframe points at.|
| `WORKTREES_DIR` | `~/missclaude-worktrees` | Where "dev mission" git worktrees are created.   |
| `PRIMARY_REPO`  | `~/mission-dashboard`| The primary checkout used when creating dev missions.|
| `MISSION_BASE_BRANCH` | `working`      | Branch new dev-mission worktrees are based on.       |

### Optional token

For a thin shared-secret layer, set `MISSION_TOKEN` (in the systemd unit, uncomment the line and
pick a value, then `daemon-reload` + `restart`). First visit with
`http://<host>:4200/?token=YOURTOKEN`; the app sets a cookie so you don't retype it. Off by default.
To listen beyond localhost, set `MISSION_HOST=0.0.0.0`.

---

## Development workflow

This repo ships an optional **multi-session role/branch workflow** so several Claude Code sessions
can develop the dashboard at once without stepping on each other — a *feature worker* role (edits
in its own git worktree) and an *integrator* role (fast-forwards finished branches into staging and
deploys). It's enforced by a `PreToolUse` hook (`.claude/hooks/prevent-misswork.py`) and launched
via `scripts/claude-miss` / `scripts/claude-miss-integrator`.

You don't need any of this to *use* Miss Claude — it's purely for contributors. Details:
[`CLAUDE.md`](CLAUDE.md) and [`docs/WORKFLOW_ROLES.txt`](docs/WORKFLOW_ROLES.txt).

There is no build and no test framework. Verify changes by syntax-checking
(`python3 -c "import ast; ast.parse(open('app.py').read())"`), running a throwaway instance on a
spare port with a temp `MISSIONS_DIR`, and `curl`-ing it.

---

## Notes

- All writes are confined to `MISSIONS_DIR`. The markdown renderer covers the common subset
  (headings, lists, checkboxes, tables, code, links, blockquotes); editing is always raw text, so
  anything the renderer doesn't display is still preserved on disk.
- Mission tabs poll for changes (~3 s) and highlight themselves when their underlying file is edited
  (e.g. by Claude in the console), without reloading the live terminal.
