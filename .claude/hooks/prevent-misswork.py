#!/usr/bin/env python3
"""
Claude Code PreToolUse guard for the Miss Claude multi-session worktree workflow.

Adapted from an internal per-repo guard for this repo — the Miss Claude /
Mission Dashboard code (a flat, stdlib-only Python project, no app/lib tree).

Two roles, carried in via the CLAUDE_MISS_ROLE env var the launch wrappers export
(scripts/claude-miss -> "feature", scripts/claude-miss-integrator ->
"integrator"). The hook subprocess inherits that env from the Claude process, so
it can tell the roles apart.

This hook is the ONLY hard guardrail, because the wrappers launch Claude with
--dangerously-skip-permissions (the permission allowlist is bypassed). It
hard-BLOCKS the actions a role must never take. The approval phrases (YES SHIP for
a feature worker; YES COMMIT / YES REBASE, and the integrator's YES INTEGRATE /
YES PUSH WORKING / YES RELEASE / YES DEPLOY for hand-driven work) cannot be
enforced here — the hook can't read the chat — so those stay behavioural,
enforced by CLAUDE.md.

Policy summary:
  * On main/master (any role): the full strict blocklist.
  * Feature worker (role=feature, or unset/unknown -> restrictive default):
      blocks push / merge / deploy(systemctl) / worktree ops / branch
      delete-rename / switching away from its branch / sudo, and blocks edits to
      OTHER worktrees or the primary checkout. Everyday work (edits in its own
      worktree, git add/commit/rebase, running app.py / tests, /tmp writes) stays
      allowed. ONE carve-out: a plain, unchained run of scripts/miss-ship.py — the
      deterministic YES SHIP path, which does the whole ship itself and enforces
      its own scope (see "the sanctioned ship path" below).
  * Both roles: mutating git (fetch/pull included) aimed at any repo other than the
      session's declared one (PRIMARY_REPO) is blocked — via -C, a preceding cd, or
      the cwd; read-only listing forms are exempt. Hand-run git against the public
      release checkout (RELEASE_DIR) is always blocked: use scripts/make-release.sh.
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
import shlex
import subprocess
import sys


PROTECTED_BRANCHES = {"main", "master"}

WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Repo areas the feature-worker write guard cares about. Writes here that fall
# OUTSIDE the session's own worktree are blocked (sibling worktrees and the
# primary checkout). Writes elsewhere (e.g. /tmp) are left alone.
#
# Env-driven so a generalized dev mission guards the LOCAL repo it actually develops:
# the worker session exports PRIMARY_REPO / WORKTREES_DIR (console-launch.sh ->
# console-session-wt.sh -> claude-miss). When unset (a plain claude-miss run, or any
# launch path that forgot to export them) we FALL BACK to the original Claude-Miss
# paths — fail-safe (guard the mission-dashboard repo), never fail-open.
def _guarded_repo_roots():
    primary = os.environ.get("PRIMARY_REPO") or os.environ.get("MISS_REPO_ROOT") \
        or os.path.expanduser("~/mission-dashboard")
    worktrees = os.environ.get("WORKTREES_DIR") or os.path.expanduser("~/missclaude-worktrees")
    # The repo's integration checkout (may live outside both of the above).
    integration = os.environ.get("MISS_INTEGRATION_WORKTREE") \
        or os.environ.get("INTEGRATION_WORKTREE") or ""
    out = []
    for p in (primary, worktrees, integration):
        if not p:
            continue
        try:
            out.append(os.path.realpath(os.path.expanduser(p)))
        except OSError:
            out.append(p)
    return tuple(out)


GUARDED_REPO_ROOTS = _guarded_repo_roots()

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
    (re.compile(r"\bgit\s+worktree\b(?!\s+list\b)"), "git worktree"),
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

# ---- the sanctioned ship path -----------------------------------------------
# `git -C <dir> -c k=v <sub>` -> `git <sub>` for pattern matching (see main()).
GIT_OPTS_PREFIX = re.compile(r"\bgit(?:\s+-C\s+\S+|\s+-c\s+\S+|\s+--git-dir=\S+|\s+--work-tree=\S+)+\s+")

# After the operator types YES SHIP, the feature worker runs ONE deterministic script —
# scripts/miss-ship.py — which integrates and then, only where the repo already defines
# them, runs its established release / deploy / verify steps for that exact branch at
# that exact commit. There is no second role and no delegation ticket: the script
# decides nothing, re-reads git before every step, skips what is already done, and stops
# the moment reality stops matching what was approved.
#
# The worker's own hands stay tied: the feature blocklist below still refuses hand-run
# integration, remote or service commands, so the script is the only route to them. This
# pattern is here so a --request/--tests string that happens to quote such a word cannot
# trip that blocklist — it recognises a plain, unchained invocation of the script itself.
SHIP_SCRIPT = re.compile(
    r"""^\s*(?:python3?\s+)?(?:\S*/)?miss-ship\.py"""
    r"""(?:\s+(?:"[^"]*"|'[^']*'|[^\s;&|`$()<>]+))*\s*$"""
)


