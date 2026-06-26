#!/usr/bin/env python3
"""Mission Dashboard — a tiny, dependency-free web UI for ops "missions".

A "mission" is just a directory under MISSIONS_DIR (default ~/missions) holding
plain markdown files (DASHBOARD.md, PLAN.md, HOSTS.md, LOG.md, HANDOFF.md,
DECISIONS.md) plus artifacts/ and scans/ subdirs. This app reads and writes
those files in the browser. The files stay normal text — edit them outside the
app any time; this is only a convenience layer.

Pure Python 3 standard library. No pip, no venv, no internet, no database.

Config (environment):
  MISSION_PORT   listen port      (default 4200)
  MISSION_HOST   bind address     (default 127.0.0.1; set 0.0.0.0 to listen on all interfaces)
  MISSIONS_DIR   data directory   (default ~/missions)
  MISSION_TOKEN  optional shared secret; if set, requests must carry ?token=... or
                 the mt cookie. OFF by default.
"""

import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("MISSION_PORT", "4200"))
HOST = os.environ.get("MISSION_HOST", "127.0.0.1")
MISSIONS_DIR = os.path.realpath(
    os.environ.get("MISSIONS_DIR", os.path.expanduser("~/missions"))
)
WORKTREES_DIR = os.path.realpath(
    os.environ.get("WORKTREES_DIR", os.path.expanduser("~/missclaude-worktrees"))
)
# Primary checkout + staging branch used by `git worktree add` when creating a DEV
# mission from the dashboard. Mirrors scripts/claude-miss (PRIMARY_REPO / BASE_BRANCH).
PRIMARY_REPO = os.path.realpath(
    os.environ.get("PRIMARY_REPO", os.path.expanduser("~/mission-dashboard"))
)
BASE_BRANCH = os.environ.get("MISSION_BASE_BRANCH", "working")
TOKEN = os.environ.get("MISSION_TOKEN", "").strip()
# Optional short label shown next to the title in the UI header (e.g. the host name).
# Empty by default -> no label is rendered.
LABEL = os.environ.get("MISSION_LABEL", "").strip()
# Port of the ttyd "Claude Console" bridge (claude-console.service). The Console tab
# iframes http://<this-host>:CONSOLE_TTYD_PORT/?arg=<mission>.
CONSOLE_TTYD_PORT = int(os.environ.get("CONSOLE_TTYD_PORT", "4201"))

# tab key -> (filename, display label). Order here is the tab order.
# The Console is no longer a tab — it is a fixed region at the top of every
# mission page (see render_mission_page). The tabs below it load their content
# in-page without reloading that live terminal iframe.
TABS = [
    ("dashboard", "DASHBOARD.md", "Dashboard"),
    ("plan", "PLAN.md", "Plan"),
    ("hosts", "HOSTS.md", "Hosts"),
    ("log", "LOG.md", "Log"),
    ("handoff", "HANDOFF.md", "Handoff"),
    ("decisions", "DECISIONS.md", "Decisions"),
    ("artifacts", None, "Artifacts"),  # special: lists files, not a single .md
]
TAB_FILE = {key: fn for key, fn, _ in TABS if fn}
TAB_LABEL = {key: label for key, _, label in TABS}
TAB_KEYS = [key for key, _, _ in TABS]

ARTIFACT_DIRS = ["artifacts", "scans"]

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

CLAUDE_INSTRUCTION = (
    "Read DASHBOARD.md before acting. Update LOG.md and DASHBOARD.md after "
    "meaningful work. Write HANDOFF.md before stopping. If chat history "
    "conflicts with these files, the files win. "
    "To log work with a precise timestamp, append via the dashboard instead of "
    "hand-editing LOG.md: "
    f"curl -s -d \"text=<entry>\" http://127.0.0.1:{PORT}/m/<mission>/log/append "
    "(it stamps a per-entry time; newest entries go on top)."
)

# Written to MISSIONS_DIR/CLAUDE.md on startup if absent (see main()). Because
# MISSIONS_DIR is a parent of every ~/missions/<name>/, Claude Code auto-loads
# this for every ops console — standing orientation about how missions work,
# with no per-mission clutter. Write-if-absent, so operator hand-edits survive.
MISSIONS_CLAUDE_MD = """\
# Missions — how mission consoles work

This directory (`~/missions/`) holds **missions**: each `~/missions/<name>/` is a folder of
markdown the Mission Dashboard (port 4200) views and edits. This file auto-loads for every ops
console.

## A mission's docs
- **DASHBOARD.md** — orient first: status, objective, current focus.
- **LOG.md** — timestamped progress, newest on top.
- **HANDOFF.md** — state / next / blockers; write before stopping.
- **PLAN.md** — steps and open questions.
- **DECISIONS.md** — durable decisions + rationale, newest on top.
- **HOSTS.md** — hosts in play for this mission.

## Working convention
- Read DASHBOARD.md before acting.
- Update LOG.md and DASHBOARD.md after meaningful work; refresh HANDOFF.md before stopping.
- If chat history conflicts with these files, **the files win**.

## Log with a precise timestamp
Append via the dashboard instead of hand-editing LOG.md (it stamps a per-entry time; newest first):

    curl -s -d "text=<entry>" http://127.0.0.1:4200/m/<mission>/log/append

## Ops vs dev console
- **Ops console** — runs in this mission folder (`~/missions/<name>/`); work the mission's docs here.
- **Dev console** — when a same-named git worktree `~/missclaude-worktrees/<name>/` exists, the
  console runs THERE as a **feature worker** (edit code, commit only after `YES COMMIT`).
"""


# ---------------------------------------------------------------------------
# Mission scaffolding templates
# ---------------------------------------------------------------------------
def scaffold(name):
    """Return {filename: initial_contents} for a fresh mission."""
    return {
        "DASHBOARD.md": (
            f"# {name} — Dashboard\n\n"
            "> **Claude instruction**\n"
            f"> {CLAUDE_INSTRUCTION}\n\n"
            "## Status\n\n_Not started._\n\n"
            "## Objective\n\n_What is this mission trying to achieve?_\n\n"
            "## Current focus\n\n- \n"
        ),
        "PLAN.md": (
            f"# {name} — Plan\n\n"
            "## Steps\n\n- [ ] \n\n## Open questions\n\n- \n"
        ),
        "HOSTS.md": (
            f"# {name} — Hosts\n\n"
            "| host | role | access | notes |\n"
            "|------|------|--------|-------|\n"
            "|      |      |        |       |\n"
        ),
        "LOG.md": (
            f"# {name} — Log\n\n"
            "_Append newest entries at the top. Record meaningful work._\n\n"
        ),
        "HANDOFF.md": (
            f"# {name} — Handoff\n\n"
            "_Write this before stopping: current state, what's next, blockers._\n\n"
            "## State\n\n## Next\n\n## Blockers\n"
        ),
        "DECISIONS.md": (
            f"# {name} — Decisions\n\n"
            "_Durable decisions and their rationale (newest at top)._\n\n"
        ),
    }


# ---------------------------------------------------------------------------
# Filesystem helpers (all confined to MISSIONS_DIR)
# ---------------------------------------------------------------------------
def safe_name(name):
    return bool(name) and bool(NAME_RE.match(name)) and name not in (".", "..")


def mission_path(name, *parts):
    """Resolve a path inside a mission and assert it stays under MISSIONS_DIR."""
    if not safe_name(name):
        raise ValueError("bad mission name")
    p = os.path.realpath(os.path.join(MISSIONS_DIR, name, *parts))
    root = os.path.realpath(os.path.join(MISSIONS_DIR, name))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("path escapes mission directory")
    return p


