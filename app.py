#!/usr/bin/env python3
# (test: inconsequential no-op change)
"""Mission Dashboard — a tiny, dependency-free web UI for ops "missions".

A "mission" is just a directory under MISSIONS_DIR (default ~/missions) holding
plain markdown files (DASHBOARD.md, PLAN.md, HOSTS.md, LOG.md, HANDOFF.md,
DECISIONS.md) plus artifacts/ and scans/ subdirs. This app reads and writes
those files in the browser. The files stay normal text — edit them outside the
app any time; this is only a convenience layer.

Pure Python 3 standard library. No pip, no venv, no internet, no database.

Config (environment):
  MISSION_PORT   listen port      (default 4200)
  MISSION_HOST   bind address     (default 0.0.0.0 — firewalld restricts who reaches it)
  MISSIONS_DIR   data directory   (default ~/missions)
  MISSION_TOKEN  optional shared secret; if set, requests must carry ?token=... or
                 the mt cookie. OFF by default (the firewall source-IP allowlist is
                 the security boundary on this box).
"""

import glob
import html
import json
import os
import random
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("MISSION_PORT", "4200"))
HOST = os.environ.get("MISSION_HOST", "0.0.0.0")
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
# Port of the ttyd "Claude Console" bridge (claude-console.service). The Console tab
# iframes http://<this-host>:CONSOLE_TTYD_PORT/?arg=<mission>.
CONSOLE_TTYD_PORT = int(os.environ.get("CONSOLE_TTYD_PORT", "4201"))
# Short label shown next to the title in the UI header. Defaults to this host's
# short hostname; set MISSION_LABEL="" to hide it.
_label = os.environ.get("MISSION_LABEL")
LABEL = (_label if _label is not None else socket.gethostname().split(".")[0]).strip()
# Committer identity for commits the dashboard itself makes (auto-init of a
# dev-mission repo, local and remote).
GIT_NAME = os.environ.get("MISSION_GIT_NAME", "Miss Claude")
GIT_EMAIL = os.environ.get("MISSION_GIT_EMAIL", "miss-claude@localhost")
# The running user's home + the standing docs the console prompts point Claude at.
# The memory-index path mirrors Claude Code's project-dir munge (home with "/"->"-").
HOME_DIR = os.path.expanduser("~")
FLEET_DOC = os.path.join(HOME_DIR, "CLAUDE.md")
MEMORY_INDEX = os.path.join(
    HOME_DIR, ".claude", "projects", HOME_DIR.replace("/", "-"), "memory", "MEMORY.md"
)

# Claude Code's per-session transcripts (used to surface each console's current
# context usage on the page). One dir per project (= console cwd, munged); one
# *.jsonl per session. See mission_context(). DEFAULT_CONTEXT_WINDOW is the usual
# 200k denominator; _context_window_for() bumps it for the Opus 1M beta.
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
DEFAULT_CONTEXT_WINDOW = 200_000

# Claude subscription PLAN usage (the 5-hour session + weekly rate limits the
# `claude` CLI's /usage view shows). THIS IS THE ONE PLACE THE DASHBOARD TOUCHES
# THE NETWORK / READS THE OAUTH CREDENTIAL — it is otherwise stdlib-only + offline
# + firewall-gated. The operator authorized this explicitly; see plan_usage().
# Endpoint, auth, and response shape confirmed live 2026-06-26 against the
# installed claude CLI (a read-only GET; consumes no message quota).
CLAUDE_CREDS = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
PLAN_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PLAN_USAGE_TTL = 60  # s — a GLOBAL value shared by every client; poll the API ≤ once/min
_plan_usage_cache = {"at": 0.0, "data": None}

# Short rolling history of the 5-hour SESSION window's utilization, kept ONLY to
# project a time-to-100% ("~full in Xh Ym") for the dashboard. Each real API refresh
# (so ≤ once/min, matching PLAN_USAGE_TTL) appends one (epoch, percent) sample; we
# keep the trailing ~45 min and fit a least-squares slope (percent/sec) to smooth out
# the bursty, quantized percent. Reset-aware: when resets_at rolls forward the percent
# drops back toward 0, so those older samples belong to a FINISHED window — we clear
# them on a resets_at change. In-memory only (a service restart just re-warms over a
# few minutes). See _record_session_history().
SESSION_HISTORY_WINDOW = 45 * 60   # s of samples to fit the slope over
SESSION_HISTORY_MIN_SPAN = 5 * 60  # s — need at least this span before estimating
_session_history = []              # [(epoch, percent)], oldest-first
_session_reset_at = {"iso": None}  # last seen resets_at, to detect a window roll

# This app's install dir (the primary checkout in production, a worktree under test).
# Used to locate the dev-guard bundle we ship to a REMOTE dev mission's host: the
# prevent-misswork.py PreToolUse hook + the settings file that wires it, plus the
# scripts/ship-rails.sh helper that copies+verifies them on the remote. Keeping the
# bundle = the live repo files means a remote dev console runs the SAME guard as a
# local one. See ensure_remote_rails() / create_remote_worktree() / console-launch.sh.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SHIP_RAILS = os.path.join(APP_DIR, "scripts", "ship-rails.sh")
# Base-branch charset for a REMOTE worktree: the value is shlex-quoted into a remote
# shell command, so this is a sanity gate (clean error), not the security boundary.
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z")

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
    "(it stamps a per-entry time; newest entries go on top). "
    f"Launch from {HOME_DIR} so the fleet CLAUDE.md and the accumulated fleet "
    "memory load; if you were started elsewhere, read "
    f"{MEMORY_INDEX} (the memory index) "
    f"and {FLEET_DOC} before acting."
)

# Written to MISSIONS_DIR/CLAUDE.md on startup if absent (see main()). Because
# MISSIONS_DIR is a parent of every ~/missions/<name>/, Claude Code auto-loads
# this for every ops console — standing orientation about how missions work,
# with no per-mission clutter. Write-if-absent, so operator hand-edits survive.
MISSIONS_CLAUDE_MD = f"""\
# Missions — how mission consoles work

This directory (`~/missions/`) holds **missions**: each `~/missions/<name>/` is a folder of
markdown the Mission Dashboard (port {PORT}) views and edits. This file auto-loads for every ops
console; the fleet doc `{FLEET_DOC}` and the fleet memory index also apply.

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

    curl -s -d "text=<entry>" http://127.0.0.1:{PORT}/m/<mission>/log/append

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


# Two short word lists for auto-naming a mission when the operator leaves the name
# blank in the Spawn modal. Joined with a dash (e.g. "brave-otter"); the result is
# always safe_name()-clean. Kept small + dependency-free (stdlib `random` only).
_NAME_ADJ = (
    "amber", "brave", "calm", "clever", "cosmic", "crisp", "dusky", "eager",
    "fizzy", "gentle", "golden", "happy", "jolly", "lucky", "mellow", "nimble",
    "plucky", "quiet", "rapid", "rusty", "shiny", "silver", "snappy", "sunny",
    "swift", "teal", "vivid", "witty", "zesty", "bold",
)
_NAME_NOUN = (
    "otter", "falcon", "maple", "cedar", "comet", "harbor", "lantern", "meadow",
    "pebble", "quartz", "raven", "river", "summit", "thicket", "willow", "badger",
    "cobalt", "ember", "fjord", "glacier", "heron", "ibis", "juniper", "kestrel",
    "lynx", "marlin", "narwhal", "orchid", "puffin", "walrus",
)


def random_mission_name(exists=os.path.exists):
    """Generate a two-word `adjective-noun` mission name not already taken.
    `exists(path)` lets callers inject the collision check (defaults to the real
    filesystem via mission_path). Falls back to a numeric suffix after a few tries."""
    for _ in range(20):
        n = "%s-%s" % (random.choice(_NAME_ADJ), random.choice(_NAME_NOUN))
        if not exists(mission_path(n)):
            return n
    # Extremely unlikely; keep it deterministic-ish and still unique.
    n = "%s-%s-%d" % (random.choice(_NAME_ADJ), random.choice(_NAME_NOUN),
                      random.randint(100, 999))
    return n


def mission_path(name, *parts):
    """Resolve a path inside a mission and assert it stays under MISSIONS_DIR."""
    if not safe_name(name):
        raise ValueError("bad mission name")
    p = os.path.realpath(os.path.join(MISSIONS_DIR, name, *parts))
    root = os.path.realpath(os.path.join(MISSIONS_DIR, name))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("path escapes mission directory")
    return p


# ---------------------------------------------------------------------------
# Per-mission metadata (mission.json sidecar)
# ---------------------------------------------------------------------------
# Each mission MAY carry a ~/missions/<name>/mission.json describing its mode
# (ops/dev/console) and target (where the console works). It is what lets the
# dev rails point at ANY local repo instead of only the mission-dashboard repo.
# Absent or malformed file => legacy mission: behave exactly as before (infer
# ops, unless a same-named worktree exists => dev-on-PRIMARY_REPO). The console
# launcher reads the same file; keep the two in sync. See mission_target().
def read_mission_meta(name):
    """Parsed mission.json dict, or None when absent/malformed. Never raises — a
    bad file is treated as a legacy (meta-less) mission, never eval'd."""
    try:
        p = mission_path(name, "mission.json")
    except ValueError:
        return None
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def write_mission_meta(name, meta):
    """Atomically write a mission's mission.json (via write_text_atomic)."""
    write_text_atomic(mission_path(name, "mission.json"),
                      json.dumps(meta, indent=2) + "\n")


def mission_target(name):
    """Normalized {mode, target, [dev]} for a mission, with legacy fallback.

    When mission.json is present and well-formed it wins. Otherwise infer from the
    filesystem the way the dashboard always has: a same-named worktree under
    WORKTREES_DIR => a dev mission on PRIMARY_REPO/BASE_BRANCH; else an ops mission
    whose console runs in the mission folder. Shared by dev_badge(),
    merged_dev_missions() and (mirrored) the console launcher so they never drift."""
    meta = read_mission_meta(name)
    if meta and isinstance(meta.get("target"), dict) and meta.get("mode"):
        # Unknown mode strings (hand-edited sidecars — e.g. "local") are treated as
        # ops so the badges and the console launcher (which normalizes identically)
        # agree on where the console runs instead of silently diverging.
        if meta.get("mode") not in ("ops", "dev", "console"):
            meta = dict(meta, mode="ops")
        return meta
    wt = os.path.join(WORKTREES_DIR, name)
    if name not in (".", "..") and os.path.isdir(wt):
        return {
            "mode": "dev",
            "target": {"kind": "local-repo", "path": PRIMARY_REPO},
            "dev": {"repo": PRIMARY_REPO, "base_branch": BASE_BRANCH, "worktree": wt},
        }
    return {"mode": "ops", "target": {"kind": "local-dir", "path": ""}}


