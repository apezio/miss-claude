#!/usr/bin/env python3
"""
mission-doc-reminder.py — gentle, rate-limited "update your mission docs" nudge.

Wired as a `UserPromptSubmit` hook in a mission-console-only settings file
(console-hooks.settings.json), attached at launch via `claude --settings <file>`
by console-session.sh (ops console) and console-session-wt.sh -> claude-miss
(dev console). It therefore fires ONLY inside a mission console, never in the
operator's own Claude sessions.

What it does: on each prompt submit, if the mission's LOG.md has gone stale while
the session is active, it injects a short reminder (via the hook's
`additionalContext`) suggesting the model log its work / refresh DASHBOARD.md /
write HANDOFF.md. The model still decides — nothing is blocked.

Inputs (env, exported by the launch scripts):
  MISSION_NAME      mission name, e.g. "claude-miss-more-frequent-doc-updates"
  MISSION_DATA_DIR  the mission's data dir, e.g. ~/missions/<name>/ (where LOG.md
                    lives, even for a dev console whose cwd is the worktree)

Rate-limiting: at most ~once per STALE_SECS (15 min), and only when LOG.md has
not changed within that window. A dot-prefixed marker file in the data dir holds
the "last reminded" time as its mtime (not a doc tab — DOC_TABS is an allowlist).

Stdlib only. Never errors out a prompt: any unexpected condition -> stay silent,
exit 0.
"""

import json
import os
import sys
import time

STALE_SECS = 15 * 60          # 15 minutes
MARKER_NAME = ".doc-reminder-state"
LOG_NAME = "LOG.md"


def dashboard_endpoint(data_dir):
    """(base_url, ca_path) for the running dashboard.

    Never guessed. Two sources, both authoritative: MISSION_SELF_URL/MISSION_TLS_CA
    exported by the launch scripts, else the .dashboard-url file app.py rewrites in the
    missions dir on every start. Anything else -> plain http on the default port, which
    is what an install without TLS has always been.

    Deliberately does NOT infer from a certificate existing on disk: certs outlive a
    switch back to http, so that guess yields an https + --cacert curl the dashboard
    refuses, and the model's log appends fail silently."""
    base = os.environ.get("MISSION_SELF_URL", "").strip().rstrip("/")
    ca = os.environ.get("MISSION_TLS_CA", "").strip()
    if not base and data_dir:
        try:
            state = os.path.join(os.path.dirname(data_dir.rstrip("/")), ".dashboard-url")
            with open(state, encoding="utf-8") as fh:
                d = json.load(fh)
            base = str(d.get("base", "")).strip().rstrip("/")
            ca = ca or str(d.get("ca", "")).strip()
        except (OSError, ValueError, AttributeError):
            pass
    if not base:
        base = "http://127.0.0.1:" + os.environ.get("MISSION_PORT", "4200")
    return base, ca


def log_append_curl(name, data_dir=""):
    """The `curl` line that appends a LOG.md entry, matching how the dashboard is
    actually served. Mirrors app.py's SELF_CURL/SELF_URL — kept here as a copy because
    hooks are standalone processes that never import the app.

    Under TLS the app's certificate comes from a private CA that is NOT in the system
    trust store, so curl has to be handed it explicitly or every append fails to verify."""
    base, ca = dashboard_endpoint(data_dir)
    cacert = f" --cacert {ca}" if ca and base.startswith("https") else ""
    return f'curl -s{cacert} -d "text=<what you did>" {base}/m/{name}/log/append'


def file_mtime(path):
    """mtime of path, or None if it doesn't exist / can't be stat'd."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def should_remind(now, log_mtime, marker_mtime, stale_secs=STALE_SECS):
    """Pure decision: should we emit a reminder right now?

    - marker_mtime is None on the very first run of a session: we have just
      established the activity baseline, so stay quiet (caller creates the
      marker). The first nudge can only come >= stale_secs later.
    - Otherwise remind only when BOTH hold:
        * it's been >= stale_secs since the last reminder (marker), and
        * LOG.md is stale: unchanged for >= stale_secs (or missing entirely).
    """
    if marker_mtime is None:
        return False
    if now - marker_mtime < stale_secs:
        return False
    log_age = float("inf") if log_mtime is None else now - log_mtime
    return log_age >= stale_secs


def build_reminder(name, log_mtime, now, data_dir=""):
    """The short, non-coercive reminder text injected as additionalContext."""
    if log_mtime is None:
        age = "has not been written yet this session"
    else:
        mins = int((now - log_mtime) // 60)
        age = f"hasn't been updated in ~{mins} min of active work"
    return (
        f"[mission-doc reminder] LOG.md for mission '{name}' {age}. "
        f"If you've done something worth recording, append a log entry now "
        f"(auto-timestamped, newest on top):\n"
        f"  {log_append_curl(name, data_dir)}\n"
        f"Keep DASHBOARD.md current, and write ~/missions/{name}/HANDOFF.md "
        f"before stopping. You decide — skip this if there's nothing noteworthy."
    )


def touch(path, when):
    """Create path if absent and set its mtime to `when` (best effort)."""
    try:
        with open(path, "a"):
            pass
        os.utime(path, (when, when))
    except OSError:
        pass


def main():
    name = os.environ.get("MISSION_NAME")
    data_dir = os.environ.get("MISSION_DATA_DIR")
    # Not a mission console (env unset) or a bogus dir -> say nothing.
    if not name or not data_dir or not os.path.isdir(data_dir):
        return

    now = time.time()
    log_mtime = file_mtime(os.path.join(data_dir, LOG_NAME))
    marker = os.path.join(data_dir, MARKER_NAME)
    marker_mtime = file_mtime(marker)

    if not should_remind(now, log_mtime, marker_mtime):
        # First run: establish the activity baseline so the 15-min clock starts
        # now. Quiet thereafter until both the marker and LOG.md go stale.
        if marker_mtime is None:
            touch(marker, now)
        return

    context = build_reminder(name, log_mtime, now, data_dir)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))
    # Reset the rate-limit clock so the next nudge is >= STALE_SECS away.
    touch(marker, now)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A reminder is advisory; never let it disrupt the prompt.
        pass
    sys.exit(0)
