# Miss Claude

**Miss Claude** (the "Mission Dashboard") is a tiny, dependency-free web UI for running ops
**"missions"** on a single Linux host — with a *real* `claude` terminal embedded in every
mission page.

A mission is just a directory of plain markdown files. The app lets you view and edit those
files in a browser, and pins a live Claude Code console (the actual CLI, over a websocket) at
the top of each mission. The files stay normal text on disk, so you can also edit them with
`vim`/`nano`/Claude any time — this app is only a convenience layer, never the source of truth.

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
  and reopen later and you land back where you left off (sessions survive reloads, not reboots).
- **Firewall-first security.** Designed to bind to a private/admin network and be gated by a
  source-IP allowlist (the same boundary you already use for SSH), with an optional shared token.
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
```

Create missions from the web UI (the **+ Create mission** box), or just
`mkdir ~/missions/<name>` and add files by hand — both work.

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
- For the recommended access model: a host firewall that can pin a port to specific source IPs
  (examples below use **`firewalld`**), plus **systemd** to run it as a service.

> The examples below use AlmaLinux/RHEL conventions (`dnf`, EPEL, `firewalld`). Adapt the package
> and firewall commands to your distro.

---

## Quick start (no sudo, dev / test)

```bash
git clone https://github.com/apezio/miss-claude ~/mission-dashboard
MISSION_PORT=4200 python3 ~/mission-dashboard/app.py
# open http://127.0.0.1:4200/  (Ctrl-C to stop)
```

This won't survive a logout/reboot and has no console — use the steps below for a real install.

---

## Access model

The app binds `0.0.0.0:4200` by default. It is meant to live behind a firewall, **not** open to
the world: the intended boundary is a source-IP allowlist on the port, exactly like SSH. So the
rule of thumb is:

> If you can SSH into the host from your network, you can open the dashboard.

There is **no password by default** — the firewall allowlist is the security boundary. If you want
a thin extra layer, set a token (see [Optional token](#optional-token)).

> Do **not** widen the firewall to `0.0.0.0/0`. Keep it pinned to your admin IPs. The console
> deliberately runs Claude with `--dangerously-skip-permissions`, so treat reachability to these
> ports as equivalent to shell access on the host.

---

## Install (recommended: `setup.sh`)

One script does the whole install: it renders the systemd units with your user/paths, opens the
firewall to your admin IPs, installs the console prerequisites (`ttyd`, `tmux`), and enables both
services. **Preview it first with `--dry-run`** — that prints exactly what it will write and run,
changing nothing.

```bash
git clone https://github.com/apezio/miss-claude ~/mission-dashboard
cd ~/mission-dashboard

# 1. See what it would do (no changes), with YOUR admin source IPs:
sudo bash setup.sh --dry-run --ip 203.0.113.10 --ip 198.51.100.20

# 2. Run it for real (prompts once for the console password):
sudo bash setup.sh --ip 203.0.113.10 --ip 198.51.100.20
```

Common options (`setup.sh --help` for the full list):

| Flag | Meaning |
|------|---------|
| `--ip IP` | An admin source IP allowed through the firewall (repeat for several). |
| `--user USER` | Account to run the services as (default: the invoking user). |
| `--label TEXT` | Short label shown in the UI header (default: the hostname). |
| `--token TOKEN` | Turn on app token auth (default: firewall only). |
| `--no-console` | Skip the in-browser Claude console (dashboard only). |
| `--console-pass PW` | ttyd basic-auth password (otherwise prompted). |
| `--no-firewall` | Don't touch firewalld (you manage the firewall yourself). |
| `--dry-run` | Print the plan and the exact unit files; change nothing. |

Run as root, anything not passed as a flag is prompted for. The dashboard ends up on
`http://<host>:4200/` and the console on `4201`, both pinned to your admin IPs; the first time the
console iframe loads, the browser asks once for the basic-auth password.

After install:

```bash
systemctl status mission-dashboard claude-console   # are they up?
sudo systemctl restart mission-dashboard            # after editing app.py (no auto-reloader)
journalctl -u mission-dashboard -f                  # live logs
tmux ls                                             # live mission sessions (mission-<name>)
```

To remove the console later: `sudo systemctl disable --now claude-console` and drop the 4201
firewall rules.

### Manual install

Prefer to do it by hand? The repo also ships editable templates: the `*.service` files (referencing
a `youruser` placeholder) and `install.sh` (dashboard unit + firewall, with placeholder
`ADMIN_IPS`). Edit those to match your host, then `sudo bash install.sh` for the dashboard and
`sudo cp claude-console.service /etc/systemd/system/` (+ a 4201 firewall rule and
`sudo dnf install -y ttyd tmux`) for the console. `setup.sh` just automates exactly these steps.

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
| `MISSION_HOST`  | `0.0.0.0`            | Bind address. Set `127.0.0.1` for localhost-only.    |
| `MISSIONS_DIR`  | `~/missions`         | Where mission directories live.                      |
| `MISSION_TOKEN` | _(unset)_            | If set, requests need `?token=...` (then a cookie).  |
| `MISSION_LABEL` | _(unset)_            | Optional short label shown beside the title (e.g. the host name). |
| `CONSOLE_TTYD_PORT` | `4201`           | Port of the ttyd console bridge the iframe points at.|
| `WORKTREES_DIR` | `~/missclaude-worktrees` | Where "dev mission" git worktrees are created.   |
| `PRIMARY_REPO`  | `~/mission-dashboard`| The primary checkout used when creating dev missions.|
| `MISSION_BASE_BRANCH` | `working`      | Branch new dev-mission worktrees are based on.       |

### Optional token

If you want a shared secret on top of the firewall, set `MISSION_TOKEN` (in the systemd unit,
uncomment the line and pick a value, then `daemon-reload` + `restart`). First visit with
`http://<host>:4200/?token=YOURTOKEN`; the app sets a cookie so you don't retype it. Off by default
to keep access dead-simple behind the firewall.

---

## Security model (summary)

- **Reachability is the control.** Pin ports 4200/4201 to your admin source IPs. Anyone who can
  reach 4201 + the basic-auth password effectively has a shell (Claude runs with
  `--dangerously-skip-permissions` on purpose, for unattended ops use).
- **No secrets in the repo.** The shipped `*.service` files carry a `CHANGE-ME-STRONG-PW`
  placeholder; the real ttyd password and any `MISSION_TOKEN` belong only in the deployed
  `/etc/systemd/system/` copies. The `missclaude-checkpoint` helper refuses to commit a
  non-placeholder credential.
- **Confined writes.** The dashboard unit uses `ProtectSystem=strict`. Mission names are restricted
  to `[A-Za-z0-9._-]`, and artifact downloads are path-checked to stay inside the mission's
  `artifacts/`/`scans/` dirs. Saves are atomic (temp file + rename).

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