def create_worktree(name):
    """Create — or attach to — the dev git worktree for a mission. Returns None on
    success, or a human-readable error string to show the operator. Never raises.

    Mirrors scripts/claude-miss' Case B: `git worktree add <WORKTREES_DIR>/<name>
    -b claude/<name> <BASE_BRANCH>`, run inside PRIMARY_REPO. If the worktree dir
    already exists, attach (reuse it) — no git run — so an operator who made the
    worktree earlier in a terminal can still 'create' the dev mission here."""
    if not safe_name(name):
        return "Invalid mission name."
    wt = os.path.join(WORKTREES_DIR, name)
    if os.path.isdir(wt):
        return None  # already a dev worktree — attach, nothing to do
    try:
        r = subprocess.run(
            ["git", "-C", PRIMARY_REPO, "worktree", "add",
             wt, "-b", "claude/" + name, BASE_BRANCH],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=30, text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return "Could not run git worktree add: %s" % e
    if r.returncode != 0:
        lines = (r.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else "git exited %d" % r.returncode
        return "git worktree add failed: %s" % tail
    return None


def merged_dev_missions():
    """Set of mission names whose claude/<name> branch is fully merged into
    BASE_BRANCH (working). One git call for the whole page; never raises —
    returns set() on any error so the dashboard still renders if git is
    unavailable. `git branch --merged working` lists every branch whose tip is
    reachable from working, i.e. has no unmerged commits left; we keep the
    claude/<name> branches and strip the prefix to recover mission names."""
    try:
        r = subprocess.run(
            ["git", "-C", PRIMARY_REPO, "branch", "--merged", BASE_BRANCH,
             "--format=%(refname:short)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if r.returncode != 0:
        return set()
    out = set()
    for line in r.stdout.splitlines():
        b = line.strip()
        if b.startswith("claude/"):
            out.add(b[len("claude/"):])
    return out


def list_missions():
    """Mission (name, mtime) pairs, sorted newest activity first.

    mtime is newest_mtime(dir) — the same "updated … ago" value shown on each
    card — so the dashboard order matches the timestamps users already see.
    Ties fall back to alphabetical for a stable, deterministic order.
    """
    if not os.path.isdir(MISSIONS_DIR):
        return []
    out = []
    for entry in sorted(os.listdir(MISSIONS_DIR)):
        d = os.path.join(MISSIONS_DIR, entry)
        if not os.path.isdir(d) or not safe_name(entry):
            continue
        out.append((entry, newest_mtime(d)))
    out.sort(key=lambda nm: (-nm[1], nm[0]))
    return out


def newest_mtime(d):
    """Most recent mtime of any file in a mission dir (recursively)."""
    latest = 0.0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                m = os.path.getmtime(os.path.join(root, f))
                if m > latest:
                    latest = m
            except OSError:
                pass
    if latest == 0.0:
        try:
            latest = os.path.getmtime(d)
        except OSError:
            pass
    return latest


# ---------------------------------------------------------------------------
# tmux session control — a mission's Claude console lives in a tmux session named
# "mission-<name>" (created by console-launch.sh). tmux is the persistence layer:
# killing a session does NOT touch the mission directory, and reopening the mission
# reloads the ttyd iframe, which re-runs console-launch.sh and recreates the session.
# NOTE: the dashboard must share the tmux server's socket namespace to see these
# (the console runs tmux under the default /tmp socket); see README / the service unit.
# ---------------------------------------------------------------------------
TMUX = os.environ.get("MISSION_TMUX", "tmux")
SESSION_PREFIX = "mission-"


def _run_tmux(*args, capture=False):
    """Run a tmux subcommand. Returns (rc, stdout). Never raises — a missing/unreachable
    tmux server just yields a non-zero rc so callers treat it as 'no sessions'."""
    try:
        r = subprocess.run(
            [TMUX, *args],
            stdout=(subprocess.PIPE if capture else subprocess.DEVNULL),
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        )
        return r.returncode, (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def running_sessions():
    """Set of mission names that currently have a tmux session at all — Claude may or may
    not still be running inside it (see claude_sessions() for that). Used to decide whether
    the index shows a kill (✕) button: a session whose Claude has exited has fallen back to
    a login shell and still needs to be killable. One `tmux list-sessions` call (no N+1)."""
    rc, out = _run_tmux("list-sessions", "-F", "#{session_name}", capture=True)
    if rc != 0:
        return set()
    return {
        line[len(SESSION_PREFIX):]
        for line in out.splitlines()
        if line.startswith(SESSION_PREFIX)
    }


def claude_sessions():
    """Set of mission names whose console has Claude ACTUALLY RUNNING — not merely a tmux
    session that has fallen back to its `bash --login` shell (console-session.sh runs
    `claude … || claude …` and then `exec bash`, so an exited/never-started Claude leaves a
    live-but-idle pane). tmux's pane_current_command is no help: it reports the pane leader
    (console-session.sh / login bash) even while Claude runs as its child. So we take one
    `ps` snapshot and walk each session's process subtree for a `claude` process — comm
    starts with "claude", which also catches the `claude-miss*` launch wrappers on their way
    up. comm carries no path/args, so a mission dir or name containing "claude" can't cause a
    false match. One `tmux list-panes` + one `ps`, then a pure-Python walk — no per-mission
    N+1; live names are always a subset of running_sessions()."""
    rc, panes = _run_tmux(
        "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}", capture=True
    )
    if rc != 0:
        return set()
    pane_pids = {}  # mission name -> [pane pid, ...] (usually one pane, but allow several)
    for line in panes.splitlines():
        sn, _, pid = line.partition("\t")
        if sn.startswith(SESSION_PREFIX) and pid.isdigit():
            pane_pids.setdefault(sn[len(SESSION_PREFIX):], []).append(int(pid))
    if not pane_pids:
        return set()
    # One process snapshot: pid, parent pid, and the short command name only.
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if r.returncode != 0:
        return set()
    children = {}  # ppid -> [pid, ...]
    comm = {}      # pid -> command name
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        pid, ppid, cmd = int(parts[0]), int(parts[1]), parts[2]
        comm[pid] = cmd
        children.setdefault(ppid, []).append(pid)

    def subtree_has_claude(root):
        stack, seen = [root], set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if comm.get(pid, "").startswith("claude"):
                return True
            stack.extend(children.get(pid, ()))
        return False

    return {
        name
        for name, pids in pane_pids.items()
        if any(subtree_has_claude(p) for p in pids)
    }


def session_running(name):
    """True if this mission's tmux session exists. '=' forces an exact-name match."""
    rc, _ = _run_tmux("has-session", "-t", "=" + SESSION_PREFIX + name)
    return rc == 0


def kill_session(name):
    """Stop a mission's Claude session cleanly (does NOT delete the mission). Sends the
    Claude TUI an EOF (Ctrl-D) so it exits gracefully and flushes its transcript, gives it
    a moment to finish writing, then ends the tmux session as a backstop. The next open
    re-creates the session, which RESUMES the conversation (console-session.sh runs
    `claude --continue`). Returns True if a session was running and is now gone.

    Targeting note: the '=' exact-match prefix only works for session commands
    (has-session/kill-session); pane commands (send-keys/list-panes) reject it. So we
    resolve this session's exact pane id and target that — globally unique, no prefix-match
    risk, and not dependent on pane_current_command (Claude runs under a wrapper, so that
    field reads 'bash' and can't tell us whether Claude is up)."""
    session = SESSION_PREFIX + name
    if not session_running(name):
        return False
    pane = None
    rc, out = _run_tmux(
        "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}", capture=True
    )
    if rc == 0:
        for line in out.splitlines():
            sn, _, pid = line.partition("\t")
            if sn == session and pid:
                pane = pid
                break
    if pane:
        # Graceful exit: Escape clears any partial input/mode, then Ctrl-D (EOF) makes
        # Claude flush its transcript and quit. Transcripts stream to disk continuously,
        # so a short settle is enough before we tear the session down.
        _run_tmux("send-keys", "-t", pane, "Escape")
        time.sleep(0.2)
        _run_tmux("send-keys", "-t", pane, "C-d")
        time.sleep(1.5)
    # Backstop: end the session regardless — removes the residual `bash --login` fallback
    # (so the mission stops showing as 'live') and covers a busy session that ignored EOF.
    _run_tmux("kill-session", "-t", "=" + session)
    return not session_running(name)


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (FileNotFoundError, IsADirectoryError):
        return ""


def write_text_atomic(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def fmt_time(ts):
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def time_tag(ts):
    """A <time> element carrying the epoch so the client can render a *live*
    relative label ('5s ago', '1h 15m ago'). The absolute string is both the
    no-JS fallback (element text) and the hover tooltip; renderRelTimes() in the
    page JS overwrites the visible text with the relative form. Epoch is
    timezone-agnostic — it renders in the viewer's local zone."""
    if not ts:
        return "—"
    abs_str = html.escape(fmt_time(ts), quote=True)
    return f'<time class=rel data-ts="{int(ts)}" title="{abs_str}">{abs_str}</time>'


def _day_epoch(y, mo, d):
    """Local-midnight epoch for a Y-M-D date (used as the day-granular fallback
    timestamp for Log entries that predate per-entry epoch markers)."""
    try:
        return time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1))
    except (OverflowError, ValueError):
        return 0


def append_log_entry(name, text):
    """Prepend a timestamped bullet to LOG.md under today's `## YYYY-MM-DD`
    heading (created if absent, as the newest day on top). The epoch is stamped
    as an invisible `<!--t:EPOCH-->` marker the Log renderer turns into a live
    relative time — so logging keeps second precision without hand-typed dates.
    Newest-first is preserved: the entry goes directly under today's heading,
    and a freshly created day-heading is inserted above any older ones."""
    path = mission_path(name, "LOG.md")
    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    entry = f"- <!--t:{now}--> {text.strip()}"
    md = read_text(path) if os.path.isfile(path) else f"# {name} — Log\n\n"
    lines = md.replace("\r\n", "\n").split("\n")
    heading = f"## {today}"

    idx = next((j for j, ln in enumerate(lines) if ln.strip() == heading), None)
    if idx is not None:
        lines.insert(idx + 1, entry)            # newest entry first within the day
    else:
        first = next((j for j, ln in enumerate(lines) if ln.startswith("## ")), None)
        block = [heading, entry, ""]
        if first is not None:
            lines[first:first] = block          # newest day above older days
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(block)

    new_md = "\n".join(lines)
    if not new_md.endswith("\n"):
        new_md += "\n"
    write_text_atomic(path, new_md)


def human_size(n):
    units = ["B", "K", "M", "G", "T"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)}{units[i]}"
    return f"{f:.1f}{units[i]}"


def _strip_md(s):
    """Drop inline markdown markers for a clean plain-text summary."""
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return s.strip()


def dashboard_summary(name, max_lines=3):
    """First few non-empty content lines of DASHBOARD.md (skipping the H1 title
    and the Claude-instruction blockquote)."""
    txt = read_text(mission_path(name, "DASHBOARD.md"))
    lines = []
    seen_title = False
    for raw in txt.splitlines():
        s = raw.strip()
        if not s or s.startswith(">"):
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
            if not seen_title:  # skip the mission's own H1 title line
                seen_title = True
                continue
        lines.append(_strip_md(s))
        if len(lines) >= max_lines:
            break
    return " · ".join(lines)


# ---------------------------------------------------------------------------
# Minimal markdown -> HTML (dependency-free)
# ---------------------------------------------------------------------------
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline(text):
    """Escape, then apply inline markdown. Code spans are protected first."""
    placeholders = []

    def stash_code(m):
        placeholders.append(m.group(1))
        return f"\x00{len(placeholders) - 1}\x00"

    # protect raw code spans before escaping the rest
    text = _INLINE_CODE.sub(stash_code, text)
    text = html.escape(text, quote=False)
    text = _LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}" '
        f'rel="noopener noreferrer">{html.escape(m.group(1))}</a>',
        text,
    )
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", text)

    def restore(m):
        code = html.escape(placeholders[int(m.group(1))])
        return f"<code>{code}</code>"

    text = re.sub(r"\x00(\d+)\x00", restore, text)
    return text


_LOG_TS = re.compile(r"^<!--t:(\d+)-->\s*(.*)$")


def _log_time_tag(ts, day=False):
    """A <time> for a Log entry. `day=True` flags the day-granular fallback so
    the client renders 'today' / 'yesterday' / 'Nd ago' instead of seconds."""
    abs_str = html.escape(fmt_time(ts), quote=True)
    extra = " data-day=1" if day else ""
    return (f'<time class="rel logtime" data-ts="{int(ts)}"{extra} '
            f'title="{abs_str}">{abs_str}</time> ')


def md_to_html(md, log_mode=False):
    """Render a useful subset of markdown to HTML. When log_mode is set, list
    items are timestamped: an explicit `<!--t:EPOCH-->` marker becomes a live
    relative time, and any unmarked bullet inherits the enclosing
    `## YYYY-MM-DD` heading's date as a day-granular fallback."""
    out = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    n = len(lines)
    para = []
    cur_day_ts = 0  # most recent `## YYYY-MM-DD` heading date (log_mode only)

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            flush_para()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue

        # blank line
        if not stripped:
            flush_para()
            i += 1
            continue

        # horizontal rule
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            htext = m.group(2).strip()
            if log_mode:
                dm = re.match(r"^(\d{4})-(\d{2})-(\d{2})\b", htext)
                if dm:
                    cur_day_ts = _day_epoch(
                        int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
                    )
            out.append(f"<h{level}>{_inline(htext)}</h{level}>")
            i += 1
            continue

        # blockquote
        if stripped.startswith(">"):
            flush_para()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(_inline(x) for x in buf) + "</blockquote>")
            continue

        # table (header row + separator row)
        if "|" in stripped and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
            flush_para()
            def cells(row):
                row = row.strip()
                if row.startswith("|"):
                    row = row[1:]
                if row.endswith("|"):
                    row = row[:-1]
                return [c.strip() for c in row.split("|")]

            header = cells(lines[i])
            i += 2  # skip header + separator
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(cells(lines[i]))
                i += 1
            thead = "".join(f"<th>{_inline(c)}</th>" for c in header)
            body = ""
            for r in rows:
                body += "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
            out.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>")
            continue

        # unordered list
        if re.match(r"^[-*+]\s+", stripped):
            flush_para()
            items = []
            while i < n and re.match(r"^[-*+]\s+", lines[i].strip()):
                item = re.sub(r"^[-*+]\s+", "", lines[i].strip())
                # per-entry epoch marker ("- <!--t:EPOCH--> text") -> live time;
                # else, in log_mode, fall back to the enclosing date heading.
                tprefix = ""
                mt = _LOG_TS.match(item)
                if mt:
                    item = mt.group(2)
                    tprefix = _log_time_tag(int(mt.group(1)))
                elif log_mode and cur_day_ts:
                    tprefix = _log_time_tag(cur_day_ts, day=True)
                # checkbox support
                cb = re.match(r"^\[([ xX])\]\s*(.*)$", item)
                if cb:
                    checked = "checked" if cb.group(1).lower() == "x" else ""
                    item = f'<input type="checkbox" disabled {checked}> ' + _inline(cb.group(2))
                    items.append(f"<li class='task'>{tprefix}{item}</li>")
                else:
                    items.append(f"<li>{tprefix}{_inline(item)}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        # ordered list
        if re.match(r"^\d+[.)]\s+", stripped):
            flush_para()
            items = []
            while i < n and re.match(r"^\d+[.)]\s+", lines[i].strip()):
                item = re.sub(r"^\d+[.)]\s+", "", lines[i].strip())
                items.append(f"<li>{_inline(item)}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue

        # default: paragraph text
        para.append(stripped)
        i += 1

    flush_para()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTML shell + styling
# ---------------------------------------------------------------------------
STYLE = """
:root { --fg:#1d2127; --muted:#6b7280; --line:#e3e6ea; --accent:#2f6f4f;
        --bg:#fafbfc; --card:#fff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
a { color:var(--accent); }
.wrap { max-width:960px; margin:0 auto; padding:0 20px 60px; }
header.top { background:var(--accent); color:#fff; padding:14px 0; margin-bottom:24px; }
header.top .wrap { padding-bottom:0; display:flex; align-items:baseline; gap:14px; }
header.top a { color:#fff; text-decoration:none; }
header.top h1 { font-size:18px; margin:0; }
header.top .sub { color:#d7e6dd; font-size:13px; }
h1,h2,h3 { line-height:1.25; }
.muted { color:#6b7280; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; margin:0 0 12px; }
.card h2 { margin:0 0 4px; font-size:17px; }
.card h2 a { text-decoration:none; }
.meta { font-size:12.5px; color:#6b7280; }
.summary { margin:6px 0 0; color:#374151; }
.badge { display:inline-block; font-size:11px; padding:2px 7px; border-radius:10px;
  border:1px solid var(--line); color:#374151; background:#f3f5f7; }
.badge.ok { background:#e7f4ec; border-color:#bfe0cc; color:#1f6b41; }
.badge.warn { background:#fdf2e3; border-color:#f0d9ad; color:#8a5a12; }
.tabs { display:flex; flex-wrap:wrap; gap:4px; border-bottom:1px solid var(--line);
  margin:18px 0 16px; }
.tabs a { padding:8px 13px; text-decoration:none; color:#374151; border:1px solid transparent;
  border-bottom:none; border-radius:6px 6px 0 0; font-size:14px; }
.tabs a.active { background:#fff; border-color:var(--line); color:var(--accent); font-weight:600;
  margin-bottom:-1px; }
.tabs a.changed { background:#fdf2e3; border-color:#f0d9ad; color:#8a5a12; font-weight:600; }
.tabs a.changed::after { content:"●"; font-size:9px; margin-left:6px; vertical-align:middle; color:#d9892b; }
.tabs a.changed.active::after { content:none; }
.rendered { background:#fff; border:1px solid var(--line); border-radius:8px; padding:6px 20px; }
.rendered pre { background:#f6f8fa; padding:12px; border-radius:6px; overflow:auto; }
.rendered code { background:#f0f2f4; padding:1px 5px; border-radius:4px; font-size:13px; }
.rendered pre code { background:none; padding:0; }
.rendered table { border-collapse:collapse; width:100%; }
.rendered th,.rendered td { border:1px solid var(--line); padding:6px 9px; text-align:left; }
.rendered blockquote { border-left:3px solid var(--accent); margin:10px 0; padding:4px 14px;
  background:#f3f7f4; color:#33503f; }
.rendered li.task { list-style:none; margin-left:-20px; }
.rendered time.logtime { display:inline-block; margin-right:6px; font-size:12px;
  color:#8a8f98; font-variant-numeric:tabular-nums; }
.logadd { display:flex; gap:8px; margin:0 0 14px; }
.logadd input[type=text] { flex:1; }
textarea { width:100%; min-height:460px; font:13.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  padding:12px; border:1px solid var(--line); border-radius:8px; resize:vertical; }
.btn { display:inline-block; background:var(--accent); color:#fff; border:none; padding:8px 16px;
  border-radius:6px; font-size:14px; cursor:pointer; text-decoration:none; }
.btn.secondary { background:#fff; color:#374151; border:1px solid var(--line); }
.row { display:flex; gap:10px; align-items:center; margin-top:10px; }
form.inline { display:flex; gap:8px; align-items:center; }
input[type=text] { padding:8px 10px; border:1px solid var(--line); border-radius:6px; font-size:14px; }
.toolbar { display:flex; gap:8px; margin:0 0 12px; }
.notice { background:#e7f4ec; border:1px solid #bfe0cc; color:#1f6b41; padding:8px 12px;
  border-radius:6px; margin-bottom:14px; font-size:14px; }
.empty { color:#6b7280; padding:30px 0; }
.card.running { border-color:#2f6fed; box-shadow:0 0 0 1px #2f6fed; }
.card.merged { border-color:#2f6f4f; box-shadow:0 0 0 1px #2f6f4f; }
.cardhead { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
.cardhead h2 { margin:0; }
.badge.live { background:#e8f0fe; border-color:#bcd2fb; color:#1a56db; }
.badge.idle { background:#f3f4f6; border-color:#d8dce2; color:#6b7280; }
.killform { margin:0; flex:0 0 auto; }
.killbtn { background:#fff; color:#b42318; border:1px solid #f0c4be; border-radius:6px;
  padding:1px 9px; font-size:15px; line-height:1.5; cursor:pointer; }
.killbtn:hover { background:#fdecea; border-color:#e0a59d; }
.claude-box { background:#f3f7f4; border:1px solid #bfe0cc; border-radius:8px; padding:12px 16px;
  margin:0 0 16px; font-size:13.5px; }
.claude-box .lbl { font-weight:600; color:#1f6b41; cursor:pointer; }
.claude-box[open] .lbl { margin-bottom:4px; }
.files td { padding:6px 10px; border-bottom:1px solid var(--line); font-size:14px; }
.files th { text-align:left; padding:6px 10px; font-size:12px; color:#6b7280; }
.console-region { margin:6px 0 4px; width:min(100vw - 96px, 1600px);
  margin-left:50%; transform:translateX(-50%); }
.console-frame { display:block; width:100%; height:55vh; border:1px solid var(--line);
  border-radius:8px; background:#0b0e14; }
.console-resizer { height:10px; margin-top:4px; border-radius:6px; cursor:ns-resize;
  background:#eef1f4; }
.console-resizer:hover { background:#dfe3e8; }
.console-dragmask { position:fixed; inset:0; z-index:9999; cursor:ns-resize; }
"""


# Shared relative-time renderer. Injected on EVERY page (index + mission), so the
# mission-list cards get live ages without pulling in the bigger mission-page JS.
# It walks `time.rel[data-ts]`, rewrites the visible text to a 2-unit relative
# label, and reschedules itself fast (1s) while anything is <60s old, else slow
# (30s). Exposes window.renderRelTimes() so in-page fragment swaps can refresh
# immediately after replacing content.
REL_JS = """
<script>
(function() {
  function rel(sec) {
    if (sec < 0) sec = 0;
    if (sec < 60) return sec <= 0 ? "just now" : sec + "s ago";
    var m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return s ? m + "m " + s + "s ago" : m + "m ago";
    var h = Math.floor(m / 60); m = m % 60;
    if (h < 24) return m ? h + "h " + m + "m ago" : h + "h ago";
    var d = Math.floor(h / 24); h = h % 24;
    return h ? d + "d " + h + "h ago" : d + "d ago";
  }
  function day(ts) {                 // day-granular fallback (legacy log entries)
    var now = new Date(), then = new Date(ts * 1000);
    var a = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var b = new Date(then.getFullYear(), then.getMonth(), then.getDate());
    var n = Math.round((a - b) / 86400000);
    return n <= 0 ? "today" : (n === 1 ? "yesterday" : n + "d ago");
  }
  var fresh = false;
  function render() {
    fresh = false;
    var now = Math.floor(Date.now() / 1000);
    document.querySelectorAll("time.rel[data-ts]").forEach(function(el) {
      var ts = parseInt(el.getAttribute("data-ts"), 10);
      if (!ts) return;
      if (el.hasAttribute("data-day")) { el.textContent = day(ts); return; }
      var age = now - ts;
      if (age < 60) fresh = true;
      el.textContent = rel(age);
    });
  }
  function tick() { render(); window.setTimeout(tick, fresh ? 1000 : 30000); }
  window.renderRelTimes = render;
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", tick);
  else tick();
})();
</script>
"""


def page(title, body, active_mission=None):
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
        '<header class=top><div class=wrap>'
        '<h1><a href="/">⛳ Miss Claude</a></h1>'
        + (f'<span class=sub>{html.escape(LABEL)}</span>' if LABEL else '')
        + "</div></header>"
        f'<div class=wrap>{body}</div>{REL_JS}</body></html>'
    )


def tok_q():
    """Token query-string suffix to keep links authenticated, if token is set."""
    return f"?token={urllib.parse.quote(TOKEN)}" if TOKEN else ""


# ===========================================================================
# REMOTE CONSOLES  (optional side feature — self-contained add-on)
# Runs Claude ON another host over SSH, wrapped in a tmux session on THIS host
# (no tmux on the remote side). Launch shape (blank name — legacy shared console):
#     ssh -tt <host> 'cd <dir> && claude --continue --dangerously-skip-permissions'
# (--continue resumes the last conversation in that dir; falls back to a fresh session.)
# With a NAME, the launcher instead keys a deterministic session id off host|dir|name
# and runs `claude --resume <id> || claude --session-id <id>` so each name is its own
# resumable conversation. See console-launch.sh for the exact remote command.
# The default mission workflow is untouched. To remove the feature entirely, delete:
#   (1) this fenced block, (2) the one `/remote` route branch in do_GET (marked
#   "REMOTE CONSOLES"), and (3) the one link line in render_index (same marker).
# Plus the matching fenced branch in console-launch.sh. No other code references it.
# ---------------------------------------------------------------------------
# host: an ssh target (config alias / hostname / user@host). dir: an absolute path.
# Strict allow-lists, kept in sync with console-launch.sh: host is passed as its own
# argv element and dir is single-quoted into the remote command (single-quote thus
# forbidden), so neither can break out of the launch command.
# \Z (not $) anchors the true end of string: Python's $ also matches just before a
# trailing newline, so it would accept "www\n"/"/tmp\n". \Z makes these byte-for-byte
# equivalent to the bash [[ =~ ]] re-validation in console-launch.sh (no newline slips
# through even if a future caller drops the .strip() below).
REMOTE_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}\Z")
REMOTE_DIR_RE = re.compile(r"^/[A-Za-z0-9 ._/@:+-]{0,255}\Z")
# Optional display name for a remote console — purely cosmetic (sets the browser
# tab title), never reaches a shell, so it only needs to be HTML-safe + bounded.
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9 ._/@:&()#+-]{1,64}\Z")

