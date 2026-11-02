#!/usr/bin/env python3
"""SessionStart hook: tell Claude which Doska project identity this console carries.

Doska Projects key a session by its worktree; this block hands Claude the values its
Doska MCP `session_start` call needs, so project context loads at startup and again
after /clear without the operator pasting anything. Claude without Doska MCP tools
is told to ignore it.

The worktree is $MISS_WORKTREE for a dev console and the mission folder
($MISSION_DATA_DIR) for an ops one, so every mission has its own key. The mission's
mission.json supplies `doska_project` and `dashboard_url` when it was started from a
Doska project ("Start session"); otherwise the dashboard URL comes from
~/.miss-claude/dashboard-url (one line, e.g. https://jump.example.com:4200) if present.

The label is what the dashboard calls the mission (app.py mission_title): mission.json's
`title` while an auto-named mission's folder still carries its placeholder name, else the
mission name. Doska's `session_start` takes no label and only names a session when it
first joins, so Claude re-sends it with `register_session`; SessionStart fires again on
/clear, which keeps the Doska label on the mission's current name.

The auto-namer sets `title` after the first prompt, i.e. after SessionStart has already
announced the placeholder. So the same script runs as a UserPromptSubmit hook with the
argument `prompt`: it compares the label with the last one announced (LABEL_MARKER in the
mission folder; absent = the placeholder $MISSION_NAME) and, only when it changed, tells
Claude to re-send it. Each name is announced once.

Wired (SessionStart, and UserPromptSubmit with `prompt`) by console-hooks.settings.json
and console-hooks-dev.settings.json via $DOSKA_PROJECT_CONTEXT. Prints nothing outside a
mission console.
Stdlib only; exit 0 always — a context nudge must never break a session.
"""

import json
import os
import sys

LABEL_MARKER = ".doska-label"


def _mission_meta(data_dir):
    try:
        with open(os.path.join(data_dir, "mission.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _default_dashboard():
    try:
        with open(os.path.expanduser("~/.miss-claude/dashboard-url"), encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def _read_marker(data_dir):
    try:
        with open(os.path.join(data_dir, LABEL_MARKER), encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return None


def _write_marker(data_dir, label):
    path = os.path.join(data_dir, LABEL_MARKER)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(label + "\n")
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def main(prompt=False):
    env = os.environ.get
    data_dir = env("MISSION_DATA_DIR", "").strip()
    worktree = env("MISS_WORKTREE", "").strip() or data_dir
    if not worktree:
        return
    meta = _mission_meta(data_dir) if data_dir else {}
    project = meta.get("doska_project") if isinstance(meta.get("doska_project"), str) else ""
    dashboard = meta.get("dashboard_url") if isinstance(meta.get("dashboard_url"), str) else ""
    title = meta.get("title") if isinstance(meta.get("title"), str) else ""
    label = title.strip() or env("MISSION_NAME", "") or os.path.basename(worktree.rstrip("/"))
    if prompt:
        if not data_dir:
            return
        announced = _read_marker(data_dir)
        if announced is None:
            announced = env("MISSION_NAME", "").strip() or label
        if announced == label:
            return
    if data_dir:
        _write_marker(data_dir, label)
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = None
    session = payload.get("session_id") if isinstance(payload, dict) else None
    print("== DOSKA LABEL CHANGED ==" if prompt else "== DOSKA PROJECTS ==")
    print(f"worktree: {worktree}")
    print(f"mission: {env('MISSION_NAME', '')}")
    print(f"branch: {env('MISS_FEATURE_BRANCH', '')}")
    print(f"repo_id: {env('MISS_REPO_ID', '')}")
    print(f"claude_session_id: {session or ''}")
    print(f"dashboard_url: {dashboard or _default_dashboard()}")
    print(f"label: {label}")
    if project:
        print(f"project_id: {project}")
    if prompt:
        print(f"This mission was renamed from \"{announced}\" to \"{label}\"."
              " If Doska MCP tools are available, send the new name to Doska now, before"
              " anything else: "
              + ("call `register_session` with these values (project_id, worktree, label"
                 " and the rest)." if project else
                 "call `session_start` with these values, then for each project it"
                 " returns call `register_session` with these values, that project_id and"
                 " the label.")
              + " If no Doska MCP tools exist, ignore this.")
        return
    print("If Doska MCP tools are available, call `session_start` with these values first"
          + (" (including project_id)" if project else "")
          + " and treat the returned project context as the shared state for this work."
          " It takes no label: for each project it returns, call `register_session` with"
          " these same values, that project_id and the label."
          " If it returns no project, carry on; when the operator asks to join a project,"
          " call `register_session` with these same values and the label.")
    print("Before working on a step, claim it: `register_session` with that workstream_id"
          " and step_id. If it refuses because another session holds the step, do not do"
          " that step; pick a free one or stop. The brief marks each next step (free) or"
          " (claimed: <session>).")
    print("Call `record_progress` at natural checkpoints only: hand-off, asking for"
          " approval, plan change, new blocker, decision needed, before a long pause."
          " Never after every message.")
    print("If no Doska MCP tools exist, continue silently.")


if __name__ == "__main__":
    try:
        main(prompt=sys.argv[1:] == ["prompt"])
    except Exception:
        pass
    sys.exit(0)
