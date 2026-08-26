#!/usr/bin/env python3
"""summarize-missions — write each mission's index-card blurb with Claude.

Cron companion to the dashboard (option "D" of the blurb work): for every
mission whose docs carry real content, feed DASHBOARD/PLAN/LOG/HANDOFF (plus
the operator's opening prompt) to `claude -p` (haiku, NO tools) and cache the
one-line answer in ~/missions/<name>/.blurb — which dashboard_summary() in
app.py prefers over its transcript/doc fallbacks. The dashboard itself never
calls Claude; it only reads this cache, so index loads stay instant.

Cheap by construction:
  - a mission is only (re)summarized when its source material changed —
    sha256 of the gathered docs is kept in .blurb.hash;
  - missions whose docs are still pure scaffold are skipped entirely (the
    dashboard's first-prompt fallback covers those);
  - at most BLURB_MAX_CALLS (default 12) Claude calls per run, spent on the
    most recently active missions first;
  - remote missions' docs are fetched over ssh (BatchMode, fail-fast) — fine
    here, off the page-load path. The blurb cache itself is always local.

Run from the dashboard user's crontab (point it at the deployed repo, so the
helpers match the running app):
  */20 * * * * ~/mission-dashboard/scripts/summarize-missions.py >> ~/missions/.blurb.log 2>&1

Env knobs: BLURB_MODEL (default haiku), BLURB_MAX_CALLS, CLAUDE_BIN,
MISSIONS_DIR (inherited by app.py). --dry-run prints what would be summarized
(and each prompt's size) without calling Claude or writing anything.
"""
import fcntl
import hashlib
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import app  # the dashboard module — mission/doc/ssh helpers, stdlib-only

MODEL = os.environ.get("BLURB_MODEL", "haiku")
MAX_CALLS = int(os.environ.get("BLURB_MAX_CALLS", "12"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or os.path.expanduser("~/.local/bin/claude")
CLAUDE_TIMEOUT = 180  # s per call; haiku with a few KB of input is far quicker
HASH_VERSION = "v1"   # bump to force re-summarize after a prompt change

# (filename, max chars fed to the model). LOG.md keeps newest entries at the
# top, so a head slice IS the recent history.
DOCS = (
    ("DASHBOARD.md", 3000),
    ("PLAN.md", 3000),
    ("LOG.md", 5000),
    ("HANDOFF.md", 2500),
)

# Lines that carry no signal: headers, blockquotes (the Claude instruction),
# table rows, empty checkboxes/bullets, and _italic placeholder lines_ from the
# scaffold. Used only to decide "has this mission any real content at all?".
_NOISE = re.compile(r"^(?:#|>|\||- \[ \]$|-$|_[^_].*_$)")


def _has_signal(txt):
    return any(
        s for s in (line.strip() for line in txt.splitlines())
        if s and not _NOISE.match(s)
    )


def _gather(name):
    """(material, has_signal) — the doc text fed to Claude for one mission."""
    host, rdir = app.mission_doc_source(name)
    parts = []
    signal = False
    for fn, cap in DOCS:
        if host:
            txt = app.ssh_read_text(host, app._remote_doc_path(rdir, fn))
        else:
            txt = app.read_text(app.mission_path(name, fn))
        # Drop the boilerplate Claude-instruction blockquote before capping.
        txt = "\n".join(
            l for l in txt.splitlines() if not l.lstrip().startswith(">")
        ).strip()
        if _has_signal(txt):
            signal = True
        parts.append("=== %s ===\n%s" % (fn, txt[:cap]))
    fp = app.first_prompt(name)
    if fp:
        parts.insert(0, "=== Operator's opening request ===\n%s" % fp)
    return "\n\n".join(parts), signal


def _prompt(name, material):
    return (
        'You write the one-line blurb shown on an ops dashboard card for the '
        'mission "%s".\nBelow are the mission\'s working documents (newest log '
        'entries first).\nReply with exactly ONE line of plain text, at most 140 '
        'characters: what the mission is for and where it stands now '
        '(outcome / current state). No markdown, no quotes, no preamble.\n\n%s\n'
        % (name, material)
    )


def _summarize(name, material):
    """One `claude -p` call -> a sanitized one-liner, or "" on any failure."""
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", MODEL, "--tools", ""],
            input=_prompt(name, material), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=CLAUDE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print("  %s: claude call failed: %s" % (name, e))
        return ""
    if r.returncode != 0:
        print("  %s: claude exited %d: %s"
              % (name, r.returncode, (r.stderr or "").strip()[:200]))
        return ""
    for line in r.stdout.splitlines():
        s = line.strip().strip('"`').strip()
        if s:
            return re.sub(r"\s+", " ", s)[:200]
    return ""


def main():
    dry = "--dry-run" in sys.argv[1:]
    if not os.path.isdir(app.MISSIONS_DIR):
        return 0
    # One runner at a time — a slow Claude call must not stack cron overlaps.
    lock = open(os.path.join(app.MISSIONS_DIR, ".blurb.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    calls = 0
    for name, _mtime in app.list_missions():  # newest activity first
        if calls >= MAX_CALLS:
            print("call budget (%d) spent — remaining missions next run" % MAX_CALLS)
            break
        material, signal = _gather(name)
        if not signal:
            continue  # pure scaffold: the dashboard's first-prompt fallback covers it
        digest = hashlib.sha256(
            (HASH_VERSION + "\0" + material).encode("utf-8", "replace")
        ).hexdigest()
        blurb_path = app.mission_path(name, ".blurb")
        hash_path = app.mission_path(name, ".blurb.hash")
        if (os.path.isfile(blurb_path)
                and app.read_text(hash_path).strip() == digest):
            continue  # unchanged since last summary
        if dry:
            print("would summarize %s (%d chars of material)" % (name, len(material)))
            continue
        calls += 1
        blurb = _summarize(name, material)
        if not blurb:
            continue  # keep any previous .blurb; retried next run
        app.write_text_atomic(blurb_path, blurb + "\n")
        app.write_text_atomic(hash_path, digest + "\n")
        print("[%s] %s: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), name, blurb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