# Resizer-only JS for the remote page (the mission page's MISSION_JS also polls tab
# state, which a remote console has none of). Same drag/persist behaviour.
REMOTE_RESIZER_JS = """
<script>
(function() {
  var frame = document.getElementById("console-frame");
  var grip  = document.getElementById("console-resizer");
  if (!frame || !grip) return;
  var KEY = "missclaude.consoleH";
  var saved = parseInt(localStorage.getItem(KEY), 10);
  if (saved) frame.style.height = saved + "px";
  function clamp(h){ return Math.max(160, Math.min(h, 2 * (window.innerHeight - 120))); }
  grip.addEventListener("pointerdown", function(e) {
    e.preventDefault();
    var startY = e.clientY, startH = frame.getBoundingClientRect().height;
    var mask = document.createElement("div");
    mask.className = "console-dragmask";
    document.body.appendChild(mask);
    function move(ev){ frame.style.height = clamp(startH + (ev.clientY - startY)) + "px"; }
    function up(){
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      mask.remove();
      localStorage.setItem(KEY, Math.round(frame.getBoundingClientRect().height));
    }
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  });
  grip.addEventListener("dblclick", function(){
    frame.style.height = ""; localStorage.removeItem(KEY);
  });
})();
</script>
"""


def _remote_console_url(host_header, rhost, rdir, rname=""):
    """ttyd URL for a remote console. ttyd's --url-arg turns each ?arg= into a
    positional arg of console-launch.sh: here `remote <host> <dir> [name]`.
    The name (when set) is passed as a 4th arg so the launcher can key a
    distinct, resumable session off it (blank name = the legacy shared session)."""
    host = (host_header or "").rsplit(":", 1)[0] or "localhost"
    url = (
        f"http://{host}:{CONSOLE_TTYD_PORT}/?arg=remote"
        f"&arg={urllib.parse.quote(rhost)}&arg={urllib.parse.quote(rdir)}"
    )
    if rname:
        url += f"&arg={urllib.parse.quote(rname)}"
    return url