def mission_location(name):
    """Human-readable (host, directory) where this mission's console works, for
    display near the mission name. `host` is None for a local target (this
    jumpbox); otherwise the ssh target the console runs on. `directory` is the
    path the console works in on that host. Derived from mission_target() so it
    never drifts from the launcher/dashboard."""
    tgt = mission_target(name)
    target = tgt.get("target") or {}
    dev = tgt.get("dev") or {}
    # Remote (ops/console or dev): host + the path the console works in there.
    host = target.get("host") or dev.get("host")
    if host:
        return host, (dev.get("worktree") or target.get("remote_dir") or "")
    # Local dev mission: the worktree it develops in.
    if tgt.get("mode") == "dev":
        return None, (dev.get("worktree") or os.path.join(WORKTREES_DIR, name))
    # Local ops/console: a chosen dir if set, else the mission folder.
    return None, (target.get("path") or mission_path(name))


# ---------------------------------------------------------------------------
# Console context usage (read from Claude Code's own session transcripts)
# ---------------------------------------------------------------------------
# Surfaces each running console's CURRENT Claude context size on the page. Ported
# from the integrator-validated prototype (mission artifacts/ctx_proto.py); see that
# mission's FINDINGS.md for the bugs each rule fixes. Pure stdlib. Reads
# ~/.claude/projects/<munged-cwd>/<session>.jsonl, the live session's last usage.
def console_cwd(name):
    """Where this mission's console runs, derived from mission_target() so it can
    never drift from the launcher/dashboard. Returns (cwd, remote_bool); a remote
    console keeps its transcripts on the remote host, so the caller shows n/a.

    Mirrors console-launch.sh's working-dir choice:
      - dev mission           -> dev.worktree (remote-repo => remote, n/a)
      - ops/console at a local target.path -> that path
      - otherwise (legacy ops, empty path) -> the mission folder ~/missions/<name>
    FINDINGS #2: a sidecar-less mission with a same-named worktree is a dev console
    in that worktree — mission_target() already handles that inference for us.

    A live LOCAL console overrides the guess: its real cwd is read from /proc, so the
    badge can't drift from where `claude` actually runs (e.g. the integrator console
    cd's into the repo, not the mission folder). The guess below is the fallback for
    when no local console is running."""
    tgt = mission_target(name)
    target = tgt.get("target") or {}
    if target.get("kind") in ("remote", "remote-repo"):
        return None, True
    if tgt.get("mode") == "dev" and (tgt.get("dev") or {}).get("host"):
        return None, True               # defensive: remote dev (kind already caught it)
    live = _live_console_cwd(name)
    if live:
        return live, False
    if tgt.get("mode") == "dev":
        dev = tgt.get("dev") or {}
        if dev.get("worktree"):
            return dev["worktree"], False
        return mission_path(name), False
    # ops/console: a chosen local dir if set, else the mission folder
    if target.get("path"):
        return target["path"], False
    return mission_path(name), False


def _project_dir_for_cwd(cwd):
    """Claude Code's per-project transcript dir: the abs cwd with every non
    [A-Za-z0-9] char replaced by '-' (case preserved; '/' and '.' both become '-').
    FINDINGS #1: verified against the real dir names under ~/.claude/projects."""
    munged = "".join(c if c.isalnum() else "-" for c in cwd)
    return os.path.join(PROJECTS_DIR, munged)


