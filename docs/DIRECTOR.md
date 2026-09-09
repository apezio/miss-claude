# Director mode (experiment)

An optional, removable layer: the operator talks to ONE mission — the **Director** — which
writes short specs, launches normal Miss Claude missions to do the work, and reports one
compact board. It is off unless you turn it on for a specific mission, and it adds no
route, no mode, no role, no daemon and no database.

```
operator  ->  Director mission  ->  normal Miss Claude missions
```

A **Director** is a normal ops mission carrying a `.director` marker file.
A **child** is a normal mission carrying a `SPEC.md`. That is the whole data model.

## Using it

```bash
# 1. Turn it on. This creates the mission if it doesn't exist, marks it, and starts
#    its console — then open that mission from the dashboard and talk to it.
python3 ~/missclaude/scripts/miss-director.py init <name>   # the deployed checkout
#    On a mission whose console is ALREADY running, restart it (the ✕ on its index
#    card) instead: the Director block is injected at SessionStart.

# 2. Talk to that console. It does the rest itself:
python3 scripts/miss-director.py spawn <child> --spec-file <file> [--no-start]
python3 scripts/miss-director.py spawn <child> --mode dev --repo /path/to/repo \
    --spec-file <file>
python3 scripts/miss-director.py board

# 3. When the operator says YES SHIP for a named child, relay it into that console:
python3 scripts/miss-director.py ship <child> [--director <name>]
```

`spawn` POSTs to the dashboard's **existing** `/create` (ops child) or `/spawn`
(dev child, feature role) on `SELF_URL`, treating only the 302 as success — both routes
answer a refusal with the index page and status 200. It then writes the child's three
files, starts its console headlessly (`MISS_NO_ATTACH=1 console-launch.sh <name>`, the
same launcher the browser uses) and types the kickoff prompt with the dashboard's own
`console_send()`. A child is an ordinary mission: own worktree, own guard hook, own
subagents, own `YES SHIP`.

## The three files

| File | Written by | Holds |
|---|---|---|
| `SPEC.md` | Director, at spawn | the goal + an acceptance checklist |
| `STATUS.md` | the child | first line `RUNNING` / `READY` / `NEEDS ME` / `BLOCKED`, then ≤3 short lines |
| `RESULT.md` | the child, when done | what was done and verified, what is left for the operator |

`board` reads only those files plus tmux liveness — never a transcript. A `READY` child is
shown as needing **the operator's `YES SHIP`**. The Director never reads or edits
application source, and never decides to ship — but it may **relay** the operator's
approval (below).

## Shipping: the Director may relay `YES SHIP`, never decide it

The operator approves a shipment; the Director is only a keyboard. When the operator types
`YES SHIP` **to the Director, naming one child**, the Director runs:

```bash
python3 scripts/miss-director.py ship <child>
```

which types `YES SHIP` into that child's console with the dashboard's own `console_send()`
— the same path `spawn` uses for the kickoff — and appends the approval to the **Director
mission's `LOG.md`**. That saves the operator opening every child console, which was the
whole friction. It is deliberately narrow:

| Gate | Why |
|---|---|
| the operator names **one** child | no wildcard, no "ship everything READY" — the approval was for a mission the operator was talking about |
| the child's `STATUS.md` must say `READY` | a mission still working is refused, with a message saying what it actually is |
| it must be a real child (a `SPEC.md`, not the Director) | an unknown or wrong name is refused; nothing is sent |
| its console must be live | the phrase is typed into that console or not at all |
| every relay is logged | `operator said YES SHIP for child "<name>" — relayed into its console`, in the Director's `LOG.md`, with the dashboard's own timestamp |
| the Director never infers | only an explicit operator `YES SHIP` naming a child unlocks it — never a board reading, never a side effect of another task. Vague approval ("ok", "do it") is not approval |

**The child's own ship path is untouched.** It still verifies its acceptance criteria,
still runs `scripts/miss-ship.py`, still passes its `PreToolUse` guard, and still ships
only its own branch. What changed is who types the phrase, not what is checked before it
is acted on. Note that the child's ship path never had a *technical* check that a human
was at that console — `miss-ship.py` compares a literal `--approval "YES SHIP"` string and
the guard hook cannot read chat, so "the operator typed it here" was always enforced by
the agent's honesty. Relaying moves that trust one hop, from the child to the Director.

The child's stop rule (injected by the hook): work only toward the acceptance criteria,
log unrelated discoveries in `LOG.md` instead of fixing them, keep `STATUS.md` current,
and when every criterion passes write `RESULT.md`, set `READY` and stop.

## Toggle

Off by default, everywhere. A mission with no `.director` and no `SPEC.md` gets an empty
hook and behaves exactly as before. On = `miss-director.py init <name>` (one command:
create if needed, mark, start); off again = `rm ~/missions/<name>/.director`.

## Removing it completely

1. `rm scripts/miss-director.py scripts/miss-director-context.py docs/DIRECTOR.md tests/test_director.py`
2. Revert the wiring (19 added lines, mostly comments, in 5 files):
   - `console-launch.sh` — the `MISS_NO_ATTACH` early return in `attach_session`
   - `console-hooks.settings.json`, `console-hooks-dev.settings.json` — the
     `$MISS_DIRECTOR_CONTEXT` SessionStart entry (guarded, so it already no-ops once the
     script is gone)
   - `console-session.sh`, `console-session-wt.sh` — the `MISS_DIRECTOR_CONTEXT` export
3. `rm ~/missions/*/.director`

Child missions keep working as ordinary missions; their `SPEC.md` / `STATUS.md` /
`RESULT.md` become inert documents.

Tests: `python3 -m unittest tests/test_director.py`.
