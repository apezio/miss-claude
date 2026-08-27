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
  MISSION_TLS_CERT / MISSION_TLS_KEY
                 serve HTTPS instead of HTTP. Unset = plain http (unchanged).
                 Generate a cert with scripts/make-certs.sh. The ttyd console bridge
                 must serve TLS from the same cert or the browser blocks its iframe.
  MISSION_TLS_CA path to the issuing CA, used only in the `curl` hints given to
                 consoles (default: ca.crt beside the cert).
  MISSION_REDIRECT_PORT
                 with TLS on, port for a tiny http->https redirect listener
                 (default 4202; 0 disables).
  MISSION_ARCHIVES_DIR
                 where the 🗑 button files a deleted mission
                 (default ~/miss-claude-archives).
  MISSION_TRASH_DELAY
                 seconds a queued delete stays undoable before it fires
                 (default 60).
"""

import glob
import hashlib
import html
import json
import os
import random
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
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
# Per-mission dev/preview server ports (see preview_port_for): a dev mission is handed
# one stable port out of this range so two worktrees of one repo never contend for the
# repo's default dev port. Env-overridable for boxes where the range is taken.
PREVIEW_PORT_BASE = int(os.environ.get("MISSION_PREVIEW_PORT_BASE", "24000"))
PREVIEW_PORT_SPAN = max(1, int(os.environ.get("MISSION_PREVIEW_PORT_SPAN", "1000")))
# Where the Spawn wizard goes looking for existing git repos to offer in the "Local
# repo" dropdown (dev missions), so the operator doesn't have to remember the path.
# Colon-separated roots, scanned two levels deep; default = the parent of PRIMARY_REPO
# (the operator's home). The path field stays free text — the dropdown only fills it in.
# How many mission cards the index shows before the "Show N more" button. The rest are
# rendered but hidden client-side, so the filter/search still runs over EVERY mission —
# a search only ever hides non-matches, never missions the cap is holding back.
# 0 disables the cap (show everything).
INDEX_LIMIT = max(0, int(os.environ.get("MISSION_INDEX_LIMIT", "25")))
# Deleting a mission (the 🗑 button on each index card) does NOT erase anything: the
# mission directory is MOVED here, wholesale, so it can be put back with a plain `mv`.
# The delete is queued, not immediate — for TRASH_DELAY seconds the card stays put with
# a countdown and an Undo button, and only then does the sweeper file it away. The
# queue lives on disk (a .trash-pending marker inside the mission), so the countdown
# survives a page reload, a second browser tab, and a dashboard restart.
ARCHIVES_DIR = os.path.realpath(
    os.environ.get("MISSION_ARCHIVES_DIR", os.path.expanduser("~/miss-claude-archives"))
)
TRASH_DELAY = max(5, int(os.environ.get("MISSION_TRASH_DELAY", "60")))
TRASH_FILE = ".trash-pending"
TRASH_TICK = 1.0        # s between sweeps while something is queued
TRASH_IDLE_TICK = 5.0   # s between sweeps when nothing is
REPO_DIRS = [
    os.path.realpath(os.path.expanduser(d))
    for d in os.environ.get(
        "MISSION_REPO_DIRS", os.path.dirname(PRIMARY_REPO)).split(":")
    if d.strip()
]
TOKEN = os.environ.get("MISSION_TOKEN", "").strip()
# TLS. Unset (the default) = plain http, exactly as before — throwaway test instances
# and dev-run keep working with no certificate. Set MISSION_TLS_CERT to a PEM file to
# serve https instead (scripts/make-certs.sh generates cert+key from a local CA);
# MISSION_TLS_KEY defaults to the cert when the two are in one combined PEM.
# MISSION_TLS_CA is only advisory: it's the CA path put into the `curl` hints handed
# to consoles, so their loopback calls to this app can verify us.
TLS_CERT = os.path.expanduser(os.environ.get("MISSION_TLS_CERT", "").strip())
TLS_KEY = os.path.expanduser(os.environ.get("MISSION_TLS_KEY", "").strip()) or TLS_CERT
TLS = bool(TLS_CERT)
TLS_CA = os.path.expanduser(
    os.environ.get("MISSION_TLS_CA", "").strip()
    or (os.path.join(os.path.dirname(TLS_CERT), "ca.crt") if TLS_CERT else "")
)
SCHEME = "https" if TLS else "http"
# Seconds a TLS handshake may take before the connection is dropped. Only bounds the
# handshake — it is cleared once the connection is up, so a slow request is unaffected.
# Generous for a human on a bad link, short enough that a stalled socket doesn't tie up
# a worker thread for long.
TLS_HANDSHAKE_TIMEOUT = float(os.environ.get("MISSION_TLS_HANDSHAKE_TIMEOUT", "20"))
# With TLS on, a plain http:// request to PORT is dropped by the TLS handshake (the
# browser shows a protocol error, not a page). This second, tiny listener exists purely
# to 301 those callers to the https URL. 0 disables it; it is never started without TLS.
REDIRECT_PORT = int(os.environ.get("MISSION_REDIRECT_PORT", "4202")) if TLS else 0
# How a console on THIS box talks back to the app (the LOG.md append hints below).
# Under TLS curl has to be pointed at our private CA — it isn't in the system trust
# store unless the operator ran update-ca-trust — or every append fails verification.
SELF_URL = f"{SCHEME}://127.0.0.1:{PORT}"
SELF_CURL = "curl -s" + (f" --cacert {TLS_CA}" if TLS and TLS_CA else "")
# Port of the ttyd "Claude Console" bridge (claude-console.service). The Console tab
# iframes <scheme>://<this-host>:CONSOLE_TTYD_PORT/?arg=<mission> (see _console_base:
# under TLS the bridge must serve https too, or the browser blocks the iframe).
CONSOLE_TTYD_PORT = int(os.environ.get("CONSOLE_TTYD_PORT", "4201"))
# Base URL the browser uses to reach that ttyd bridge, WITHOUT a trailing slash or
# query (the console helpers append "/?arg=..."). Empty (default) preserves the
# original behavior: the console is dialed at <scheme>://<request-host>:CONSOLE_TTYD_PORT.
# Set this to a SAME-ORIGIN path (e.g. "/u/<id>/console-ws") when the dashboard runs behind
# a reverse proxy that terminates TLS and routes the ttyd port under a path — so the iframe
# stays same-origin (no mixed content) and the raw ttyd port is never exposed to the browser.
CONSOLE_BASE_URL = os.environ.get("CONSOLE_BASE_URL", "").strip().rstrip("/")
# Same-origin path prefix under which this whole dashboard is mounted by a reverse
# proxy (e.g. "/u/<id>"). Empty (default) = mounted at the origin root (original
# behavior). When set, every internally-generated link/redirect is prefixed with it
# (via bp()), and incoming request paths have it stripped before routing — the proxy
# passes the full prefixed path straight through (no rewrite), symmetric with ttyd's
# --base-path. Console-iframe URLs are prefixed separately via CONSOLE_BASE_URL, which
# in that deployment already begins with APP_BASE, so bp()/_redirect() must NOT double
# it (they guard on an existing APP_BASE prefix). Mirrors CONSOLE_BASE_URL.
APP_BASE = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
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
PLAN_USAGE_STALE_MAX = 900  # s — bridge fetch blips with the last good reading, no longer
_plan_usage_cache = {"at": 0.0, "data": None, "good_at": 0.0}

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
    f"{SELF_CURL} -d \"text=<entry>\" {SELF_URL}/m/<mission>/log/append "
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

    {SELF_CURL} -d "text=<entry>" {SELF_URL}/m/<mission>/log/append

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
        # The worktree itself says which repo it belongs to; only a worktree git can't
        # identify falls back to the dashboard's default repo.
        repo = repo_root_of(wt) or PRIMARY_REPO
        return {
            "mode": "dev",
            "target": {"kind": "local-repo", "path": repo},
            "dev": dev_meta(repo, BASE_BRANCH, worktree=wt),
        }
    return {"mode": "ops", "target": {"kind": "local-dir", "path": ""}}


def dev_identity(name):
    """The normalized dev identity of a mission (mode == dev), or None for ops/console.
    Fills the fields older sidecars lack (role, branch, repo_id) from what they DO
    record, so every reader sees one shape:
      {role, repo, repo_id, base_branch, worktree, branch, integration_worktree, host}"""
    tgt = mission_target(name)
    if tgt.get("mode") != "dev":
        return None
    dev = dict(tgt.get("dev") or {})
    dev.setdefault("role", "feature")
    if dev["role"] not in ("feature", "integrator"):
        dev["role"] = "feature"
    host = dev.get("host") or (tgt.get("target") or {}).get("host")
    if host:
        dev["host"] = host
    repo = dev.get("repo") or PRIMARY_REPO
    if not host:
        repo = os.path.realpath(os.path.expanduser(repo))
    dev["repo"] = repo
    dev.setdefault("repo_id", repo_id_of(repo))
    dev.setdefault("base_branch", BASE_BRANCH)
    if dev["role"] == "feature":
        wt = dev.get("worktree") or os.path.join(WORKTREES_DIR, name)
        dev["worktree"] = wt
        dev.setdefault("branch", "claude/" + (os.path.basename(wt.rstrip("/")) or name))
    return dev


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
    # Local dev mission: the worktree it develops in (an integrator: the checkout that
    # holds the integration branch).
    if tgt.get("mode") == "dev":
        if dev.get("role") == "integrator":
            return None, (dev.get("integration_worktree") or dev.get("repo") or "")
        return None, (dev.get("worktree") or os.path.join(WORKTREES_DIR, name))
    # Local ops/console: a chosen dir if set, else the mission folder.
    return None, (target.get("path") or mission_path(name))


def location_line(name):
    """The "where this console runs" readout — server + working directory — as one
    .meta line. Shared by the mission page header and the index cards so the two can
    never drift: both go through mission_location(), i.e. mission.json when present
    and the legacy inference otherwise, which is what console-launch.sh does too.
    The directory half is dropped when unknown (a remote mission whose sidecar
    carries no dir) rather than rendering an empty <code>."""
    host, directory = mission_location(name)
    server = host or socket.gethostname()
    out = ('<div class="meta loc">'
           f'<span title="server the console runs on">🖥 {html.escape(server)}</span>')
    if directory:
        out += (' · <code title="directory the console works in">'
                f'{html.escape(directory)}</code>')
    return out + "</div>"


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
    cd's into the repo, not the mission folder). The guess (_console_cwd_guess) is
    the fallback for when no local console is running."""
    cwd, remote = _console_cwd_guess(name)
    if remote:
        return None, True
    live = _live_console_cwd(name)
    if live:
        return live, False
    return cwd, False


def _console_cwd_guess(name):
    """console_cwd() minus the live /proc override: the metadata-derived
    (cwd, remote_bool). Cheap (no tmux/proc calls), so per-card index code
    (first_prompt) can afford it — and the LAUNCH cwd is where the original
    session's transcript lives, which is exactly what the blurb wants."""
    tgt = mission_target(name)
    target = tgt.get("target") or {}
    if target.get("kind") in ("remote", "remote-repo"):
        return None, True
    if tgt.get("mode") == "dev" and (tgt.get("dev") or {}).get("host"):
        return None, True               # defensive: remote dev (kind already caught it)
    if tgt.get("mode") == "dev":
        dev = tgt.get("dev") or {}
        if dev.get("worktree"):
            return dev["worktree"], False
        return mission_path(name), False
    # ops/console: a chosen local dir if set, else the mission folder
    if target.get("path"):
        return target["path"], False
    return mission_path(name), False


