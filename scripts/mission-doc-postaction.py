#!/usr/bin/env python3
"""
mission-doc-postaction.py — record a milestone so the Stop hook can nudge docs.

Part of the event-driven mission-doc nudge. The trick this file exists to work
around: a PostToolUse hook's `additionalContext` DOES reach the model, but the
model treats text injected next to a tool result as untrusted (prompt-injection
defense) and refuses to act on it ("that didn't come from you"). A `Stop` hook's
feedback, by contrast, is the trusted "you're not done yet" channel the model
acts on. So we split the job:

  * THIS hook (PostToolUse, Bash) silently RECORDS that a milestone command just
    ran, into a marker file in the mission data dir. It prints nothing — the
    model never sees it.
  * mission-doc-stop.py (Stop hook) reads that marker at end-of-turn and, if the
    docs are stale, blocks the stop with a reason telling the model to update
    LOG/DASHBOARD now — which it does, in the same session it committed in.

Milestones: a commit (feature worker), or a fast-forward integrate / release /
service restart (integrator). Keyed off the ACTION (the command), not the role,
so it works however the console was launched. The commit case confirms a real
commit just landed (HEAD younger than COMMIT_RECENT_SECS) so a failed/no-op
`git commit` records nothing.

Wired as a `PostToolUse` hook (matcher "Bash") in the mission-console-only
settings file (console-hooks.settings.json), attached at launch via
`claude --settings <file>` by console-session.sh (ops), console-session-wt.sh ->
claude-miss (dev), and scripts/claude-miss-integrator (integrator, when given its
mission name). It therefore only ever runs inside a mission/integrator console.

Inputs:
  stdin  PostToolUse event JSON: {tool_name, tool_input:{command}, cwd, ...}
  env    MISSION_DATA_DIR  the mission's data dir, where LOG.md + the marker live

Stdlib only. Never errors out a tool call: any unexpected condition -> exit 0.
"""

import json
import os
import re
import subprocess
import sys
import time

COMMIT_RECENT_SECS = 45     # a real commit's HEAD is younger than this right after commit
MARKER_NAME = ".doc-nudge-pending"

# Milestone command classifiers (checked in this order; first match wins).
COMMIT_RE = re.compile(r"\bgit\s+commit\b")
GIT_MERGE_RE = re.compile(r"\bgit\s+merge\b")
FF_ONLY_RE = re.compile(r"--ff-only\b")
DEPLOY_RE = re.compile(r"\bsystemctl\s+(?:restart|reload)\b")
# "release" = moving the deploy branch (main/master) forward, or publishing to a
# remote. Feature workers can't push/merge (hard-blocked) so these mean integrator.
RELEASE_RE = re.compile(
    r"\bgit\s+push\b"                                       # push working / publish
    r"|\bgit\s+branch\s+-f\s+(?:main|master)\b"             # branch -f main working
    r"|:(?:main|master)\b"                                  # refspec  working:main
    r"|\bgit\s+update-ref\s+refs/heads/(?:main|master)\b"   # update-ref refs/heads/main
)


def classify(command):
    """Map a bash command to a milestone action, or None if it isn't one."""
    if COMMIT_RE.search(command):
        return "commit"
    if GIT_MERGE_RE.search(command) and FF_ONLY_RE.search(command):
        return "integrate"
    if DEPLOY_RE.search(command):
        return "deploy"
    if RELEASE_RE.search(command):
        return "release"
    return None


def run_git(cwd, *args):
    """Run a read-only git command in cwd; return stripped stdout or None."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def commit_just_landed(cwd, now, recent=COMMIT_RECENT_SECS):
    """True if HEAD's commit time is within `recent` seconds of now — i.e. a real
    commit just landed (so a failed / no-op `git commit` records nothing)."""
    out = run_git(cwd, "log", "-1", "--format=%ct")
    if not out:
        return False
    try:
        return now - int(out) < recent
    except ValueError:
        return False


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        return
    if event.get("tool_name") != "Bash":
        return
    command = (event.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command:
        return

    data_dir = os.environ.get("MISSION_DATA_DIR")
    # Not a mission/integrator console (env unset) or a bogus dir -> nothing to do.
    if not data_dir or not os.path.isdir(data_dir):
        return

    action = classify(command)
    if action is None:
        return

    now = time.time()
    cwd = event.get("cwd") or os.getcwd()
    if action == "commit":
        if not commit_just_landed(cwd, now):
            return

    # Record the milestone for the Stop hook. Line 1 = action label; line 2 = the repo
    # dir the command ran in (so the background updater can inspect what changed). The
    # file's mtime is the "when". Best effort — a write failure just means no nudge.
    try:
        with open(os.path.join(data_dir, MARKER_NAME), "w") as fh:
            fh.write(action + "\n" + cwd)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Recording is advisory; never let it disrupt the tool call.
        pass
    sys.exit(0)
