#!/usr/bin/env python3
"""
mission-console-session.py — record which transcript this mission console is writing.

Wired as `SessionStart` + `UserPromptSubmit` hooks in the mission-console-only settings
files (console-hooks.settings.json, console-hooks-dev.settings.json), attached at launch
via `claude --settings <file>` by console-session.sh (ops), console-session-wt.sh ->
claude-miss (dev) and scripts/claude-miss-integrator. It therefore fires ONLY inside a
mission console, never in the operator's own Claude sessions — and never in the detached
`claude -p` doc updater, which mission-doc-stop.py spawns with no --settings and with the
hook env stripped.

Why: the dashboard's context badge has to know WHICH ~/.claude/projects/<dir>/<uuid>.jsonl
belongs to this mission's console, and every way of inferring that from the cwd drifts.
"Newest transcript in the cwd's project dir" catches any other session sharing the dir —
a second mission launched at $HOME, or that doc updater, whose cwd is the mission folder.
The deterministic uuid console-launch.sh pins is only the id the console STARTED from: a
console outlives it, because a /clear opens a NEW session file and abandons the old one
mid-process (verified; --resume and a restart do keep the id), after which the pinned one
stops growing and the badge freezes on its last size.

The console knows the answer with no inference at all — Claude Code hands every hook the
live `transcript_path` — so it writes it down and app.py (live_console_transcript) reads
it. SessionStart covers startup / resume / clear / compact; UserPromptSubmit is the
belt-and-braces catch for any other id change, and costs one small write per prompt.

Inputs: the hook JSON payload on stdin (`transcript_path`, `session_id`, `cwd`), plus
  MISSION_DATA_DIR  the mission's data dir (~/missions/<name>/) — where the marker goes,
                    which for a dev console is NOT the cwd (that's the worktree).
Output: <MISSION_DATA_DIR>/.console-session, a dot-file (DOC_TABS is an allowlist, so it
  can never show up as a doc tab), rewritten atomically.

Stdlib only. Never blocks or errors a prompt: any unexpected condition -> silent exit 0.
"""

import json
import os
import sys
import time

MARKER_NAME = ".console-session"


def main():
    data_dir = os.environ.get("MISSION_DATA_DIR", "").strip()
    if not data_dir or not os.path.isdir(data_dir):
        return
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(payload, dict):
        return
    path = payload.get("transcript_path")
    # NOT checked: that the file exists. It usually does NOT yet — verified live, Claude
    # Code passes the path at SessionStart and even at the first UserPromptSubmit before
    # writing anything to it. The path is authoritative regardless; the reader is the one
    # that handles "named but not written yet" (-> the session is still starting).
    # A junk payload leaves the previous marker in place, which beats blanking it.
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return
    rec = {
        "transcript_path": os.path.realpath(path),
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "event": payload.get("hook_event_name"),
        "updated": int(time.time()),
    }
    # Atomic replace: the dashboard reads this file on every context poll, so it must
    # never observe a half-written one. Same-dir temp keeps the rename on one filesystem.
    tmp = os.path.join(data_dir, MARKER_NAME + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
            fh.write("\n")
        os.replace(tmp, os.path.join(data_dir, MARKER_NAME))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass          # a doc-bookkeeping hook must never take the console down
    sys.exit(0)