def _tail_lines(path, want_bytes=131072):
    """Yield non-empty lines from the END of a (possibly large) file, newest first,
    reading only the last want_bytes (FINDINGS #6 — transcripts run to megabytes;
    never slurp them). The caller widens the window once if the last usage is far
    back. Drops a partial first line when not reading from the start."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - want_bytes)
        fh.seek(start)
        chunk = fh.read()
    if start > 0:
        nl = chunk.find(b"\n")
        chunk = chunk[nl + 1:] if nl != -1 else b""
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if line:
            yield line


def _usage_tokens(usage):
    """Current context occupancy from a message.usage block (FINDINGS #3): input +
    cache-creation + cache-read; output_tokens is NOT part of the context."""
    if not isinstance(usage, dict):
        return None
    return (usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0))


def _context_window_for(tokens, model):
    """Context-window denominator. FINDINGS #5: 200k is not universal — an Opus
    1M-beta console genuinely runs past 200k (seen live at 228,930). There is NO
    authoritative per-session signal to read: the transcript carries no `betas`
    marker or context-window field, and the model id is identical ('claude-opus-4-8')
    on the 200k and 1M windows (confirmed 2026-06-26 against the cmiss-release 1M
    session). So we infer: bump to 1M once usage exceeds 200k, keeping the bar <=100%."""
    return 1_000_000 if tokens > DEFAULT_CONTEXT_WINDOW else DEFAULT_CONTEXT_WINDOW


def latest_context(cwd):
    """Current context for the LIVE session at `cwd` = the newest transcript by mtime
    in the cwd's project dir. FINDINGS #4: no cross-session fallback — a restarted
    console leaves older session files whose stale numbers would mislead; if the live
    session has no usage yet, return {"state":"starting"}. FINDINGS #7: the cwd field
    is verified so a munge collision can't surface another mission's number.

    A /compact writes an `isCompactSummary` line but NO usage block; the true
    post-compact size is unknown until the next turn's API call. Scanning
    newest-first, if we meet that marker BEFORE any usage, the only usage we can
    find is the pre-compact (stale, high) one — so return {"state":"compacted"}
    rather than that misleading number. It self-corrects to "ok" on the next turn,
    whose fresh usage block then precedes the compact marker. Returns None when
    there's no transcript dir/file at all."""
    files = sorted(glob.glob(os.path.join(_project_dir_for_cwd(cwd), "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    if not files:
        return None
    target = os.path.realpath(cwd)
    f = files[0]                         # live session = most recently written
    # Walk newest-first, tracking each usage block's position relative to the
    # most-recent /compact marker so we can show the *impact* ("150k -> 26k")
    # instead of a bare word:
    #   newest = the newest usage of all (the live size when no compact is in play)
    #   post   = newest usage NEWER than the compact marker (the immediate
    #            post-compact size); `n_after` counts usages newer than the marker
    #   pre    = first usage OLDER than the compact marker (the stale pre-compact
    #            size we used to discard).
    newest = pre = post = cwd_seen = None
    n_after = 0
    saw_compact = False
    for want in (131072, 1_048_576):     # widen once if the markers are far back
        newest = pre = post = cwd_seen = None
        n_after = 0
        saw_compact = False
        for line in _tail_lines(f, want):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if cwd_seen is None and d.get("cwd"):
                cwd_seen = os.path.realpath(d["cwd"])
            if d.get("isCompactSummary") and not saw_compact:
                saw_compact = True       # the most-recent compaction
            msg = d.get("message")
            if isinstance(msg, dict) and msg.get("usage"):
                t = _usage_tokens(msg["usage"])
                if t:
                    pair = (t, msg.get("model"))
                    if newest is None:
                        newest = pair
                    if not saw_compact:
                        post = pair      # keep last -> usage right after the compact
                        n_after += 1
                    elif pre is None:
                        pre = pair       # first usage before the compact = stale size
        if newest is not None or saw_compact:
            break
    if cwd_seen is not None and cwd_seen != target:
        return None                      # munge collision -> not our dir

    def _ctx(pair):
        t, m = pair
        w = _context_window_for(t, m)
        return {"tokens": t, "model": m, "window": w, "pct": round(100 * t / w, 1)}

    if saw_compact:
        # A /compact dropped the live size; show how far it fell rather than the
        # stale pre-compact number. Past the immediate post-compact turn (n_after
        # >= 2) context has grown on its own again, so revert to the live figure.
        if n_after >= 2 and newest is not None:
            return {"state": "ok", **_ctx(newest)}
        out = {"state": "compacted"}
        if pre is not None:
            out["pre"] = _ctx(pre)
        if post is not None:             # exists once the first post-compact turn runs
            out["post"] = _ctx(post)
        return out
    if newest is None:
        return {"state": "starting"}
    return {"state": "ok", **_ctx(newest)}


def mission_context(name):
    """Public reader for the /m/<name>/context.json endpoint. Never raises: any
    unexpected error degrades to {"state":"none"} so a card never breaks. States:
    ok | starting | none | remote (FINDINGS #8)."""
    try:
        cwd, remote = console_cwd(name)
        if remote:
            return {"state": "remote"}
        info = latest_context(cwd)
        return info if info else {"state": "none"}
    except Exception:
        return {"state": "none"}


# ---------------------------------------------------------------------------
# Subscription plan usage (5-hour session + weekly rate limits)
# ---------------------------------------------------------------------------
def _oauth_access_token():
    """Current OAuth access token from the Claude CLI credential file, or None.
    A running mission console's `claude` keeps this refreshed on disk; we only
    read it. NEVER logged, and never returned to the browser (only the derived
    percentages are)."""
    try:
        with open(CLAUDE_CREDS, encoding="utf-8") as fh:
            oauth = (json.load(fh) or {}).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return None
    return oauth.get("accessToken") or None


def _fetch_plan_usage():
    """One read-only GET to the OAuth usage endpoint -> {state, session, weekly}.
    session/weekly are each {percent, resets_at, severity} or None. Never raises."""
    tok = _oauth_access_token()
    if not tok:
        return {"state": "none"}
    req = urllib.request.Request(PLAN_USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": "miss-claude-dashboard",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
    except Exception:
        return {"state": "none"}      # offline / 401 / bad JSON -> show nothing
    # The `limits` array is pre-bucketed (kind/group/percent/severity/resets_at);
    # fall back to the top-level five_hour/seven_day utilization objects.
    def pick(group, kind=None):
        for lim in d.get("limits") or []:
            if lim.get("group") == group and (kind is None or lim.get("kind") == kind):
                return {"percent": lim.get("percent"),
                        "resets_at": lim.get("resets_at"),
                        "severity": lim.get("severity")}
        return None

    def from_window(w):
        return {"percent": w.get("utilization"), "resets_at": w.get("resets_at"),
                "severity": "normal"} if isinstance(w, dict) else None

    session = pick("session") or from_window(d.get("five_hour"))
    weekly = pick("weekly", "weekly_all") or pick("weekly") or from_window(d.get("seven_day"))
    if session is None and weekly is None:
        return {"state": "none"}
    return {"state": "ok", "session": session, "weekly": weekly}


def _record_session_history(session, now):
    """Append the current 5-hour session utilization to the rolling history and
    return a least-squares projection of when it reaches 100%, as epoch MILLISECONDS
    (what the browser's Date() wants), or None if we can't/shouldn't estimate:
    no percent, not yet enough span, or usage flat/declining (idle). Reset-aware —
    a change in resets_at means a fresh window, so the finished window's samples are
    dropped before fitting. Never raises."""
    if not isinstance(session, dict):
        return None
    pct = session.get("percent")
    if pct is None:
        return None
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    resets_at = session.get("resets_at")
    hist = _session_history
    if resets_at != _session_reset_at["iso"]:   # new window -> old samples are stale
        _session_reset_at["iso"] = resets_at
        hist.clear()
    hist.append((now, pct))
    cutoff = now - SESSION_HISTORY_WINDOW       # keep only the trailing window
    while len(hist) > 2 and hist[0][0] < cutoff:
        hist.pop(0)
    if len(hist) < 3 or (hist[-1][0] - hist[0][0]) < SESSION_HISTORY_MIN_SPAN:
        return None                             # warming up — too little data yet
    n = len(hist)                               # least-squares slope, percent/sec
    mt = sum(t for t, _ in hist) / n
    mp = sum(p for _, p in hist) / n
    den = sum((t - mt) ** 2 for t, _ in hist)
    if den == 0:
        return None
    slope = sum((t - mt) * (p - mp) for t, p in hist) / den
    remaining = 100.0 - hist[-1][1]
    if slope <= 0 or remaining <= 0:            # idle/flat/declining -> no ETA
        return None
    return int((now + remaining / slope) * 1000)


def plan_usage():
    """Subscription plan usage for the dashboard's two fill-bars, cached
    PLAN_USAGE_TTL seconds (a global value, not per-mission — every client shares
    one cache so the API is polled at most once a minute). On each real refresh the
    session window's utilization is recorded so the response can carry a projected
    `eta_full_ms` (time-to-100%, epoch ms) on session. Never raises."""
    now = time.time()
    c = _plan_usage_cache
    if c["data"] is not None and now - c["at"] < PLAN_USAGE_TTL:
        return c["data"]
    data = _fetch_plan_usage()
    if isinstance(data, dict) and data.get("state") == "ok" \
            and isinstance(data.get("session"), dict):
        data["session"]["eta_full_ms"] = _record_session_history(data["session"], now)
    c["at"], c["data"] = now, data
    return c["data"]


def _ensure_local_repo(repo, base_branch):
    """Ensure <repo> exists and is a git repo, initializing a fresh one if it's missing
    or not yet a repo, so a dev mission can target a brand-new local repo. A new repo
    gets an initial commit on <base_branch> so `git worktree add -b … <base>` has a
    commit to branch from; if the directory already holds files (a non-git dir init'd
    in place) those files are staged into that initial commit so the claude/<name>
    worktree forks from the real content, not an empty tree. Returns None on success or
    a human-readable error. Never raises. An existing git repo is left untouched
    (caller's base/worktree handling applies)."""
    if not BRANCH_RE.match(base_branch):
        return "Invalid base branch (letters, numbers, . _ / - only)."
    if os.path.isdir(repo):
        try:
            chk = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--git-dir"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
            head = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "-q", "HEAD"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return "Could not run git in %s: %s" % (repo, e)
        if chk.returncode == 0 and head.returncode == 0:
            return None  # already a git repo with commits — use as-is
        # A git repo with NO commits yet (fresh `git init`, unborn HEAD) falls
        # through: `git worktree add … <base>` would fail with "invalid reference",
        # so give it the same branch + initial-commit treatment as a brand-new repo
        # (re-running `git init` on an existing repo is safe — it just reinitializes).
    # Missing dir, or a dir that isn't a git repo yet — initialize one. symbolic-ref
    # (vs. `init -b`) names the initial branch portably on older git, and the initial
    # commit makes <base_branch> a real ref so the subsequent `worktree add` can branch
    # off it. The inline user.name/email keep the commit from failing where git identity
    # is unset.
    try:
        os.makedirs(repo, exist_ok=True)
    except OSError as e:
        return "Could not create repo dir %s: %s" % (repo, e)
    steps = [
        ["git", "init", repo],
        ["git", "-C", repo, "symbolic-ref", "HEAD", "refs/heads/" + base_branch],
    ]
    for cmd in steps:
        try:
            r = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=30, text=True,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return "Could not initialize git repo at %s: %s" % (repo, e)
        if r.returncode != 0:
            lines = (r.stdout or "").strip().splitlines()
            tail = lines[-1] if lines else "git exited %d" % r.returncode
            return "Could not initialize git repo at %s: %s" % (repo, tail)
    # Best-effort stage of any pre-existing files (honoring a .gitignore if present) so the
    # initial commit captures real content. Tolerate failure — e.g. unreadable files under
    # the dir: the repo + commit must still succeed, and --allow-empty covers a truly empty
    # dir. Anything that fails to stage just won't be in <base> (no worse than before).
    try:
        subprocess.run(
            ["git", "-C", repo, "add", "-A"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    commit = ["git", "-C", repo, "-c", "user.email=" + GIT_EMAIL,
              "-c", "user.name=" + GIT_NAME, "commit", "--allow-empty",
              "-m", "Initial commit (miss-claude dev mission)"]
    try:
        r = subprocess.run(
            commit, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=120, text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return "Could not initialize git repo at %s: %s" % (repo, e)
    if r.returncode != 0:
        lines = (r.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else "git exited %d" % r.returncode
        return "Could not initialize git repo at %s: %s" % (repo, tail)
    return None


def _detect_base_branch(repo):
    """Best-guess base/staging branch for a LOCAL <repo>, used when the Spawn form's
    base field is left blank (hardcoding `main` broke both directions: missclaude
    stages on `working`, plenty of repos live on `master`). Preference order: a
    `working` branch if the repo has one (the Miss Claude staging convention), else
    the repo's currently checked-out branch (symbolic-ref works even on an unborn
    HEAD, returning the configured initial branch), else "main" — which is also the
    answer for a repo that doesn't exist yet (it becomes the initial branch
    _ensure_local_repo creates). Never raises."""
    def _git(*args):
        try:
            r = subprocess.run(
                ["git", "-C", repo, *args],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None
    if _git("show-ref", "--verify", "refs/heads/working") is not None:
        return "working"
    head = _git("symbolic-ref", "--short", "HEAD")
    return head or "main"


def create_worktree(name, repo=None, base_branch=None):
    """Create — or attach to — the dev git worktree for a mission. Returns None on
    success, or a human-readable error string to show the operator. Never raises.

    Mirrors scripts/claude-miss' Case B: `git worktree add <WORKTREES_DIR>/<name>
    -b claude/<name> <base_branch>`, run inside <repo>. `repo`/`base_branch` default
    to the Claude-Miss globals (PRIMARY_REPO/BASE_BRANCH) so the legacy call stays
    valid; a Spawn dev mission passes the operator-chosen local repo instead. If <repo>
    doesn't exist or isn't a git repo yet, a fresh repo is initialized (see
    _ensure_local_repo) so a dev mission can target a brand-new repo. If the worktree
    dir already exists, attach (reuse it) — no git run — so an operator who made the
    worktree earlier in a terminal can still 'create' the dev mission here."""
    repo = PRIMARY_REPO if repo is None else os.path.realpath(os.path.expanduser(repo))
    base_branch = BASE_BRANCH if base_branch is None else base_branch
    if not safe_name(name):
        return "Invalid mission name."
    err = _ensure_local_repo(repo, base_branch)
    if err:
        return err
    wt = os.path.join(WORKTREES_DIR, name)
    if os.path.isdir(wt):
        return None  # already a dev worktree — attach, nothing to do
    try:
        r = subprocess.run(
            ["git", "-C", repo, "worktree", "add",
             wt, "-b", "claude/" + name, base_branch],
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


def ensure_remote_rails(host):
    """Copy + VERIFY the dev guard (prevent-misswork.py + the settings that wires it)
    on a remote host, by delegating to scripts/ship-rails.sh. Returns None on success
    or a human-readable error string. Fail-closed: any failure means the guard is NOT
    confirmed present on the remote, so the caller must refuse to create/launch the
    remote dev mission — a remote dev console runs Claude --dangerously-skip-permissions
    and must never do so without its PreToolUse guardrail. Never raises."""
    if not os.path.isfile(SHIP_RAILS):
        return "Missing ship-rails.sh (expected %s)." % SHIP_RAILS
    try:
        r = subprocess.run(
            ["bash", SHIP_RAILS, host],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return "Could not run ship-rails.sh: %s" % e
    if r.returncode != 0:
        lines = (r.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else "ship-rails exited %d" % r.returncode
        return "Could not install/verify guard rails on %s: %s" % (host, tail)
    return None


def create_remote_worktree(name, host, repo, base_branch):
    """Create — or attach to — a dev git worktree for a mission on a REMOTE git repo
    over SSH, after shipping+verifying the guard rails there. Returns
    (worktree_path, resolved_base_branch, None) on success, or (None, None,
    error_string). Never raises.

    The remote analogue of create_worktree(): runs `git -C <repo> worktree add
    $HOME/missclaude-worktrees/<name> -b claude/<name> <base>` on the remote host (so
    $HOME expands to the remote user's home). An EMPTY base_branch means auto-detect
    on the remote (mirrors _detect_base_branch: `working` if the repo has it, else
    the checked-out branch, else main) — the resolved name is echoed back so the
    caller can record it in mission.json. name/repo/base are allow-list validated
    AND shlex-quoted into the remote command, so they cannot break out of it. If the
    worktree already exists, attach (reuse it). The mission docs stay LOCAL on the
    jumpbox (like a remote ops mission) — only the worktree + console live remotely."""
    if not safe_name(name):
        return None, None, "Invalid mission name."
    if not REMOTE_HOST_RE.match(host):
        return None, None, "Invalid remote host."
    if not REMOTE_DIR_RE.match(repo):
        return None, None, "Remote repo must be an absolute path (no single quotes)."
    if base_branch and not BRANCH_RE.match(base_branch):
        return None, None, "Invalid base branch (letters, numbers, . _ / - only)."
    # Guard FIRST: never provision a remote dev mission whose console couldn't be guarded.
    err = ensure_remote_rails(host)
    if err:
        return None, None, err
    remote = (
        "set -e; repo=%s; name=%s; base=%s; "
        # Blank base => detect on the remote: prefer a `working` staging branch,
        # else the repo's checked-out branch, else main (a repo that doesn't exist
        # yet is init'd with the chosen base as its initial branch below).
        'if [ -z "$base" ]; then '
        'if git -C "$repo" show-ref --verify --quiet refs/heads/working 2>/dev/null; then base=working; '
        'elif b=$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null) && [ -n "$b" ]; then base="$b"; '
        'else base=main; fi; '
        'fi; '
        'wtdir="$HOME/missclaude-worktrees"; wt="$wtdir/$name"; '
        # Init a fresh repo if <repo> is missing or not yet a git repo, so a remote dev
        # mission can target a brand-new repo. The initial commit makes <base> a real ref
        # for the worktree add; symbolic-ref names the branch portably on older git, and
        # the inline identity keeps the commit from failing if git user is unset. `add -A`
        # (best-effort: || true tolerates unreadable files) stages any pre-existing files
        # so the worktree forks from real content; --allow-empty still covers an empty dir.
        # Init if <repo> is missing, not a git repo, or a repo with NO commits yet
        # (unborn HEAD — `worktree add` would fail with "invalid reference"; re-running
        # `git init` on it is harmless). Mirrors _ensure_local_repo.
        'if ! git -C "$repo" rev-parse --verify -q HEAD >/dev/null 2>&1; then '
        'mkdir -p "$repo"; '
        'git init "$repo" >/dev/null 2>&1 || { echo "ERR git init failed: $repo" >&2; exit 3; }; '
        'git -C "$repo" symbolic-ref HEAD "refs/heads/$base"; '
        'git -C "$repo" add -A >/dev/null 2>&1 || true; '
        # ("%%"-escaped: this whole string still goes through %-formatting below)
        'git -C "$repo" -c user.email=' + shlex.quote(GIT_EMAIL).replace("%", "%%")
        + ' -c user.name=' + shlex.quote(GIT_NAME).replace("%", "%%") + ' '
        'commit --allow-empty -m "Initial commit (miss-claude dev mission)" >/dev/null 2>&1 '
        '|| { echo "ERR initial commit failed in $repo" >&2; exit 3; }; '
        'fi; '
        # Last two stdout lines are the protocol: resolved base branch, then the
        # worktree path (the caller records both in mission.json).
        'if [ -d "$wt" ]; then echo "$base"; echo "$wt"; exit 0; fi; '
        'mkdir -p "$wtdir"; '
        'git -C "$repo" worktree add "$wt" -b "claude/$name" "$base" >/dev/null 2>&1 '
        '|| { echo "ERR git worktree add failed (does base \\"$base\\" exist?)" >&2; exit 4; }; '
        'echo "$base"; echo "$wt"'
    ) % (shlex.quote(repo), shlex.quote(name), shlex.quote(base_branch))
    try:
        r = subprocess.run(
            ["ssh", host, remote],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, None, "Could not run remote git worktree add on %s: %s" % (host, e)
    if r.returncode != 0:
        lines = (r.stderr or r.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else "ssh exited %d" % r.returncode
        return None, None, "Remote worktree failed on %s: %s" % (host, tail)
    out = (r.stdout or "").strip().splitlines()
    wt = out[-1] if out else ""
    base = out[-2].strip() if len(out) >= 2 else ""
    if not wt.startswith("/"):
        return None, None, "Remote worktree path not returned by %s." % host
    if not BRANCH_RE.match(base):
        return None, None, "Remote base branch not returned by %s." % host
    return wt, base, None


def _dev_missions_by_repo():
    """Map (repo, base_branch) -> set of dev-mission names. Reads each mission's
    target (mission.json or legacy inference) so missions developing different
    local repos are grouped by the repo whose `branch --merged` decides them."""
    groups = {}
    if not os.path.isdir(MISSIONS_DIR):
        return groups
    for name in os.listdir(MISSIONS_DIR):
        if not safe_name(name) or not os.path.isdir(os.path.join(MISSIONS_DIR, name)):
            continue
        tgt = mission_target(name)
        if tgt.get("mode") != "dev":
            continue
        # Remote dev missions develop a repo on another host; merged-detection here runs
        # LOCAL git, so skip them (their merged state isn't computable from the jumpbox).
        if (tgt.get("target") or {}).get("kind") == "remote-repo" \
                or (tgt.get("dev") or {}).get("host"):
            continue
        dev = tgt.get("dev") or {}
        repo = os.path.realpath(os.path.expanduser(dev.get("repo") or PRIMARY_REPO))
        base = dev.get("base_branch") or BASE_BRANCH
        groups.setdefault((repo, base), set()).add(name)
    return groups


def merged_dev_missions():
    """Set of dev-mission names whose claude/<name> branch is fully merged into its
    own base branch (working, by default). Groups missions by (repo, base) and runs
    one `git branch --merged <base>` per group; never raises — returns whatever it
    could compute so the dashboard still renders if git is unavailable. `git branch
    --merged` lists every branch whose tip is reachable from base, i.e. has no
    unmerged commits left; we keep the claude/<name> branches that belong to that
    group and strip the prefix to recover mission names."""
    out = set()
    for (repo, base), names in _dev_missions_by_repo().items():
        try:
            r = subprocess.run(
                ["git", "-C", repo, "branch", "--merged", base,
                 "--format=%(refname:short)"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, text=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            b = line.strip()
            if b.startswith("claude/") and b[len("claude/"):] in names:
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


def _running_claude_pid(name):
    """PID of the `claude` process inside this mission's tmux session, or None.
    Same subtree walk as claude_sessions() but scoped to one session, so a caller
    can read the console's REAL working dir from /proc/<pid>/cwd instead of guessing
    it from mission metadata. Per-mission (one tmux + one ps), unlike the all-at-once
    claude_sessions()."""
    rc, panes = _run_tmux(
        "list-panes", "-t", "=" + SESSION_PREFIX + name,
        "-F", "#{pane_pid}", capture=True,
    )
    if rc != 0:
        return None
    pane_pids = [int(p) for p in panes.split() if p.isdigit()]
    if not pane_pids:
        return None
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    children = {}  # ppid -> [pid, ...]
    comm = {}      # pid -> command name
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        pid, ppid, cmd = int(parts[0]), int(parts[1]), parts[2]
        comm[pid] = cmd
        children.setdefault(ppid, []).append(pid)
    stack, seen = list(pane_pids), set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if comm.get(pid, "").startswith("claude"):
            return pid
        stack.extend(children.get(pid, ()))
    return None


def _live_console_cwd(name):
    """The actual cwd of a live LOCAL console (read from /proc), or None when nothing
    is running or it can't be read. Authoritative over the metadata guess in
    console_cwd() — they diverge whenever a console cd's somewhere the sidecar doesn't
    record (e.g. the integrator console runs in the repo, not the mission folder)."""
    pid = _running_claude_pid(name)
    if not pid:
        return None
    try:
        return os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        return None


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


def mission_search_text(name, limit=4000):
    """Lowercased plaintext haystack (mission name + DASHBOARD.md + HANDOFF.md
    content) used by the index page's client-side filter box. Markdown markers are
    dropped and whitespace collapsed so the per-card data-search attribute stays
    compact; bounded to `limit` chars so big docs can't bloat the index HTML."""
    parts = [name]
    for fn in ("DASHBOARD.md", "HANDOFF.md"):
        parts.append(_strip_md(read_text(mission_path(name, fn))))
    blob = re.sub(r"\s+", " ", " ".join(parts)).strip().lower()
    return blob[:limit]


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
header.top .wrap { padding-bottom:0; display:flex; align-items:center; gap:14px;
  flex-wrap:wrap; }
header.top a { color:#fff; text-decoration:none; }
header.top h1 { font-size:18.9px; margin:0; }
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
.badge.danger { background:#fdeaea; border-color:#f0c2c2; color:#9b1c1c; }
.badge.ctx { font-variant-numeric:tabular-nums; }
/* Claude plan usage — twin meters tucked into the masthead, right side. Reads on
   the green header: translucent-white tracks, crisp white fill, amber/coral only
   when a threshold trips. Four-column grid wraps to two stacked rows. */
.hdr-usage { margin-left:auto; display:grid; grid-template-columns:auto 70px auto auto;
  align-items:center; gap:4px 8px; }
.hdr-usage[hidden] { display:none; }
.hdr-usage .u-label { font-size:9.5px; font-weight:700; text-transform:uppercase;
  letter-spacing:.07em; color:rgba(255,255,255,.72); }
.hdr-usage .u-bar { height:5px; border-radius:4px; background:rgba(255,255,255,.22);
  overflow:hidden; }
.hdr-usage .u-fill { height:100%; width:0; border-radius:4px;
  background:rgba(255,255,255,.95); transition:width .4s ease; }
.hdr-usage .u-fill.warn { background:#f3c46a; }
.hdr-usage .u-fill.danger { background:#f2918d; }
.hdr-usage .u-pct { font-size:11px; font-weight:700; color:#fff; text-align:right;
  font-variant-numeric:tabular-nums; }
.hdr-usage .u-reset { font-size:10px; color:rgba(255,255,255,.55); white-space:nowrap;
  font-variant-numeric:tabular-nums; }
/* Projected time-to-100% for the session window — its own full-width line under the
   Session bar. Coral when the burn-out lands BEFORE the window resets (you'll throttle
   first); muted otherwise. Collapses to nothing while warming up / idle (:empty). */
.hdr-usage .u-eta { grid-column:1 / -1; font-size:10px; text-align:right;
  color:rgba(255,255,255,.5); font-variant-numeric:tabular-nums; }
.hdr-usage .u-eta.before-reset { color:#f2918d; font-weight:600; }
.hdr-usage .u-eta:empty { display:none; }
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
#filterbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
#filterbar input[type=text] { flex:1; min-width:220px; }
.pills { display:flex; align-items:center; gap:6px; flex:0 0 auto; }
.pill { font-size:11px; padding:2px 10px; border-radius:10px; cursor:pointer;
  background:#fff; border:1px solid var(--line); color:var(--muted); line-height:1.6; }
.pill:hover { border-color:#c7ccd3; }
.pill.active { background:#e7f4ec; border-color:#bfe0cc; color:#1f6b41; }
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
.modal-overlay { position:fixed; inset:0; background:rgba(17,24,39,.45); z-index:1000;
  display:flex; align-items:flex-start; justify-content:center; padding:7vh 16px; }
.modal-overlay[hidden] { display:none; }
.modal { background:#fff; border:1px solid var(--line); border-radius:10px; padding:18px 20px;
  width:min(540px,100%); box-shadow:0 12px 40px rgba(0,0,0,.18); }
.modal h2 { margin:0 0 2px; font-size:18px; }
.modal .step { font-size:11.5px; font-weight:600; color:var(--accent); text-transform:uppercase;
  letter-spacing:.04em; margin:15px 0 6px; }
.modal .seg { display:flex; gap:6px; flex-wrap:wrap; }
.modal .seg label { border:1px solid var(--line); border-radius:6px; padding:6px 11px; font-size:13.5px;
  cursor:pointer; display:inline-flex; gap:6px; align-items:center; }
.modal .seg label:has(input:checked) { border-color:var(--accent); background:#f3f7f4;
  color:var(--accent); font-weight:600; }
.modal .seg label[hidden] { display:none; }
.modal .fields { margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }
.modal .fields[hidden] { display:none; }
.modal .fields input[type=text] { flex:1; min-width:190px; }
.modal .hint { font-size:12px; color:var(--muted); margin:8px 0 0; }
.modal .form-error { font-size:13px; color:#c0392b; margin:12px 0 0; font-weight:600; }
.modal .form-error[hidden] { display:none; }
.modal input.field-error { border-color:#c0392b; box-shadow:0 0 0 2px rgba(192,57,43,.15); }
.modal .actions { display:flex; justify-content:flex-end; gap:8px; margin-top:20px; }
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


# Index-only live filter. Matches whitespace-separated terms (AND) against each
# card's data-search blob (mission name + dashboard contents, lowercased server-side),
# toggling card visibility with no server round-trip. "/" focuses the box; Esc clears.
FILTER_JS = """
<script>
(function() {
  var box = document.getElementById("mission-filter");
  if (!box) return;
  var cards = Array.prototype.slice.call(document.querySelectorAll("[data-search]"));
  var none  = document.getElementById("filter-none");
  var pills = Array.prototype.slice.call(document.querySelectorAll(".pill"));
  var sel = "";   // single selected status token; "" => show all
  function statusOk(c) {
    if (!sel) return true;                          // nothing selected => all
    var have = (c.getAttribute("data-status") || "").split(/\\s+/);
    return have.indexOf(sel) !== -1;
  }
  function apply() {
    var terms = box.value.toLowerCase().split(/\\s+/).filter(Boolean);
    var shown = 0;
    cards.forEach(function(c) {
      var hay = c.getAttribute("data-search") || "";
      var ok = terms.every(function(t) { return hay.indexOf(t) !== -1; }) && statusOk(c);
      c.hidden = !ok;
      if (ok) shown++;
    });
    if (none) none.hidden = shown !== 0;
  }
  pills.forEach(function(p) {
    p.addEventListener("click", function() {
      var s = p.getAttribute("data-status");
      // single-select: clicking "all" or the already-active pill clears to all
      sel = (s === "all" || s === sel) ? "" : s;
      pills.forEach(function(q) {
        var qs = q.getAttribute("data-status");
        q.classList.toggle("active", sel ? qs === sel : qs === "all");
      });
      apply();
    });
  });
  // Index ✕ button: stop the session via fetch (no confirm, no page reload) and
  // patch the card in place — drop the live outline, the live/idle + context badges,
  // the kill form, and recompute data-status so the status pills re-filter correctly.
  function patchKilledCard(card) {
    if (!card) return;
    card.classList.remove("running");
    var toks = (card.getAttribute("data-status") || "").split(/\\s+/).filter(function(t) {
      return t && t !== "live" && t !== "idle";
    });
    if (!toks.length) toks.push("none");
    card.setAttribute("data-status", toks.join(" "));
    Array.prototype.slice.call(
      card.querySelectorAll(".badge.live, .badge.idle, .badge.ctx, .killform")
    ).forEach(function(el) { el.remove(); });
  }
  Array.prototype.slice.call(document.querySelectorAll(".killform")).forEach(function(form) {
    form.addEventListener("submit", function(e) {
      e.preventDefault();
      var card = form.closest(".card");
      var btn = form.querySelector("button");
      if (btn) btn.disabled = true;
      fetch(form.action, { method: "POST", headers: { "X-Requested-With": "fetch" } })
        .then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function() { patchKilledCard(card); apply(); })
        .catch(function() { window.location.reload(); });   // fall back to a full refresh
    });
  });
  box.addEventListener("input", apply);
  box.addEventListener("keydown", function(e) {
    if (e.key === "Escape") { box.value = ""; apply(); }
  });
  document.addEventListener("keydown", function(e) {
    var t = document.activeElement;
    var typing = t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); box.focus(); }
  });
  apply();   // re-apply if the browser restored a value on back/forward
})();
</script>
"""


# Index-only context badge. For each card with a console session, poll
# /m/<name>/context.json (URL incl. token baked into data-ctx-url server-side) and
# render the live Claude context size as "<tokens> · <pct>%". States (see
# mission_context): ok -> show figure; starting -> "starting"; compacted ->
# "compacted <pre> -> <post> · %" (the compaction's impact; falls back to
# "compacted from <pre>" until the post-compact turn writes its size); remote -> "remote";
# none / fetch error -> stay hidden (never error a card). Polls slower than the
# mission page (the figure moves slowly and there can be many cards).
CTX_JS = """
<script>
(function() {
  var CTX_MS = __CTX_MS__;
  var els = Array.prototype.slice.call(document.querySelectorAll("[data-ctx-url]"));
  if (!els.length) return;
  function fmt(n) {                      // 80460 -> "80k", 1500 -> "1.5k", 950 -> "950"
    if (n >= 1000) {
      var k = n / 1000;
      return (k >= 10 ? Math.round(k) : k.toFixed(1).replace(/\\.0$/, "")) + "k";
    }
    return String(n);
  }
  function paint(el, d) {
    el.className = "badge ctx";          // reset any prior warn/danger
    if (d && d.state === "ok") {
      el.textContent = fmt(d.tokens) + " · " + Math.round(d.pct) + "%";
      el.title = d.tokens.toLocaleString() + " / " + d.window.toLocaleString()
               + " tokens (" + d.pct + "%)" + (d.model ? " · " + d.model : "");
      // Colour by ABSOLUTE tokens against the 200k base window, not d.pct: once
      // usage passes 200k the window bumps to 1M (so d.pct collapses back to ~20%).
      // Anchoring to tokens keeps red latched from 180k up — 200k->1M is all red.
      var BASE = 200000;
      if (d.tokens >= BASE * 0.90) el.className += " danger";
      else if (d.tokens >= BASE * 0.75) el.className += " warn";
      el.hidden = false;
    } else if (d && d.state === "starting") {
      el.textContent = "starting"; el.title = "Console started; no context yet";
      el.hidden = false;
    } else if (d && d.state === "compacted") {
      el.className += " warn";
      var pre = d.pre ? fmt(d.pre.tokens) : null;
      var post = d.post ? fmt(d.post.tokens) : null;
      if (pre && post) {                 // full impact: was -> now · %
        el.textContent = "compacted " + pre + " \\u2192 " + post + " · " + Math.round(d.post.pct) + "%";
        el.title = "Compacted from " + d.pre.tokens.toLocaleString() + " to "
                 + d.post.tokens.toLocaleString() + " tokens ("
                 + d.post.pct + "% of " + d.post.window.toLocaleString() + ")";
      } else if (pre) {                  // just compacted; post size not written yet
        el.textContent = "compacted from " + pre + " · new size next turn";
        el.title = "Just compacted from " + d.pre.tokens.toLocaleString()
                 + " tokens; post-compact size shows after the next turn";
      } else {
        el.textContent = "compacted";
        el.title = "Context just compacted; new size shows after the next turn";
      }
      el.hidden = false;
    } else if (d && d.state === "remote") {
      el.textContent = "ctx n/a"; el.title = "Remote console — context lives on the remote host";
      el.hidden = false;
    } else {
      el.hidden = true;                  // none / unknown -> show nothing
    }
  }
  function one(el) {
    fetch(el.getAttribute("data-ctx-url"))
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) { paint(el, d); })
      .catch(function() {});             // leave the badge as-is on a transient error
  }
  function poll() { els.forEach(one); }
  poll();
  setInterval(poll, CTX_MS);
})();
</script>
"""


# Plan-usage card poller. Fills the two fill-bars from /usage.json (URL incl.
# token baked into data-usage-url). state==ok shows the card with "<pct>% used" +
# a "Resets in <Xh Ym>" countdown derived from resets_at; warn ≥75%, danger ≥90%.
# Under the Session bar it also paints a projected "~full in <Xh Ym>" from the
# server's eta_full_ms (least-squares time-to-100%), coloured coral when that lands
# BEFORE the window resets (you'd be throttled first). Any other state (none / fetch
# error) leaves the card hidden. Polls every 60s to match the server-side cache TTL.
USAGE_JS = """
<script>
(function() {
  var USAGE_MS = 60000;
  var card = document.getElementById('plan-usage');
  if (!card) return;
  var url = card.getAttribute('data-usage-url');
  function untilReset(iso) {
    if (!iso) return '';
    var ms = new Date(iso).getTime() - Date.now();
    if (isNaN(ms)) return '';
    if (ms <= 0) return 'resetting…';
    var m = Math.floor(ms / 60000), h = Math.floor(m / 60); m = m % 60;
    var d = Math.floor(h / 24); h = h % 24;
    var t = d ? (d + 'd ' + h + 'h') : (h ? (h + 'h ' + m + 'm') : (m + 'm'));
    return '↻ ' + t;
  }
  function paintBar(prefix, info) {
    var fill = document.getElementById('us-' + prefix + '-fill');
    var pctEl = document.getElementById('us-' + prefix + '-pct');
    var resetEl = document.getElementById('us-' + prefix + '-reset');
    if (!info || info.percent == null) {                 // window not applicable
      fill.style.width = '0'; fill.className = 'u-fill';
      pctEl.textContent = ''; resetEl.textContent = ''; return;
    }
    var pct = Math.max(0, Math.min(100, info.percent));
    fill.style.width = pct + '%';
    fill.className = 'u-fill' + (pct >= 90 ? ' danger' : pct >= 75 ? ' warn' : '');
    pctEl.textContent = Math.round(info.percent) + '%';
    resetEl.textContent = untilReset(info.resets_at);
  }
  function fmtDur(ms) {                                   // ms -> "Xh Ym" / "Ym"
    var m = Math.floor(ms / 60000), h = Math.floor(m / 60); m = m % 60;
    return h ? (h + 'h ' + m + 'm') : (m + 'm');
  }
  function paintEta(info) {
    var el = document.getElementById('us-session-eta');
    if (!el) return;
    el.className = 'u-eta';
    if (!info || info.eta_full_ms == null) { el.textContent = ''; return; }
    var ms = info.eta_full_ms - Date.now();
    if (ms <= 0) { el.textContent = '~full now'; el.className = 'u-eta before-reset'; return; }
    el.textContent = '~full in ' + fmtDur(ms);
    var reset = info.resets_at ? new Date(info.resets_at).getTime() : NaN;
    if (!isNaN(reset) && info.eta_full_ms < reset) el.className = 'u-eta before-reset';
  }
  function poll() {
    fetch(url).then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      if (d && d.state === 'ok') {
        paintBar('session', d.session);
        paintBar('weekly', d.weekly);
        paintEta(d.session);
        card.hidden = false;
      } else {
        card.hidden = true;
      }
    }).catch(function() {});                              // leave as-is on a blip
  }
  poll();
  setInterval(poll, USAGE_MS);
})();
</script>
"""


def spawn_modal():
    """The "+ Spawn" two-step modal: pick a MODE (Mission / Dev Mission / Console),
    then a LOCATION valid for that mode, and POST to /spawn. A thin launcher over the
    existing flows — the server delegates (see do_POST /spawn). Vanilla JS, no deps.
    The location radios + field groups are shown/hidden by JS per the chosen mode, so
    the operator only ever sees valid combinations:
      Mission     -> Local dir | Remote dir   (ops console at the target; docs local)
      Dev Mission -> Local repo | Remote repo (git worktree on claude/<name> + rails)
      Console     -> Local dir | Remote dir   (stateless console; no mission folder)."""
    tok = tok_q()
    return (
        '<div class="modal-overlay" id=spawn-modal hidden>'
        '<div class=modal role=dialog aria-modal=true aria-label="Open a console">'
        f'<form method=post action="/spawn{tok}">'
        '<h2>Open</h2>'
        '<p class=hint>Pick what kind of session, then where it runs.</p>'

        '<p class=step>1 · What</p>'
        '<div class=seg id=spawn-mode>'
        '<label><input type=radio name=mode value=ops checked> Mission</label>'
        '<label><input type=radio name=mode value=dev> Dev Mission</label>'
        '<label><input type=radio name=mode value=console> Console</label>'
        '</div>'
        '<p class=hint id=spawn-modehint></p>'

        '<p class=step>2 · Where</p>'
        '<div class=seg id=spawn-kind>'
        '<label><input type=radio name=kind value=local-dir checked> Local dir</label>'
        '<label><input type=radio name=kind value=remote> Remote dir</label>'
        '<label><input type=radio name=kind value=local-repo> Local repo</label>'
        '<label><input type=radio name=kind value=remote-repo> Remote repo</label>'
        '</div>'
        '<div class=fields data-loc="local-dir local-repo">'
        '<input type=text name=path placeholder="absolute path (blank = your home dir)" '
        'pattern="/[A-Za-z0-9 ._/@:+-]*" title="absolute path on the jumpbox; blank defaults to your home dir">'
        '</div>'
        '<div class=fields data-loc="remote remote-repo" hidden>'
        '<input type=text name=host placeholder="host (e.g. www or user@host)" '
        'pattern="[A-Za-z0-9][A-Za-z0-9._@-]*" title="ssh alias, hostname, or user@host">'
        '<input type=text name=dir placeholder="/srv/projects/my-app" '
        'pattern="/[A-Za-z0-9 ._/@:+-]*" title="absolute path on the remote host">'
        '</div>'
        '<div class=fields data-mode="dev" hidden>'
        '<input type=text name=base placeholder="base branch (blank = auto-detect)" '
        'pattern="[A-Za-z0-9._/-]*" title="base branch the worktree forks from; '
        'blank auto-detects: a working branch if the repo has one, else its current branch">'
        '</div>'

        '<p class=step>Name</p>'
        '<div class=fields>'
        '<input type=text name=name placeholder="mission name (blank = two random words)" '
        'pattern="[A-Za-z0-9 ._/@:&()#+-]*" title="mission name; blank auto-names it. For Console it is just a tab label">'
        '</div>'

        '<p class=form-error id=spawn-error role=alert hidden></p>'
        '<div class=actions>'
        '<button type=button class="btn secondary" id=spawn-cancel>Cancel</button>'
        '<button type=submit class=btn>Open</button>'
        '</div>'
        '</form></div></div>'
        + SPAWN_JS
    )


SPAWN_JS = """
<script>
(function() {
  var modal = document.getElementById('spawn-modal');
  if (!modal) return;
  var openBtn = document.getElementById('spawn-open');
  var cancel  = document.getElementById('spawn-cancel');
  var form    = modal.querySelector('form');
  // Locations valid per mode (mode is chosen first). Dev needs a git repo (local-repo /
  // remote-repo); Mission and Console run in a plain dir (local-dir / remote). A remote
  // dev mission gets the worker rails shipped to the remote host over SSH (see app.py
  // ensure_remote_rails); the matrix here mirrors VALID_KINDS in do_POST /spawn.
  var LOCS = {
    ops:     ['local-dir','remote'],
    dev:     ['local-repo','remote-repo'],
    console: ['local-dir','remote']
  };
  var HINTS = {
    ops:     'Mission — creates ~/missions/<name>/ docs; the console works at the target.',
    dev:     'Dev Mission — adds a git worktree (branch claude/<name>) + worker rails; a fresh repo is git-init\\'d if the path is new (existing files are committed); a remote repo gets the rails shipped over SSH.',
    console: 'Console — stateless session; no mission folder is created.'
  };
  function val(n){ var r = form.querySelector('input[name='+n+']:checked'); return r ? r.value : ''; }
  function sync() {
    var mode = val('mode');
    // Console opens the live terminal in a NEW tab (the operator stays on the
    // index); ops/dev navigate the current tab to the new mission's dashboard.
    form.target = (mode === 'console') ? '_blank' : '';
    // 1) show only the locations valid for this mode; keep a valid one selected.
    var valid = LOCS[mode] || ['local-dir'];
    form.querySelectorAll('#spawn-kind label').forEach(function(l){
      var inp = l.querySelector('input'); var ok = valid.indexOf(inp.value) >= 0;
      l.hidden = !ok; inp.disabled = !ok;
    });
    if (valid.indexOf(val('kind')) < 0) {
      var first = form.querySelector('#spawn-kind input[value="'+valid[0]+'"]');
      if (first) first.checked = true;
    }
    // 2) show the field group (path vs host+dir) for the selected location.
    var kind = val('kind');
    form.querySelectorAll('[data-loc]').forEach(function(g){
      g.hidden = g.getAttribute('data-loc').split(' ').indexOf(kind) < 0;
    });
    // 3) dev-only base-branch field. Name is always optional now — a blank name is
    //    auto-generated server-side for ops/dev (and is the shared label for Console).
    form.querySelectorAll('[data-mode]').forEach(function(g){
      g.hidden = g.getAttribute('data-mode') !== mode;
    });
    var mh = document.getElementById('spawn-modehint'); if (mh) mh.textContent = HINTS[mode] || '';
  }
  // Inline validation: keep the modal open on a bad entry, flag the offending field, and
  // show the reason here rather than bouncing to the index with a server error. Mirrors the
  // required-ness the server enforces in do_POST /spawn (dev needs a repo path; remote needs
  // host + absolute dir; ops/console local path is optional = home dir).
  var errBox = document.getElementById('spawn-error');
  function fld(n){ return form.querySelector('input[type=text][name='+n+']'); }
  function clearErrs(){
    if (errBox){ errBox.hidden = true; errBox.textContent = ''; }
    form.querySelectorAll('input.field-error').forEach(function(i){ i.classList.remove('field-error'); });
  }
  function fail(input, msg){
    if (errBox){ errBox.textContent = msg; errBox.hidden = false; }
    if (input){ input.classList.add('field-error'); input.focus(); }
    return false;
  }
  function validate(){
    clearErrs();
    var mode = val('mode'), kind = val('kind');
    if (kind === 'remote' || kind === 'remote-repo'){
      var h = fld('host'), d = fld('dir');
      if (!h.value.trim()) return fail(h, 'Remote host is required.');
      var dv = d.value.trim();
      // Dev needs a real repo path (nothing to auto-detect on the remote); ops/console
      // leave it blank to land in the operator's home dir there, same as local.
      if (mode === 'dev' && !dv) return fail(d, 'Repo path is required (an absolute path on the remote host).');
      if (dv && dv.charAt(0) !== '/') return fail(d, 'Remote directory must be an absolute path (starting with /).');
    } else {
      var p = fld('path');
      var v = p.value.trim();
      if (mode === 'dev' && !v) return fail(p, 'Repo path is required (an absolute path on the jumpbox).');
      if (v && v.charAt(0) !== '/') return fail(p, 'Path must be absolute (starting with /).');
    }
    return true;
  }
  function show(){ modal.hidden = false; clearErrs(); sync(); }
  function hide(){ modal.hidden = true; }
  if (openBtn) openBtn.addEventListener('click', show);
  if (cancel)  cancel.addEventListener('click', hide);
  modal.addEventListener('click', function(e){ if (e.target === modal) hide(); });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && !modal.hidden) hide(); });
  form.addEventListener('change', function(){ clearErrs(); sync(); });
  form.addEventListener('submit', function(e){
    if (!validate()) { e.preventDefault(); return; }
    if (form.target === '_blank') hide();   // stays on the index; console opens in its own tab
  });
  sync();
})();
</script>
"""


def page(title, body, active_mission=None):
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
        '<header class=top><div class=wrap>'
        '<h1><a href="/">👩‍✈️ Miss Claude</a></h1>'
        + (f'<span class=sub>{html.escape(LABEL)}</span>' if LABEL else '')
        +
        # Claude subscription plan usage — twin meters on the right of the masthead,
        # filled by USAGE_JS from /usage.json. Hidden until the poll resolves a usable
        # state, so it never shows empty when offline / token stale. The URL (incl.
        # token) is baked in server-side; the JS needs no token handling.
        '<div class=hdr-usage id=plan-usage data-usage-url="'
        + html.escape("/usage.json" + tok_q(), quote=True) + '" hidden>'
        '<span class=u-label>Session</span>'
        '<div class=u-bar><div class="u-fill" id=us-session-fill></div></div>'
        '<span class=u-pct id=us-session-pct></span>'
        '<span class=u-reset id=us-session-reset></span>'
        '<span class=u-eta id=us-session-eta></span>'
        '<span class=u-label>Weekly</span>'
        '<div class=u-bar><div class="u-fill" id=us-weekly-fill></div></div>'
        '<span class=u-pct id=us-weekly-pct></span>'
        '<span class=u-reset id=us-weekly-reset></span>'
        '</div>'
        "</div></header>"
        f'<div class=wrap>{body}</div>{REL_JS}{USAGE_JS}</body></html>'
    )


def tok_q():
    """Token query-string suffix to keep links authenticated, if token is set."""
    return f"?token={urllib.parse.quote(TOKEN)}" if TOKEN else ""


# ===========================================================================
# REMOTE CONSOLES  (optional side feature — self-contained add-on)
# Runs Claude ON another host over SSH, wrapped in a tmux session on THIS jumpbox
# (no tmux on the remote side). Launch shape (blank name — legacy shared console):
#     ssh -tt <host> 'cd <dir> && claude --continue --dangerously-skip-permissions'
# (--continue resumes the last conversation in that dir; falls back to a fresh session.)
# With a NAME, the launcher instead keys a deterministic session id off host|dir|name
# and runs `claude --resume <id> || claude --session-id <id>` so each name is its own
# resumable conversation. See console-launch.sh for the exact remote command.
# The default mission workflow is untouched. render_index no longer links here directly
# (Spawn -> Console -> Remote dir covers the same launch); the /remote page and route
# still work standalone. To remove the feature entirely, delete: (1) this fenced block,
# (2) the one `/remote` route branch in do_GET (marked "REMOTE CONSOLES"). Plus the
# matching fenced branch in console-launch.sh. No other code references it.
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


def _local_console_url(host_header, ldir, lname=""):
    """ttyd URL for a LOCAL console (Console mode, Local Dir target): a stateless Claude
    in a jumpbox directory, no mission folder. ttyd's --url-arg turns each ?arg= into a
    positional arg of console-launch.sh: here `local <dir> [name]` (mirrors the remote
    console's `remote <host> <dir> [name]`). The dir is the only required field."""
    host = (host_header or "").rsplit(":", 1)[0] or "localhost"
    url = (
        f"http://{host}:{CONSOLE_TTYD_PORT}/?arg=local"
        f"&arg={urllib.parse.quote(ldir)}"
    )
    if lname:
        url += f"&arg={urllib.parse.quote(lname)}"
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
        '<p class=muted style="font-size:13px">Run Claude on another fleet host over SSH, '
        'in a tmux session on this jumpbox (nothing is installed/changed on the remote '
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
          '<input type=text name=dir size=34 placeholder="/srv/projects/my-app" '
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
            'open in fullscreen tab ↗</a></div>'
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

    # Unified launcher: pick target + mode (Mission / Dev Mission / Console). Replaces
    # the old classic create form (name field + "+ Create mission"/"+ Create dev
    # mission" buttons) — Spawn covers that fast path too (Mission mode + Local dir).
    body.append(
        '<div class=card style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
        '<button class=btn id=spawn-open type=button>+ Open</button>'
        '<span class=muted style="font-size:12.5px">pick a mode (Mission · Dev Mission · '
        'Console), then where it runs (local or remote)</span>'
        '</div>'
    )
    body.append(spawn_modal())

    if not missions:
        body.append('<div class=empty>No missions yet. Create one above.</div>')
    else:
        # Live client-side filter: matches the typed terms against each card's
        # data-search blob (name + dashboard contents). No new route — pure JS.
        body.append(
            '<div class=card id=filterbar>'
            '<input type=text id=mission-filter autocomplete=off spellcheck=false '
            'placeholder="Filter missions by name or dashboard contents…  ( press / to focus )" '
            'aria-label="Filter missions">'
            '<div class=pills role=group aria-label="Filter by status">'
            '<button type=button class="pill active" data-status=all>All</button>'
            '<button type=button class=pill data-status=live>Live</button>'
            '<button type=button class=pill data-status=idle>Idle</button>'
            '<button type=button class=pill data-status=merged>Merged</button>'
            '<button type=button class=pill data-status=unmerged>Not merged</button>'
            '<button type=button class=pill data-status=none>No session</button>'
            "</div>"
            "</div>"
        )
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
        # "Not merged": a dev mission whose branch claude/<name> is not (yet) fully
        # merged into base. Reflects branch state, not the console, so it's independent
        # of live/idle. Merged detection is local-only, so a remote dev mission (never
        # in merged_set) reads as not-merged — correct, its merge state is unknown here.
        is_unmerged = mission_target(name).get("mode") == "dev" and name not in merged_set
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
                f'<form class=killform method=post action="{kill_action}">'
                '<button class=killbtn type=submit title="Stop session (resumes on reopen)" '
                'aria-label="Stop session (resumes on reopen)">✕</button></form>'
            )
        else:
            kill_btn = ""
        search_blob = html.escape(mission_search_text(name), quote=True)
        # Context badge: only for missions with a live/idle session (a console exists,
        # so "current context" is meaningful). Empty placeholder filled by CTX_JS from
        # /m/<name>/context.json; the full URL (incl. token) is baked in server-side so
        # the JS needs no token handling. Hidden until the poll resolves a usable state.
        if has_session:
            ctx_url = f"/m/{urllib.parse.quote(name)}/context.json" + tok_q()
            ctx_badge = f' <span class="badge ctx" data-ctx-url="{html.escape(ctx_url, quote=True)}" hidden></span>'
        else:
            ctx_badge = ""
        # Machine-readable status for the filter pillboxes (multi-token: an idle
        # session whose branch is also merged carries both). Mirrors the badge/outline
        # logic above so the pills filter on the same states the operator sees.
        status_tokens = []
        if is_live:
            status_tokens.append("live")
        elif has_session:
            status_tokens.append("idle")
        if is_merged:
            status_tokens.append("merged")
        if is_unmerged:
            status_tokens.append("unmerged")
        if not status_tokens:
            status_tokens.append("none")
        status_attr = " ".join(status_tokens)
        body.append(
            f'<div class="{card_cls}" data-search="{search_blob}" data-status="{status_attr}">'
            '<div class=cardhead>'
            f'<h2><a href="{href}">{html.escape(name)}</a></h2>'
            f"{kill_btn}"
            "</div>"
            f'<div class=meta>updated {time_tag(mtime)}{live}{ctx_badge} &nbsp; {dev_badge(name)}{mb} &nbsp; {hb}</div>'
            + (f'<p class=summary>{html.escape(summ)}</p>' if summ else "")
            + "</div>"
        )
    if missions:
        body.append('<div class=empty id=filter-none hidden>No missions match your filter.</div>')
        body.append(FILTER_JS)
        body.append(CTX_JS.replace("__CTX_MS__", "15000"))  # many cards -> poll slower
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
    # Mode comes from mission_target() — mission.json when present, else the legacy
    # worktree-existence inference (no git). A dev mission's console runs in its
    # worktree as a feature worker (see console-launch.sh); an ops console runs in
    # the mission folder / target dir. Shared by the mission header and the
    # mission-list cards so they never drift. `name` is already NAME_RE-validated.
    tgt = mission_target(name)
    if tgt.get("mode") == "dev":
        dev = tgt.get("dev") or {}
        wt = dev.get("worktree") or os.path.join(WORKTREES_DIR, name)
        host = dev.get("host")
        if host:
            # Remote dev: the repo/worktree live on another host — do NOT realpath them
            # against the local FS. Show host:repo so the operator sees it is remote.
            repo = dev.get("repo") or ""
            label = os.path.basename(repo.rstrip("/")) or repo
            prefix = html.escape(host) + ":" + html.escape(label) + " · "
            title = ("Console runs on %s in the dev worktree %s (repo %s)"
                     % (host, wt, repo))
            return (
                f'<span class="badge" title="{html.escape(title)}">'
                f'{prefix}dev · claude/{html.escape(name)}</span>'
            )
        repo = os.path.realpath(os.path.expanduser(dev.get("repo") or PRIMARY_REPO))
        # Prefix the badge with the repo basename for any repo other than the default
        # Miss Claude checkout, so the operator can tell which project this develops.
        prefix = ""
        if repo != PRIMARY_REPO:
            prefix = html.escape(os.path.basename(repo) or repo) + " · "
        return (
            f'<span class="badge" title="Console runs in the dev worktree '
            f'{html.escape(wt)} (repo {html.escape(repo)})">'
            f'{prefix}dev · claude/{html.escape(name)}</span>'
        )
    return (
        '<span class="badge idle" title="No dev worktree — console runs in the '
        'mission folder">ops</span>'
    )


def render_mission_header(name, extra="", ctx=""):
    # `ctx` is the (hidden-until-polled) context badge placeholder — it sits right
    # after the mission name, before the ops/dev pill.
    badge = dev_badge(name)
    host, directory = mission_location(name)
    server = host or socket.gethostname()
    loc = (
        '<div class=meta style="margin-top:3px">'
        f'<span title="server the console runs on">🖥 {html.escape(server)}</span> · '
        f'<code title="directory the console works in">{html.escape(directory)}</code>'
        '</div>'
    )
    return (
        f"<h1 style='margin:4px 0 0'>{html.escape(name)} {ctx}{badge}{extra}</h1>"
        f"{loc}"
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


def _ttyd_listening():
    """True if the ttyd console bridge (claude-console.service) is accepting
    connections on CONSOLE_TTYD_PORT. Checked via 127.0.0.1 since ttyd runs on
    this same host; a refused connect returns immediately, so the cost per page
    render is negligible. Used to surface the two-service/two-port cause clearly
    instead of the browser's generic "refused to connect" inside the iframe."""
    try:
        with socket.create_connection(("127.0.0.1", CONSOLE_TTYD_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _ttyd_down_notice():
    """One-line hint rendered above a console iframe when ttyd isn't listening."""
    return (
        f'<div class=notice>Console unavailable: nothing is listening on port '
        f'{CONSOLE_TTYD_PORT} on this host — the Claude console runs as a separate '
        'service (<code>claude-console.service</code> / ttyd) from the dashboard. '
        'Start it with <code>sudo systemctl start claude-console.service</code>, '
        'then reload this page.</div>'
    )


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
    console_link = (
        ' <span class=meta style="font-weight:normal">'
        f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
        "open in fullscreen tab ↗</a></span>"
    )
    # Context badge (same placeholder + CTX_JS poller as the index card): only when a
    # session exists, so "current context" is meaningful. Empty/hidden until the poll
    # resolves a usable state; the token-bearing URL is baked in server-side. Rendered
    # inside the h1, between the mission name and the ops/dev pill.
    ctx_badge = ""
    if session_running(name):
        ctx_url = f"/m/{urllib.parse.quote(name)}/context.json" + tok_q()
        ctx_badge = f'<span class="badge ctx" data-ctx-url="{html.escape(ctx_url, quote=True)}" hidden></span> '
    body = [render_mission_header(name, console_link, ctx_badge)]
    body.append('<div class=console-region>')
    if not _ttyd_listening():
        body.append(_ttyd_down_notice())
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
    body.append(CTX_JS.replace("__CTX_MS__", "5000"))   # single badge -> poll faster (no-op if none present)
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

        # Global (not per-mission): the operator's Claude subscription plan usage
        # (5-hour session + weekly), polled by the front-end for the usage bars.
        if path == "/usage.json":
            return self._send_json(plan_usage())

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

            # current Claude context usage for this mission's console (see
            # mission_context); polled by the front-end to render a small badge.
            if rest == "context.json":
                return self._send_json(mission_context(name))

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
            is_dev = form.get("dev", [""])[0] == "1"
            if is_dev:
                err = create_worktree(name)
                if err:
                    return self._send_html(render_index(
                        f'Could not create dev mission "{name}": {err}'))
            os.makedirs(d, exist_ok=True)
            for sub in ARTIFACT_DIRS:
                os.makedirs(mission_path(name, sub), exist_ok=True)
            for fn, contents in scaffold(name).items():
                write_text_atomic(mission_path(name, fn), contents)
            # Write a minimal mission.json so old-path missions are first-class too
            # (the launcher + badges fall back to inference without it, but recording
            # the mode/target up front keeps everything consistent — task 9 of the plan).
            if is_dev:
                wt = os.path.join(WORKTREES_DIR, name)
                write_mission_meta(name, {
                    "mode": "dev",
                    "target": {"kind": "local-repo", "path": PRIMARY_REPO},
                    "dev": {"repo": PRIMARY_REPO, "base_branch": BASE_BRANCH,
                            "worktree": wt},
                })
            else:
                write_mission_meta(name, {
                    "mode": "ops",
                    "target": {"kind": "local-dir", "path": d},
                })
            return self._redirect(f"/m/{urllib.parse.quote(name)}/dashboard" + tok_q())

        # Spawn wizard: one route that delegates to the existing flows. Picks a MODE then
        # a LOCATION valid for it (the two-step modal in render_index):
        #   console -> 302 to the ttyd console URL (remote OR local dir; no folder).
        #   ops/dev -> create ~/missions/<name>/ + scaffold + mission.json. dev also
        #              creates the worktree FIRST (local: create_worktree; remote:
        #              create_remote_worktree, which ships+verifies the guard rails).
        if path == "/spawn":
            mode = (form.get("mode", [""])[0]).strip()
            kind = (form.get("kind", [""])[0]).strip()
            lpath = (form.get("path", [""])[0]).strip()
            rhost = (form.get("host", [""])[0]).strip()
            rdir = (form.get("dir", [""])[0]).strip()
            # Blank base branch = auto-detect per repo (working > checked-out branch >
            # main; see _detect_base_branch). Hardcoding `main` here broke both ways:
            # missclaude stages on `working`, and plenty of repos live on `master`.
            base = (form.get("base", [""])[0]).strip()
            if base and not BRANCH_RE.match(base):
                return self._send_html(render_index(
                    "Invalid base branch (letters, numbers, . _ / - only)."))
            rawname = (form.get("name", [""])[0]).strip()
            if mode not in ("ops", "dev", "console"):
                return self._error(HTTPStatus.BAD_REQUEST, "Unknown spawn mode.")
            # Convenience defaults: a blank LOCAL path means "the operator's home dir"
            # (ops/console only — NOT dev, since a dev mission would git-init that dir,
            # and silently turning $HOME into a repo is never intended). A blank NAME
            # (ops/dev only) gets an auto-generated two-word name; Console keeps a blank
            # name (= the legacy shared-console label).
            if not lpath and mode in ("ops", "console"):
                lpath = os.path.expanduser("~")
            if not rawname and mode in ("ops", "dev"):
                rawname = random_mission_name()
            # Locations each mode allows (mirrors LOCS in SPAWN_JS). Enforced here too so a
            # hand-crafted POST can't pair, e.g., dev with a non-repo dir. Dev develops a
            # git repo (local-repo / remote-repo); Mission + Console run in a plain dir.
            VALID_KINDS = {
                "ops": ("local-dir", "remote"),
                "dev": ("local-repo", "remote-repo"),
                "console": ("local-dir", "remote"),
            }
            if kind not in VALID_KINDS[mode]:
                return self._error(HTTPStatus.BAD_REQUEST,
                                   "That location is not valid for this mode.")
            is_remote = kind in ("remote", "remote-repo")

            # Console mode is stateless — no mission folder, just bounce to the ttyd URL
            # (the browser navigates to the live terminal). Remote or a local dir.
            if mode == "console":
                rname = rawname if REMOTE_NAME_RE.match(rawname) else ""
                if is_remote:
                    # Blank dir = the operator's home dir ON THE REMOTE HOST (mirrors the
                    # local-dir default below) — console-launch.sh's `cd '<dir>'` is a
                    # no-op on an empty string, leaving a fresh SSH login shell at $HOME.
                    if not REMOTE_HOST_RE.match(rhost) or (rdir and not REMOTE_DIR_RE.match(rdir)):
                        return self._send_html(render_index(
                            "Console needs a valid remote host (and, if given, an absolute directory)."))
                    return self._redirect(
                        _remote_console_url(self.headers.get("Host", ""), rhost, rdir, rname))
                if not REMOTE_DIR_RE.match(lpath):
                    return self._send_html(render_index(
                        "Console needs an absolute local directory (no single quotes)."))
                rp = os.path.realpath(os.path.expanduser(lpath))
                if not os.path.isdir(rp):
                    return self._send_html(render_index(f"No such directory: {rp}"))
                return self._redirect(
                    _local_console_url(self.headers.get("Host", ""), rp, rname))

            # ops / dev: validate the name first.
            name = re.sub(r"\s+", "-", rawname)
            name = re.sub(r"-{2,}", "-", name).strip("-")
            if not safe_name(name):
                return self._error(HTTPStatus.BAD_REQUEST,
                                   "Invalid name (use letters, numbers, spaces, . _ - only).")
            d = mission_path(name)
            if os.path.exists(d):
                return self._send_html(render_index(f'Mission "{name}" already exists.'))

            # Build the target + run the only fallible step (the worktree create, local or
            # remote) BEFORE touching the filesystem, so a failure leaves no half-built
            # mission behind. dev_meta is the mission.json "dev" block (None for ops).
            dev_meta = None
            if kind == "local-dir":
                if not REMOTE_DIR_RE.match(lpath):
                    return self._send_html(render_index(
                        "Target path must be an absolute path (no single quotes)."))
                rp = os.path.realpath(os.path.expanduser(lpath))
                if not os.path.isdir(rp):
                    return self._send_html(render_index(f"No such directory: {rp}"))
                target = {"kind": "local-dir", "path": rp}
            elif kind == "remote":
                # Blank dir = the operator's home dir on the remote host (same default as
                # local-dir above; console-launch.sh's `cd '<dir>'` no-ops on "").
                if not REMOTE_HOST_RE.match(rhost) or (rdir and not REMOTE_DIR_RE.match(rdir)):
                    return self._send_html(render_index(
                        "Invalid remote host (or directory not an absolute path)."))
                target = {"kind": "remote", "host": rhost, "remote_dir": rdir}
            elif kind == "local-repo":
                if not REMOTE_DIR_RE.match(lpath):
                    return self._send_html(render_index(
                        "Repo path must be an absolute path (no single quotes)."))
                # No isdir guard: a dev mission may target a brand-new repo — create_worktree
                # (via _ensure_local_repo) git-inits one if the path is missing or not a repo.
                rp = os.path.realpath(os.path.expanduser(lpath))
                target = {"kind": "local-repo", "path": rp}
                if not base:
                    base = _detect_base_branch(rp)
                err = create_worktree(name, rp, base)
                if err:
                    return self._send_html(render_index(
                        f'Could not create dev mission "{name}": {err}'))
                dev_meta = {"repo": rp, "base_branch": base,
                            "worktree": os.path.join(WORKTREES_DIR, name)}
            elif kind == "remote-repo":
                if not (REMOTE_HOST_RE.match(rhost) and REMOTE_DIR_RE.match(rdir)):
                    return self._send_html(render_index("Invalid remote host or repo path."))
                target = {"kind": "remote-repo", "host": rhost, "remote_dir": rdir}
                # A blank base is detected ON the remote; the resolved name comes back
                # so mission.json records the real branch, not a placeholder.
                wt, base, err = create_remote_worktree(name, rhost, rdir, base)
                if err:
                    return self._send_html(render_index(
                        f'Could not create remote dev mission "{name}": {err}'))
                dev_meta = {"repo": rdir, "base_branch": base,
                            "worktree": wt, "host": rhost}
            else:
                return self._error(HTTPStatus.BAD_REQUEST, "Unknown target kind.")

            meta = {"mode": mode, "target": target}
            if dev_meta is not None:
                meta["dev"] = dev_meta

            os.makedirs(d, exist_ok=True)
            for sub in ARTIFACT_DIRS:
                os.makedirs(mission_path(name, sub), exist_ok=True)
            for fn, contents in scaffold(name).items():
                write_text_atomic(mission_path(name, fn), contents)
            write_mission_meta(name, meta)
            return self._redirect(f"/m/{urllib.parse.quote(name)}/dashboard" + tok_q())

        # kill a mission's running tmux/Claude session (keeps the mission dir).
        # Must come before the tab-save match below, since "kill" matches [a-z]+.
        mk = re.match(r"^/m/([^/]+)/kill$", path)
        if mk:
            name = urllib.parse.unquote(mk.group(1))
            if not safe_name(name) or not os.path.isdir(mission_path(name)):
                return self._error(HTTPStatus.NOT_FOUND, "No such mission.")
            killed = kill_session(name)
            # AJAX path (the index ✕ button): the page patches the card in place, so
            # just report the outcome. Non-JS form posts still get the full re-render.
            if self.headers.get("X-Requested-With") == "fetch":
                return self._send_json({"killed": bool(killed)})
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
    if not _ttyd_listening():
        print(f"WARNING: nothing listening on 127.0.0.1:{CONSOLE_TTYD_PORT} — "
              "the Claude console bridge (claude-console.service / ttyd) isn't up; "
              "mission Console iframes will fail until it is started.",
              file=sys.stderr, flush=True)
    auth = "token required" if TOKEN else "no app auth (firewall-restricted)"
    print(f"Mission Dashboard listening on http://{HOST}:{PORT}  "
          f"missions={MISSIONS_DIR}  [{auth}]", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