def render_remote_page(host_header, rhost="", rdir="", rname=""):
    """The /remote page: a host+dir form and, once both are valid, the live console
    iframe (Claude running on the remote host). Stateless — no stored list; reopening
    the same host+dir re-attaches the same tmux session (named in console-launch.sh).
    rname is an optional label: it becomes the browser tab title AND keys a distinct,
    resumable Claude session — a given name always resumes its own conversation, a new
    name starts a separate one. Blank name keeps the legacy shared (resume-last) console."""
    rhost = (rhost or "").strip()
    rdir = (rdir or "").strip()
    rname = (rname or "").strip()
    if not REMOTE_NAME_RE.match(rname):
        rname = ""
    submitted = bool(rhost or rdir)
    valid = bool(REMOTE_HOST_RE.match(rhost) and REMOTE_DIR_RE.match(rdir))
    tokfield = (f'<input type=hidden name=token value="{html.escape(TOKEN, quote=True)}">'
                if TOKEN else "")
    body = [
        '<div class=card>'
        f'<p class=meta><a href="/{tok_q()}">← missions</a></p>'
        '<h2>Remote console</h2>'
        '<p class=muted style="font-size:13px">Run Claude on another host over SSH, '
        'in a tmux session on this host (nothing is installed/changed on the remote '
        'beyond starting Claude). Leave <em>name</em> blank to resume the last conversation '
        'in that dir; give a name to keep a distinct, resumable console — the same name '
        'always resumes its own conversation, a new name starts a separate one. '
        'Equivalent to:<br>'
        "<code>ssh -tt &lt;host&gt; 'cd &lt;dir&gt; &amp;&amp; claude --continue "
        "--dangerously-skip-permissions'</code></p>"
        # GET form: a method=get form drops any query string in `action`, so the token
        # (if any) must ride as a hidden field, not via tok_q() on the action.
        '<form class=inline method=get action="/remote">'
        + tokfield
        + '<input type=text name=name size=18 placeholder="name (optional, for the tab)" '
          f'value="{html.escape(rname, quote=True)}" '
          'pattern="[A-Za-z0-9 ._/@:&()#+-]{1,64}" '
          'title="optional label shown as the browser tab title">'
        + '<input type=text name=host placeholder="host (e.g. www or user@host)" '
          f'value="{html.escape(rhost, quote=True)}" '
          'pattern="[A-Za-z0-9][A-Za-z0-9._@-]*" '
          'title="ssh target: a ~/.ssh/config alias, hostname, or user@host" required>'
          '<input type=text name=dir size=34 placeholder="/home/youruser/project" '
          f'value="{html.escape(rdir, quote=True)}" '
          'pattern="/[A-Za-z0-9 ._/@:+-]*" '
          'title="absolute start directory on the remote host" required>'
          '<button class=btn type=submit>Open remote console</button>'
        '</form>'
        '</div>'
    ]
    if submitted and not valid:
        body.append(
            '<div class=notice>Invalid host or directory. Host: a config alias, hostname, '
            'or user@host. Dir: an absolute path (no single quotes).</div>'
        )
    if valid:
        url = _remote_console_url(host_header, rhost, rdir, rname)
        body.append('<div class=console-region>')
        body.append(
            '<div class=meta style="margin-bottom:6px">'
            + (f'<strong>{html.escape(rname)}</strong> · ' if rname else '')
            + 'Claude on '
            f'<code>{html.escape(rhost)}</code> in <code>{html.escape(rdir)}</code> · '
            f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
            'open in new tab ↗</a></div>'
        )
        body.append(
            f'<iframe class=console-frame id=console-frame src="{html.escape(url, quote=True)}" '
            'title="Remote Claude console"></iframe>'
        )
        body.append(
            '<div class=console-resizer id=console-resizer role=separator '
            'aria-orientation=horizontal '
            'title="Drag to resize · double-click to reset"></div>'
        )
        body.append('</div>')  # /console-region
        body.append(REMOTE_RESIZER_JS)
    title = f"{rname} · Remote console" if rname else "Remote console"
    return page(title, "\n".join(body))
