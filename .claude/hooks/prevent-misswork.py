#!/usr/bin/env python3
"""
Claude Code PreToolUse guard for the Miss Claude multi-session worktree workflow.

For the Miss Claude / Mission Dashboard code (a flat, stdlib-only Python project,
no app/lib tree).

Two roles, carried in via the CLAUDE_MISS_ROLE env var the launch wrappers export
(scripts/claude-miss -> "feature", scripts/claude-miss-integrator ->
"integrator"). The hook subprocess inherits that env from the Claude process, so
it can tell the roles apart.

This hook is the ONLY hard guardrail, because the wrappers launch Claude with
--dangerously-skip-permissions (the permission allowlist is bypassed). It
hard-BLOCKS the actions a role must never take. The approval phrases
(YES COMMIT / YES REBASE / YES INTEGRATE / YES PUSH WORKING / YES RELEASE /
YES DEPLOY) cannot be enforced here — the hook can't read the chat — so those
stay behavioural, enforced by CLAUDE.md.

Policy summary:
  * On main/master (any role): the full strict blocklist.
  * Feature worker (role=feature, or unset/unknown -> restrictive default):
      blocks push / merge / deploy(systemctl) / worktree ops / branch
      delete-rename / switching away from its branch / sudo, and blocks edits to
      OTHER worktrees or the primary checkout. Everyday work (edits in its own
      worktree, git add/commit/rebase, running app.py / tests, /tmp writes) stays
      allowed.
  * Integrator (role=integrator): blocks editing application code (*.py, *.sh,
      *.service, scripts/), force-push, non-fast-forward merge, and rebase.
      Fast-forward merge, pushing working, moving master, and deploy
      (sudo systemctl restart) are allowed here (gated behaviourally by the
      YES ... phrases).

Exit codes:
  0 -> allow
  2 -> block (message on stderr is shown to Claude)
"""

import json
import os
import re
import subprocess
import sys


PROTECTED_BRANCHES = {"main", "master"}

WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Repo areas the feature-worker write guard cares about. Writes here that fall
# OUTSIDE the session's own worktree are blocked (sibling worktrees and the
# primary checkout). Writes elsewhere (e.g. /tmp) are left alone.
# Defaults resolve under the running user's home; override with MISSION_PRIMARY_REPO /
# MISSION_WORKTREES_DIR if your checkout lives elsewhere.
GUARDED_REPO_ROOTS = (
    os.environ.get("MISSION_PRIMARY_REPO", os.path.expanduser("~/mission-dashboard")),
    os.environ.get("MISSION_WORKTREES_DIR", os.path.expanduser("~/missclaude-worktrees")),
)

# Application-code the integrator must not edit (it does not write feature code).
# This project is flat: source is *.py / *.sh / *.service at the root plus a
# scripts/ dir. docs/, .claude/, and *.md are intentionally NOT blocked, so
# workflow/doc edits stay allowed for the integrator.
INTEGRATOR_BLOCKED_PREFIXES = ("scripts/",)
INTEGRATOR_BLOCKED_SUFFIXES = (".py", ".sh", ".service")


# ---- main/master: full strict blocklist -------------------------------------
MASTER_BASH_PATTERNS = [
    (re.compile(r"\bgit\s+commit\b"), "git commit"),
    (re.compile(r"\bgit\s+push\b"), "git push"),
    (re.compile(r"\bgit\s+add\b"), "git add"),
    (re.compile(r"\bgit\s+reset\b"), "git reset"),
    (re.compile(r"\bgit\s+rebase\b"), "git rebase"),
    (re.compile(r"\bgit\s+merge\b"), "git merge"),
    (re.compile(r"\bgit\s+cherry-pick\b"), "git cherry-pick"),
    (re.compile(r"\bgit\s+revert\b"), "git revert"),
    (re.compile(r"\bgit\s+stash\b"), "git stash"),
    (re.compile(r"\bgit\s+clean\b"), "git clean"),
    (re.compile(r"\bgit\s+restore\b"), "git restore"),
    (re.compile(r"\bgit\s+checkout\b(?!\s+-b\b)"), "git checkout (non -b)"),
    (re.compile(r"\bgit\s+switch\b(?!\s+-c\b)"), "git switch (non -c)"),
    (re.compile(r"\bpip\d?\s+(install|uninstall)\b"), "pip package change"),
    (re.compile(r"(^|[\s;&|])rm(\s|$)"), "rm"),
    (re.compile(r"(^|[\s;&|])mv(\s|$)"), "mv"),
    (re.compile(r"(^|[\s;&|])cp(\s|$)"), "cp"),
    (re.compile(r"\bchmod\b"), "chmod"),
    (re.compile(r"\bchown\b"), "chown"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r">>"), "redirect append (>>)"),
    (re.compile(r"(?:^|[^0-9&<>])>(?!&|>)"), "redirect write (>)"),
    (re.compile(r"\btee\s"), "tee"),
    (re.compile(r"\bcurl\s.*\s-[oO]\b"), "curl -o (write file)"),
    (re.compile(r"\bwget\b"), "wget"),
    (re.compile(r"\bsystemctl\s+(start|stop|restart|reload|enable|disable)\b"),
        "systemctl state change"),
]

