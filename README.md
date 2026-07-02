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

---

# Miss Claude

A tiny, dependency-free web UI for running ops **"missions"** on a single Linux host — with a
real `claude` terminal embedded in every mission page. You can't lose a session, can't get
disconnected, and Claude keeps its own notes in markdown files as it works.

![Mission list](docs/img/mission-list.png)

## What it is

A mission is just a directory of markdown files. Miss Claude views/edits them in the browser and
pins a live, resumable Claude Code console (the actual CLI, over a websocket) at the top of each
mission. Close the tab, switch networks, reboot — you land back exactly where you left off.

Built with the **Python 3 standard library only**: no pip, no venv, no Node, no database, no
internet. System `python3` (3.9+) is the sole hard dependency. (The console also uses
[`ttyd`](https://github.com/tsl0922/ttyd) + `tmux` + the `claude` CLI.)

## Why

- **No lost work.** `tmux` keeps every session alive across SSH drops, network changes, sleep, and reboots.
- **Files as truth.** Missions are folders of markdown — grep, diff, edit, and sync them with any tool.
- **A real console, not a chat box.** Slash commands, plan mode, permission prompts — the actual `claude` TUI in the browser, and copy/paste that just works.
- **Searchable.** Find the Claude that had the thing you need by name or dashboard contents.
- **Remote-capable.** Run Claude on remote servers, fully resumable.
- **At-a-glance status.** Session/weekly usage bars plus colour-coded per-console context, so you know when to `/compact` or `/clear`.
- **Zero build.** One `app.py`. Clone and run.

## A mission page

A live Claude console on top; markdown tabs below — **Dashboard · Plan · Hosts · Log · Handoff ·
Decisions · Artifacts**. Claude updates the files as it works, and a tab highlights when its file
changes, without reloading the terminal.

![Mission page](docs/img/mission-page.png)

Each mission is just a folder:

```
~/missions/<name>/
  DASHBOARD.md PLAN.md HOSTS.md LOG.md HANDOFF.md DECISIONS.md
  artifacts/  scans/  mission.json   # optional sidecar: where the console runs
```

## Open a mission

The **+ Open** wizard picks a **mode** — Mission (ops docs + a console), Dev Mission (also creates
a `git worktree` + worker guardrails), or Console (a stateless session) — then **where** it runs:
local or remote (`host` + `dir`).

![Open wizard](docs/img/open-wizard.png)

## Quick start

```bash
git clone https://github.com/apezio/miss-claude ~/mission-dashboard
MISSION_PORT=4200 python3 ~/mission-dashboard/app.py
# open http://127.0.0.1:4200/
```

For a real install (systemd units + the console prerequisites), preview then run:

```bash
cd ~/mission-dashboard
sudo bash setup.sh --dry-run   # prints exactly what it will write; changes nothing
sudo bash setup.sh             # installs + enables both services
```

The dashboard ends up on `:4200`, the console on `:4201`. See `setup.sh --help` for flags
(`--user`, `--label`, `--token`, `--no-console`, …). The console runs Claude with
`--dangerously-skip-permissions` on purpose — keep the dashboard behind your own access controls.

## Configuration

All optional; the common ones:

| Variable | Default | Meaning |
|---|---|---|
| `MISSION_PORT` | `4200` | Port to listen on. |
| `MISSION_HOST` | `127.0.0.1` | Bind address; `0.0.0.0` to listen on all interfaces. |
| `MISSIONS_DIR` | `~/missions` | Where mission directories live. |
| `MISSION_TOKEN` | _(unset)_ | If set, requests need `?token=…` (then a cookie). |
| `MISSION_LABEL` | _(unset)_ | Short label shown beside the title. |

## Contributing

An optional multi-session role/branch workflow lets several Claude sessions develop the dashboard
at once without stepping on each other, enforced by a `PreToolUse` hook — you don't need any of it
to *use* Miss Claude. See [`CLAUDE.md`](CLAUDE.md) and
[`docs/WORKFLOW_ROLES.txt`](docs/WORKFLOW_ROLES.txt).

No build, no test framework: syntax-check `app.py`, run a throwaway instance on a spare port with a
temp `MISSIONS_DIR`, and `curl` it.
