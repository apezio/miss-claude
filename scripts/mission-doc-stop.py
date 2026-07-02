#!/usr/bin/env python3
"""
mission-doc-stop.py — at end of turn, get the mission docs updated if the session
just hit a milestone (commit / integrate / release / deploy) and the docs are stale.

This is the half of the event-driven nudge that acts on the milestone.
mission-doc-postaction.py (a PostToolUse hook) silently drops a marker file when a
milestone command runs (line 1 = action, line 2 = the repo dir it ran in); THIS Stop
hook reads it when the turn ends and, if LOG.md hasn't been refreshed, gets the docs
updated. Two ways, in order of preference:

  1. PREFERRED — spawn a DETACHED background `claude -p` that updates LOG/DASHBOARD
     out-of-band, and let the foreground session stop cleanly. This keeps the doc
     churn (curl + file edits) OUT of the operator's console window. The background
     updater runs with cwd = the mission data dir (so no repo CLAUDE.md / role rails)
     and with the doc-hook env stripped, so it can't re-arm this nudge.
  2. FALLBACK — if no `claude` binary can be found, return {"decision":"block",
     "reason": ...} so the FOREGROUND session does it instead (a Stop reason is the
     trusted "you're not done yet" channel the model acts on, unlike a PostToolUse
     additionalContext, which the model refuses as an untrusted injection). Better
     stale-in-the-window than not updated at all.

One-shot + loop-safe: the marker is cleared whenever this hook runs (whether it acts
or finds the docs already fresh), and it bails if Claude Code reports it is already
inside a stop-hook continuation (`stop_hook_active`). So it fires at most once per
milestone and can never wedge the session in a loop.

Wired as a `Stop` hook in the mission-console-only settings file
(console-hooks.settings.json). Runs only inside a mission/integrator console.

Inputs:
  stdin  Stop event JSON: {stop_hook_active, cwd, ...}
  env    MISSION_NAME      mission name (for the dashboard log-append URL)
         MISSION_DATA_DIR  the mission's data dir, where LOG.md + the marker live

Stdlib only. Never wedge a stop: any unexpected condition -> let it stop, exit 0.
"""

import json
import os
import shutil
import subprocess
import sys
import time

QUIET_SECS = 60             # LOG.md touched this recently -> docs are fresh, don't nudge
MARKER_NAME = ".doc-nudge-pending"
LOG_NAME = "LOG.md"
# Where the background updater's transcript lands (matches the wrappers' CLAUDE_LOGS).
BG_LOG_DIR = os.environ.get("CLAUDE_LOGS") or os.path.expanduser("~/missclaude-logs")
CLAUDE_FALLBACK = os.path.expanduser("~/.local/bin/claude")


def file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def clear(path):
    try:
        os.remove(path)
    except OSError:
        pass


def build_reason(action, name):
    """The Stop-hook feedback that makes the model update its mission docs now."""
    append = (
        f'  curl -s -d "text=<what changed>" '
        f"http://127.0.0.1:4200/m/{name}/log/append"
    )
    if action == "commit":
        did = "committed in this worktree"
        what = "this commit"
    elif action == "integrate":
        did = "fast-forwarded a feature branch into staging (working)"
        what = "the integrate (which branch, what it adds)"
    elif action == "release":
        did = "pushed / moved the deploy branch (a release)"
        what = "the release"
    else:  # deploy
        did = "restarted the service (a deploy)"
        what = "the deploy (what was deployed)"
    return (
        f"Before you finish: you {did} for mission '{name}' but haven't updated "
        f"the mission docs. Update ALL the relevant mission docs for {what} now, then "
        f"stop — don't stop after only one. At minimum log it in LOG.md AND refresh "
        f"DASHBOARD.md to reflect current state; also update PLAN.md / DECISIONS.md / "
        f"HANDOFF.md / HOSTS.md if this affects them. The LOG.md entry can be appended "
        f"with:\n{append}\n"
        f"(If a given doc genuinely has nothing worth changing, leave it; if there's "
        f"nothing worth recording anywhere, say so in one line and stop.)"
    )


