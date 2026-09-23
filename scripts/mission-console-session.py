#!/usr/bin/env python3
"""
mission-console-session.py — record which transcript this mission console is writing.

Wired as `SessionStart` + `UserPromptSubmit` hooks in the mission-console-only settings
files (console-hooks.settings.json, console-hooks-dev.settings.json), attached at launch
via `claude --settings <file>` by console-session.sh (ops), console-session-wt.sh ->
claude-miss (dev) and scripts/claude-miss-integrator. It therefore fires ONLY inside a
mission console, never in the operator's own Claude sessions.

Why: the dashboard's context badge has to know WHICH ~/.claude/projects/<dir>/<uuid>.jsonl
belongs to this mission's console, and every way of inferring that from the cwd drifts.
"Newest transcript in the cwd's project dir" catches any other session sharing the dir —
e.g. a second mission launched at $HOME. The deterministic uuid console-launch.sh pins is only the id the console STARTED from: a
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

The marker also keeps `previous`: the transcripts this console /clear-ed away, newest
first (at most PREVIOUS_CAP). Nothing in a transcript points at the one it replaced, so
this hook — which sees both paths, the old marker's and the SessionStart:clear payload's —
is the only place that link can be recorded. The chat page reads it to keep the earlier
conversation on screen above a "Context cleared" bar. The list is carried over while the
path stays the same and dropped when the path changes for any other reason (a /resume of
another conversation, a fresh startup): those are not this conversation's history.

Stdlib only. Never blocks or errors a prompt: any unexpected condition -> silent exit 0.
"""

import json
import os
import sys
import time

MARKER_NAME = ".console-session"
PREVIOUS_CAP = 5


def previous_chain(old, path, source):
    """The `previous` list for a marker about to name `path`, given the old marker."""
    if not isinstance(old, dict):
        return []
    prev = old.get("previous")
    prev = [p for p in prev if isinstance(p, str)] if isinstance(prev, list) else []
    old_path = old.get("transcript_path")
    if old_path == path:
        return prev[:PREVIOUS_CAP]
    if source == "clear" and isinstance(old_path, str) and old_path.endswith(".jsonl"):
        return ([old_path] + [p for p in prev if p != path])[:PREVIOUS_CAP]
    return []


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
    path = os.path.realpath(path)
    marker = os.path.join(data_dir, MARKER_NAME)
    try:
        with open(marker, encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = None
    rec = {
        "transcript_path": path,
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "event": payload.get("hook_event_name"),
        # SessionStart's source (startup|resume|clear|compact): after a /clear the
        # named transcript stays unwritten until the next prompt, and this is how
        # the canvas tells that idle-at-the-prompt state from a console still
        # starting up (mission_activity_detail).
        "source": payload.get("source"),
        "updated": int(time.time()),
        "previous": previous_chain(old, path, payload.get("source")),
    }
    # Atomic replace: the dashboard reads this file on every context poll, so it must
    # never observe a half-written one. Same-dir temp keeps the rename on one filesystem.
    tmp = os.path.join(data_dir, MARKER_NAME + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
            fh.write("\n")
        os.replace(tmp, marker)
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