def is_ship_script(command):
    """True for a lone `python3 .../miss-ship.py ...` call (no chaining, no redirects)."""
    return bool(SHIP_SCRIPT.match(command))


MERGE_FF_ONLY = re.compile(r"--ff-only\b")
MERGE_NON_OP = re.compile(r"--(abort|continue|quit)\b")


# ---- repo identity: which repository does a git command act on? ------------
# A session is declared for ONE repo (PRIMARY_REPO, exported by the launchers from the
# mission's recorded identity). Mutating git commands that target a different repo —
# via `git -C <other>` or a cwd that is not a checkout of the declared repo — are
# blocked for both roles: an integrator must only integrate ITS repo, a worker must
# only commit in ITS worktree. Read-only git (log/status/diff) is left alone.
GIT_OPTS = r"(?:(?:-c\s+\S+|--no-pager|--git-dir=\S+|-C\s+(?:\"[^\"]+\"|'[^']+'|\S+))\s+)*"
GIT_C = re.compile(r"\bgit\s+(?:(?:-c\s+\S+|--no-pager|--git-dir=\S+)\s+)*-C\s+(\"[^\"]+\"|'[^']+'|\S+)")
# Every git subcommand that can change a repo's refs, index, working tree or object
# store — including fetch/pull (they write refs and objects) — with the rest of that
# shell segment captured so read-only forms can be told apart below.
GIT_MUTATING = re.compile(
    r"\bgit\s+" + GIT_OPTS +
    r"(commit|add|merge|push|pull|fetch|rebase|reset|checkout|switch|restore|stash|"
    r"cherry-pick|revert|clean|branch|tag|worktree|remote|am|apply|mv|rm|update-ref|"
    r"symbolic-ref|clone|submodule|notes|replace|filter-branch|gc|prune|reflog)\b([^;&|\n]*)"
)
# `cd <dir>` in the same command: git that follows acts on <dir>, not the session cwd.
CD_TARGET = re.compile(r"(?:^|[;&|]\s*)cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")

# Read-only forms of otherwise-mutating subcommands (listing/inspecting): allowed
# anywhere. The option sets are deliberately explicit — an unknown flag counts as
# mutating, so a new git option can only ever make the guard stricter.
_BRANCH_LIST_OPTS = {"--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
                     "--show-current", "--merged", "--no-merged", "--contains", "--no-contains",
                     "--points-at", "--color", "--no-color", "--column", "--no-column", "-i",
                     "--ignore-case", "--abbrev"}
_TAG_LIST_OPTS = {"--list", "-l", "-n", "--contains", "--no-contains", "--merged", "--no-merged",
                  "--points-at", "--column", "--no-column", "-i", "--ignore-case", "--color"}