# === end REMOTE CONSOLES ===


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------
def render_index(notice=""):
    missions = list_missions()
    running = running_sessions()   # tmux session exists (drives the ✕ kill button)
    live_set = claude_sessions()   # Claude actually running (drives the blue border + badge)
    merged_set = merged_dev_missions()   # dev branches fully merged into working (green border)
    body = []
    if notice:
        body.append(f'<div class=notice>{html.escape(notice)}</div>')

    body.append(
        '<div class=card><form class=inline method=post action="/create'
        + tok_q()
        + '">'
        '<input type=text name=name placeholder="new-mission-name" '
        'pattern="[A-Za-z0-9 ._-]+" title="letters, numbers, spaces, . _ - (spaces become dashes)" required>'
        '<button class=btn type=submit>+ Create mission</button>'
        '<button class=btn type=submit name=dev value=1 '
        'title="Also create a git worktree (branch claude/&lt;name&gt;) so the console '
        'runs as a dev feature worker">+ Create dev mission</button>'
        '<span class=muted style="font-size:12.5px">'
        'ops: creates ~/missions/&lt;name&gt;/ &nbsp;·&nbsp; dev: also adds a worktree'
        '</span>'
        "</form></div>"
    )
    # REMOTE CONSOLES add-on: one unobtrusive link to the optional /remote page.
    body.append(
        '<p class=meta style="margin:2px 2px 14px">'
        f'<a href="/remote{tok_q()}">🖥 Remote console</a> '
        '<span class=muted style="font-size:12.5px">— run Claude on another host '
        '(optional)</span></p>'
    )

    if not missions:
        body.append('<div class=empty>No missions yet. Create one above.</div>')
    for name, mtime in missions:
        d = mission_path(name)
        summ = dashboard_summary(name)
        handoff = mission_path(name, "HANDOFF.md")
        has_handoff = os.path.isfile(handoff) and os.path.getsize(handoff) > 0
        if has_handoff:
            hb = f'<span class="badge ok">handoff · {time_tag(os.path.getmtime(handoff))}</span>'
        else:
            hb = '<span class="badge warn">no handoff</span>'
        href = f"/m/{urllib.parse.quote(name)}/dashboard" + tok_q()
        has_session = name in running
        is_live = name in live_set
        # Green outline when the dev branch claude/<name> is fully merged into working —
        # the work landed. Live (an active Claude session) is the stronger signal and wins,
        # so a merged mission only goes green once it's idle.
        is_merged = name in merged_set and not is_live
        # Blue outline + "● live" badge only when Claude is actually running. A session that
        # exists but whose Claude has exited (fallen back to a login shell) shows "○ idle".
        # The kill (✕) button appears for either, so an idle session is still clearable.
        if is_live:
            card_cls = "card running"
        elif is_merged:
            card_cls = "card merged"
        else:
            card_cls = "card"
        mb = (
            f' <span class="badge ok" title="Branch claude/{html.escape(name)} '
            'is fully merged into working">merged</span>'
        ) if is_merged else ""
        if is_live:
            live = ' <span class="badge live">● live</span>'
        elif has_session:
            live = (
                ' <span class="badge idle" title="Session open but Claude has exited — '
                'reopen the mission to start/resume it">○ idle</span>'
            )
        else:
            live = ""
        if has_session:
            kill_action = f"/m/{urllib.parse.quote(name)}/kill" + tok_q()
            kill_btn = (
                f'<form class=killform method=post action="{kill_action}" '
                "onsubmit=\"return confirm('Stop the Claude session for "
                f"{html.escape(name)}? It exits cleanly and resumes where it left off when you reopen the mission.')\">"
                '<button class=killbtn type=submit title="Stop session (resumes on reopen)" '
                'aria-label="Stop session (resumes on reopen)">✕</button></form>'
            )
        else:
            kill_btn = ""
        body.append(
            f'<div class="{card_cls}">'
            '<div class=cardhead>'
            f'<h2><a href="{href}">{html.escape(name)}</a></h2>'
            f"{kill_btn}"
            "</div>"
            f'<div class=meta>updated {time_tag(mtime)}{live} &nbsp; {dev_badge(name)}{mb} &nbsp; {hb}</div>'
            + (f'<p class=summary>{html.escape(summ)}</p>' if summ else "")
            + "</div>"
        )
    return page("Missions", "\n".join(body))