# ---- feature worker: narrow blocklist (only cross-branch / dangerous) -------
FEATURE_BASH_PATTERNS = [
    (re.compile(r"\bgit\s+push\b"), "git push"),
    (re.compile(r"\bgit\s+merge\b"), "git merge"),
    (re.compile(r"\bgit\s+worktree\b"), "git worktree"),
    (re.compile(r"\bgit\s+branch\s+-[dDmM]\b"), "git branch delete/rename"),
    (re.compile(r"\bgit\s+switch\b(?!\s+-c\b)"), "git switch (leaving your branch)"),
    (re.compile(r"\bsystemctl\s+(start|stop|restart|reload|enable|disable)\b"),
        "systemctl state change (deploy)"),
    (re.compile(r"\bsudo\b"), "sudo"),
]

# git checkout to another branch is blocked for feature workers, but creating a
# branch (-b) and restoring files (checkout -- <path>) stay allowed.
FEATURE_CHECKOUT = re.compile(r"\bgit\s+checkout\b(?!\s+-b\b)")
CHECKOUT_FILE_RESTORE = re.compile(r"\s--(\s|$)")

# ---- integrator: force-push, non-ff merge, rebase ---------------------------
INTEGRATOR_FORCE_PUSH = re.compile(
    r"\bgit\s+push\b.*(--force-with-lease|--force\b|\s-f\b|\s\+)"
)
INTEGRATOR_REBASE = re.compile(r"\bgit\s+rebase\b")
GIT_MERGE = re.compile(r"\bgit\s+merge\b")
MERGE_FF_ONLY = re.compile(r"--ff-only\b")
MERGE_NON_OP = re.compile(r"--(abort|continue|quit)\b")


def run_git(cwd, *args):
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


def get_branch(cwd):
    return run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def get_repo_root(cwd):
    return run_git(cwd, "rev-parse", "--show-toplevel")


def resolve_path(file_path, cwd):
    if not file_path or not isinstance(file_path, str):
        return None
    if os.path.isabs(file_path):
        return os.path.normpath(file_path)
    return os.path.normpath(os.path.join(cwd, file_path))


def under(path, root):
    """True if path == root or sits inside root."""
    return path == root or path.startswith(root.rstrip("/") + os.sep)


def match(patterns, command):
    for pattern, label in patterns:
        if pattern.search(command):
            return label
    return None


def block(message):
    sys.stderr.write(message + "\n")
    sys.exit(2)


def feature_write_blocked(abspath, repo_root):
    """Block writes into another worktree or the primary checkout. Allow the
    session's own worktree and anywhere outside the repo areas (e.g. /tmp)."""
    for guarded in GUARDED_REPO_ROOTS:
        if under(abspath, guarded):
            if repo_root and under(abspath, repo_root):
                return False
            return True
    return False


