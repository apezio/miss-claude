#!/usr/bin/env python3
"""mission-env.py — turn a mission's mission.json into the MISS_* environment.

    eval "$(python3 scripts/mission-env.py ~/missions/<name>/mission.json)"

Prints one shell-quoted `KEY=value` assignment per line (safe to eval: every value
goes through shlex.quote). This is the ONE place the console launcher reads a
mission's identity from, so what console-launch.sh exports into the tmux pane, what
the guard hook and role-context hook see inside Claude, and what app.py recorded at
spawn are the same facts. Nothing here consults the process cwd or a default repo:
a field that is not in the file comes out empty, and the launcher falls back to its
legacy inference only when the whole file is absent.

Keys (all always printed, empty when unknown):
  MISS_MODE                 ops | dev | console | ""   (unknown strings => ops)
  MISS_TARGET_KIND          local-dir | remote | local-repo | remote-repo
  MISS_TARGET_PATH          local dir/repo path
  MISS_TARGET_HOST          ssh host (remote targets)
  MISS_TARGET_REMOTE_DIR    remote dir / repo path
  MISS_ROLE                 feature | integrator          (dev only)
  MISS_REPO_ROOT            main checkout of the repo     (dev)
  MISS_REPO_ID              stable repo id                (dev)
  MISS_WORKTREE             the feature worktree          (dev/feature)
  MISS_FEATURE_BRANCH       claude/<slug>                 (dev/feature)
  MISS_INTEGRATION_BRANCH   the branch features integrate into (dev)
  MISS_INTEGRATION_WORKTREE checkout holding that branch  (dev, when known)
  MISS_PREVIEW_PORT         per-mission dev-server port   (dev/feature, when recorded)
  MISS_SESSION_ID           pinned resume uuid            (renamed missions)
  MISS_AGENT                codex | claude | ""           (which CLI the console runs;
                            empty/absent = claude, like every pre-toggle sidecar)

A malformed file yields all-empty values (exit 0) — the launcher then treats the
mission as legacy, exactly as before. Standard library only.
"""

import json
import os
import re
import shlex
import sys

KEYS = [
    "MISS_MODE", "MISS_TARGET_KIND", "MISS_TARGET_PATH", "MISS_TARGET_HOST",
    "MISS_TARGET_REMOTE_DIR", "MISS_ROLE", "MISS_REPO_ROOT", "MISS_REPO_ID",
    "MISS_WORKTREE", "MISS_FEATURE_BRANCH", "MISS_INTEGRATION_BRANCH",
    "MISS_INTEGRATION_WORKTREE", "MISS_PREVIEW_PORT", "MISS_SESSION_ID",
    "MISS_AGENT",
]

# Sanity gates (clean failure, not the security boundary — values are shell-quoted).
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}\Z")
PATH_RE = re.compile(r"^/[A-Za-z0-9 ._/@:+-]{0,255}\Z")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z")
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}\Z")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def _s(v, rx=None):
    if not isinstance(v, str):
        return ""
    v = v.replace("\n", " ").replace("\t", " ")
    if rx is not None and not rx.match(v):
        return ""
    return v


def mission_env(meta):
    """Dict of the KEYS above for a parsed mission.json (dict), all strings."""
    env = {k: "" for k in KEYS}
    if not isinstance(meta, dict):
        return env
    t = meta.get("target") if isinstance(meta.get("target"), dict) else {}
    d = meta.get("dev") if isinstance(meta.get("dev"), dict) else {}
    mode = meta.get("mode")
    if isinstance(mode, str) and mode and mode not in ("ops", "dev", "console"):
        mode = "ops"    # mirrors app.py mission_target(): unknown mode => ops
    env["MISS_MODE"] = _s(mode)
    env["MISS_TARGET_KIND"] = _s(t.get("kind"))
    env["MISS_TARGET_PATH"] = _s(t.get("path"))
    env["MISS_TARGET_HOST"] = _s(t.get("host"), HOST_RE)
    env["MISS_TARGET_REMOTE_DIR"] = _s(t.get("remote_dir"))
    env["MISS_SESSION_ID"] = _s(meta.get("session_id"), UUID_RE)
    agent = _s(meta.get("agent"))
    env["MISS_AGENT"] = agent if agent in ("claude", "codex") else ""
    if env["MISS_MODE"] == "dev":
        role = _s(d.get("role"))
        env["MISS_ROLE"] = role if role in ("feature", "integrator") else "feature"
        env["MISS_REPO_ROOT"] = _s(d.get("repo"), PATH_RE)
        env["MISS_REPO_ID"] = _s(d.get("repo_id"), ID_RE)
        env["MISS_INTEGRATION_BRANCH"] = _s(d.get("base_branch"), BRANCH_RE)
        env["MISS_INTEGRATION_WORKTREE"] = _s(d.get("integration_worktree"), PATH_RE)
        if env["MISS_ROLE"] == "feature":
            wt = _s(d.get("worktree"), PATH_RE)
            env["MISS_WORKTREE"] = wt
            branch = _s(d.get("branch"), BRANCH_RE)
            if not branch and wt:
                branch = "claude/" + os.path.basename(wt.rstrip("/"))
            env["MISS_FEATURE_BRANCH"] = branch
            port = d.get("preview_port")
            if isinstance(port, int) and 1024 <= port <= 65535:
                env["MISS_PREVIEW_PORT"] = str(port)
        host = _s(d.get("host"), HOST_RE)
        if host and not env["MISS_TARGET_HOST"]:
            env["MISS_TARGET_HOST"] = host
    return env


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: mission-env.py <mission.json>\n")
        return 2
    try:
        with open(argv[1], encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        meta = None
    for k, v in mission_env(meta).items():
        print("%s=%s" % (k, shlex.quote(v)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