def live_console_transcript(name):
    """Absolute path of the transcript the mission's console is writing RIGHT NOW, as
    recorded by the console itself, or None.

    This is the only exact answer available. Everything else here infers a transcript
    from the cwd, and both inferences drift:
      - newest-*.jsonl-in-dir picks up any other session sharing the cwd — a second
        mission launched at $HOME, or the dashboard's own detached `claude -p` doc
        updater, which runs with cwd = the mission folder (mission-doc-stop.py);
      - the pinned uuid (_pinned_session_id) is only the id the console STARTED from.
        A console outlives that id — verified: a /clear opens a NEW session file and
        abandons the old one mid-process (--resume and a restart do keep it), after
        which the pinned file stops growing and its last size freezes on the badge
        forever. This box's integrator console had drifted two ids down that chain,
        pinned 3d9eb8ff -> e27e8328 -> live 1dc18967, and read 6 days stale.

    So the console reports its own identity instead: scripts/mission-console-session.py
    runs as a SessionStart + UserPromptSubmit hook inside the mission console only
    (console-hooks*.settings.json, wired by the launch scripts) and writes the hook
    payload's `transcript_path` to <mission dir>/.console-session on every start, clear,
    resume and prompt. The bg doc updater can't clobber it: it is spawned with no
    --settings and with the hook env stripped, so these hooks never fire for it.

    Returns None (fall back to the inferences) when the marker is absent — every console
    started before this shipped, until it is next opened. The recorded path is verified
    to be a *.jsonl under PROJECTS_DIR, so a hand-edited marker can only ever point at
    another transcript, never elsewhere on the filesystem. It is NOT required to exist:
    Claude Code names the transcript before it writes it, so a just-started console has a
    valid marker and no file — latest_context() reports that as "starting", which is the
    truth, instead of falling back to whatever neighbour wrote last."""
    try:
        with open(mission_path(name, ".console-session"), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    path = rec.get("transcript_path") if isinstance(rec, dict) else None
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return None
    path = os.path.realpath(path)
    root = os.path.realpath(PROJECTS_DIR)
    if not path.startswith(root + os.sep):
        return None
    return path


def _pinned_session_id(name):
    """The Claude session UUID this mission's console STARTED from, or None when the
    newest transcript in the console's cwd is safe to use instead.

    console-launch.sh pins an ops mission whose console works in a CHOSEN local dir
    (kind local-dir / local-repo with a path), because that dir is usually SHARED — a
    blank Path in the Spawn form means $HOME, and every such mission (plus every stray
    `claude` run) writes into that one ~/.claude/projects dir, so "newest transcript in
    the dir" is some *other* session's conversation. It runs
    `claude --resume <uuid> || --session-id <uuid>` with uuid = mission.json's pinned
    session_id (a renamed mission keeps its original) else uuid5(NAMESPACE_URL, <name>),
    i.e. `uuidgen --sha1 --namespace @url --name <name>`. Mirror that recipe so the
    readouts follow the same conversation the console does.

    Deliberately NARROWER than the launcher on one point: a chosen dir that is the
    mission's OWN folder (or below it) is not shared with anybody, so there is no
    collision to solve and pinning only hurts — it would freeze the badge on the id the
    console started from (see live_console_transcript). The launcher still pins those,
    and should: it pins to keep each mission's CONVERSATION separate, while this pins
    only to identify which file to READ, and the two answers are allowed to differ.

    Keep the rest in sync with console-launch.sh; first_prompt() and mission_context()
    both go through this."""
    tgt = mission_target(name)
    target = tgt.get("target") or {}
    path = target.get("path")
    if (tgt.get("mode") != "ops"
            or target.get("kind") not in ("local-dir", "local-repo")
            or not path):
        return None
    own = os.path.realpath(mission_path(name))
    chosen = os.path.realpath(path)
    if chosen == own or chosen.startswith(own + os.sep):
        return None                      # private to this mission -> nothing to collide with
    sid = (read_mission_meta(name) or {}).get("session_id")
    if isinstance(sid, str) and re.fullmatch(r"[0-9a-f-]{36}", sid):
        return sid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


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


def latest_context(cwd, session_id=None, transcript=None):
    """Current context for the LIVE session at `cwd` = the newest transcript by mtime
    in the cwd's project dir. FINDINGS #4: no cross-session fallback — a restarted
    console leaves older session files whose stale numbers would mislead; if the live
    session has no usage yet, return {"state":"starting"}. FINDINGS #7: on that
    newest-in-dir path the cwd field is verified so a munge collision can't surface
    another mission's number (the two paths below identify the file outright instead).

    Which transcript, in order of how certain the identification is:
      - `transcript` — the exact file the console says it is writing, from its own hook
        (live_console_transcript). `cwd` is then only the guard's reference and is
        ignored. This is the one that stays right across /clear, /resume and restarts.
      - `session_id` (see _pinned_session_id) — the uuid the console STARTED from, at
        `<uuid>.jsonl`. Needed whenever the cwd is shared: at a chosen local dir — $HOME
        for most ops missions — "newest in the dir" is whichever console wrote last, so
        every mission at that dir reported the SAME number (one busy session's). Returns
        None when that transcript doesn't exist yet (the mission's own conversation
        hasn't started), rather than falling back to a neighbour's.
      - neither — newest in the dir, with the cwd guard. Right for a console whose dir
        is its own.

    A /compact writes an `isCompactSummary` line but NO usage block; the true
    post-compact size is unknown until the next turn's API call. Scanning
    newest-first, if we meet that marker BEFORE any usage, the only usage we can
    find is the pre-compact (stale, high) one — so return {"state":"compacted"}
    rather than that misleading number. It self-corrects to "ok" on the next turn,
    whose fresh usage block then precedes the compact marker. Returns None when
    there's no transcript dir/file at all."""
    pdir = _project_dir_for_cwd(cwd)
    if transcript:
        f = transcript                   # the console's own answer — already verified
        if not os.path.isfile(f):
            # Named but not written yet: a console that just started (or /clear'd) has
            # its next transcript's path before the file exists. It has no usage, and
            # borrowing a neighbour's would be exactly the bug this avoids.
            return {"state": "starting"}
    elif session_id:
        f = os.path.join(pdir, session_id + ".jsonl")   # this mission's own conversation
        if not os.path.isfile(f):
            return None
    else:
        files = sorted(glob.glob(os.path.join(pdir, "*.jsonl")),
                       key=os.path.getmtime, reverse=True)
        if not files:
            return None
        f = files[0]                     # live session = most recently written
    target = os.path.realpath(cwd)
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
    if (session_id is None and transcript is None
            and cwd_seen is not None and cwd_seen != target):
        return None                      # munge collision -> not our dir
    # No cwd check once the file was identified outright (by hook or by uuid): that IS
    # this mission's conversation, which beats any dir match — and a session's `cwd`
    # field follows the console's own `cd`, e.g. fleet-maintenance launches at $HOME and
    # works in ~/missions/fleet-maintenance, which the check would call somebody else's.

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
    ok | starting | none | remote (FINDINGS #8).

    Requires a running session for any non-remote state: the badge placeholder is
    now ALWAYS emitted server-side (no more render-time has_session/session_running
    gate — that gate made the badge vanish for a page's whole lifetime if the tmux
    check happened to miss at the one moment the page rendered). So this is the only
    place left that must refuse to show a number for a dead console — otherwise a
    killed/restarted session would surface its last transcript's stale size forever."""
    try:
        if not session_running(name):
            return {"state": "none"}
        # Best identification first: the console's own hook-recorded transcript, which
        # needs no cwd at all. Then the pinned uuid, looked for at the LAUNCH cwd — that
        # project dir is where the pinned file lives, so the live-/proc override
        # console_cwd() applies would only mislead. Then newest-in-dir, where the /proc
        # cwd is exactly what we want.
        live = live_console_transcript(name)
        sid = None if live else _pinned_session_id(name)
        if live or sid:
            cwd, remote = _console_cwd_guess(name)
        else:
            cwd, remote = console_cwd(name)
        if remote:
            return {"state": "remote"}
        info = latest_context(cwd, sid, live)
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
    if isinstance(data, dict) and data.get("state") == "ok":
        if isinstance(data.get("session"), dict):
            data["session"]["eta_full_ms"] = _record_session_history(data["session"], now)
        c["good_at"] = now
    elif isinstance(c["data"], dict) and c["data"].get("state") == "ok" \
            and now - c["good_at"] < PLAN_USAGE_STALE_MAX:
        # A failed refresh (network blip, or a 401 while the claude CLI rotates the
        # OAuth token on disk) used to get cached as "none", which blanks the header
        # bars for EVERY client for a whole TTL. Serve the last good reading through
        # such blips instead; a real outage still goes dark after PLAN_USAGE_STALE_MAX.
        c["at"] = now                     # still at most one API attempt per TTL
        return c["data"]
    c["at"], c["data"] = now, data
    return c["data"]


# ---------------------------------------------------------------------------
# Repo identity — the one place a dev mission's git identity is derived
# ---------------------------------------------------------------------------
# A dev mission is (repo, feature worktree, feature branch, integration branch,
# integration worktree). Everything below records those explicitly in mission.json's
# `dev` block (see dev_meta()) so that later steps — the console launcher, the guard
# hook, the badges, the integrator — derive from the SAME recorded facts instead of
# re-guessing from the process cwd, a worktree's basename, or the dashboard's own
# default repo. `scripts/mission-env.py` turns that block into the environment the
# console exports (MISS_REPO_ROOT / MISS_REPO_ID / MISS_WORKTREE / MISS_FEATURE_BRANCH
# / MISS_INTEGRATION_BRANCH / MISS_INTEGRATION_WORKTREE), which is what survives a
# service restart and a /clear.

def _git_out(cwd, *args, timeout=10):
    """stdout of `git -C <cwd> <args>` (stripped), or None on any failure. Never raises."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_root_of(path):
    """The MAIN checkout of the git repo <path> belongs to (realpath), or None.

    A linked worktree resolves to the repository it was created from (via the
    common git dir), so a worktree's identity is the repo's — never the worktree's
    own directory. A path that is not inside any git repo returns None."""
    if not path:
        return None
    common = _git_out(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        # Older git (no --path-format): a relative answer is relative to <path>'s toplevel.
        common = _git_out(path, "rev-parse", "--git-common-dir")
        if common is None:
            return None
        if not os.path.isabs(common):
            top = _git_out(path, "rev-parse", "--show-toplevel") or path
            common = os.path.join(top, common)
    common = os.path.realpath(common)
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return common   # bare repo: the git dir itself is the identity


def repo_id_of(repo_root):
    """Stable, human-readable id for a repo: <basename>-<8 hex of the realpath>.
    The hash keeps two repos with the same directory name (e.g. two `frontend`
    checkouts) distinct; the basename keeps it readable in badges and env."""
    root = os.path.realpath(repo_root)
    return "%s-%s" % (os.path.basename(root) or "repo",
                      hashlib.sha1(root.encode("utf-8")).hexdigest()[:8])


def same_repo(a, b):
    """True when two paths (repo roots or worktrees) belong to the same repository."""
    ra, rb = repo_root_of(a), repo_root_of(b)
    return ra is not None and ra == rb


def worktree_branch(worktree):
    """The branch checked out in <worktree> (short name), or None (detached/absent)."""
    return _git_out(worktree, "symbolic-ref", "--short", "HEAD")


def integration_worktree_of(repo, branch):
    """The checkout (realpath) of <repo> that has <branch> checked out — the ONLY place
    a fast-forward of that branch can be performed with a working tree — or None when
    the branch is checked out nowhere. Reads `git worktree list --porcelain`, so a
    linked worktree holding the branch counts just like the main checkout."""
    out = _git_out(repo, "worktree", "list", "--porcelain")
    if not out:
        return None
    cur = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line.startswith("branch ") and cur:
            if line[len("branch "):] == "refs/heads/" + branch:
                return os.path.realpath(cur)
    return None


def integration_worktree_path(repo_root, branch):
    """Where the dashboard puts a dedicated integration checkout when <branch> is
    checked out nowhere: WORKTREES_DIR/.integration/<repo_id>--<branch>. Hidden from
    the mission-name namespace (a mission can't be named `.integration`), keyed by
    repo id so two repos never share one, and inside WORKTREES_DIR so the feature
    guard treats it as foreign territory for every worker."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "base"
    return os.path.join(WORKTREES_DIR, ".integration", repo_id_of(repo_root) + "--" + slug)


def ensure_integration_worktree(repo, branch):
    """Resolve — creating if needed — the checkout of <repo> holding <branch>.
    Returns (path, None) or (None, error). If the branch is checked out in the main
    checkout or any linked worktree, that is the answer (nothing is created). Otherwise
    a dedicated worktree is added at integration_worktree_path() WITHOUT -b (the
    branch must already exist). Never raises."""
    if not BRANCH_RE.match(branch or ""):
        return None, "Invalid integration branch."
    root = repo_root_of(repo)
    if root is None:
        return None, "%s is not a git repository." % repo
    found = integration_worktree_of(root, branch)
    if found:
        return found, None
    if _git_out(root, "show-ref", "--verify", "refs/heads/" + branch) is None:
        return None, "Branch %s does not exist in %s." % (branch, root)
    dest = integration_worktree_path(root, branch)
    if os.path.isdir(dest):
        if same_repo(dest, root) and worktree_branch(dest) == branch:
            return dest, None
        return None, ("%s exists but is not a checkout of %s on %s — move it aside."
                      % (dest, root, branch))
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        r = subprocess.run(
            ["git", "-C", root, "worktree", "add", dest, branch],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60, text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, "Could not create the integration worktree: %s" % e
    if r.returncode != 0:
        lines = (r.stdout or "").strip().splitlines()
        return None, "git worktree add failed: %s" % (lines[-1] if lines else r.returncode)
    return os.path.realpath(dest), None


def dev_meta(repo, base_branch, worktree=None, role="feature", host=None,
             integration_worktree=None):
    """Build a mission.json `dev` block — the durable identity of a dev mission:

        repo                  main checkout (repo_root)      role   feature | integrator
        repo_id               stable id (see repo_id_of)     branch feature branch (claude/<slug>)
        worktree              the feature checkout           base_branch  integration branch
        integration_worktree  checkout holding base_branch   host   (remote dev only)
        preview_port          a per-mission port for any dev/preview server

    `base_branch` is the integration branch (kept under its historical key so every
    existing sidecar and reader stays valid). For a LOCAL repo the root/id/integration
    worktree are resolved with git; a REMOTE repo (host set) records what the caller
    knows — its identity can't be probed from here. Never raises."""
    d = {"role": role, "repo": repo, "base_branch": base_branch}
    if host:
        d["host"] = host
        d["repo_id"] = repo_id_of(repo)          # keyed by the remote path (best effort)
    else:
        root = repo_root_of(repo) or os.path.realpath(os.path.expanduser(repo))
        d["repo"] = root
        d["repo_id"] = repo_id_of(root)
        if integration_worktree is None:
            integration_worktree = integration_worktree_of(root, base_branch)
        if integration_worktree:
            d["integration_worktree"] = integration_worktree
    if worktree:
        d["worktree"] = worktree
        d["branch"] = "claude/" + (os.path.basename(worktree.rstrip("/")))
    return d


def preview_port_for(name):
    """A stable per-mission port in [PREVIEW_PORT_BASE, +PREVIEW_PORT_SPAN) for a
    dev/preview server, derived from the mission name so it survives restarts and
    /clear without any registry. Two missions in one repo therefore never fight
    over a repo's default port (e.g. Vite's 5173) and a feature worktree can't
    accidentally become "the" dev server: the canonical one is whatever runs from
    the integration worktree on the repo's own port."""
    h = int(hashlib.sha1(name.encode("utf-8")).hexdigest()[:8], 16)
    return PREVIEW_PORT_BASE + (h % PREVIEW_PORT_SPAN)


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
        # Attach — but only to a worktree of THIS repo. Mission names are global while
        # worktrees are per-repo, so a leftover `<name>` worktree from another repo
        # would otherwise be silently adopted and the mission's recorded repo would
        # not be the repo its console actually works in.
        if not same_repo(wt, repo):
            return ("%s already exists but belongs to a different repository (%s) — "
                    "pick another mission name." % (wt, repo_root_of(wt) or "unknown"))
        return None  # already a dev worktree of this repo — attach, nothing to do
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
    """Map (repo, base_branch) -> {branch_slug: mission_name} for local dev
    missions. Reads each mission's target (mission.json or legacy inference) so
    missions developing different local repos are grouped by the repo whose
    `branch --merged` decides them. branch_slug is the WORKTREE's basename — the
    branch is claude/<slug> — which equals the mission name unless the mission
    was renamed (the worktree/branch keep their original name; see
    rename_mission)."""
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
        dev = dev_identity(name)
        if dev is None or dev.get("role") != "feature":
            continue   # an integrator mission has no feature branch to be merged
        # Keyed by the RECORDED branch (claude/<slug>) — never re-derived from a name.
        slug = dev["branch"][len("claude/"):] if dev["branch"].startswith("claude/") \
            else dev["branch"]
        groups.setdefault((dev["repo"], dev["base_branch"]), {})[slug] = name
    return groups


def merged_dev_missions():
    """Set of dev-mission names whose claude/<name> branch is fully merged into its
    own base branch (working, by default). Groups missions by (repo, base) and runs
    one `git branch --merged <base>` per group; never raises — returns whatever it
    could compute so the dashboard still renders if git is unavailable. `git branch
    --merged` lists every branch whose tip is reachable from base, i.e. has no
    unmerged commits left; we keep the claude/<slug> branches that belong to that
    group and map each slug back to its mission name (they differ after a rename)."""
    out = set()
    for (repo, base), slugs in _dev_missions_by_repo().items():
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
            if b.startswith("claude/") and b[len("claude/"):] in slugs:
                out.add(slugs[b[len("claude/"):]])
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
    """Most recent mtime of any file in a mission dir (recursively). The .blurb*
    cache files (written by the summarize-missions cron, not by mission work) are
    skipped so a blurb refresh doesn't fake activity / reorder the index — and so is
    the .trash-pending marker, so queuing (or undoing) a delete doesn't count as
    activity and shuffle the card up the list mid-countdown."""
    latest = 0.0
    for root, _dirs, files in os.walk(d):
        for f in files:
            if f.startswith(".blurb") or f == TRASH_FILE:
                continue
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


def _tmux_pane_snapshot():
    """One `list-panes -a` + one `ps` snapshot, shared by claude_sessions() and
    adhoc_console_sessions() so a single render_index() call pays for each subprocess
    once instead of twice. Returns (panes, children, comm):
      panes    — list of (session_name, pane_pid, pane_start_command, pane_current_path)
      children — ppid -> [pid, ...]
      comm     — pid -> short command name (no path/args)
    """
    rc, out = _run_tmux(
        "list-panes", "-a", "-F",
        "#{session_name}\t#{pane_pid}\t#{pane_start_command}\t#{pane_current_path}",
        capture=True,
    )
    panes = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4 or not parts[1].isdigit():
                continue
            sn, pid, start_cmd, cur_path = parts
            panes.append((sn, int(pid), start_cmd, cur_path))
    children, comm = {}, {}
    if panes:
        try:
            r = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,comm="],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, text=True,
            )
        except (OSError, subprocess.SubprocessError):
            r = None
        if r is not None and r.returncode == 0:
            for line in r.stdout.splitlines():
                p = line.split(None, 2)
                if len(p) < 3 or not (p[0].isdigit() and p[1].isdigit()):
                    continue
                pid, ppid, cmd = int(p[0]), int(p[1]), p[2]
                comm[pid] = cmd
                children.setdefault(ppid, []).append(pid)
    return panes, children, comm


def _subtree_has(children, comm, root, wanted):
    """True if `root` or any of its descendants (per the `children`/`comm` maps from
    _tmux_pane_snapshot()) has a comm starting with `wanted`."""
    stack, seen = [root], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if comm.get(pid, "").startswith(wanted):
            return True
        stack.extend(children.get(pid, ()))
    return False


def claude_sessions(panes, children, comm):
    """Set of mission names whose console has Claude ACTUALLY RUNNING — not merely a tmux
    session that has fallen back to its `bash --login` shell (console-session.sh runs
    `claude … || claude …` and then `exec bash`, so an exited/never-started Claude leaves a
    live-but-idle pane). tmux's pane_current_command is no help: it reports the pane leader
    (console-session.sh / login bash) even while Claude runs as its child. So we walk each
    session's process subtree (from the shared _tmux_pane_snapshot()) for a `claude`
    process — comm starts with "claude", which also catches the `claude-miss*` launch
    wrappers on their way up. comm carries no path/args, so a mission dir or name
    containing "claude" can't cause a false match. Live names are always a subset of
    running_sessions()."""
    pane_pids = {}  # mission name -> [pane pid, ...] (usually one pane, but allow several)
    for sn, pid, _start_cmd, _cur_path in panes:
        if sn.startswith(SESSION_PREFIX):
            pane_pids.setdefault(sn[len(SESSION_PREFIX):], []).append(pid)
    return {
        name
        for name, pids in pane_pids.items()
        if any(_subtree_has(children, comm, p, "claude") for p in pids)
    }


# Ad-hoc "Console" sessions (Spawn wizard -> Console mode) are deliberately stateless —
# see _remote_console_url/_local_console_url and console-launch.sh's REMOTE/LOCAL CONSOLE
# blocks: /spawn just 302s to the ttyd URL, nothing is ever written to MISSIONS_DIR. Their
# ONLY record is the tmux session itself, named by console-launch.sh's deterministic hash:
# `remote-<12 hex>` (ssh to another host) or `local-<12 hex>` (runs in a jumpbox dir).
ADHOC_SESSION_RE = re.compile(r"^(remote|local)-[0-9a-f]{6,32}\Z")


def adhoc_console_sessions(panes, children, comm):
    """List every live ad-hoc console purely from tmux/ps state (no new persistence —
    these sessions were never meant to be tracked), using the shared _tmux_pane_snapshot()
    (same live/idle subtree-walk as claude_sessions()). Returns dicts sorted by name:
    {name, kind ('remote'/'local'), live (bool), target (best-effort display string)}."""
    sessions = {}
    for sn, pid, start_cmd, cur_path in panes:
        if ADHOC_SESSION_RE.match(sn):
            sessions[sn] = (pid, start_cmd, cur_path)
    if not sessions:
        return []

    out = []
    for name, (pane_pid, start_cmd, cur_path) in sessions.items():
        kind = "remote" if name.startswith("remote-") else "local"
        # A local console runs Claude directly, so "claude" shows up locally. A remote
        # console's Claude runs ON THE OTHER HOST over ssh — there is no local `claude`
        # process to find, ever. Its liveness signal is the local `ssh` child: once ssh
        # exits, console-launch.sh's wrapper falls through to `exec bash --login -i`,
        # which replaces the pane's own process rather than leaving a dead child behind.
        wanted = "claude" if kind == "local" else "ssh"
        live = _subtree_has(children, comm, pane_pid, wanted)
        out.append({
            "name": name,
            "kind": kind,
            "live": live,
            "target": _adhoc_console_target(kind, start_cmd, cur_path),
        })
    out.sort(key=lambda s: s["name"])
    return out


def _adhoc_console_target(kind, start_cmd, cur_path):
    """Best-effort display string for an ad-hoc console. Nothing is persisted for these,
    so the tmux pane's original start command is the only source: strip the backslash
    shell-escaping console-launch.sh's `printf %q` baked in, then pull the ssh host and
    the remote `cd '<dir>'` back out. A local console has no ssh wrapper to parse, so use
    the pane's live current path instead."""
    if kind == "local":
        return cur_path or "~"
    plain = start_cmd.replace("\\", "")
    host_m = re.search(r"ssh -tt (\S+)", plain)
    host = host_m.group(1) if host_m else "?"
    dir_m = re.search(r"cd '([^']*)'", plain)
    directory = dir_m.group(1) if dir_m and dir_m.group(1) else "~"
    return f"{host}:{directory}"


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


def _session_pane(session):
    """The tmux pane id (e.g. '%7') of a session's first pane, or None.

    Session commands accept the '=' exact-match prefix, but pane commands
    (send-keys/copy-mode/capture-pane) reject it — so anything that talks to a pane
    resolves the pane id here first: globally unique, no prefix-match risk."""
    rc, out = _run_tmux(
        "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}", capture=True
    )
    if rc != 0:
        return None
    for line in out.splitlines():
        sn, _, pid = line.partition("\t")
        if sn == session and pid:
            return pid
    return None


def _kill_tmux_session(session):
    """Stop an arbitrary tmux session cleanly (mission or ad-hoc console). Sends the
    Claude TUI an EOF (Ctrl-D) so it exits gracefully and flushes its transcript, gives it
    a moment to finish writing, then ends the tmux session as a backstop. Returns True if
    a session was running and is now gone.

    Targeting note: the '=' exact-match prefix only works for session commands
    (has-session/kill-session); pane commands (send-keys/list-panes) reject it. So we
    resolve this session's exact pane id and target that — globally unique, no prefix-match
    risk, and not dependent on pane_current_command (Claude runs under a wrapper, so that
    field reads 'bash' and can't tell us whether Claude is up)."""
    rc, _ = _run_tmux("has-session", "-t", "=" + session)
    if rc != 0:
        return False
    pane = _session_pane(session)
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
    rc, _ = _run_tmux("has-session", "-t", "=" + session)
    return rc != 0


def kill_session(name):
    """Stop a mission's Claude session cleanly (does NOT delete the mission). The next
    open re-creates the session, which RESUMES the conversation (console-session.sh runs
    `claude --continue`)."""
    return _kill_tmux_session(SESSION_PREFIX + name)


# ---------------------------------------------------------------------------
# Console key bar — typing into a console from the page instead of the terminal.
#
# The ttyd terminal (port 4201) is a DIFFERENT ORIGIN from this app, so the page
# around the iframe cannot reach into it to synthesise keystrokes. It can, however,
# talk to the same tmux session ttyd is attached to — `tmux send-keys` lands in the
# pane exactly as if it were typed. That is the whole trick behind this section, and
# it is what makes a phone usable: no phone keyboard has Esc, and a touch screen has
# no scrollback, no selection and no paste into a canvas-drawn terminal.
#
# Everything here is targeted BY SESSION NAME, validated against the same shapes the
# rest of the app already knows ("mission-<name>" from console-launch.sh, or an ad-hoc
# "remote-<hex>"/"local-<hex>"), and then only ever reached through a resolved pane id.
# No value from the request is interpolated into a shell command — tmux is exec'd
# directly with an argv (see _run_tmux), so pasted text needs no quoting.
# ---------------------------------------------------------------------------
CONSOLE_SESSION_RE = re.compile(
    r"^(?:" + re.escape(SESSION_PREFIX) + r"[A-Za-z0-9._-]{1,64}"
    r"|(?:remote|local)-[0-9a-f]{6,32})\Z"
)

# Key buttons -> the tmux key name send-keys understands. Deliberately small: the
# keys a phone keyboard cannot produce (Esc/Tab/arrows) plus the ones that answer a
# Claude prompt (Enter accepts the highlighted choice; 1/2/3 pick a numbered one).
# Ctrl-D is NOT here on purpose — that quits Claude; the ✕ kill button is the way out.
CONSOLE_KEYS = {
    "esc": "Escape",
    "enter": "Enter",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "tab": "Tab",
    "btab": "BTab",          # Shift-Tab — cycles Claude's permission/plan modes
    "bspace": "BSpace",
    "1": "1",
    "2": "2",
    "3": "3",
    "ctrl-c": "C-c",         # interrupt what Claude is doing (twice = clear input)
}

# Scroll buttons — the phone's browser cannot scroll the terminal canvas itself.
# Claude's TUI keeps the ALTERNATE screen (see console-session.sh), so the conversation
# lives inside Claude and tmux's own history stays empty: these send Claude the very
# same PageUp/PageDown a desktop keyboard sends (fn-Up/fn-Down on a Mac), which pages
# the replies while the prompt box and status line stay pinned to the bottom. Driving
# tmux copy-mode here instead (tried, reverted) scrolled the whole viewport and took
# the prompt off-screen with it.
CONSOLE_SCROLL = {"pgup", "pgdn", "bottom"}

# "⤓ Live" — Claude's pager has no one jump-to-bottom key, so page down hard enough to
# reach it from any realistic depth. Presses at the bottom are verified no-ops, and
# tmux takes the whole burst in a single send-keys.
SCROLL_BOTTOM_PAGES = 200

MAX_PASTE = 80000   # characters accepted in one insert (large pastes OK, still not a file)


def _pane_in_copy_mode(pane):
    rc, out = _run_tmux("display-message", "-p", "-t", pane, "#{pane_in_mode}",
                        capture=True)
    return rc == 0 and out.strip() == "1"


def console_send(session, action, text="", submit=False):
    """Deliver one key / scroll step / chunk of text to a live console.

    Returns (ok, message). Unknown actions and dead sessions are refused rather than
    guessed at, so a stale page (a console killed in another tab) says so instead of
    silently typing into whatever session later takes that name."""
    if not CONSOLE_SESSION_RE.match(session or ""):
        return False, "Bad console session name."
    if _run_tmux("has-session", "-t", "=" + session)[0] != 0:
        return False, "That console is not running."
    pane = _session_pane(session)
    if not pane:
        return False, "That console has no pane."

    if action == "text":
        if not text:
            return False, "Nothing to send."
        if len(text) > MAX_PASTE:
            return False, "Too much text (max %d characters)." % MAX_PASTE
        # Insert via the paste buffer, not send-keys -l: -p wraps it in bracketed-paste
        # markers, which is how a real paste arrives, so a multi-line insert lands as
        # ONE block in Claude's prompt instead of submitting at every newline. The text
        # is left in the prompt for editing — "Insert ⏎" is a separate Enter below.
        rc, _ = _run_tmux("set-buffer", "--", text.replace("\r\n", "\n"))
        if rc != 0:
            return False, "tmux would not take the text."
        rc, _ = _run_tmux("paste-buffer", "-p", "-t", pane)
        if rc != 0:
            return False, "tmux would not paste into the console."
        if submit:
            time.sleep(0.15)   # let the TUI ingest the paste before it is submitted
            _run_tmux("send-keys", "-t", pane, "Enter")
        return True, "sent"

    if action in CONSOLE_SCROLL:
        # Someone can still put a pane into tmux copy-mode by hand (the prefix key);
        # that mode would swallow these keys, so drop back to the live pane first and
        # let the button page Claude rather than tmux's (empty) scrollback.
        if _pane_in_copy_mode(pane):
            _run_tmux("send-keys", "-X", "-t", pane, "cancel")
        if action == "pgup":
            keys = ["PPage"]
        elif action == "pgdn":
            keys = ["NPage"]
        else:  # bottom
            keys = ["NPage"] * SCROLL_BOTTOM_PAGES
        rc, _ = _run_tmux("send-keys", "-t", pane, *keys)
        return (rc == 0), ("sent" if rc == 0 else "tmux refused that scroll.")

    key = CONSOLE_KEYS.get(action)
    if not key:
        return False, "Unknown key."
    # A key press while scrolled back would go to copy-mode, not Claude — snap the
    # pane back to the live prompt first so the button does what the label says.
    if _pane_in_copy_mode(pane):
        _run_tmux("send-keys", "-X", "-t", pane, "cancel")
    rc, _ = _run_tmux("send-keys", "-t", pane, key)
    return (rc == 0), ("sent" if rc == 0 else "tmux refused that key.")


def console_capture(session, lines=120):
    """The console's visible screen plus `lines` of scrollback, as plain text.

    This is the "select and copy" escape hatch: xterm.js draws the terminal on a
    canvas, so a phone cannot select text in it — the page shows this in a normal
    textarea instead, which every mobile browser can select from and copy.

    A mission console's Claude owns the alternate screen, where tmux has no history to
    give, so there it is the visible screen alone: to copy something further back,
    scroll to it with ▲ first and grab the text a screen at a time."""
    if not CONSOLE_SESSION_RE.match(session or ""):
        return None
    pane = _session_pane(session)
    if not pane:
        return None
    lines = max(0, min(int(lines), 2000))
    # -J unwraps lines the terminal hard-wrapped, so long paths/commands come back
    # as one copyable line rather than screen-width fragments.
    rc, out = _run_tmux("capture-pane", "-p", "-J", "-S", "-%d" % lines,
                        "-t", pane, capture=True)
    if rc != 0:
        return None
    # -J pads to the terminal width; trim so a copy out of the box doesn't carry a
    # tail of spaces, and drop the empty rows below the prompt.
    return "\n".join(ln.rstrip() for ln in out.splitlines()).rstrip("\n") + "\n"


def rename_mission(old, new_raw):
    """Rename a mission — the directory AND everything that keys off the name — so
    the console conversation still resumes under the new name. Returns
    (new_name, message) on success, (None, error_message) otherwise. In order:

      1. slug + validate the new name (same rules as /create and /spawn);
      2. stop a running console session first (graceful, same as the ✕ button —
         the mission page's iframe keeps one open almost always);
      3. pin the resume key: a mission whose console resumes via the name-derived
         session UUID (ops at a chosen local dir or on a remote host) gets that
         uuid RECORDED in mission.json as session_id, which console-launch.sh
         prefers over re-deriving one from the (new) name;
      4. rename ~/missions/<old> -> ~/missions/<new>;
      5. migrate Claude's transcript dir when the console cwd IS the mission
         folder (claude --continue keys history off the cwd);
      6. write mission.json under the new name — materializing the legacy
         inference too, so a sidecar-less dev mission keeps its old-named
         worktree instead of silently decaying to ops.

    A dev mission's worktree and claude/<name> branch are deliberately NOT
    renamed: the console cwd (and its --continue history) live in the worktree,
    and the branch may already be pushed/reviewed elsewhere. dev_badge() and
    merged_dev_missions() read the branch from the worktree path in mission.json,
    so they stay correct after a rename."""
    new = re.sub(r"\s+", "-", (new_raw or "").strip())
    new = re.sub(r"-{2,}", "-", new).strip("-")
    if not safe_name(new):
        return None, "Invalid name (use letters, numbers, spaces, . _ - only)."
    if not safe_name(old) or not os.path.isdir(mission_path(old)):
        return None, "No such mission."
    if new == old:
        return None, "That is already the mission's name."
    old_dir = mission_path(old)
    new_dir = os.path.join(MISSIONS_DIR, new)
    if os.path.exists(new_dir):
        return None, 'A mission named "%s" already exists.' % new
    meta = mission_target(old)   # normalized; materializes the legacy inference
    target = dict(meta.get("target") or {})
    meta["target"] = target
    mode = meta.get("mode")
    kind = target.get("kind") or ""
    path = target.get("path") or ""
    # Guard BEFORE auto-stopping: if the live Claude's real cwd (from /proc) is not
    # where the launcher will reopen the console, it is a HAND-STARTED session — e.g.
    # the operator ran claude-miss-integrator in the pane, which cd's into the repo.
    # Its conversation is keyed to that other cwd, so the post-rename relaunch could
    # not resume it; killing it here would silently drop the operator into a different
    # (older) conversation. Refuse and say how to proceed instead.
    if mode == "dev":
        devm = meta.get("dev") or {}
        if devm.get("role") == "integrator":
            expected_cwd = devm.get("integration_worktree") or devm.get("repo") or old_dir
        else:
            expected_cwd = devm.get("worktree") or os.path.join(WORKTREES_DIR, old)
    else:
        expected_cwd = path or old_dir
    live_cwd = _live_console_cwd(old)
    if live_cwd and os.path.realpath(live_cwd) != os.path.realpath(expected_cwd):
        return None, (
            'Not renamed: the console for "%s" is running Claude in %s, not its '
            "usual directory (%s) — reopening after a rename would resume a "
            "different conversation. Finish or stop that session yourself (✕) "
            "first; you can always get back to it later with "
            "`cd %s && claude --continue`." % (old, live_cwd, expected_cwd, live_cwd))
    stopped = False
    if session_running(old):
        stopped = kill_session(old)
        if not stopped and session_running(old):
            return None, ('Could not stop the running console session for "%s" — '
                          "try the ✕ button on the index, then rename again." % old)
    # Resume-key pinning: these targets resume via uuid5(<mission name>) (see the
    # matching branches of console-launch.sh); record the OLD name's uuid so the
    # conversation survives the rename. An already-pinned id (earlier rename) wins.
    uuid_keyed = mode == "ops" and (
        kind == "remote" or (kind in ("local-dir", "local-repo") and path))
    if uuid_keyed and not meta.get("session_id"):
        meta["session_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, old))
    # Decide the transcript migration BEFORE the paths move: the console cwd is the
    # mission folder itself for a legacy/blank-path ops mission and for one whose
    # chosen path IS the folder (/create writes target.path = the mission dir).
    cwd_is_folder = (mode != "dev" and kind != "remote"
                     and (not path or os.path.realpath(path) == old_dir))
    if path and os.path.realpath(path) == old_dir:
        target["path"] = new_dir
    try:
        os.rename(old_dir, new_dir)
    except OSError as e:
        return None, "Could not rename the mission directory: %s" % e
    # Belt + braces: the mission page's ttyd iframe reconnect-loops, so a fresh
    # mission-<old> session may have raced back in between the kill and the move.
    # Its directory is gone now; clear it so it can't linger as an unlisted zombie.
    if stopped and session_running(old):
        kill_session(old)
    if cwd_is_folder:
        # Claude keys transcripts off the cwd (~/.claude/projects/<munged-cwd>/);
        # carry them over so --continue / --resume still find the conversation.
        src = _project_dir_for_cwd(old_dir)
        dst = _project_dir_for_cwd(new_dir)
        try:
            if os.path.isdir(src) and not os.path.exists(dst):
                os.rename(src, dst)
            elif os.path.isdir(src) and os.path.isdir(dst):
                for fn in os.listdir(src):
                    if not os.path.exists(os.path.join(dst, fn)):
                        os.rename(os.path.join(src, fn), os.path.join(dst, fn))
        except OSError:
            pass   # best-effort: a failed migration only costs resume, not data
    try:
        write_mission_meta(new, meta)
    except (OSError, ValueError):
        return new, ('Renamed mission "%s" to "%s", but could not update its '
                     "mission.json — check the mission folder." % (old, new))
    msg = 'Renamed mission "%s" to "%s".' % (old, new)
    if stopped:
        msg += " Its console session was stopped and resumes on reopen."
    return new, msg


# ---------------------------------------------------------------------------
# Deleting a mission — a queued, undoable move into ARCHIVES_DIR
# ---------------------------------------------------------------------------
# Nothing here erases data. "Delete" means: mark the mission with a .trash-pending
# marker carrying a deadline, leave it fully intact and working for TRASH_DELAY
# seconds (the index card shows a countdown + Undo), then have the sweeper thread
# MOVE the whole directory into ARCHIVES_DIR. Restoring an archived mission is a
# plain `mv ~/miss-claude-archives/<name> ~/missions/`.
#
# The deadline lives on disk rather than in the browser on purpose: the operator
# runs many tabs, and a delete queued in one must count down the same everywhere,
# survive a reload, and still happen if the tab is closed. It also means a delete
# queued before a dashboard restart is honoured (fired on the first sweep) instead
# of being silently forgotten.
#
# What is deliberately NOT touched, for the same reason rename_mission() leaves them
# alone: a dev mission's checkout under WORKTREES_DIR and its claude/<name> branch
# (that work may be committed, pushed or reviewed elsewhere), and Claude's transcript
# directory. Archiving a mission files its DOCS away, nothing more.
# ---------------------------------------------------------------------------

_TRASH_FAILED = set()   # names whose archive failed — warn once, not once per tick


def trash_marker(name):
    return mission_path(name, TRASH_FILE)


def trash_due(name):
    """Epoch seconds at which <name>'s queued delete fires, or 0.0 if none is
    queued. Never raises: a missing, unreadable or malformed marker reads as "not
    queued", which is the safe direction (the mission simply stays)."""
    try:
        with open(trash_marker(name), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return float(data.get("due") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def queue_trash(name):
    """Queue <name> for deletion TRASH_DELAY seconds from now. Returns
    (seconds_left, "") on success, (0, error) otherwise. Re-queuing an already
    queued mission is a no-op that reports the EXISTING countdown — a double-click
    (or a second tab) must not push the deadline out."""
    if not safe_name(name) or not os.path.isdir(mission_path(name)):
        return 0, "No such mission."
    now = time.time()
    due = trash_due(name)
    if due > now:
        return int(round(due - now)), ""
    try:
        write_text_atomic(trash_marker(name),
                          json.dumps({"due": now + TRASH_DELAY, "queued": now}) + "\n")
    except OSError as exc:
        return 0, "Could not queue the delete: %s" % exc
    return TRASH_DELAY, ""


def cancel_trash(name):
    """Undo: drop the marker so the sweeper leaves the mission alone. Returns
    (True, msg) / (False, msg). Nothing else was changed while it was queued, so
    there is nothing to put back."""
    if not safe_name(name):
        return False, "No such mission."
    try:
        os.remove(trash_marker(name))
    except FileNotFoundError:
        return False, 'No delete was queued for "%s".' % name
    except OSError as exc:
        return False, "Could not cancel the delete: %s" % exc
    _TRASH_FAILED.discard(name)
    return True, 'Kept "%s" — its queued delete was cancelled.' % name


def _archive_dest(name):
    """Free path under ARCHIVES_DIR for <name>. Keeps the plain name when it is free
    (so a restore is just `mv` back), else name.<timestamp>, else name.<timestamp>-N."""
    dest = os.path.join(ARCHIVES_DIR, name)
    if not os.path.exists(dest):
        return dest
    stamp = "%s.%s" % (dest, time.strftime("%Y%m%d-%H%M%S"))
    if not os.path.exists(stamp):
        return stamp
    n = 2
    while os.path.exists("%s-%d" % (stamp, n)):
        n += 1
    return "%s-%d" % (stamp, n)


def archive_mission(name):
    """Move ~/missions/<name>/ into ARCHIVES_DIR. Returns (dest, "") on success,
    ("", error) otherwise. A running console is stopped first — its cwd is about to
    move out from under it. The marker is cleared only AFTER a successful move, so a
    failed archive stays queued and is retried on the next sweep instead of leaving a
    mission that is neither deleted nor pending."""
    if not safe_name(name):
        return "", "Invalid mission name."
    src = mission_path(name)
    if not os.path.isdir(src):
        return "", "No such mission."
    if session_running(name):
        kill_session(name)
    try:
        os.makedirs(ARCHIVES_DIR, exist_ok=True)
    except OSError as exc:
        return "", "Could not create %s: %s" % (ARCHIVES_DIR, exc)
    dest = _archive_dest(name)
    try:
        shutil.move(src, dest)
    except (OSError, shutil.Error) as exc:
        return "", "Could not archive %s: %s" % (src, exc)
    try:
        os.remove(os.path.join(dest, TRASH_FILE))
    except OSError:
        pass   # cosmetic: the archived copy is just tidier without it
    # Belt + braces, the same race rename_mission() guards: the mission page's ttyd
    # iframe reconnect-loops, so a fresh mission-<name> session can have raced back in
    # between the kill and the move. Its directory is gone now; clear it so it cannot
    # linger as an unlisted zombie.
    if session_running(name):
        kill_session(name)
    return dest, ""


def sweep_trash():
    """Archive every mission whose queued delete has come due. Returns how many are
    still pending, which is all the sweeper thread needs to pick its next tick."""
    if not os.path.isdir(MISSIONS_DIR):
        return 0
    now = time.time()
    pending = 0
    for entry in sorted(os.listdir(MISSIONS_DIR)):
        if not safe_name(entry) or not os.path.isdir(os.path.join(MISSIONS_DIR, entry)):
            continue
        due = trash_due(entry)
        if not due:
            continue
        if due > now:
            pending += 1
            continue
        dest, err = archive_mission(entry)
        if err:
            pending += 1
            if entry not in _TRASH_FAILED:
                _TRASH_FAILED.add(entry)
                print("WARNING: could not archive mission %r: %s (staying queued, "
                      "will retry)" % (entry, err), file=sys.stderr, flush=True)
        else:
            _TRASH_FAILED.discard(entry)
            print("archived mission %r -> %s" % (entry, dest), flush=True)
    return pending


def _start_trash_sweeper():
    """Daemon thread that fires due deletes. Idles at TRASH_IDLE_TICK and tightens to
    TRASH_TICK while anything is queued, so a countdown the operator is watching lands
    within a second of its deadline while an idle box barely stats anything. The loop
    body can never die: a sweep that raises is logged and retried."""
    def loop():
        while True:
            try:
                pending = sweep_trash()
            except Exception as exc:   # a dead sweeper would mean deletes never fire
                print("WARNING: mission trash sweep failed: %s" % exc,
                      file=sys.stderr, flush=True)
                pending = 0
            time.sleep(TRASH_TICK if pending else TRASH_IDLE_TICK)
    threading.Thread(target=loop, daemon=True).start()


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


# ---------------------------------------------------------------------------
# Remote mission docs — read-only SSH viewing
# ---------------------------------------------------------------------------
# A remote mission's docs live in the directory its console actually runs in on
# the remote host, not under ~/missions/<name>/ on the jumpbox. The dashboard is
# a thin, READ-ONLY window onto them: it ssh-reads on the single-mission page and
# ssh-stats for the poll loop, but never writes remotely (editing happens in the
# console). Local missions are unchanged — their docs stay under mission_path().
# These helpers mirror read_text()/os.path.getmtime()'s never-raise, missing=""/0
# contract, so call sites branch purely on location, not on error handling.
SSH_DOC_TIMEOUT = 8   # s; a per-page-load read must fail fast, never hang a request


def _ssh_doc_cmd(host, remote_cmd):
    # BatchMode: a host that would prompt (password/host-key) errors out fast
    # instead of hanging a request thread; ConnectTimeout caps the connect wait so
    # an unreachable host also fails fast rather than sitting on the TCP handshake.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, remote_cmd]


def _remote_doc_path(directory, fn):
    """Join a fixed mission-doc filename onto the remote console dir. `fn` is always
    one of the hardcoded TAB_FILE values (no user input, no traversal). A blank
    `directory` means the console runs in the remote HOME (the launcher's `cd ''`
    is a no-op), so return the bare, home-relative filename — NOT "/<fn>"."""
    d = directory.rstrip("/")
    return (d + "/" + fn) if d else fn


def mission_doc_source(name):
    """(host, dir) when this mission's docs live on a REMOTE host — the dir its
    console runs in, where the markdown docs sit. (None, None) for a local mission,
    whose docs stay under ~/missions/<name>/ as they always have. Derived from
    mission_location() so it never drifts from the launcher/badges."""
    host, directory = mission_location(name)
    if host:
        return host, directory
    return None, None


def ssh_read_text(host, path):
    """Remote `cat` of a doc. A missing file or ANY ssh failure => "" — matching
    read_text()'s FileNotFoundError -> "" contract. Never raises."""
    try:
        r = subprocess.run(
            _ssh_doc_cmd(host, "cat -- " + shlex.quote(path)),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=SSH_DOC_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.decode("utf-8", "replace")


def ssh_stat_mtime(host, path):
    """Remote mtime (epoch float) of a doc; 0.0 if missing/unreachable. GNU
    `stat -c %Y` (the fleet is Linux). Never raises."""
    try:
        r = subprocess.run(
            _ssh_doc_cmd(host, "stat -c %Y -- " + shlex.quote(path) + " 2>/dev/null"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=SSH_DOC_TIMEOUT, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return 0.0


def ssh_stat_mtimes(host, directory, filenames):
    """{fn: mtime} for several docs in `directory` in ONE round-trip — so the 3s
    poll loop costs one ssh call per remote mission, not one per tab. GNU stat
    prints `<path>|<mtime>` per readable file and skips missing ones (2>/dev/null),
    so absent/unreadable files just keep their 0.0 default. Never raises."""
    result = {fn: 0.0 for fn in filenames}
    if not filenames:
        return result
    by_path = {_remote_doc_path(directory, fn): fn for fn in filenames}
    cmd = ("stat -c %n'|'%Y -- "
           + " ".join(shlex.quote(p) for p in by_path) + " 2>/dev/null")
    try:
        r = subprocess.run(
            _ssh_doc_cmd(host, cmd),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=SSH_DOC_TIMEOUT, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return result
    for line in (r.stdout or "").splitlines():
        p, sep, mt = line.rpartition("|")
        if not sep:
            continue
        fn = by_path.get(p)
        if fn is None:
            continue
        try:
            result[fn] = float(mt.strip())
        except ValueError:
            pass
    return result


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


_BLURB_MAX = 160  # chars an index-card blurb is clipped to

# Scaffold placeholder lines (see scaffold()) that must never surface as a blurb —
# they are what made untouched missions all read "Status · Not started. · Objective".
_SCAFFOLD_PLACEHOLDERS = frozenset((
    "_Not started._",
    "_What is this mission trying to achieve?_",
))


def _clip_line(s, limit=_BLURB_MAX):
    """Collapse whitespace and clip to one card-sized line."""
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def first_prompt(name):
    """The operator's FIRST real message to this mission's console — "what this
    session is for", in the operator's own words — read from the OLDEST Claude
    transcript at the console's launch cwd (resumes/relaunches write newer files;
    the oldest holds the opening prompt). "" when there's nothing usable. Local
    consoles only: a remote console keeps its transcripts on the remote host.

    Uses the cheap metadata cwd guess (no tmux//proc per card); the guess IS the
    launch cwd, which is where the original session's transcript lives even if a
    live console has since cd'd elsewhere. Lines that aren't a human prompt are
    skipped: meta/sidechain lines, command wrappers (`<command-name>`/caveat
    blocks, all '<'-prefixed), and compact-resume preambles. A transcript whose
    cwd field disagrees (munge collision, FINDINGS #7) is skipped entirely.

    An ops mission at a chosen local dir shares that cwd (typically $HOME) with
    other missions, so "oldest transcript in the dir" would surface some OTHER
    session's opening prompt. _pinned_session_id() gives those consoles' pinned
    session UUID: read <uuid>.jsonl only, and return "" when that session doesn't
    exist yet (never guess)."""
    try:
        cwd, remote = _console_cwd_guess(name)
    except ValueError:
        return ""
    if remote or not cwd:
        return ""
    pdir = _project_dir_for_cwd(cwd)
    sid = _pinned_session_id(name)
    if sid:
        files = [os.path.join(pdir, sid + ".jsonl")]
        files = [f for f in files if os.path.isfile(f)]
    else:
        # Mission-unique cwd (dev worktree / the mission folder): the oldest
        # session file there is this mission's original conversation.
        try:
            files = sorted(glob.glob(os.path.join(pdir, "*.jsonl")),
                           key=os.path.getmtime)
        except OSError:
            return ""
    target = os.path.realpath(cwd)
    for f in files[:3]:  # oldest first = the original session; cap the scan
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(65536)
        except OSError:
            continue
        for line in head.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("cwd") and os.path.realpath(d["cwd"]) != target:
                break  # another project's transcript landed here — skip the file
            if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
                continue
            msg = d.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                continue
            txt = txt.strip()
            if (not txt or txt.startswith("<")
                    or txt.startswith("Caveat:")
                    or txt.startswith("This session is being continued")):
                continue
            return _clip_line(txt)
    return ""


def dashboard_summary(name, max_lines=3):
    """One-line card blurb. Prefers, in order:
      1. .blurb — a Claude-written summary cached in the mission folder by
         scripts/summarize-missions.py (cron). Local cache, so it works for
         remote missions too — no per-index ssh.
      2. first_prompt() — the operator's opening message to the console.
      3. The first real content lines of DASHBOARD.md (headers, blockquote and
         scaffold placeholders skipped, so an untouched dashboard yields ""
         rather than "Status · Not started. · Objective")."""
    blurb = read_text(mission_path(name, ".blurb")).strip()
    if blurb:
        return _clip_line(blurb.splitlines()[0], limit=200)
    fp = first_prompt(name)
    if fp:
        return fp
    # A remote mission's docs live on the remote host; the index deliberately does
    # NOT ssh per-mission-per-load (latency), and its stale local scaffold would
    # mislead — so skip the excerpt. The card still links through to the live view.
    if mission_doc_source(name)[0]:
        return ""
    txt = read_text(mission_path(name, "DASHBOARD.md"))
    lines = []
    for raw in txt.splitlines():
        s = raw.strip()
        if not s or s.startswith(">") or s.startswith("#"):
            continue
        if s in _SCAFFOLD_PLACEHOLDERS or s == "-":
            continue
        lines.append(_strip_md(s))
        if len(lines) >= max_lines:
            break
    return " · ".join(lines)


def mission_search_text(name, limit=4000):
    """Lowercased plaintext haystack (mission name + where the console runs +
    DASHBOARD.md + HANDOFF.md content) used by the index page's client-side filter
    box. Markdown markers are dropped and whitespace collapsed so the per-card
    data-search attribute stays compact; bounded to `limit` chars so big docs can't
    bloat the index HTML. The location is in the blob because the cards now show it
    (location_line) — typing a host or a path should find the cards displaying it."""
    host, directory = mission_location(name)
    parts = [name, host or "", directory or "",
             read_text(mission_path(name, ".blurb"))]
    # Remote missions: no per-index ssh (see dashboard_summary) — name + blurb only.
    if mission_doc_source(name)[0]:
        return re.sub(r"\s+", " ", " ".join(parts)).strip().lower()[:limit]
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
/* "Where this console runs" readout (see location_line): server + working
   directory, on the mission page header and on every index card. Paths run long,
   so wrap them anywhere rather than let one stretch the card on a phone. */
.loc { margin-top:3px; overflow-wrap:anywhere; }
.loc code { font-size:12px; }
.summary { margin:6px 0 0; color:#374151; }
.badge { display:inline-block; font-size:11px; padding:2px 7px; border-radius:10px;
  border:1px solid var(--line); color:#374151; background:#f3f5f7; }
.badge.ok { background:#e7f4ec; border-color:#bfe0cc; color:#1f6b41; }
.badge.warn { background:#fdf2e3; border-color:#f0d9ad; color:#8a5a12; }
.badge.danger { background:#fdeaea; border-color:#f0c2c2; color:#9b1c1c; }
.badge.ctx { font-variant-numeric:tabular-nums; }
.badge.model { font-weight:600; background:#eef0fb; border-color:#cfd6f5; color:#3b3f8f; }
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
/* Queued delete (the 🗑 button). ONE class on the card drives the whole swap —
   red outline, countdown bar in, the rename/kill/trash buttons out — so the
   server can render an already-pending card (another tab, a reload, a restart)
   in exactly the state the JS puts it in when you click. Declared after
   .running/.merged so a pending delete outranks them. */
.card.trashing { border-color:#b42318; box-shadow:0 0 0 1px #b42318; }
.trashbar { display:none; }
.card.trashing .trashbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  background:#fdecea; border:1px solid #f0c4be; border-radius:6px; padding:6px 10px;
  margin:0 0 10px; font-size:13px; color:#b42318; }
.card.trashing .cardbtns { display:none; }
.card.trashing .meta, .card.trashing .summary { opacity:.5; }
.trashmsg { font-weight:600; flex:0 0 auto; }
.trashnote { color:#8a4b45; font-size:12px; }
.untrashform { margin:0; flex:0 0 auto; }
.undobtn { padding:2px 12px; font-size:13px; }
.cardhead { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
.cardhead h2 { margin:0; }
.badge.live { background:#e8f0fe; border-color:#bcd2fb; color:#1a56db; }
.badge.idle { background:#f3f4f6; border-color:#d8dce2; color:#6b7280; }
.killform, .trashform { margin:0; flex:0 0 auto; }
.killbtn { background:#fff; color:#b42318; border:1px solid #f0c4be; border-radius:6px;
  padding:1px 9px; font-size:15px; line-height:1.5; cursor:pointer; }
.killbtn:hover { background:#fdecea; border-color:#e0a59d; }
.trashbtn { background:#fff; color:#b42318; border:1px solid #f0c4be; border-radius:6px;
  padding:1px 9px; font-size:15px; line-height:1.5; cursor:pointer; }
.trashbtn:hover { background:#fdecea; border-color:#e0a59d; }
.cardbtns { display:flex; gap:6px; align-items:flex-start; flex:0 0 auto; }
.renamebtn { background:#fff; color:#4b5563; border:1px solid var(--line); border-radius:6px;
  padding:1px 9px; font-size:15px; line-height:1.5; cursor:pointer; }
.renamebtn:hover { background:#f3f4f6; border-color:#c7ccd4; }
h1 .renamebtn { font-size:13px; vertical-align:middle; }
/* Touch targets. These two are tapped on a phone, where a ~22px-tall control is a
   coin toss — and a tap that lands a few pixels off is swallowed as a scroll.
   touch-action:manipulation also drops the browser's double-tap-zoom delay. */
.killbtn, .renamebtn, .trashbtn { min-width:40px; min-height:38px; padding:4px 12px;
  font-size:16px; touch-action:manipulation; }
.undobtn { min-height:34px; touch-action:manipulation; }
h1 .renamebtn { min-width:34px; min-height:30px; padding:2px 9px; }
/* The model/context badges arrive from a poll a moment AFTER the page paints. As
   display:none placeholders they made every card below them jump the instant the
   poll landed — which is exactly when a tap on ✕ ends up on the wrong card. Reserve
   the space they will occupy and only flip visibility, so the pre-poll layout is
   the post-poll layout. */
   Only cards that HAVE a console session carry .reserve (the badge can't appear
   without one), so a phone screen isn't padded out with blanks for idle missions. */
.badge.ctx.reserve[hidden], .badge.model.reserve[hidden] {
  display:inline-block; visibility:hidden; }
.badge.ctx.reserve[hidden] { min-width:5.5em; }
.badge.model.reserve[hidden] { min-width:3.5em; }
.files td { padding:6px 10px; border-bottom:1px solid var(--line); font-size:14px; }
.files th { text-align:left; padding:6px 10px; font-size:12px; color:#6b7280; }
.console-region { margin:6px 0 4px; width:min(100vw - 96px, 1600px);
  margin-left:50%; transform:translateX(-50%); }
/* Phones: the console is the page. The 96px desktop gutter costs ~11 terminal
   columns on a 390px screen (the pane was coming out 30 wide), so go full-bleed
   and trim the page padding — every pixel here is a column Claude can draw in. */
@media (max-width: 760px) {
  .wrap { padding:0 10px 60px; }
  .console-region { width:100vw; }
  .console-frame { border-left:0; border-right:0; border-radius:0; }
}
.console-frame { display:block; width:100%; height:55vh; border:1px solid var(--line);
  border-radius:8px; background:#0b0e14; }
/* Drag handle under the console. touch-action:none is what makes it work on a
   phone at all — without it the browser claims the drag as a page scroll and
   cancels the pointer stream. Tall enough (with a visible grab bar) to hit with a
   thumb; the ± buttons on the key bar are the no-drag fallback. */
.console-resizer { height:18px; margin-top:4px; border-radius:6px; cursor:ns-resize;
  background:#eef1f4; touch-action:none; display:flex; align-items:center;
  justify-content:center; }
.console-resizer::after { content:""; width:46px; height:4px; border-radius:2px;
  background:#c3cad3; }
.console-resizer:hover { background:#dfe3e8; }
.console-dragmask { position:fixed; inset:0; z-index:9999; cursor:ns-resize; }
/* Console key bar — the touch-screen stand-in for keys a phone keyboard doesn't
   have (Esc/Tab/arrows), for scrollback, and for select-copy-paste. Buttons are
   sized for a thumb (>=38px) and the rows scroll sideways rather than reflowing
   into an unpredictable grid. */
.keybar { margin:6px 0 2px; display:flex; flex-direction:column; gap:6px; }
.keybar .keyrow { display:flex; gap:6px; align-items:center; overflow-x:auto;
  padding-bottom:2px; -webkit-overflow-scrolling:touch; }
.keybar .key { flex:0 0 auto; min-width:44px; min-height:38px; padding:6px 10px;
  background:#fff; color:#374151; border:1px solid var(--line); border-radius:8px;
  font-size:15px; line-height:1.2; cursor:pointer; touch-action:manipulation;
  -webkit-user-select:none; user-select:none; }
.keybar .key:hover { background:#f3f4f6; }
.keybar .key:active, .keybar .key.hit { background:#e7f0ff; border-color:#9dbcf5; }
.keybar .key.wide { min-width:auto; font-size:13px; }
.keybar .key.warn { color:#b42318; border-color:#f0c4be; }
/* Dictation: aria-pressed is the listening state (set by KEYBAR_JS), so the
   button says the same thing to a screen reader and to the eye. */
.keybar .key[aria-pressed="true"] { color:#b42318; border-color:#f0c4be;
  background:#fdf1ef; animation:keymic 1.4s ease-in-out infinite; }
@keyframes keymic { 50% { background:#f7d7d2; } }
@media (prefers-reduced-motion:reduce) {
  .keybar .key[aria-pressed="true"] { animation:none; }
}
/* 16px keeps iOS from zooming the page when the field takes focus */
.keybar .keytext { flex:1 1 200px; min-width:120px; min-height:38px; font-size:16px;
  padding:7px 10px; border:1px solid var(--line); border-radius:8px; font-family:inherit; }
.keybar .keynote { font-size:12px; color:#6b7280; }
.keybar .grab { display:none; }
.keybar .grab[data-open="1"] { display:block; }
.keybar .grabtext { width:100%; min-height:180px; font:12px/1.45 ui-monospace,SFMono-Regular,
  Menlo,Consolas,monospace; padding:8px; border:1px solid var(--line); border-radius:8px;
  background:#fff; color:#1d2127; white-space:pre; }
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
.modal .fields input[type=text], .modal .fields select { flex:1; min-width:190px; }
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
  // Show only the first LIMIT matching MISSION cards (ad-hoc console cards are never
  // capped — there are only ever a handful). The cap is applied to the cards that
  // already passed the filter, so a search still reaches every mission on the box and
  // the button then offers the rest of the matches. 0 = no cap.
  // "Show N more" reveals one more page of LIMIT; "Show all" drops the cap outright;
  // once nothing is left to reveal the pair collapses back to a single "Show fewer".
  var LIMIT = __INDEX_LIMIT__;
  var limit = LIMIT;              // 0 => uncapped
  var moreBtn = document.getElementById("show-more");
  var allBtn  = document.getElementById("show-all");
  var moreWrap = document.getElementById("show-more-wrap");
  function statusOk(c) {
    if (!sel) return true;                          // nothing selected => all
    var have = (c.getAttribute("data-status") || "").split(/\\s+/);
    return have.indexOf(sel) !== -1;
  }
  function apply() {
    var terms = box.value.toLowerCase().split(/\\s+/).filter(Boolean);
    var shown = 0, matched = 0, hiddenByCap = 0;
    cards.forEach(function(c) {
      var hay = c.getAttribute("data-search") || "";
      var ok = terms.every(function(t) { return hay.indexOf(t) !== -1; }) && statusOk(c);
      if (ok && (c.getAttribute("data-status") || "").indexOf("console") === -1) {
        matched++;
        if (limit && matched > limit) { ok = false; hiddenByCap++; }
      }
      c.hidden = !ok;
      if (ok) shown++;
    });
    if (none) none.hidden = shown !== 0;
    if (moreWrap && moreBtn && allBtn) {
      if (hiddenByCap) {
        moreBtn.textContent = "Show " + Math.min(LIMIT, hiddenByCap) + " more";
        allBtn.textContent = "Show all " + matched;
        moreBtn.hidden = false;
        allBtn.hidden = hiddenByCap <= LIMIT;   // "more" already shows the rest
        moreWrap.hidden = false;
      } else if (LIMIT && matched > LIMIT) {
        moreBtn.textContent = "Show fewer";     // fully revealed => collapse back
        moreBtn.hidden = false;
        allBtn.hidden = true;
        moreWrap.hidden = false;
      } else {
        moreWrap.hidden = true;                 // everything already fits
      }
    }
  }
  if (moreBtn) moreBtn.addEventListener("click", function() {
    // Same button collapses once there is nothing left to reveal (see apply()).
    limit = (limit && limit < matchedCount()) ? limit + LIMIT : LIMIT;
    apply();
    if (limit === LIMIT && moreWrap) moreWrap.scrollIntoView({block: "nearest"});
  });
  if (allBtn) allBtn.addEventListener("click", function() { limit = 0; apply(); });
  // Missions currently passing the filter — how far "show more" can still go.
  function matchedCount() {
    var terms = box.value.toLowerCase().split(/\\s+/).filter(Boolean);
    var n = 0;
    cards.forEach(function(c) {
      if ((c.getAttribute("data-status") || "").indexOf("console") !== -1) return;
      var hay = c.getAttribute("data-search") || "";
      if (terms.every(function(t) { return hay.indexOf(t) !== -1; }) && statusOk(c)) n++;
    });
    return n;
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
      card.querySelectorAll(".badge.live, .badge.idle, .badge.ctx, .badge.model, .killform")
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
  // Seam for TRASH_JS: when a queued delete fires, its card is gone from the DOM
  // and must leave the filter set too — otherwise it keeps counting toward the
  // "Show N more" total and toward the "no cards match" check.
  window.missionFilter = {
    apply: apply,
    forget: function(card) {
      var i = cards.indexOf(card);
      if (i !== -1) cards.splice(i, 1);
      apply();
    }
  };
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


# The 🗑 / Undo countdown on the index. The DEADLINE IS THE SERVER'S — this only
# renders it. /trash and /untrash are the truth (a marker file inside the mission);
# the sweeper thread, not this script, is what actually archives anything, so closing
# the tab still deletes and a second tab shows the same countdown.
#
# Both actions are plain <form>s so the page degrades: with JS off, 🗑 round-trips and
# comes back with the card already counting down, and Undo round-trips back. With JS
# on, the submit is intercepted and the card swaps in place — one class on the card
# (.trashing) drives the whole visual change, which is exactly what render_index()
# emits for a card that was already queued elsewhere.
#
# Seconds-remaining, never an absolute epoch, crosses the wire: the browser clock is
# not the dashboard's. Each card's remaining time is turned into a LOCAL deadline
# (data-trash-due) the moment it is queued or the page loads. One shared ticker walks
# the pending cards — the same "no timer per element" rule the badge polls follow.
TRASH_JS = """
<script>
(function() {
  // Fire a beat AFTER the deadline: the sweeper archives on its own tick, and a card
  // that vanished before the server had actually moved anything would be a lie the
  // next reload contradicts.
  var GRACE_MS = 2000;
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  if (!cards.length) return;
  function arm(card, secs) {
    card.setAttribute("data-trash-due", String(Date.now() + secs * 1000));
    card.classList.add("trashing");
    tick();
  }
  function disarm(card) {
    card.removeAttribute("data-trash-due");
    card.classList.remove("trashing");
  }
  function fire(card) {
    disarm(card);
    // Drop it from the filter set BEFORE removing the node, so the "Show N more"
    // count and the "no cards match" notice stay honest.
    if (window.missionFilter) window.missionFilter.forget(card);
    if (card.parentNode) card.parentNode.removeChild(card);
  }
  function tick() {
    var now = Date.now();
    cards.forEach(function(card) {
      var due = parseInt(card.getAttribute("data-trash-due"), 10);
      if (!due) return;
      if (now >= due + GRACE_MS) { fire(card); return; }
      var left = Math.ceil((due - now) / 1000);
      var msg = card.querySelector(".trashmsg");
      if (msg) msg.textContent = left > 0 ? "Deleting in " + left + "s…" : "Archiving…";
    });
  }
  // Seconds still on the clock for a delete queued elsewhere, rendered server-side.
  cards.forEach(function(card) {
    var left = parseInt(card.getAttribute("data-trash-left"), 10);
    if (left >= 0 && card.classList.contains("trashing")) arm(card, left);
  });
  function post(form, done) {
    var btn = form.querySelector("button");
    if (btn) btn.disabled = true;
    fetch(form.action, { method: "POST", headers: { "X-Requested-With": "fetch" } })
      .then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function(d) { if (btn) btn.disabled = false; done(d); })
      .catch(function() { window.location.reload(); });   // fall back to the round trip
  }
  Array.prototype.slice.call(document.querySelectorAll(".trashform")).forEach(function(f) {
    f.addEventListener("submit", function(e) {
      e.preventDefault();
      var card = f.closest(".card");
      post(f, function(d) { arm(card, d.secs); });
    });
  });
  Array.prototype.slice.call(document.querySelectorAll(".untrashform")).forEach(function(f) {
    f.addEventListener("submit", function(e) {
      e.preventDefault();
      var card = f.closest(".card");
      post(f, function() { disarm(card); });
    });
  });
  // 500ms, not 1000: at one tick per second the displayed number visibly lags the
  // one the operator is counting down in their head.
  setInterval(tick, 500);
  tick();
})();
</script>
"""


# Index-only context badge. For each card with a console session, poll
# /m/<name>/context.json (URL incl. token baked into data-ctx-url server-side) and
# render the live Claude context size as "<tokens> · <pct>%". States (see
# mission_context): ok -> show figure; starting -> "starting"; compacted ->
# "compacted <pre> -> <post> · %" (the compaction's impact; falls back to
# "compacted from <pre>" until the post-compact turn writes its size); remote -> "remote";
# none / fetch error -> stay hidden (never error a card). Two cadences, chosen per badge
# from the state the poll comes back with: a mission with a live console is worth
# watching, one without cannot change until a console starts. Nothing polls at all while
# the tab is hidden.
CTX_JS = """
<script>
(function() {
  var FAST_MS = __CTX_MS__;              // missions with a live console
  var SLOW_MS = __CTX_SLOW_MS__;         // everything else — just enough to notice one starting
  var els = Array.prototype.slice.call(document.querySelectorAll("[data-ctx-url]"));
  if (!els.length) return;
  function fmt(n) {                      // 80460 -> "80k", 1500 -> "1.5k", 950 -> "950"
    if (n >= 1000) {
      var k = n / 1000;
      return (k >= 10 ? Math.round(k) : k.toFixed(1).replace(/\\.0$/, "")) + "k";
    }
    return String(n);
  }
  function modelName(m) {                 // "claude-opus-4-8[1m]" -> "Opus 4.8 1M"
    if (!m) return "";
    var s = String(m);
    var oneM = /\\[1m\\]/i.test(s);
    s = s.replace(/\\[1m\\]/ig, "").replace(/^claude-/, "").replace(/-\\d{8}$/, "");
    var parts = s.split("-");
    var fam = parts.shift() || "";
    fam = fam.charAt(0).toUpperCase() + fam.slice(1);
    var ver = parts.join(".");
    var out = ver ? fam + " " + ver : fam;
    return oneM ? out + " 1M" : out;
  }
  function paintModel(el, model) {        // fills the sibling model badge (left of ctx)
    var mEl = el.parentNode ? el.parentNode.querySelector(".badge.model") : null;
    if (!mEl) return;
    var name = modelName(model);
    if (name) { mEl.textContent = name; mEl.title = "Model: " + model; mEl.hidden = false; }
    else { mEl.hidden = true; }
  }
  function paint(el, d) {
    // Model badge: from d.model (ok) or the post/pre usage (compacted); hidden otherwise.
    paintModel(el, d && (d.model || (d.post && d.post.model) || (d.pre && d.pre.model)));
    el.classList.remove("warn", "danger");   // reset colour, keep .reserve (layout)
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
  // Per-badge cadence. A mission's context number can only move while a console is
  // attached to it, so only those are worth watching closely; an idle, remote or
  // console-less mission will read the same forever. The server doesn't have to tell
  // us which is which — context.json's own state does, so every badge classifies
  // itself from its first reply and picks its cadence from there. That also makes it
  // self-correcting in both directions: start a console and the next slow tick
  // promotes that badge to fast, close one and it demotes itself.
  function cadence(d) {
    if (!d) return SLOW_MS;                        // fetch error / no JSON
    return (d.state === "ok" || d.state === "starting" || d.state === "compacted")
      ? FAST_MS : SLOW_MS;                         // none / remote -> can't change
  }
  function one(el) {
    el.ctxDue = Infinity;                // in flight — don't let a tick double-fire it
    fetch(el.getAttribute("data-ctx-url"))
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) { paint(el, d); el.ctxDue = Date.now() + cadence(d); })
      .catch(function() { el.ctxDue = Date.now() + SLOW_MS; });  // badge as-is, back off
  }
  // The ticker runs at FAST_MS but only fires the badges that are actually due, so the
  // slow ones ride along on the same timer instead of needing one each.
  function poll() {
    var now = Date.now();
    els.forEach(function(el) { if (!el.ctxDue || now >= el.ctxDue) one(el); });
  }
  poll();
  // A hidden tab polls NOTHING. This is the big one: the index fans out one request
  // per mission card (160+ on this box), so a backgrounded index tab was by far the
  // largest source of traffic on the dashboard — and nobody was looking at the answer.
  // Browsers only throttle these timers, they don't stop them. Gating is free because
  // the visibilitychange handler below catches up the moment you come back, so a gated
  // tab is never stale for longer than it takes to actually look at it.
  setInterval(function() { if (!document.hidden) poll(); }, FAST_MS);
  // Coming back also catches up anything that fell due while we were gated or merely
  // throttled — without this a tab foregrounded after a long stint in the background
  // sits showing stale/hidden badges, which reads as "missing until I refresh".
  // poll() honours ctxDue, so a quick flip away and back costs nothing.
  document.addEventListener("visibilitychange", function() { if (!document.hidden) poll(); });
  window.addEventListener("pageshow", function(e) { if (e.persisted) poll(); });
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
  // Hidden tabs don't poll — see the context-badge poller for the reasoning.
  setInterval(function() { if (!document.hidden) poll(); }, USAGE_MS);
  // Same as the context-badge poller: a gated (or merely throttled) background tab
  // would leave the header bars stale/hidden for up to USAGE_MS after it is
  // foregrounded — poll immediately on visibility (and on a bfcache restore) instead.
  document.addEventListener("visibilitychange", function() { if (!document.hidden) poll(); });
  window.addEventListener("pageshow", function(e) { if (e.persisted) poll(); });
})();
</script>
"""


_REPO_CACHE = {"t": 0.0, "repos": []}
_REPO_CACHE_TTL = 60.0


def local_repos():
    """Absolute paths of git repos under REPO_DIRS, at most two levels deep.

    Feeds the Spawn modal's "Local repo" dropdown. Cheap (a couple of stat-ed
    directory listings) and cached for a minute, since the modal is rendered on
    every index load. Worktrees and mission folders are skipped: a dev mission's
    worktree is not a repo you'd start another dev mission on."""
    now = time.time()
    if now - _REPO_CACHE["t"] < _REPO_CACHE_TTL:
        return _REPO_CACHE["repos"]
    skip = {WORKTREES_DIR, MISSIONS_DIR}
    found, seen = [], set()

    def add(d):
        if d in seen or d in skip:
            return False
        if not os.path.exists(os.path.join(d, ".git")):
            return False
        seen.add(d)
        found.append(d)
        return True

    def kids(d):
        try:
            return sorted(
                os.path.join(d, e) for e in os.listdir(d) if not e.startswith(".")
            )
        except OSError:
            return []

    for root in REPO_DIRS:
        add(root)
        for child in kids(root):
            if not os.path.isdir(child) or child in skip:
                continue
            if add(child):
                continue          # a repo: don't descend into its subprojects
            for grand in kids(child):
                if os.path.isdir(grand):
                    add(grand)
    found.sort(key=lambda d: (os.path.basename(d).lower(), d))
    _REPO_CACHE.update(t=now, repos=found)
    return found


def repo_picker():
    """The optional repo <select> for the Spawn modal. Empty string when nothing was
    discovered, so the modal falls back to the plain path field exactly as before."""
    repos = local_repos()
    if not repos:
        return ""
    opts = ['<option value="">pick a repo… (or type a path)</option>']
    for d in repos:
        opts.append(
            f'<option value="{html.escape(d, quote=True)}">'
            f'{html.escape(os.path.basename(d))} — {html.escape(d)}</option>'
        )
    return (
        '<div class=fields data-loc="local-repo">'
        '<select id=spawn-repo aria-label="pick an existing repo">'
        + "".join(opts) + "</select></div>"
    )


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
        f'<form method=post action="{APP_BASE}/spawn{tok}">'
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
        + repo_picker() +
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
        '<div class=seg id=spawn-role>'
        '<label><input type=radio name=role value=feature checked> Feature worker</label>'
        '<label><input type=radio name=role value=integrator> Integrator</label>'
        '</div>'
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
    dev:     'Dev Mission — Feature worker: a git worktree (branch claude/<name>) + worker rails; a fresh repo is git-init\\'d if the path is new. Integrator: the console that fast-forwards finished branches into this repo\\'s staging checkout (local repo only).',
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
    if (mode === 'dev' && val('role') === 'integrator' && kind === 'remote-repo')
      return fail(fld('dir'), 'An integrator mission needs a local repo (remote integrators are not supported yet).');
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
  // Repo dropdown (dev / local repo): picking one just fills the path field, which
  // stays authoritative and free-text — the list is a memory aid, not a constraint.
  var repoSel = document.getElementById('spawn-repo');
  if (repoSel) repoSel.addEventListener('change', function(){
    if (!repoSel.value) return;
    var p = fld('path'); if (p){ p.value = repoSel.value; clearErrs(); }
  });
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


def rename_modal():
    """The shared "Rename mission" dialog — one per page, opened by any .renamebtn
    (each button bakes in its mission's name + POST action + where to return via
    data-* attributes; RENAME_JS copies them into the form). Same .modal markup,
    validation, and open/cancel behavior as the Spawn wizard. POSTs to
    /m/<name>/rename (see do_POST / rename_mission)."""
    return (
        '<div class="modal-overlay" id=rename-modal hidden>'
        '<div class=modal role=dialog aria-modal=true aria-label="Rename mission">'
        '<form method=post action="">'
        '<h2>Rename mission</h2>'
        '<p class=hint id=rename-hint></p>'
        '<div class=fields>'
        '<input type=text name=newname placeholder="new mission name" '
        'pattern="[A-Za-z0-9 ._-]+" title="letters, numbers, spaces, . _ - only">'
        '</div>'
        '<p class=hint>Keeps all mission files and the console conversation. '
        'A running console is stopped first and resumes under the new name on reopen; '
        'a dev mission keeps its existing worktree and branch.</p>'
        '<input type=hidden name=back value=index>'
        '<p class=form-error id=rename-error role=alert hidden></p>'
        '<div class=actions>'
        '<button type=button class="btn secondary" id=rename-cancel>Cancel</button>'
        '<button type=submit class=btn>Rename</button>'
        '</div>'
        '</form></div></div>'
        + RENAME_JS
    )


RENAME_JS = """
<script>
(function() {
  var modal = document.getElementById('rename-modal');
  if (!modal) return;
  var form   = modal.querySelector('form');
  var input  = form.querySelector('input[name=newname]');
  var backF  = form.querySelector('input[name=back]');
  var hint   = document.getElementById('rename-hint');
  var errBox = document.getElementById('rename-error');
  function clearErr() {
    if (errBox) { errBox.hidden = true; errBox.textContent = ''; }
    input.classList.remove('field-error');
  }
  function show(btn) {
    form.action = btn.getAttribute('data-action') || '';
    input.value = btn.getAttribute('data-name') || '';
    backF.value = btn.getAttribute('data-back') || 'index';
    if (hint) hint.textContent = 'Current name: ' + (btn.getAttribute('data-name') || '');
    clearErr();
    modal.hidden = false;
    input.focus(); input.select();
  }
  function hide() { modal.hidden = true; }
  Array.prototype.slice.call(document.querySelectorAll('.renamebtn[data-action]'))
    .forEach(function(b) { b.addEventListener('click', function() { show(b); }); });
  var cancel = document.getElementById('rename-cancel');
  if (cancel) cancel.addEventListener('click', hide);
  modal.addEventListener('click', function(e) { if (e.target === modal) hide(); });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && !modal.hidden) hide();
  });
  // Inline validation mirroring the server's slugging rules (spaces become dashes
  // there): keep the modal open on a bad/empty name instead of a server error page.
  form.addEventListener('submit', function(e) {
    clearErr();
    var v = input.value.trim();
    if (!v || !/^[A-Za-z0-9 ._-]+$/.test(v)) {
      e.preventDefault();
      if (errBox) {
        errBox.textContent = 'Name must use letters, numbers, spaces, . _ - only.';
        errBox.hidden = false;
      }
      input.classList.add('field-error');
      input.focus();
    }
  });
})();
</script>
"""


def page(title, body, active_mission=None):
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
        '<header class=top><div class=wrap>'
        f'<h1><a href="{APP_BASE}/">👩‍✈️ Miss Claude</a></h1>'
        + (f'<span class=sub>{html.escape(LABEL)}</span>' if LABEL else '')
        +
        # Claude subscription plan usage — twin meters on the right of the masthead,
        # filled by USAGE_JS from /usage.json. Hidden until the poll resolves a usable
        # state, so it never shows empty when offline / token stale. The URL (incl.
        # token) is baked in server-side; the JS needs no token handling.
        '<div class=hdr-usage id=plan-usage data-usage-url="'
        + html.escape(bp("/usage.json") + tok_q(), quote=True) + '" hidden>'
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


def keybar_js():
    """KEYBAR_JS with its server-side placeholders filled. Both render sites (the
    remote console page and the mission page) go through here so a new placeholder
    can never be substituted at one and missed at the other."""
    return (KEYBAR_JS
            .replace("__TOK_JS__",
                     json.dumps(f"token={urllib.parse.quote(TOKEN)}" if TOKEN else ""))
            .replace("__BASE_JS__", json.dumps(APP_BASE)))


def tok_q():
    """Token query-string suffix to keep links authenticated, if token is set."""
    return f"?token={urllib.parse.quote(TOKEN)}" if TOKEN else ""


def bp(path=""):
    """Prefix an app-internal absolute path with APP_BASE (the reverse-proxy mount
    point). No-op when APP_BASE is unset. Idempotent — a path already under APP_BASE
    (e.g. a CONSOLE_BASE_URL-derived console link) is returned unchanged, so callers
    never double the prefix."""
    if not APP_BASE or not path.startswith("/") or path.startswith(APP_BASE + "/") or path == APP_BASE:
        return path
    return APP_BASE + path


def _strip_base(path):
    """Inverse of bp() for an incoming request path: drop the APP_BASE mount prefix
    the reverse proxy left on, so route matching sees origin-root paths. No-op when
    APP_BASE is unset or absent from the path."""
    if not APP_BASE:
        return path
    if path == APP_BASE:
        return "/"
    if path.startswith(APP_BASE + "/"):
        return path[len(APP_BASE):]
    return path


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
def render_key_bar(session):
    """Touch controls for the console iframe above it (see console_send()).

    Rendered for any console whose tmux session name the page knows: every mission
    console ("mission-<name>") and every NAMED ad-hoc console (whose name is a
    deterministic hash — see console-launch.sh). An unnamed ad-hoc console gets a
    random session name that is never recorded, so there is nothing to aim at and
    the bar is simply left off.

    Nothing here is mobile-only: the same buttons work with a mouse, and the copy
    box is the only way to select terminal text on any device (xterm.js draws to a
    canvas). Keys are sent server-side via tmux, so the cross-origin ttyd iframe
    never has to be touched."""
    sess = html.escape(session, quote=True)

    def key(action, label, cls="key", title=""):
        t = f' title="{html.escape(title, quote=True)}"' if title else ""
        return (f'<button type=button class="{cls}" data-key="{html.escape(action, quote=True)}"'
                f'{t}>{html.escape(label)}</button>')

    return (
        f'<div class=keybar id=keybar data-session="{sess}">'
        # Row 1 — answer a prompt / move around. Enter accepts the highlighted
        # choice; the digits pick a numbered one directly.
        '<div class=keyrow>'
        + key("esc", "Esc", title="Escape — cancel the current input or mode")
        + key("enter", "⏎ Accept", "key wide", title="Enter — accept the highlighted choice")
        + key("up", "↑") + key("down", "↓") + key("left", "←") + key("right", "→")
        + key("1", "1") + key("2", "2") + key("3", "3")
        + key("tab", "⇥", title="Tab")
        + key("btab", "⇧⇥", title="Shift-Tab — cycle Claude's mode")
        + key("bspace", "⌫", title="Backspace")
        + key("ctrl-c", "^C", "key warn", title="Ctrl-C — interrupt Claude")
        + '</div>'
        # Row 2 — scroll Claude's conversation (PageUp/PageDown) + the copy-out box.
        '<div class=keyrow>'
        + key("pgup", "▲ Scroll up", "key wide")
        + key("pgdn", "▼ Scroll down", "key wide")
        + key("bottom", "⤓ Live", "key wide", title="Back down to the newest replies")
        + '<button type=button class="key wide" id=keybar-grab '
          'title="Copy text out of the terminal">⧉ Copy text</button>'
        # Height controls — the drag grip works on touch now, but a button is a
        # surer thing on a phone. Same stored height as the grip.
        + '<button type=button class=key id=keybar-taller title="Taller console">⤢+</button>'
        + '<button type=button class=key id=keybar-shorter title="Shorter console">⤡−</button>'
        + '<span class=keynote id=keybar-note></span>'
        + '</div>'
        # Row 3 — type/paste into the prompt. "Insert" leaves the text in the
        # prompt so it can still be edited (in the console or by inserting more);
        # "Insert ⏎" submits it.
        '<div class=keyrow>'
        # Dictation. No data-key, so the tmux-key dispatcher below leaves it
        # alone; hidden until the JS confirms the browser can actually do it.
        '<button type=button class=key id=keybar-mic hidden '
        'title="Dictate into the text field" aria-pressed=false>🎤</button>'
        '<input type=text class=keytext id=keybar-text '
        'placeholder="type or paste into the console…" autocapitalize=off '
        'autocorrect=off spellcheck=false>'
        + '<button type=button class="key wide" id=keybar-insert>Insert</button>'
        + '<button type=button class="key wide" id=keybar-send>Insert ⏎</button>'
        + '</div>'
        '<div class=grab id=keybar-grabbox>'
        '<textarea class=grabtext id=keybar-grabtext readonly '
        'aria-label="console text"></textarea>'
        '</div>'
        '</div>'
    )


# Raw string: the dictation fixup table below is full of \b word boundaries, which
# a normal Python string would quietly turn into backspace characters.
KEYBAR_JS = r"""
<script>
(function() {
  var bar = document.getElementById("keybar");
  if (!bar) return;
  var session = bar.getAttribute("data-session");
  var tok     = __TOK_JS__;                 // "token=..." or "" (see tok_q)
  var APP_BASE = __BASE_JS__;               // reverse-proxy mount prefix, or "" (see bp).
                                            // NB: distinct from CTX_JS's own `BASE`
                                            // (the 200k context window) — different IIFE.
  var note    = document.getElementById("keybar-note");
  var textIn  = document.getElementById("keybar-text");
  var grabBox = document.getElementById("keybar-grabbox");
  var grabTxt = document.getElementById("keybar-grabtext");
  var noteTimer = null;

  function say(msg, bad) {
    if (!note) return;
    note.textContent = msg || "";
    note.style.color = bad ? "#b42318" : "#6b7280";
    clearTimeout(noteTimer);
    if (msg) noteTimer = setTimeout(function(){ note.textContent = ""; }, 4000);
  }

  function post(params) {
    params.set("session", session);
    return fetch(APP_BASE + "/console/key" + (tok ? "?" + tok : ""), {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: params.toString()
    }).then(function(r){ return r.json().catch(function(){ return {ok:false, msg:"Bad reply."}; }); })
      .then(function(j){ if (!j.ok) say(j.msg || "Failed.", true); return j; })
      .catch(function(){ say("Dashboard unreachable.", true); return {ok:false}; });
  }

  function sendKey(action) {
    var p = new URLSearchParams();
    p.set("action", action);
    return post(p);
  }

  // Buttons must not steal focus from the terminal (a phone would otherwise pop
  // its keyboard up and down on every tap), hence preventDefault on pointerdown.
  bar.addEventListener("pointerdown", function(e) {
    var b = e.target.closest("button.key");
    if (b) e.preventDefault();
  });
  bar.addEventListener("click", function(e) {
    var b = e.target.closest("button[data-key]");
    if (!b) return;
    b.classList.add("hit");
    setTimeout(function(){ b.classList.remove("hit"); }, 120);
    sendKey(b.getAttribute("data-key"));
  });

  function insert(submit) {
    var v = textIn.value;
    if (!v) { textIn.focus(); return; }
    var p = new URLSearchParams();
    p.set("action", "text");
    p.set("text", v);
    if (submit) p.set("submit", "1");
    post(p).then(function(j){ if (j.ok) textIn.value = ""; });
  }
  document.getElementById("keybar-insert").addEventListener("click", function(){ insert(false); });
  document.getElementById("keybar-send").addEventListener("click", function(){ insert(true); });
  // Enter in the text field = "Insert ⏎" (the common case: answer and submit).
  textIn.addEventListener("keydown", function(e) {
    if (e.key === "Enter") { e.preventDefault(); insert(true); }
  });

  // Console height, in steps — same element, clamp and localStorage key as the drag
  // grip (wireResizer / REMOTE_RESIZER_JS), so the two controls agree.
  (function() {
    var frame = document.getElementById("console-frame");
    if (!frame) return;
    var KEY = "missclaude.consoleH";
    function bump(px) {
      var h = frame.getBoundingClientRect().height + px;
      h = Math.max(160, Math.min(h, 2 * (window.innerHeight - 120)));
      frame.style.height = Math.round(h) + "px";
      localStorage.setItem(KEY, Math.round(h));
    }
    document.getElementById("keybar-taller").addEventListener("click", function(){ bump(90); });
    document.getElementById("keybar-shorter").addEventListener("click", function(){ bump(-90); });
  })();

  document.getElementById("keybar-grab").addEventListener("click", function() {
    if (grabBox.getAttribute("data-open") === "1") {
      grabBox.removeAttribute("data-open");
      return;
    }
    say("reading console…");
    fetch(APP_BASE + "/console/pane.txt?session=" + encodeURIComponent(session) +
          (tok ? "&" + tok : ""))
      .then(function(r){ return r.json(); })
      .then(function(j) {
        if (!j.ok) { say(j.msg || "Could not read the console.", true); return; }
        grabTxt.value = j.text;
        grabBox.setAttribute("data-open", "1");
        grabTxt.scrollTop = grabTxt.scrollHeight;
        say("select and copy — tap ⧉ again to close");
      })
      .catch(function(){ say("Dashboard unreachable.", true); });
  });

  // Dictation. Speech lands in the text field above, never straight in the
  // console: the console runs Claude with --dangerously-skip-permissions, so a
  // mis-transcription has to be readable and editable before Insert is pressed.
  // Recognition is the browser's own (Chrome's Web Speech API) — the server
  // stays stdlib-only and never sees audio. Note Chrome's implementation sends
  // the audio to Google, so treat it like any other cloud service.
  (function() {
    var micBtn = document.getElementById("keybar-mic");
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    // Needs a secure context (i.e. the https origin); plain http has no API at
    // all. Never show a control that cannot work.
    if (!micBtn || !SR || !window.isSecureContext) return;
    micBtn.hidden = false;

    // Ops vocabulary — general-purpose speech recognition mangles this domain.
    // Ordered, longest phrases first, applied to FINALIZED text only (running it
    // on interim results makes the words jump around as you speak). Add a row
    // here when something new comes out wrong; that is the whole maintenance
    // story for this feature.
    var FIXUPS = [
      [/\b(?:dash dash )?f\.?\s*f\.?\s*only\b/gi, "--ff-only"],
      [/\bdash dash\s*/gi, "--"],   // trailing space eaten: "dash dash verbose" -> "--verbose"
      [/\bpseudo\b/gi, "sudo"],
      [/\bsystem control\b/gi, "systemctl"],
      [/\b(?:tea|t)[ -]?mux\b/gi, "tmux"],
      [/\bget (status|commit|log|diff|add|push|pull|rebase|branch|checkout|worktree)\b/gi, "git $1"],
      [/\b(?:ess ess h|s s h|ss h)\b/gi, "ssh"],
      [/\bmiss claude\b/gi, "Miss Claude"],
      // The operator saying an approval phrase IS the approval, and CLAUDE.md
      // wants them exact-uppercase. They still stop in the field for review.
      [/\byes commit\b/gi, "YES COMMIT"],
      [/\byes rebase\b/gi, "YES REBASE"],
      [/\byes integrate\b/gi, "YES INTEGRATE"],
      [/\byes push working\b/gi, "YES PUSH WORKING"],
      [/\byes release\b/gi, "YES RELEASE"],
      [/\byes deploy\b/gi, "YES DEPLOY"]
    ];
    function fixup(s) {
      for (var i = 0; i < FIXUPS.length; i++) s = s.replace(FIXUPS[i][0], FIXUPS[i][1]);
      return s;
    }

    var MAX_TEXT = 80000;             // mirrors MAX_PASTE server-side
    var rec = null, listening = false, restarts = 0, startedAt = 0, committed = "";

    function paint() { micBtn.setAttribute("aria-pressed", listening ? "true" : "false"); }

    function stop(msg, bad) {
      listening = false;
      paint();
      if (rec) { try { rec.stop(); } catch (e) {} }
      if (msg) say(msg, bad);
    }

    function start() {
      // Dictate onto the end of whatever is already typed.
      var have = textIn.value.replace(/\s+$/, "");
      committed = have ? have + " " : "";
      restarts = 0;
      rec = new SR();
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = "en-US";
      rec.maxAlternatives = 1;

      rec.onresult = function(e) {
        var interim = "";
        for (var i = e.resultIndex; i < e.results.length; i++) {
          var t = e.results[i][0].transcript;
          if (e.results[i].isFinal) committed += fixup(t).replace(/^\s+/, "") + " ";
          else interim += t;
        }
        var v = committed + interim;
        if (v.length > MAX_TEXT) { v = v.slice(0, MAX_TEXT); committed = v; }
        textIn.value = v;
      };

      rec.onerror = function(e) {
        var err = e.error || "";
        if (err === "not-allowed" || err === "service-not-allowed") {
          stop("microphone blocked — allow it in the browser, then tap 🎤 again", true);
        } else if (err === "audio-capture") {
          stop("no microphone found", true);
        }
        // "aborted" is our own stop(); "no-speech"/"network" are transient and
        // land in onend, which restarts.
      };

      rec.onend = function() {
        if (!listening) return;       // a deliberate stop
        // Chrome ends the session after ~60s of quiet and on transient network
        // errors — restart while the operator still thinks it is listening. A
        // session that actually ran was a normal cycle; only back-to-back
        // instant ends mean something is really broken.
        if (Date.now() - startedAt > 2000) restarts = 0;
        if (++restarts > 5) { stop("dictation stopped", true); return; }
        startedAt = Date.now();
        try { rec.start(); } catch (e) { stop("dictation stopped", true); }
      };

      startedAt = Date.now();
      try { rec.start(); } catch (e) { say("Could not start dictation.", true); return; }
      listening = true;
      paint();
      say("listening — tap 🎤 again to stop, then Insert");
    }

    micBtn.addEventListener("click", function() {
      if (listening) stop("dictation off"); else start();
    });
    // Esc stops but keeps the text, so a bad sentence can be edited not lost.
    document.addEventListener("keydown", function(e) {
      if (listening && e.key === "Escape") stop("dictation off");
    });
    // Never let the browser's mic indicator outlive the page or a tab switch.
    document.addEventListener("visibilitychange", function() {
      if (document.hidden && listening) stop("dictation off");
    });
    window.addEventListener("beforeunload", function() { if (listening) stop(); });
  })();
})();
</script>
"""


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


def _console_base(host_header):
    """Base URL (no trailing slash/query) the browser uses to reach the ttyd console
    bridge. CONSOLE_BASE_URL wins when set (a same-origin reverse-proxy path); otherwise
    the console is dialed directly at <scheme>://<request-host>:CONSOLE_TTYD_PORT,
    preserving the original single-host behavior. All three console-URL builders route
    through here.

    The scheme follows THIS app's: a browser hard-blocks an http:// iframe inside an
    https:// page (mixed content), so when the dashboard serves TLS the ttyd bridge must
    too — run it with `ttyd --ssl --ssl-cert ... --ssl-key ...` off the same certificate
    (setup.sh and dev-run.sh do). ttyd's client picks ws:// vs wss:// from the scheme it
    was loaded over, so nothing else has to change."""
    if CONSOLE_BASE_URL:
        return CONSOLE_BASE_URL
    host = (host_header or "").rsplit(":", 1)[0] or "localhost"
    return f"{SCHEME}://{host}:{CONSOLE_TTYD_PORT}"


def _remote_console_url(host_header, rhost, rdir, rname=""):
    """ttyd URL for a remote console. ttyd's --url-arg turns each ?arg= into a
    positional arg of console-launch.sh: here `remote <host> <dir> [name]`.
    The name (when set) is passed as a 4th arg so the launcher can key a
    distinct, resumable session off it (blank name = the legacy shared session)."""
    url = (
        f"{_console_base(host_header)}/?arg=remote"
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
    url = (
        f"{_console_base(host_header)}/?arg=local"
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
        f'<p class=meta><a href="{APP_BASE}/{tok_q()}">← missions</a></p>'
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
        f'<form class=inline method=get action="{APP_BASE}/remote">'
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
        # Key bar only for a NAMED remote console: console-launch.sh derives its tmux
        # session name deterministically from host|dir|name (uuidgen --sha1 --namespace
        # @url == uuid5), so the page can name the session to type into. An unnamed
        # console gets a random session name that is never recorded anywhere.
        if rname:
            sess = "remote-" + uuid.uuid5(
                uuid.NAMESPACE_URL, f"{rhost}|{rdir}|{rname}").hex[:12]
            body.append(render_key_bar(sess))
        body.append('</div>')  # /console-region
        body.append(REMOTE_RESIZER_JS)
        if rname:
            body.append(keybar_js())
    title = f"{rname} · Remote console" if rname else "Remote console"
    return page(title, "\n".join(body))
# === end REMOTE CONSOLES ===


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------
def render_index(notice=""):
    missions = list_missions()
    running = running_sessions()   # tmux session exists (drives the ✕ kill button)
    panes, children, comm = _tmux_pane_snapshot()   # shared by claude_sessions() below
    live_set = claude_sessions(panes, children, comm)  # Claude actually running (blue border + badge)
    merged_set = merged_dev_missions()   # dev branches fully merged into working (green border)
    consoles = adhoc_console_sessions(panes, children, comm)   # ad-hoc Console-mode sessions
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
    if missions or consoles:
        # Live client-side filter: matches the typed terms against each card's
        # data-search blob (name + dashboard contents, or console kind/target).
        # No new route — pure JS. The Consoles pill isolates the ad-hoc consoles
        # section below (data-status=console on every console card); the rest of
        # the pills apply to mission cards as before, and also match a console
        # card's live/idle state since those cards carry that token too.
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
            '<button type=button class=pill data-status=console>Consoles</button>'
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
        href = bp(f"/m/{urllib.parse.quote(name)}/dashboard") + tok_q()
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
        # Queued delete (🗑): the card stays listed and fully functional for the
        # countdown — only the .trashing class changes, so an Undo is a no-op revert.
        # Rendered from the on-disk marker, so a reload / a second tab / a dashboard
        # restart all pick the countdown up where it actually is.
        trash_left = 0
        due = trash_due(name)
        if due:
            trash_left = max(0, int(round(due - time.time())))
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
            ' <span class="badge ok" title="This mission&#39;s dev branch is '
            'fully merged into its base branch">merged</span>'
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
            kill_action = bp(f"/m/{urllib.parse.quote(name)}/kill") + tok_q()
            kill_btn = (
                f'<form class=killform method=post action="{kill_action}">'
                '<button class=killbtn type=submit title="Stop session (resumes on reopen)" '
                'aria-label="Stop session (resumes on reopen)">✕</button></form>'
            )
        else:
            kill_btn = ""
        search_blob = html.escape(mission_search_text(name), quote=True)
        # Context badge: placeholder is ALWAYS emitted (not gated on has_session at
        # render time — that made the badge vanish for a card's whole page lifetime
        # whenever the render happened to land before/between session detection, with
        # no way back short of a full reload). mission_context() itself now refuses to
        # report anything but "none" when no session is running, so a dead/never-started
        # console still can't show a stale number; CTX_JS's own poll picks up the state
        # live once a session starts, no reload needed. Empty until the poll resolves a
        # usable state; the token-bearing URL is baked in server-side.
        ctx_url = bp(f"/m/{urllib.parse.quote(name)}/context.json") + tok_q()
        # Model badge sits to the LEFT of the context badge; CTX_JS fills both from
        # the same context.json poll (the model rides in d.model). Wrapped so the JS
        # can find the model sibling from the ctx element via the shared parent.
        # `reserve` (only when a session exists, i.e. a badge is actually coming) makes
        # the placeholders hold their eventual size instead of collapsing, so the poll
        # that fills them can't shuffle the cards under a thumb — see the CSS note.
        res = " reserve" if has_session else ""
        ctx_badge = (
            ' <span class="ctxwrap">'
            f'<span class="badge model{res}" hidden></span> '
            f'<span class="badge ctx{res}" data-ctx-url="{html.escape(ctx_url, quote=True)}" hidden></span>'
            '</span>'
        )
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
        # data-trash-left is SECONDS REMAINING, not an absolute deadline: the browser
        # clock is not the dashboard's, and a skewed one would show a nonsense
        # countdown. TRASH_JS turns it into a local deadline at load.
        if due:
            card_cls += " trashing"
        left_attr = f' data-trash-left="{trash_left}"' if due else ""
        body.append(
            f'<div class="{card_cls}" data-search="{search_blob}" '
            f'data-status="{status_attr}"{left_attr}>'
            + trash_bar(name, trash_left if due else TRASH_DELAY)
            + '<div class=cardhead>'
            f'<h2><a href="{href}">{html.escape(name)}</a></h2>'
            # 🗑 goes LAST, not next to ✎: it is the only one of the three that is
            # more than a session action, and on a phone an edge button is the one a
            # thumb reaches deliberately rather than clips on the way past.
            f'<div class=cardbtns>{rename_button(name, "index")}'
            f'{kill_btn}{trash_button(name)}</div>'
            "</div>"
            f'<div class=meta>updated {time_tag(mtime)}{live}{ctx_badge} &nbsp; {dev_badge(name)}{mb} &nbsp; {hb}</div>'
            # Where this mission's console actually works (server + directory) —
            # the same readout the mission page header carries, so the list answers
            # "which box / which checkout is this one on?" without opening it.
            + location_line(name)
            + (f'<p class=summary>{html.escape(summ)}</p>' if summ else "")
            + "</div>"
        )
    # Cap the visible list at INDEX_LIMIT (FILTER_JS hides the overflow and drives this
    # button). Rendered hidden and only revealed by the JS, so with JS off — or with no
    # overflow — the list behaves exactly as it did before: every mission visible.
    if missions and INDEX_LIMIT:
        body.append(
            '<div class=card id=show-more-wrap hidden '
            'style="display:flex;justify-content:center;gap:8px;flex-wrap:wrap">'
            '<button type=button class="btn secondary" id=show-more hidden></button>'
            '<button type=button class="btn secondary" id=show-all hidden></button>'
            '</div>'
        )
    # The rename dialog must land AFTER the cards for the same parse-time reason as
    # FILTER_JS below: RENAME_JS binds every .renamebtn already in the DOM.
    if missions:
        body.append(rename_modal())
    # render_adhoc_consoles() must land in the HTML BEFORE FILTER_JS: the script's
    # querySelectorAll("[data-search]") runs as the parser reaches it, so any card
    # markup appended after it wouldn't exist in the DOM yet.
    body.append(render_adhoc_consoles(consoles))
    if missions or consoles:
        body.append('<div class=empty id=filter-none hidden>No cards match your filter.</div>')
        body.append(FILTER_JS.replace('__INDEX_LIMIT__', str(INDEX_LIMIT)))
    if missions:
        # After FILTER_JS: it binds the cards already parsed, and TRASH_JS calls into
        # the seam FILTER_JS publishes (window.missionFilter) when a delete fires.
        body.append(TRASH_JS)
        # Live consoles stay snappy; the ~160 idle cards drop to a 5-min heartbeat that
        # exists only to notice a console being started (see the cadence note in CTX_JS).
        body.append(CTX_JS.replace("__CTX_MS__", "15000")
                          .replace("__CTX_SLOW_MS__", "300000"))
    return page("Missions", "\n".join(body))


def render_adhoc_consoles(consoles):
    """'Ad-hoc consoles' section: Console-mode sessions (Spawn wizard -> Console) are
    deliberately stateless (no mission folder — see adhoc_console_sessions()), so unlike
    missions above, this list is derived live from tmux/ps on every render, not from
    disk (`consoles` is the shared snapshot's adhoc_console_sessions() result, computed
    once in render_index() — no extra subprocesses). Reuses the same
    .card/.badge/.killform markup as mission cards so the existing kill JS
    (patchKilledCard, the document-wide .killform fetch handler) needs no changes, and
    carries the same data-search/data-status attributes so the mission filterbar's Live/
    Idle pills and the dedicated Consoles pill (data-status=console) both match these
    cards too. Renders nothing when there are none."""
    if not consoles:
        return ""
    rows = []
    for c in consoles:
        card_cls = "card running" if c["live"] else "card"
        if c["live"]:
            badge = ' <span class="badge live">● live</span>'
        else:
            badge = (
                ' <span class="badge idle" title="Session open but Claude has exited — '
                'reopening this console tab starts/resumes it">○ idle</span>'
            )
        kill_action = bp(f"/console/{urllib.parse.quote(c['name'])}/kill") + tok_q()
        kill_btn = (
            f'<form class=killform method=post action="{kill_action}">'
            '<button class=killbtn type=submit title="End this console" '
            'aria-label="End this console">✕</button></form>'
        )
        status_attr = "console " + ("live" if c["live"] else "idle")
        search_blob = html.escape(f'{c["kind"]} {c["name"]} {c["target"]}'.lower(), quote=True)
        rows.append(
            f'<div class="{card_cls}" data-search="{search_blob}" data-status="{status_attr}" '
            'style="display:flex;align-items:center;'
            'justify-content:space-between;gap:10px">'
            '<div>'
            f'<strong>{html.escape(c["kind"])} console</strong>{badge}'
            f'<div class=meta>{html.escape(c["target"])}</div>'
            '</div>'
            f'{kill_btn}'
            '</div>'
        )
    return (
        '<h2 style="font-size:15px;margin:22px 0 8px">Ad-hoc consoles</h2>'
        '<p class=muted style="font-size:12.5px;margin:0 0 8px">Stateless Console-mode '
        'sessions (Spawn &rarr; Console) — no mission folder, listed here straight from '
        'tmux. Killing one ends it for good; there is nothing to reopen.</p>'
        + "".join(rows)
    )


def render_tabs(name, active):
    # Each tab keeps a real href (full-page route — works with JS disabled) plus
    # a data-tab hook the in-page JS uses to intercept clicks and toggle the
    # "changed" highlight. See the inlined script in render_mission_page.
    items = []
    for key in TAB_KEYS:
        cls = "active" if key == active else ""
        href = bp(f"/m/{urllib.parse.quote(name)}/{key}") + tok_q()
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
    dev = dev_identity(name)
    if dev is not None:
        if dev.get("role") == "integrator":
            repo = dev["repo"]
            base = dev.get("base_branch") or BASE_BRANCH
            iwt = dev.get("integration_worktree") or "?"
            title = ("Integrator console for repo %s — runs in the checkout holding %s (%s)"
                     % (repo, base, iwt))
            label = html.escape(os.path.basename(repo.rstrip("/")) or repo)
            return (f'<span class="badge" title="{html.escape(title)}">'
                    f'{label} · integrator · {html.escape(base)}</span>')
        wt = dev["worktree"]
        # The branch is RECORDED in mission.json (claude/<worktree basename> for older
        # sidecars) — it travels with the worktree, not the mission name.
        branch = dev["branch"]
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
                f'{prefix}dev · {html.escape(branch)}</span>'
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
            f'{prefix}dev · {html.escape(branch)}</span>'
        )
    return (
        '<span class="badge idle" title="No dev worktree — console runs in the '
        'mission folder">ops</span>'
    )


def rename_button(name, back, label="✎"):
    """A .renamebtn that opens the shared rename dialog (see rename_modal). Bakes in
    the mission's name, the POST action (incl. token), and where to land after a
    successful rename (`back`: 'index' or 'dashboard') as data-* attributes."""
    return (
        f'<button class=renamebtn type=button data-name="{html.escape(name, quote=True)}" '
        f'data-action="{bp("/m/" + urllib.parse.quote(name) + "/rename")}{tok_q()}" '
        f'data-back={back} title="Rename mission" aria-label="Rename mission">'
        f'{html.escape(label)}</button>'
    )


def trash_button(name):
    """The 🗑 on an index card: POSTs to /m/<name>/trash, which QUEUES the delete
    (see queue_trash) rather than doing it. A plain form on purpose — TRASH_JS
    intercepts it for the in-place countdown, and with JS off it still works as a
    round-trip that re-renders the index with the card already counting down."""
    action = bp("/m/" + urllib.parse.quote(name) + "/trash") + tok_q()
    title = ("Delete mission — %ds to undo, then its files move to %s/"
             % (TRASH_DELAY, ARCHIVES_DIR))
    return (
        f'<form class=trashform method=post action="{action}">'
        f'<button class=trashbtn type=submit title="{html.escape(title, quote=True)}" '
        'aria-label="Delete mission">🗑</button></form>'
    )


def trash_bar(name, left):
    """The countdown + Undo strip inside a card. ALWAYS rendered (CSS shows it only
    on a .trashing card) so undoing is pure class-toggling on the client and the
    token-bearing Undo URL is baked in server-side, the way every other action on
    this page is. `left` is the seconds still on the clock for a mission that is
    already queued, else the full TRASH_DELAY as the not-yet-used default."""
    action = bp("/m/" + urllib.parse.quote(name) + "/untrash") + tok_q()
    return (
        '<div class=trashbar role=status>'
        f'<span class=trashmsg>Deleting in {left}s…</span>'
        f'<form class=untrashform method=post action="{action}">'
        '<button class="btn secondary undobtn" type=submit>Undo</button></form>'
        f'<span class=trashnote>files move to {html.escape(ARCHIVES_DIR)}/ — '
        'nothing is erased</span>'
        '</div>'
    )


def render_mission_header(name, extra="", ctx=""):
    # `ctx` is the (hidden-until-polled) context badge placeholder — it sits right
    # after the mission name, before the ops/dev pill.
    badge = dev_badge(name)
    loc = location_line(name)
    return (
        f"<h1 style='margin:4px 0 0'>{html.escape(name)} {ctx}{badge} "
        f"{rename_button(name, 'dashboard', '✎ rename')}{extra}</h1>"
        f"{loc}"
    )


def file_tab_inner(name, tab, saved=False):
    """Inner HTML for a markdown tab — no mission header / tab nav / page chrome.
    Shared by the initial page render and the ?fragment=1 endpoint."""
    fn = TAB_FILE[tab]
    host, rdir = mission_doc_source(name)
    if host:
        rpath = _remote_doc_path(rdir, fn)
        content = ssh_read_text(host, rpath)
        mtime = ssh_stat_mtime(host, rpath)
        exists = mtime > 0
    else:
        path = mission_path(name, fn)
        content = read_text(path)
        exists = os.path.isfile(path)
        mtime = os.path.getmtime(path) if exists else 0
    body = []

    if saved:
        body.append('<div class=notice>Saved.</div>')

    body.append(
        f'<div class=meta style="margin-bottom:8px"><strong>{fn}</strong> · '
        f"{('last saved ' + time_tag(mtime)) if exists else 'new file'}"
        + (f' · <span class=muted>live from <code>{html.escape(host)}</code>:'
           f'<code>{html.escape(rdir)}</code></span>' if host else "")
        + "</div>"
    )

    # Log tab: a quick-add box that POSTs to the timestamping append endpoint
    # (stamps a per-entry epoch marker) instead of a raw file edit. Local only —
    # a remote mission's docs are read-only from the dashboard (see below).
    if tab == "log" and not host:
        log_action = bp(f"/m/{urllib.parse.quote(name)}/log/append") + tok_q()
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

    if host:
        # Read-only window onto the remote copy — editing happens in the console,
        # where Claude reads/writes these docs locally (the dashboard never writes
        # over SSH). No edit form / Save button for a remote mission.
        body.append(
            '<p class=meta style="margin-top:16px">Read-only view of the copy on '
            f'<code>{html.escape(host)}</code>. Edit it in the mission console — '
            'a remote mission keeps its docs where Claude runs.</p>'
        )
        return "\n".join(body)

    # edit form (local missions only)
    action = bp(f"/m/{urllib.parse.quote(name)}/{tab}") + tok_q()
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
                    bp(f"/m/{urllib.parse.quote(name)}/raw/"
                       + urllib.parse.quote(rel))
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
    return (
        f"{_console_base(host_header)}/"
        f"?arg={urllib.parse.quote(name)}"
    )


def tab_state(name):
    """Per-tab last-modified time (epoch seconds), for the polling endpoint.
    File tabs use the file mtime; the artifacts tab uses the newest mtime across
    its artifacts/ + scans/ dirs."""
    state = {}
    host, rdir = mission_doc_source(name)
    # Remote docs: one batched ssh stat for all file tabs (not one call per tab per
    # poll). Artifacts stay local for this read-only phase (see file tabs above).
    remote = (ssh_stat_mtimes(host, rdir, [TAB_FILE[k] for k in TAB_KEYS if k != "artifacts"])
              if host else None)
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
        elif remote is not None:
            state[key] = remote.get(TAB_FILE[key], 0.0)
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
  var POLL_MS = 5000;
  var base = %(base_js)s + "/m/" + encodeURIComponent(MISSION) + "/";
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
      // Capture the pointer so the drag keeps tracking even if the finger leaves the
      // (thin) grip, and end cleanly on pointercancel — which is how a touch drag
      // ends when the browser decides it was a scroll after all.
      try { grip.setPointerCapture(e.pointerId); } catch (err) {}
      var startY = e.clientY, startH = frame.getBoundingClientRect().height;
      var mask = document.createElement("div");
      mask.className = "console-dragmask";
      document.body.appendChild(mask);
      function move(ev){ frame.style.height = clamp(startH + (ev.clientY - startY)) + "px"; }
      function up(){
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        document.removeEventListener("pointercancel", up);
        try { grip.releasePointerCapture(e.pointerId); } catch (err) {}
        mask.remove();
        localStorage.setItem(KEY, Math.round(frame.getBoundingClientRect().height));
      }
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
      document.addEventListener("pointercancel", up);
    });
    grip.addEventListener("dblclick", function(){
      frame.style.height = ""; localStorage.removeItem(KEY);   // back to the 55vh default
    });
  }

  wireForm();
  wireResizer();
  poll();
  // Hidden tabs don't poll. The first poll above still runs ungated so `seen` gets its
  // baseline at load — otherwise a mission page opened in a background tab would light
  // up every tab as "changed" the first time you looked at it.
  setInterval(function() { if (!document.hidden) poll(); }, POLL_MS);
  // Unlike the badge pollers this page had no catch-up handler, which gating alone
  // would have turned into a real regression: a doc written by the console while the
  // tab sat hidden would not surface until the next tick after refocus. Poll straight
  // away on visibility (and on a bfcache restore) so returning to the tab is current.
  document.addEventListener("visibilitychange", function() { if (!document.hidden) poll(); });
  window.addEventListener("pageshow", function(e) { if (e.persisted) poll(); });
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
    # Context badge (same placeholder + CTX_JS poller as the index card): ALWAYS
    # emitted now, not gated on session_running() at render time — that gate made the
    # badge vanish for this page's whole lifetime whenever the one-shot render happened
    # to land before/between session detection, with no way back short of a reload.
    # mission_context() itself refuses to report anything but "none" when no session
    # is running, so a dead/never-started console still can't show a stale number;
    # CTX_JS's own poll picks it up live once a session starts. Rendered inside the h1,
    # between the mission name and the ops/dev pill.
    ctx_url = bp(f"/m/{urllib.parse.quote(name)}/context.json") + tok_q()
    ctx_badge = (
        '<span class="ctxwrap"><span class="badge model" hidden></span> '
        f'<span class="badge ctx" data-ctx-url="{html.escape(ctx_url, quote=True)}" hidden></span></span> '
    )
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
    # Touch controls for the console above (Esc/arrows/Enter, scrollback, copy,
    # paste) — the phone's missing keyboard, driven through tmux. See render_key_bar.
    body.append(render_key_bar(SESSION_PREFIX + name))
    body.append("</div>")  # /console-region
    body.append(render_tabs(name, active))
    body.append('<div id=tabcontent>' + tab_inner(name, active) + "</div>")
    body.append(rename_modal())   # opened by the header's ✎ rename button
    body.append(MISSION_JS % {
        "name_js": json.dumps(name),
        "tok_js": json.dumps(f"token={urllib.parse.quote(TOKEN)}" if TOKEN else ""),
        "base_js": json.dumps(APP_BASE),
    })
    # Single badge (no-op if none present), so the cadence costs nothing here either
    # way; the slow tier still matters, because starting a console in this page's own
    # iframe should light the badge up without a reload.
    body.append(CTX_JS.replace("__CTX_MS__", "10000")
                      .replace("__CTX_SLOW_MS__", "60000"))
    body.append(keybar_js())
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

    # Keep-alive. Without this the stdlib answers HTTP/1.0 and hangs up after EVERY
    # response, so each poll tick costs a fresh TCP connect + full TLS handshake. The
    # dashboard polls on timers, and the index fans out one /context.json per mission
    # card — 160+ on this box, in a burst through a 6-socket pool. Chrome caps a host
    # at 6 connections: the pool ends
    # up racing its own reuse against the server's close, and a request that keeps
    # landing on an already-closed socket dies as ERR_TOO_MANY_RETRIES.
    # Every response helper below sends an exact Content-Length, which is what makes
    # persistent connections safe here — if you add a new one, it must do the same or
    # send `Connection: close`.
    protocol_version = "HTTP/1.1"

    # Counterpart to keep-alive: an idle persistent connection otherwise pins its
    # worker thread forever. socketserver applies this to the connection in setup(),
    # and handle_one_request() turns the resulting timeout into a clean close.
    # Must stay clear of the slowest thing that still counts as a live tab, otherwise
    # we recreate the race we just fixed: /usage.json ticks at 60s, and a backgrounded
    # tab is throttled by Chrome to ~1 tick/min, so anything near 60s would close the
    # socket right as the next poll reuses it. 120s leaves a full tick of headroom.
    timeout = 120

    # ---- helpers ----------------------------------------------------------
    # Whether THIS request proved it holds the token. Class-level default is False so
    # every path is fail-closed: an unauthenticated response must never carry the token,
    # in the body or as a Set-Cookie. Set per request by _authed(); the handler instance
    # is reused across keep-alive requests, so it is assigned on every call, not once.
    _req_authed = False

    def _authed(self, qs):
        self._req_authed = self._check_auth(qs)
        return self._req_authed

    def _check_auth(self, qs):
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
        # Refresh the cookie only for a caller that ALREADY authenticated. Sending it
        # unconditionally meant the 401 response itself handed out a working credential:
        # one unauthenticated request and the browser was logged in.
        if TOKEN and self._req_authed:
            self.send_header("Set-Cookie", f"mt={TOKEN}; Path={APP_BASE}/; HttpOnly; SameSite=Strict")
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
        # bp() prefixes app-internal targets with APP_BASE and leaves already-prefixed
        # console URLs (CONSOLE_BASE_URL-derived) untouched, so both kinds redirect right.
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", bp(location))
        # Bodyless, but still needs the length: under HTTP/1.1 keep-alive a response
        # with no Content-Length has no framing, so the client would sit waiting for a
        # body that never comes instead of moving on to the redirect.
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _error(self, status, msg):
        card = (f'<div class=card><h2>{status.value} {status.phrase}</h2>'
                f'<p class=muted>{html.escape(msg)}</p>')
        if not self._req_authed:
            # Unauthenticated: emit a bare page. Neither the "← home" link (tok_q()) nor
            # page()'s own masthead (which bakes tok_q() into data-usage-url) may render,
            # or the 401 body itself becomes the credential it is refusing to accept.
            return self._send_html(
                '<!doctype html><meta charset=utf-8><title>Error</title>'
                f'<style>{STYLE}</style>{card}</div>', status)
        self._send_html(page("Error", card
                             + f'<p><a href="{APP_BASE}/{tok_q()}">← home</a></p></div>'), status)

    # ---- GET --------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = _strip_base(parsed.path)

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

        # Console text for the key bar's "Copy text" box — the only way to select
        # text out of the canvas-drawn terminal (see console_capture).
        if path == "/console/pane.txt":
            sess = qs.get("session", [""])[0]
            try:
                lines = int(qs.get("lines", ["120"])[0])
            except ValueError:
                lines = 120
            text = console_capture(sess, lines)
            if text is None:
                return self._send_json({"ok": False, "msg": "That console is not running."},
                                       HTTPStatus.NOT_FOUND)
            return self._send_json({"ok": True, "text": text})

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
        path = _strip_base(parsed.path)

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
                    "dev": dict(dev_meta(PRIMARY_REPO, BASE_BRANCH, worktree=wt),
                                preview_port=preview_port_for(name)),
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
            # Dev role: a FEATURE worker (default: its own worktree on claude/<name>) or
            # the repo's INTEGRATOR (runs in the checkout that holds the integration
            # branch; no feature worktree). Recorded in mission.json so the console
            # launcher — not the operator's shell history — decides which wrapper runs.
            role = (form.get("role", ["feature"])[0]).strip() or "feature"
            if role not in ("feature", "integrator"):
                return self._error(HTTPStatus.BAD_REQUEST, "Unknown dev role.")
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
            # mission behind. dmeta is the mission.json "dev" block (None for ops).
            dmeta = None
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
                if role == "integrator":
                    # No feature worktree: the integrator lives in the ONE checkout
                    # that holds the integration branch (found, or created under
                    # WORKTREES_DIR/.integration when the branch is checked out
                    # nowhere). Resolved now and recorded, so it never depends on
                    # which branch the operator's checkout happens to be on later.
                    if repo_root_of(rp) is None:
                        return self._send_html(render_index(
                            f'Could not create integrator mission "{name}": {rp} '
                            "is not a git repository."))
                    iwt, err = ensure_integration_worktree(rp, base)
                    if err:
                        return self._send_html(render_index(
                            f'Could not create integrator mission "{name}": {err}'))
                    dmeta = dev_meta(rp, base, role="integrator",
                                     integration_worktree=iwt)
                else:
                    err = create_worktree(name, rp, base)
                    if err:
                        return self._send_html(render_index(
                            f'Could not create dev mission "{name}": {err}'))
                    dmeta = dev_meta(rp, base, worktree=os.path.join(WORKTREES_DIR, name))
                    dmeta["preview_port"] = preview_port_for(name)
            elif kind == "remote-repo":
                if not (REMOTE_HOST_RE.match(rhost) and REMOTE_DIR_RE.match(rdir)):
                    return self._send_html(render_index("Invalid remote host or repo path."))
                if role == "integrator":
                    return self._send_html(render_index(
                        "An integrator mission on a remote repo is not supported yet — "
                        "run the integrator on that host, or use a local repo."))
                target = {"kind": "remote-repo", "host": rhost, "remote_dir": rdir}
                # A blank base is detected ON the remote; the resolved name comes back
                # so mission.json records the real branch, not a placeholder.
                wt, base, err = create_remote_worktree(name, rhost, rdir, base)
                if err:
                    return self._send_html(render_index(
                        f'Could not create remote dev mission "{name}": {err}'))
                dmeta = dev_meta(rdir, base, worktree=wt, host=rhost)
                dmeta["preview_port"] = preview_port_for(name)
            else:
                return self._error(HTTPStatus.BAD_REQUEST, "Unknown target kind.")

            meta = {"mode": mode, "target": target}
            if dmeta is not None:
                meta["dev"] = dmeta

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

        # rename a mission (keeps its files + console conversation; see
        # rename_mission). Like /kill, this must precede the tab-save match
        # below — "rename" matches its [a-z]+ group.
        mr = re.match(r"^/m/([^/]+)/rename$", path)
        if mr:
            old = urllib.parse.unquote(mr.group(1))
            if not safe_name(old) or not os.path.isdir(mission_path(old)):
                return self._error(HTTPStatus.NOT_FOUND, "No such mission.")
            new, msg = rename_mission(old, form.get("newname", [""])[0])
            # From the mission page: land on the renamed mission's dashboard (its
            # console iframe relaunches under the new name and resumes). From the
            # index — and on any error — re-render the index with the outcome.
            if new is not None and form.get("back", [""])[0] == "dashboard":
                return self._redirect(f"/m/{urllib.parse.quote(new)}/dashboard" + tok_q())
            return self._send_html(render_index(msg))

        # Queue a mission's delete (the 🗑 button). Nothing is moved here — the
        # mission is marked and the sweeper archives it TRASH_DELAY seconds later
        # unless /untrash lands first. Like /kill and /rename, this must precede the
        # tab-save match below: "trash" matches its [a-z]+ group.
        mt = re.match(r"^/m/([^/]+)/trash$", path)
        if mt:
            name = urllib.parse.unquote(mt.group(1))
            if not safe_name(name) or not os.path.isdir(mission_path(name)):
                return self._error(HTTPStatus.NOT_FOUND, "No such mission.")
            secs, err = queue_trash(name)
            if self.headers.get("X-Requested-With") == "fetch":
                if err:
                    return self._send_json({"queued": False, "error": err},
                                           HTTPStatus.INTERNAL_SERVER_ERROR)
                return self._send_json({"queued": True, "secs": secs})
            if err:
                return self._send_html(render_index(err))
            return self._send_html(render_index(
                f'Deleting "{name}" in {secs}s — press Undo on its card to keep it. '
                f"Its files move to {ARCHIVES_DIR}/ (nothing is erased)."))

        # Undo a queued delete. Cheap by design: queuing changed nothing but a marker,
        # so cancelling is just removing it.
        mu = re.match(r"^/m/([^/]+)/untrash$", path)
        if mu:
            name = urllib.parse.unquote(mu.group(1))
            if not safe_name(name) or not os.path.isdir(mission_path(name)):
                return self._error(HTTPStatus.NOT_FOUND, "No such mission.")
            ok, msg = cancel_trash(name)
            if self.headers.get("X-Requested-With") == "fetch":
                return self._send_json({"cancelled": bool(ok), "msg": msg})
            return self._send_html(render_index(msg))

        # Key bar -> console: one key, one scroll step, or a chunk of text, delivered
        # to the console's tmux pane (see console_send). Always JSON — the bar is a
        # fetch-driven widget, never a form post.
        if path == "/console/key":
            ok, msg = console_send(
                form.get("session", [""])[0],
                form.get("action", [""])[0],
                form.get("text", [""])[0],
                form.get("submit", [""])[0] == "1",
            )
            return self._send_json({"ok": ok, "msg": msg},
                                   HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)

        # kill an ad-hoc Console-mode session (see adhoc_console_sessions()). Unlike a
        # mission kill, there is no folder/state behind this — ending it is final, the
        # name (a console-launch.sh hash) simply stops matching any tmux session.
        ck = re.match(r"^/console/([^/]+)/kill$", path)
        if ck:
            name = urllib.parse.unquote(ck.group(1))
            if not ADHOC_SESSION_RE.match(name):
                return self._error(HTTPStatus.NOT_FOUND, "No such console.")
            killed = _kill_tmux_session(name)
            if self.headers.get("X-Requested-With") == "fetch":
                return self._send_json({"killed": bool(killed)})
            note = f'Ended console "{name}".' if killed else f'No running console "{name}".'
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
            if mission_doc_source(name)[0]:
                return self._error(HTTPStatus.FORBIDDEN,
                    "Remote mission docs are read-only in the dashboard — log via the console.")
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
            if mission_doc_source(name)[0]:
                return self._error(HTTPStatus.FORBIDDEN,
                    "Remote mission docs are read-only in the dashboard — edit them in the console.")
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


class RedirectHandler(BaseHTTPRequestHandler):
    """Bounces plain http:// callers to the https:// dashboard (see REDIRECT_PORT).

    Only reachable when TLS is on. It answers every method the same way — a 301 to the
    same path on the TLS port — because there is nothing here to serve; the point is that
    an old bookmark lands on a working page instead of a TLS-handshake error. Note this
    can only rescue callers of the REDIRECT port: once PORT speaks TLS, an http:// request
    to PORT itself is unintelligible to the server and cannot be redirected."""

    protocol_version = "HTTP/1.1"
    server_version = "MissionDashboard-redirect"

    def _bounce(self):
        host = (self.headers.get("Host") or HOST).rsplit(":", 1)[0] or "localhost"
        port = "" if PORT == 443 else f":{PORT}"
        loc = f"https://{host}{port}{self.path}"
        body = (f'<!doctype html><meta charset=utf-8><title>Moved</title>'
                f'<p>This dashboard now speaks HTTPS: '
                f'<a href="{html.escape(loc, quote=True)}">{html.escape(loc)}</a>').encode()
        self.send_response(HTTPStatus.MOVED_PERMANENTLY)
        self.send_header("Location", loc)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _bounce

    def log_message(self, fmt, *args):
        sys.stderr.write("redirect %s - %s\n" % (self.address_string(), fmt % args))


def _start_redirect_listener():
    """Serve RedirectHandler on REDIRECT_PORT in a daemon thread. Best effort: a busy
    port is a warning, never a reason to fail the dashboard itself."""
    try:
        class _R(ThreadingHTTPServer):
            daemon_threads = True
        rd = _R((HOST, REDIRECT_PORT), RedirectHandler)
    except OSError as exc:
        print(f"WARNING: http->https redirect listener not started on {HOST}:"
              f"{REDIRECT_PORT} ({exc}); http:// callers get a protocol error instead "
              "of a redirect.", file=sys.stderr, flush=True)
        return
    threading.Thread(target=rd.serve_forever, daemon=True).start()
    print(f"http->https redirects on http://{HOST}:{REDIRECT_PORT}", flush=True)


def main():
    os.makedirs(MISSIONS_DIR, exist_ok=True)
    # Drop standing mission orientation where every ops console auto-loads it.
    # Write-if-absent so operator hand-edits to the live file are never clobbered.
    claude_md = os.path.join(MISSIONS_DIR, "CLAUDE.md")
    if not os.path.exists(claude_md):
        write_text_atomic(claude_md, MISSIONS_CLAUDE_MD)
    # Publish how we are ACTUALLY reachable, for the mission-doc hooks. They are
    # standalone processes that can't import this module, and the alternative —
    # guessing from whether a certificate happens to sit in ~/.miss-claude/tls — is
    # wrong in both directions: certs outlive a switch back to http, and a hand-wired
    # TLS install may keep them elsewhere. A wrong guess hands the model a curl the
    # dashboard refuses, and log appends fail silently. Rewritten every start (NOT
    # write-if-absent) so it tracks the running config, and dot-prefixed to stay out
    # of the doc tabs.
    # Best effort: this is a convenience for the hooks, never a reason to refuse to
    # start. Without it they fall back to the exported env, then to plain http.
    try:
        write_text_atomic(
            os.path.join(MISSIONS_DIR, ".dashboard-url"),
            json.dumps({"base": SELF_URL, "ca": TLS_CA if TLS else ""}) + "\n",
        )
    except OSError as exc:
        print(f"WARNING: could not write {MISSIONS_DIR}/.dashboard-url ({exc}); "
              "mission-doc hooks will fall back to $MISSION_SELF_URL or plain http.",
              file=sys.stderr, flush=True)
    # Default stdlib listen backlog is 5, which overflows under a burst of
    # concurrent browser connections (kernel logs "possible SYN flooding on
    # :4200" + drops/slows connects). Raise it well under net.core.somaxconn.
    class _Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True
        # Set to an ssl.SSLContext by the TLS block below; None means plain http and
        # every TLS branch here is skipped, so the no-TLS path is untouched.
        tls_ctx = None

        def get_request(self):
            """accept() + a handshake deadline. The deadline is what stops a silent
            client from parking on the connection forever; it is cleared again once
            the handshake is done so normal request I/O keeps its old blocking
            behavior."""
            sock, addr = self.socket.accept()
            if self.tls_ctx is not None:
                sock.settimeout(TLS_HANDSHAKE_TIMEOUT)
            return sock, addr

        def process_request_thread(self, request, client_address):
            """Same contract as ThreadingMixIn's, with the TLS handshake moved in here
            so it runs on the worker thread instead of the accept loop."""
            if self.tls_ctx is not None:
                try:
                    request = self.tls_ctx.wrap_socket(request, server_side=True)
                except (OSError, ssl.SSLError):
                    # Plaintext client, scanner, or a handshake that hit the deadline.
                    # Not worth a log line — this is what the internet does to an open
                    # port. wrap_socket detaches the raw socket only on SUCCESS, so on
                    # failure `request` is still the real one and still needs closing.
                    try:
                        request.close()
                    except OSError:
                        pass
                    return
                # Handshake done: drop the deadline so a slow-but-live client isn't cut
                # off mid-request (restores the pre-TLS blocking semantics).
                request.settimeout(None)
            try:
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)
    httpd = _Server((HOST, PORT), Handler)
    if TLS:
        # Fail LOUD and early on a bad cert/key: a dashboard that silently fell back to
        # plaintext would be worse than one that didn't start (the operator would think
        # it was encrypted). load_cert_chain raises on a missing file or mismatched key.
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        except (OSError, ssl.SSLError) as exc:
            sys.exit(f"FATAL: cannot load TLS cert/key ({TLS_CERT} / {TLS_KEY}): {exc}\n"
                     "Generate them with: bash scripts/make-certs.sh")
        # Handshake PER CONNECTION, in the worker thread — never on the listening
        # socket. Wrapping the listener would put the handshake inside accept(), and
        # accept() is the one place the whole server serializes: a single client that
        # opens a TCP connection and then sends nothing stalls every other request for
        # as long as it holds the socket, because nothing is dispatched to a thread
        # until accept() returns. A browser preconnect, a port scan or a TCP health
        # check is enough to do it. So the listener stays plain, get_request() arms a
        # handshake deadline, and the wrap happens in process_request_thread below.
        httpd.tls_ctx = ctx
        if REDIRECT_PORT:
            _start_redirect_listener()
    # Fires deletes queued by the 🗑 button, including any left queued across a
    # restart (their deadline has passed, so the first sweep files them away).
    _start_trash_sweeper()
    if not _ttyd_listening():
        print(f"WARNING: nothing listening on 127.0.0.1:{CONSOLE_TTYD_PORT} — "
              "the Claude console bridge (claude-console.service / ttyd) isn't up; "
              "mission Console iframes will fail until it is started.",
              file=sys.stderr, flush=True)
    auth = "token required" if TOKEN else "no app auth (firewall-restricted)"
    tls = f"TLS {os.path.basename(TLS_CERT)}" if TLS else "no TLS"
    print(f"Mission Dashboard listening on {SCHEME}://{HOST}:{PORT}  "
          f"missions={MISSIONS_DIR}  [{auth}; {tls}]", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