def integrator_write_blocked(abspath, repo_root):
    """Block edits to application code; allow docs/, .claude/, *.md, and
    anything outside the repo."""
    if not repo_root or not under(abspath, repo_root):
        return False
    rel = "" if abspath == repo_root else abspath[len(repo_root) + 1:]
    if rel.startswith(INTEGRATOR_BLOCKED_PREFIXES):
        return True
    if rel.endswith(INTEGRATOR_BLOCKED_SUFFIXES):
        return True
    return False


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # malformed input -> don't block

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    cwd = event.get("cwd") or os.getcwd()

    branch = get_branch(cwd)
    repo_root = get_repo_root(cwd)
    role = os.environ.get("CLAUDE_MISS_ROLE", "").strip().lower()
    # Anything that isn't an explicit integrator session is treated as the
    # restrictive feature-worker role (unknown/plain sessions can never push or
    # deploy).
    is_integrator = role == "integrator"

    command = tool_input.get("command", "") if tool_name == "Bash" else ""
    if not isinstance(command, str):
        command = ""

    # --- main/master: strict, regardless of role ----------------------------
    if branch in PROTECTED_BRANCHES:
        escape_hint = (
            "You are on the deploy branch (master). Feature work belongs in a "
            "worktree on a claude/<slug> branch. Start one with claude-miss."
        )
        if tool_name in WRITE_TOOLS:
            block(
                f"Blocked: refusing {tool_name} on protected branch "
                f"'{branch}'.\n{escape_hint}"
            )
        if tool_name == "Bash":
            label = match(MASTER_BASH_PATTERNS, command)
            if label is not None:
                block(
                    f"Blocked: '{label}' is not allowed on protected branch "
                    f"'{branch}'.\nCommand: {command}\n{escape_hint}"
                )
        sys.exit(0)

    # --- integrator role -----------------------------------------------------
    if is_integrator:
        if tool_name in WRITE_TOOLS:
            abspath = resolve_path(tool_input.get("file_path"), cwd)
            if abspath and integrator_write_blocked(abspath, repo_root):
                block(
                    "Blocked: the integrator does not write feature code. "
                    "Edit to application code refused.\n"
                    f"File: {abspath}\n"
                    "Fix: ask a feature worker to make this change in its own "
                    "worktree, then integrate the branch."
                )
            sys.exit(0)
        if tool_name == "Bash":
            if INTEGRATOR_FORCE_PUSH.search(command):
                block(
                    "Blocked: the integrator never force-pushes.\n"
                    f"Command: {command}\n"
                    "Fix: use a plain fast-forward push. If a branch won't "
                    "fast-forward, its feature worker must rebase it first."
                )
            if INTEGRATOR_REBASE.search(command):
                block(
                    "Blocked: the integrator does not rebase. Integration is "
                    "fast-forward-only.\n"
                    f"Command: {command}\n"
                    "Fix: if a branch isn't current with working, tell its "
                    "feature worker to rebase after you approve with YES REBASE."
                )
            if GIT_MERGE.search(command) and not MERGE_FF_ONLY.search(command) \
                    and not MERGE_NON_OP.search(command):
                block(
                    "Blocked: integrate with --ff-only (fast-forward only).\n"
                    f"Command: {command}\n"
                    "Fix: git merge --ff-only <claude/branch>  (only after the "
                    "operator approves with YES INTEGRATE)."
                )
        sys.exit(0)

    # --- feature worker role (default) ---------------------------------------
    if tool_name in WRITE_TOOLS:
        abspath = resolve_path(tool_input.get("file_path"), cwd)
        if abspath and feature_write_blocked(abspath, repo_root):
            block(
                "Blocked: a feature worker edits only its own worktree.\n"
                f"File: {abspath}\nThis worktree: {repo_root}\n"
                "Fix: make this change in the session that owns that worktree."
            )
        sys.exit(0)

    if tool_name == "Bash":
        label = match(FEATURE_BASH_PATTERNS, command)
        if label is None and FEATURE_CHECKOUT.search(command) \
                and not CHECKOUT_FILE_RESTORE.search(command):
            label = "git checkout (leaving your branch)"
        if label is not None:
            block(
                f"Blocked: a feature worker can't run '{label}'.\n"
                f"Command: {command}\n"
                "Feature workers stay on their own branch and don't push, "
                "merge, or deploy. When this branch is ready, tell the operator "
                "\"ready for integrator\" and let the integrator session take "
                "it into staging (working).\n"
                "(You may still edit files here and, after the operator types "
                "YES COMMIT, git add / git commit.)"
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