def render_tabs(name, active):
    # Each tab keeps a real href (full-page route — works with JS disabled) plus
    # a data-tab hook the in-page JS uses to intercept clicks and toggle the
    # "changed" highlight. See the inlined script in render_mission_page.
    items = []
    for key in TAB_KEYS:
        cls = "active" if key == active else ""
        href = f"/m/{urllib.parse.quote(name)}/{key}" + tok_q()
        items.append(
            f'<a class="{cls}" data-tab="{key}" href="{href}">{TAB_LABEL[key]}</a>'
        )
    return '<nav class=tabs id=tabs>' + "".join(items) + "</nav>"


def dev_badge(name):
    # Pure directory-existence read (no git): if a same-named worktree exists, the console
    # runs there as a feature worker (see console-launch.sh), so flag the mission "dev".
    # `name` is already validated by NAME_RE; still exclude .|.. before joining a path.
    # Shared by the mission header and the mission-list cards so they never drift.
    if name not in (".", "..") and os.path.isdir(os.path.join(WORKTREES_DIR, name)):
        return (
            f'<span class="badge" title="Console runs in the dev worktree '
            f'{html.escape(os.path.join(WORKTREES_DIR, name))}">dev · claude/{html.escape(name)}</span>'
        )
    return (
        '<span class="badge idle" title="No dev worktree — console runs in the '
        'mission folder">ops</span>'
    )


def render_mission_header(name):
    badge = dev_badge(name)
    return (
        f'<div class=meta style="margin-bottom:2px"><a href="/{tok_q()}">← all missions</a></div>'
        f"<h1 style='margin:4px 0 0'>{html.escape(name)} {badge}</h1>"
    )


def file_tab_inner(name, tab, saved=False):
    """Inner HTML for a markdown tab — no mission header / tab nav / page chrome.
    Shared by the initial page render and the ?fragment=1 endpoint."""
    fn = TAB_FILE[tab]
    path = mission_path(name, fn)
    content = read_text(path)
    exists = os.path.isfile(path)
    mtime = os.path.getmtime(path) if exists else 0
    body = []

    if saved:
        body.append('<div class=notice>Saved.</div>')

    body.append(
        f'<div class=meta style="margin-bottom:8px"><strong>{fn}</strong> · '
        f"{('last saved ' + time_tag(mtime)) if exists else 'new file'}</div>"
    )

    # Log tab: a quick-add box that POSTs to the timestamping append endpoint
    # (stamps a per-entry epoch marker) instead of a raw file edit.
    if tab == "log":
        log_action = f"/m/{urllib.parse.quote(name)}/log/append" + tok_q()
        body.append(
            f'<form class=logadd method=post action="{log_action}">'
            '<input type=hidden name=ui value=1>'
            '<input type=text name=text placeholder="Add a log entry…" '
            'autocomplete=off required>'
            '<button class=btn type=submit>+ Log</button>'
            "</form>"
        )

    # rendered view
    body.append('<div class=rendered>' + (md_to_html(content, log_mode=(tab == "log")) if content.strip() else '<p class=muted>(empty)</p>') + "</div>")

    # edit form
    action = f"/m/{urllib.parse.quote(name)}/{tab}" + tok_q()
    body.append(
        '<details class=editor style="margin-top:16px"><summary class=btn style="display:inline-block">✎ Edit</summary>'
        f'<form class=editform method=post action="{action}" style="margin-top:12px">'
        f'<textarea name=content spellcheck=false>{html.escape(content)}</textarea>'
        '<div class=row><button class=btn type=submit>Save</button>'
        '<span class=muted>writes directly to '
        f"{html.escape(fn)}</span></div>"
        "</form></details>"
    )
    return "\n".join(body)


def tab_inner(name, tab, saved=False):
    """Dispatcher: inner HTML for any tab (markdown or the special artifacts list)."""
    if tab == "artifacts":
        return artifacts_tab_inner(name)
    return file_tab_inner(name, tab, saved=saved)


def artifacts_tab_inner(name):
    """Inner HTML for the Artifacts tab — no page chrome (see tab_inner)."""
    body = []
    for sub in ARTIFACT_DIRS:
        d = mission_path(name, sub)
        body.append(f"<h2>{sub}/</h2>")
        if not os.path.isdir(d):
            body.append('<p class=muted>(directory missing)</p>')
            continue
        rows = []
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, mission_path(name))
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                href = (
                    f"/m/{urllib.parse.quote(name)}/raw/"
                    + urllib.parse.quote(rel)
                    + tok_q()
                )
                rows.append(
                    f"<tr><td><a href=\"{href}\">{html.escape(rel)}</a></td>"
                    f"<td class=muted>{human_size(st.st_size)}</td>"
                    f"<td class=muted>{time_tag(st.st_mtime)}</td></tr>"
                )
        if rows:
            body.append(
                '<table class=files><thead><tr><th>file</th><th>size</th><th>modified</th>'
                "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
            )
        else:
            body.append('<p class=muted>(empty)</p>')
        body.append(
            f'<p class=meta>Drop files into <code>~/missions/{html.escape(name)}/{sub}/</code> '
            "to have them appear here.</p>"
        )
    return "\n".join(body)