_BRANCH_LIST_TRIGGERS = {"--list", "-l", "--show-current", "--merged", "--no-merged",
                         "--contains", "--no-contains", "--points-at", "-a", "-r", "-v", "-vv"}
_TAG_LIST_TRIGGERS = {"--list", "-l", "-n", "--contains", "--no-contains", "--merged",
                      "--no-merged", "--points-at"}


def _tokens(rest):
    try:
        return shlex.split(rest, posix=True)
    except ValueError:
        return rest.split()


def _list_form(tokens, opts, triggers):
    """branch/tag: read-only when every option is a listing option and either a
    listing trigger is present (positional args are then patterns) or there are no
    positional args at all (bare `git branch` / `git tag` = list)."""
    listing = False
    positional = 0
    for t in tokens:
        if t.startswith("-"):
            base = t.split("=", 1)[0]
            if base not in opts:
                return False
            if base in triggers:
                listing = True
        else:
            positional += 1
    return listing or positional == 0


def readonly_git_form(sub, rest):
    """True when `git <sub> <rest>` only lists/inspects."""
    toks = _tokens(rest)
    if sub == "branch":
        return _list_form(toks, _BRANCH_LIST_OPTS, _BRANCH_LIST_TRIGGERS)
    if sub == "tag":
        return _list_form(toks, _TAG_LIST_OPTS, _TAG_LIST_TRIGGERS)
    if sub == "worktree":
        return bool(toks) and toks[0] == "list"
    if sub == "stash":
        return bool(toks) and toks[0] in ("list", "show")   # bare `git stash` = push
    if sub == "remote":
        return not toks or toks[0] in ("-v", "--verbose", "show", "get-url")
    if sub == "reflog":
        return not toks or toks[0] in ("show", "-n") or toks[0].startswith("-")
    if sub == "notes":
        return not toks or toks[0] in ("list", "show")
    if sub == "submodule":
        return bool(toks) and toks[0] in ("status", "summary", "foreach")
    return False


def repo_of(path):
    """Main checkout of the repo <path> belongs to (realpath), or None."""
    common = run_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    common = os.path.realpath(common)
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def declared_repo():
    """The repo this session is declared for, or None when nothing was declared."""
    p = os.environ.get("PRIMARY_REPO") or os.environ.get("MISS_REPO_ROOT")
    return os.path.realpath(os.path.expanduser(p)) if p else None


def release_repo():
    """The public-release checkout (scripts/make-release.sh's RELEASE_DIR): the
    declared repo's local.env, else ~/missclaude-release. Realpath, or None."""
    d = os.environ.get("RELEASE_DIR", "")
    mine = declared_repo()
    if not d and mine:
        try:
            with open(os.path.join(mine, "local.env"), encoding="utf-8") as fh:
                for line in fh:
                    m = re.match(r'\s*RELEASE_DIR=["\']?([^"\'\n#]+)', line)
                    if m:
                        d = m.group(1).strip()
        except OSError:
            pass
    d = d or "~/missclaude-release"
    d = d.replace("$HOME", os.path.expanduser("~"))
    return os.path.realpath(os.path.expanduser(d))