def build_prompt(action, name, data_dir, repo_dir):
    """The instruction handed to the detached background Claude that updates the docs.
    It runs with cwd=data_dir (the mission folder), so `repo_dir` — where the milestone
    command actually ran — is passed explicitly for inspecting what changed."""
    log_url = f"http://127.0.0.1:4200/m/{name}/log/append"
    git_hint = (
        f"Run `git -C {repo_dir} log -1 --stat` (and `git -C {repo_dir} show HEAD` for "
        f"the diff if needed) to see exactly what changed."
        if repo_dir else
        "Inspect the mission's repo to see what changed if you can determine it."
    )
    return (
        f"You are a non-interactive mission-doc updater (not a dev session). A '{action}' "
        f"milestone just happened for mission '{name}'. Update that mission's docs to reflect "
        f"it, then stop. Do not ask questions, do not write feature code, do not touch git "
        f"state — only update docs.\n"
        f"1. {git_hint}\n"
        f"2. Append a concise one-line LOG.md entry with:\n"
        f'   curl -s -d "text=<what changed>" {log_url}\n'
        f"3. Refresh DASHBOARD.md in {data_dir} so its Status / Current focus match reality.\n"
        f"4. Update PLAN.md / DECISIONS.md / HANDOFF.md / HOSTS.md in {data_dir} ONLY if this "
        f"milestone actually affects them.\n"
        f"Keep edits short and factual. Leave any doc that genuinely needs no change."
    )


def spawn_bg_updater(action, name, data_dir, repo_dir):
    """Launch a detached headless `claude -p` to update the docs out-of-band, so the
    foreground session stops cleanly instead of doing it inline (which clutters its
    window). Returns True if spawned. The child runs with cwd=data_dir (no repo CLAUDE.md
    / role rails) and with the doc-hook env stripped, so it can't re-arm this nudge."""
    claude = shutil.which("claude") or (
        CLAUDE_FALLBACK if os.path.exists(CLAUDE_FALLBACK) else None
    )
    if not claude:
        return False

    env = dict(os.environ)
    # Don't let the background updater inherit (and re-fire) the nudge wiring.
    for key in ("MISSION_DOC_STOP", "MISSION_DOC_POSTACTION", "MISSION_DOC_REMINDER"):
        env.pop(key, None)

    log_dir = BG_LOG_DIR if os.path.isdir(BG_LOG_DIR) else data_dir
    try:
        log_fh = open(os.path.join(log_dir, "doc-bg.log"), "ab")
    except OSError:
        log_fh = subprocess.DEVNULL

    prompt = build_prompt(action, name, data_dir, repo_dir)
    try:
        subprocess.Popen(
            [claude, "-p", prompt, "--dangerously-skip-permissions"],
            cwd=data_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,   # fully detach from this hook / the foreground session
        )
        return True
    except Exception:
        return False
    finally:
        if log_fh is not subprocess.DEVNULL:
            try:
                log_fh.close()      # the child keeps its own dup'd fd
            except Exception:
                pass


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        return
    # Already inside a stop-hook continuation -> never block again (loop guard).
    if event.get("stop_hook_active"):
        return

    name = os.environ.get("MISSION_NAME")
    data_dir = os.environ.get("MISSION_DATA_DIR")
    if not name or not data_dir or not os.path.isdir(data_dir):
        return

    marker = os.path.join(data_dir, MARKER_NAME)
    try:
        with open(marker) as fh:
            raw = fh.read()
    except OSError:
        return  # no milestone since last nudge -> let it stop

    # Whatever we decide, consume the marker now: nudge at most once per milestone.
    clear(marker)
    # Marker format: line 1 = action, line 2 (optional) = the repo dir the command ran in.
    lines = raw.splitlines()
    action = lines[0].strip() if lines else ""
    repo_dir = (lines[1].strip() if len(lines) > 1 else "") or event.get("cwd") or ""
    if action not in ("commit", "integrate", "release", "deploy"):
        return

    # Docs already refreshed around the milestone -> nothing to do.
    log_mtime = file_mtime(os.path.join(data_dir, LOG_NAME))
    if log_mtime is not None and time.time() - log_mtime < QUIET_SECS:
        return

    # Preferred: offload the doc update to a detached background Claude, so it never
    # clutters THIS session's window. If we can't spawn one, fall back to blocking the
    # stop with a reason so the foreground session does it (better stale docs than none).
    if spawn_bg_updater(action, name, data_dir, repo_dir):
        return
    print(json.dumps({"decision": "block", "reason": build_reason(action, name)}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A reminder is advisory; never wedge the stop.
        pass
    sys.exit(0)