def _console_url(name, host_header):
    """Build the ttyd URL for a mission, deriving the host from the request's
    Host header (so it works regardless of which name/IP reached the dashboard)."""
    host = (host_header or "").rsplit(":", 1)[0] or "localhost"
    return (
        f"http://{host}:{CONSOLE_TTYD_PORT}/"
        f"?arg={urllib.parse.quote(name)}"
    )


def tab_state(name):
    """Per-tab last-modified time (epoch seconds), for the polling endpoint.
    File tabs use the file mtime; the artifacts tab uses the newest mtime across
    its artifacts/ + scans/ dirs."""
    state = {}
    for key in TAB_KEYS:
        if key == "artifacts":
            latest = 0.0
            for sub in ARTIFACT_DIRS:
                d = mission_path(name, sub)
                if os.path.isdir(d):
                    m = newest_mtime(d)
                    if m > latest:
                        latest = m
            state[key] = latest
        else:
            path = mission_path(name, TAB_FILE[key])
            state[key] = os.path.getmtime(path) if os.path.isfile(path) else 0.0
    return state


# Inlined client JS for the single mission page. Templated with the JSON-escaped
# mission name and the token query suffix so every fetch stays authenticated.
MISSION_JS = """
<script>
(function() {
  var MISSION = %(name_js)s;
  var TOK = %(tok_js)s;            // "" or "token=..."; url() prefixes "?"/"&" as needed
  var POLL_MS = 3000;
  var base = "/m/" + encodeURIComponent(MISSION) + "/";
  function url(path, q) {
    var u = base + path;
    if (TOK) u += (u.indexOf("?") === -1 ? "?" : "&") + TOK;
    if (q) u += (u.indexOf("?") === -1 ? "?" : "&") + q;
    return u;
  }
  var tabsNav = document.getElementById("tabs");
  var content = document.getElementById("tabcontent");
  var active = tabsNav.querySelector("a.active");
  active = active ? active.getAttribute("data-tab") : "dashboard";
  var seen = null;                 // baseline mtimes; null until first poll

  function tabLink(tab) { return tabsNav.querySelector('a[data-tab="' + tab + '"]'); }

  // Is the editor for the current content open and dirty? Used to pause refresh.
  function editing() {
    var d = content.querySelector("details.editor");
    if (!d || !d.open) return false;
    var ta = d.querySelector("textarea");
    return ta ? ta.value !== ta.defaultValue : false;
  }

  function wireForm() {
    var form = content.querySelector("form.editform");
    if (!form) return;
    form.addEventListener("submit", function(ev) {
      ev.preventDefault();
      var ta = form.querySelector("textarea");
      var body = "content=" + encodeURIComponent(ta ? ta.value : "");
      fetch(form.getAttribute("action"), {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: body
      }).then(function() { load(active, true); });
    });
  }

  function load(tab, saved) {
    return fetch(url(tab, saved ? "fragment=1&saved=1" : "fragment=1"))
      .then(function(r) { return r.text(); })
      .then(function(html) {
        content.innerHTML = html;
        if (window.renderRelTimes) window.renderRelTimes();  // live ages in new fragment
        active = tab;
        var lnk = tabLink(tab);
        if (lnk) { lnk.classList.add("active"); lnk.classList.remove("changed"); }
        tabsNav.querySelectorAll("a").forEach(function(a) {
          if (a.getAttribute("data-tab") !== tab) a.classList.remove("active");
        });
        wireForm();
      });
  }

  // Tab clicks -> in-page swap (console iframe untouched). href stays a real
  // route for no-JS fallback.
  tabsNav.addEventListener("click", function(ev) {
    var a = ev.target.closest("a[data-tab]");
    if (!a) return;
    ev.preventDefault();
    var tab = a.getAttribute("data-tab");
    if (seen) seen[tab] = lastState ? lastState[tab] : seen[tab];  // clear pending change
    load(tab, false);
  });

  var lastState = null;
  function poll() {
    fetch(url("state")).then(function(r) { return r.json(); }).then(function(st) {
      lastState = st;
      if (seen === null) { seen = st; return; }   // first poll = baseline, no highlight
      Object.keys(st).forEach(function(tab) {
        if (st[tab] > (seen[tab] || 0)) {
          if (tab === active) {
            if (!editing()) { seen[tab] = st[tab]; load(tab, false); }
            // else: leave highlighted+stale until save/close; do not advance seen
          } else {
            var lnk = tabLink(tab);
            if (lnk) lnk.classList.add("changed");
          }
        }
      });
    }).catch(function() {});
  }

  function wireResizer() {
    var frame = document.getElementById("console-frame");
    var grip  = document.getElementById("console-resizer");
    if (!frame || !grip) return;
    var KEY = "missclaude.consoleH";
    var saved = parseInt(localStorage.getItem(KEY), 10);
    if (saved) frame.style.height = saved + "px";
    function clamp(h){ return Math.max(160, Math.min(h, 2 * (window.innerHeight - 120))); }
    grip.addEventListener("pointerdown", function(e) {
      e.preventDefault();
      var startY = e.clientY, startH = frame.getBoundingClientRect().height;
      var mask = document.createElement("div");
      mask.className = "console-dragmask";
      document.body.appendChild(mask);
      function move(ev){ frame.style.height = clamp(startH + (ev.clientY - startY)) + "px"; }
      function up(){
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        mask.remove();
        localStorage.setItem(KEY, Math.round(frame.getBoundingClientRect().height));
      }
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    });
    grip.addEventListener("dblclick", function(){
      frame.style.height = ""; localStorage.removeItem(KEY);   // back to the 55vh default
    });
  }

  wireForm();
  wireResizer();
  poll();
  setInterval(poll, POLL_MS);
})();
</script>
"""


def render_mission_page(name, host_header, active="dashboard"):
    """The single mission page: a persistent Console region on top, the tab nav,
    and an in-page content container the JS swaps tab fragments into. Falls back
    to full-page tab routes when JS is disabled."""
    if active not in TAB_KEYS:
        active = "dashboard"
    url = _console_url(name, host_header)
    body = [render_mission_header(name)]
    body.append(
        '<details class=claude-box><summary class=lbl>Claude instruction</summary>'
        + html.escape(CLAUDE_INSTRUCTION)
        + "</details>"
    )
    body.append('<div class=console-region>')
    body.append(
        '<div class=meta style="margin-bottom:6px">tmux session '
        f"<code>mission-{html.escape(name)}</code> · "
        f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
        "open in new tab ↗</a></div>"
    )
    body.append(
        f'<iframe class=console-frame id=console-frame src="{html.escape(url, quote=True)}" '
        'title="Claude console"></iframe>'
    )
    body.append(
        '<div class=console-resizer id=console-resizer role=separator '
        'aria-orientation=horizontal '
        'title="Drag to resize · double-click to reset"></div>'
    )
    body.append("</div>")  # /console-region
    body.append(render_tabs(name, active))
    body.append('<div id=tabcontent>' + tab_inner(name, active) + "</div>")
    body.append(MISSION_JS % {
        "name_js": json.dumps(name),
        "tok_js": json.dumps(f"token={urllib.parse.quote(TOKEN)}" if TOKEN else ""),
    })
    return page(f"{name} · {TAB_LABEL[active]}", "\n".join(body))