def foreign_repo_target(command, cwd):
    """If <command> runs mutating git (fetch/pull included) against a repo other than
    the declared one — via `git -C <other>`, a `cd <other>` earlier in the command,
    or a cwd that is not a checkout of the declared repo — return the offending repo
    path; else None. Read-only forms (`branch --list`, `tag -l`, `worktree list`,
    `stash list`, `remote -v`, ...) are never foreign. Unknown/unresolvable => None
    (never block on a guess)."""
    mine = declared_repo()
    if not mine:
        return None
    mutating = [m for m in GIT_MUTATING.finditer(command)
                if not readonly_git_form(m.group(1), m.group(2))]
    if not mutating:
        return None
    targets = []
    for m in GIT_C.finditer(command):
        t = m.group(1).strip("\"'")
        targets.append(os.path.normpath(os.path.join(cwd, os.path.expanduser(t))))
    for m in CD_TARGET.finditer(command):
        t = m.group(1).strip("\"'")
        t = t.replace("$HOME", os.path.expanduser("~"))
        targets.append(os.path.normpath(os.path.join(cwd, os.path.expanduser(t))))
    if not targets:
        targets.append(cwd)
    for t in targets:
        r = repo_of(t)
        if r is not None and r != mine:
            return r
    return None


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
    # The role patterns below match `git push`, `git merge`, ... — so `git -C <dir>
    # push` (or `git -c k=v push`) must read the same to them. The raw command is kept
    # for the pieces that care WHERE it acts (foreign_repo_target, is_ship_script).
    raw_command = command
    command = GIT_OPTS_PREFIX.sub("git ", command)

    # --- main/master: strict, regardless of role ----------------------------
    # One carve-out: a generalized dev mission's repo may use main/master AS its
    # staging branch (BASE_BRANCH env, exported by the launch wrappers — most repos
    # have no separate `working`). The INTEGRATOR must be able to integrate there,
    # so it falls through to the integrator rules below (ff-only merge, no
    # force-push, no rebase, no app-code edits) instead of the full blocklist.
    # Feature workers and unknown roles stay fully blocked on main/master, and the
    # integrator gets the strict treatment on any protected branch that is NOT its
    # declared staging. Miss Claude itself is unaffected (its staging is `working`).
    staging = os.environ.get("BASE_BRANCH", "").strip()
    if branch in PROTECTED_BRANCHES and not (is_integrator and branch == staging):
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

    # --- cross-repo guard (both roles) ---------------------------------------
    # Mutating git aimed at a repo other than the one this session is declared for
    # (PRIMARY_REPO from the mission's recorded identity): blocked. This is what
    # keeps an integrator for repo A from fast-forwarding repo B, and a worker from
    # committing into a checkout that isn't its own, whatever the cwd happens to be.
    if tool_name == "Bash":
        other = foreign_repo_target(raw_command, cwd)
        if other is not None and other == release_repo():
            block(
                "Blocked: git against the public release checkout (%s) is never run by "
                "hand.\nCommand: %s\nReleases go through scripts/make-release.sh "
                "(--dry-run to preview, --push to publish; it scrubs + leak-gates the "
                "tree first), or through a dev mission spawned ON the release repo."
                % (other, command)
            )
        if other is not None:
            block(
                "Blocked: this session is declared for repo %s but the command acts "
                "on %s.\nCommand: %s\nA session never changes a repo it was not "
                "started for — use (or spawn) the mission for that repo."
                % (declared_repo(), other, command)
            )

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
        # The one sanctioned way a feature worker reaches any of the blocked verbs:
        # scripts/miss-ship.py, the deterministic YES SHIP path (see above). Matched on
        # the whole command so a quoted --request/--tests string can't smuggle anything
        # alongside it, and so such a string can't trip the blocklist by mentioning a verb.
        if is_ship_script(raw_command):
            sys.exit(0)
        label = match(FEATURE_BASH_PATTERNS, command)
        if label is None and FEATURE_CHECKOUT.search(command) \
                and not CHECKOUT_FILE_RESTORE.search(command):
            label = "git checkout (leaving your branch)"
        if label is not None:
            block(
                f"Blocked: a feature worker can't run '{label}' by hand.\n"
                f"Command: {command}\n"
                "Feature workers stay on their own branch. Shipping is not done step by "
                "step: once the operator has typed exactly YES SHIP, commit your work and "
                "run scripts/miss-ship.py --approval \"YES SHIP\" — it integrates and runs "
                "this repo's established release/deploy/verify steps itself, and refuses "
                "anything it was not approved for.\n"
                "(You may still edit files here and, after YES COMMIT or YES SHIP, "
                "git add <explicit paths> / git commit.)"
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