# ---------------------------------------------------------------------------
# Static file types for artifact downloads
# ---------------------------------------------------------------------------
TEXTY = {".md", ".txt", ".log", ".json", ".csv", ".yaml", ".yml", ".conf", ".cfg", ".ini", ".sh", ".py"}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "MissionDashboard/1.0"

    # ---- helpers ----------------------------------------------------------
    def _authed(self, qs):
        if not TOKEN:
            return True
        if qs.get("token", [""])[0] == TOKEN:
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return "mt" in cookie and cookie["mt"].value == TOKEN

    def _send_html(self, body, status=HTTPStatus.OK, extra_headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if TOKEN:
            self.send_header("Set-Cookie", f"mt={TOKEN}; Path=/; HttpOnly; SameSite=Strict")
        for k, v in (extra_headers or {}):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, obj, status=HTTPStatus.OK):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def _error(self, status, msg):
        self._send_html(page("Error", f'<div class=card><h2>{status.value} {status.phrase}</h2>'
                              f'<p class=muted>{html.escape(msg)}</p>'
                              f'<p><a href="/{tok_q()}">← home</a></p></div>'), status)

    # ---- GET --------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if not self._authed(qs):
            return self._error(HTTPStatus.UNAUTHORIZED, "Missing or bad token.")

        if path == "/" or path == "":
            return self._send_html(render_index())

        # REMOTE CONSOLES add-on: the optional /remote page (host+dir form + console).
        if path == "/remote":
            return self._send_html(render_remote_page(
                self.headers.get("Host", ""),
                qs.get("host", [""])[0],
                qs.get("dir", [""])[0],
                qs.get("name", [""])[0],
            ))

        # /m/<name>/...
        m = re.match(r"^/m/([^/]+)/(.+)$", path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            rest = m.group(2)
            if not safe_name(name) or not os.path.isdir(mission_path(name)):
                return self._error(HTTPStatus.NOT_FOUND, "No such mission.")

            # mtime poll endpoint (drives in-page freshness + tab highlights)
            if rest == "state":
                return self._send_json(tab_state(name))

            # Console is no longer a standalone view; bounce old links to the page.
            if rest == "console":
                return self._redirect(
                    f"/m/{urllib.parse.quote(name)}/dashboard" + tok_q()
                )

            raw = re.match(r"^raw/(.+)$", rest)
            if raw:
                return self._serve_raw(name, urllib.parse.unquote(raw.group(1)))

            if rest in TAB_KEYS:
                saved = qs.get("saved", [""])[0] == "1"
                # ?fragment=1 -> just the tab's inner HTML (for in-page swaps);
                # otherwise the full single mission page with this tab preselected.
                if qs.get("fragment", [""])[0] == "1":
                    return self._send_html(tab_inner(name, rest, saved=saved))
                return self._send_html(
                    render_mission_page(name, self.headers.get("Host", ""), active=rest)
                )

            return self._error(HTTPStatus.NOT_FOUND, "Unknown tab.")

        # bare /m/<name> -> dashboard
        m2 = re.match(r"^/m/([^/]+)/?$", path)
        if m2:
            name = urllib.parse.unquote(m2.group(1))
            return self._redirect(f"/m/{urllib.parse.quote(name)}/dashboard" + tok_q())

        return self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def do_HEAD(self):
        self.do_GET()

    def _serve_raw(self, name, relpath):
        try:
            full = mission_path(name, relpath)
        except ValueError:
            return self._error(HTTPStatus.FORBIDDEN, "Bad path.")
        # only allow files under artifacts/ or scans/
        allowed = any(
            full == mission_path(name, sub) or full.startswith(mission_path(name, sub) + os.sep)
            for sub in ARTIFACT_DIRS
        )
        if not allowed or not os.path.isfile(full):
            return self._error(HTTPStatus.NOT_FOUND, "No such artifact.")
        ext = os.path.splitext(full)[1].lower()
        ctype = "text/plain; charset=utf-8" if ext in TEXTY else "application/octet-stream"
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._error(HTTPStatus.NOT_FOUND, "Cannot read file.")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if ctype.startswith("application/"):
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(full)}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---- POST -------------------------------------------------------------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if not self._authed(qs):
            return self._error(HTTPStatus.UNAUTHORIZED, "Missing or bad token.")

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)
        path = parsed.path

        if path == "/create":
            name = (form.get("name", [""])[0]).strip()
            name = re.sub(r"\s+", "-", name)        # spaces -> dashes
            name = re.sub(r"-{2,}", "-", name).strip("-")  # collapse/trim dashes
            if not safe_name(name):
                return self._error(HTTPStatus.BAD_REQUEST,
                                   "Invalid name (use letters, numbers, spaces, . _ - only).")
            d = mission_path(name)
            if os.path.exists(d):
                return self._send_html(render_index(f'Mission "{name}" already exists.'))
            # DEV mission: create (or attach to) the worktree FIRST — the only fallible
            # step — so a git failure leaves no half-built mission behind.
            if form.get("dev", [""])[0] == "1":
                err = create_worktree(name)
                if err:
                    return self._send_html(render_index(
                        f'Could not create dev mission "{name}": {err}'))
            os.makedirs(d, exist_ok=True)
            for sub in ARTIFACT_DIRS:
                os.makedirs(mission_path(name, sub), exist_ok=True)
            for fn, contents in scaffold(name).items():
                write_text_atomic(mission_path(name, fn), contents)
            return self._redirect(f"/m/{urllib.parse.quote(name)}/dashboard" + tok_q())

        # kill a mission's running tmux/Claude session (keeps the mission dir).
        # Must come before the tab-save match below, since "kill" matches [a-z]+.
        mk = re.match(r"^/m/([^/]+)/kill$", path)
        if mk:
            name = urllib.parse.unquote(mk.group(1))
            if not safe_name(name) or not os.path.isdir(mission_path(name)):
                return self._error(HTTPStatus.NOT_FOUND, "No such mission.")
            killed = kill_session(name)
            note = (f'Stopped the session for "{name}" cleanly — reopening the mission resumes the conversation.'
                    if killed else f'No running session for "{name}".')
            return self._send_html(render_index(note))

        # append a timestamped entry to LOG.md (stamps a per-entry epoch marker).
        # Distinct path from the tab-save route (POST /m/<name>/log) on purpose —
        # body field is `text` (not `content`). `ui=1` => redirect back to the tab.
        la = re.match(r"^/m/([^/]+)/log/append$", path)
        if la:
            name = urllib.parse.unquote(la.group(1))
            if not safe_name(name) or not os.path.isdir(mission_path(name)):
                return self._error(HTTPStatus.NOT_FOUND, "No such mission.")
            text = (form.get("text", [""])[0]).strip()
            if not text:
                return self._error(HTTPStatus.BAD_REQUEST, "Empty log entry.")
            append_log_entry(name, text)
            if form.get("ui", [""])[0] == "1":
                return self._redirect(f"/m/{urllib.parse.quote(name)}/log" + tok_q())
            return self._send_html("ok\n")

        # save a tab file
        m = re.match(r"^/m/([^/]+)/([a-z]+)$", path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            tab = m.group(2)
            if not safe_name(name) or not os.path.isdir(mission_path(name)):
                return self._error(HTTPStatus.NOT_FOUND, "No such mission.")
            if tab not in TAB_FILE:
                return self._error(HTTPStatus.BAD_REQUEST, "Cannot save this tab.")
            content = form.get("content", [""])[0]
            # normalise newlines, ensure trailing newline
            content = content.replace("\r\n", "\n")
            if content and not content.endswith("\n"):
                content += "\n"
            write_text_atomic(mission_path(name, TAB_FILE[tab]), content)
            return self._redirect(f"/m/{urllib.parse.quote(name)}/{tab}" + tok_q() +
                                  ("&" if TOKEN else "?") + "saved=1")

        return self._error(HTTPStatus.NOT_FOUND, "Not found.")

    # quieter logging
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    os.makedirs(MISSIONS_DIR, exist_ok=True)
    # Drop standing mission orientation where every ops console auto-loads it.
    # Write-if-absent so operator hand-edits to the live file are never clobbered.
    claude_md = os.path.join(MISSIONS_DIR, "CLAUDE.md")
    if not os.path.exists(claude_md):
        write_text_atomic(claude_md, MISSIONS_CLAUDE_MD)
    # Default stdlib listen backlog is 5, which overflows under a burst of
    # concurrent browser connections (kernel logs "possible SYN flooding on
    # :4200" + drops/slows connects). Raise it well under net.core.somaxconn.
    class _Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True
    httpd = _Server((HOST, PORT), Handler)
    auth = "token required" if TOKEN else "no app auth"
    print(f"Mission Dashboard listening on http://{HOST}:{PORT}  "
          f"missions={MISSIONS_DIR}  [{auth}]", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
