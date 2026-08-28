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
import time


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

# ---- SHIP role: the `miss-integrator` SUBAGENT -------------------------------
# A feature mission's operator types YES SHIP once; the feature worker writes a
# delegation ticket (scripts/miss-ship-ticket.py) and spawns the miss-integrator
# subagent. Claude Code tags every tool call a subagent makes with `agent_type` in
# the hook input, and the parent's calls carry none — so THIS is where integrator
# power is granted: only to calls from that subagent, only for what the ticket
# names, each re-verified against git right before it runs. The feature worker
# itself stays a feature worker; it cannot use the ticket.
#
# Allowed (with the ticket): the ff-only merge of ticket.branch at ticket.commit
# into ticket.base inside ticket.integration_worktree; `git push` of ticket.base
# (and ticket.release_branch) to ticket.remote, plain refs only; the exact
# release / deploy / verify command strings the ticket carries; `git fetch`;
# read-only git; writing the ticket's own log. Everything else that mutates is
# blocked: other branches/refs, force, reset/rebase/checkout/switch/tag/remote/
# worktree, other sudo/systemctl, make-release.sh, edits to app code or to the
# guard/rails files.
# `git -C <dir> -c k=v push` -> `git push` for pattern matching (see main()).
GIT_OPTS_PREFIX = re.compile(r"\bgit(?:\s+-C\s+\S+|\s+-c\s+\S+|\s+--git-dir=\S+|\s+--work-tree=\S+)+\s+")

SHIP_AGENT_TYPE = "miss-integrator"
SHIP_TTL_HOURS = 3
SHIP_MUTATING = re.compile(
    r"\bgit\s+(push|merge|commit|add|reset|rebase|cherry-pick|revert|stash|clean|restore|"
    r"checkout|switch|branch|tag|remote|worktree|update-ref|symbolic-ref|reflog|gc|prune|"
    r"filter-branch|replace|notes|am|apply|pull)(?![-\w])"
    r"|\bsudo\b|\bsystemctl\b|\bmake-release\.sh\b|(^|[\s;&|])(rm|mv|cp|chmod|chown)(\s|$)"
    r"|>>|(?:^|[^0-9&<>])>(?!&|>)|\btee\s"
)
SHIP_GUARD_FILES = (".claude/hooks/", "console-hooks", "miss-rails.settings.json",
                    "scripts/miss-", "scripts/claude-miss", "console-launch.sh", "console-session")


def ship_ticket_path():
    """Where scripts/miss-ship-ticket.py puts this session's ticket (kept in step)."""
    tdir = os.environ.get("MISS_TICKET_DIR", "").strip() or os.path.expanduser("~/.miss-claude/tickets")
    repo_id = os.environ.get("MISS_REPO_ID", "").strip()
    branch = os.environ.get("MISS_FEATURE_BRANCH", "").strip()
    if not repo_id or not branch:
        return ""
    return os.path.join(tdir, "%s--%s.json" % (repo_id, branch.replace("/", "_")))


def ship_ticket():
    """(ticket, why-not). Ticket must exist, parse, carry YES SHIP, be young enough."""
    path = ship_ticket_path()
    if not path:
        return None, "this session has no recorded repo id / feature branch (MISS_REPO_ID, MISS_FEATURE_BRANCH)"
    try:
        with open(path) as fh:
            t = json.load(fh)
    except (OSError, ValueError):
        return None, "no valid YES SHIP ticket at %s" % path
    need = ("approval", "repo", "branch", "commit", "base", "integration_worktree", "created")
    if not isinstance(t, dict) or any(not t.get(k) for k in need) or t["approval"] != "YES SHIP":
        return None, "malformed ticket %s" % path
    try:
        if time.time() - float(t["created"]) > SHIP_TTL_HOURS * 3600:
            return None, "ticket %s has expired" % path
    except (TypeError, ValueError):
        return None, "malformed ticket %s" % path
    return t, ""


def ship_log(ticket, text):
    """Append a line to the ticket's step log (and the mission's mirror) — best effort."""
    for path in (ticket.get("log"), os.path.join(ticket["mission_dir"], "ship.log")
                 if ticket.get("mission_dir") else None):
        if not path:
            continue
        try:
            with open(path, "a") as fh:
                fh.write("%s guard: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text.replace("\n", " | ")))
        except OSError:
            pass


def rev(repo, ref):
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def ship_check(tool_name, tool_input, command, cwd):
    """Enforce the ticket for the miss-integrator subagent. Blocks or returns."""
    t, why = ship_ticket()
    if t is None:
        if tool_name in WRITE_TOOLS or SHIP_MUTATING.search(command):
            block("Blocked: the integrator subagent has no valid delegation — %s.\n"
                  "Report NEEDS_ATTENTION; the feature worker writes a ticket with "
                  "scripts/miss-ship-ticket.py only after the operator types YES SHIP." % why)
        return
    repo, branch, base, iwt = t["repo"], t["branch"], t["base"], t["integration_worktree"]
    remote, rel_branch = t.get("remote") or "", t.get("release_branch") or ""

    def drift(what):
        msg = ("Blocked: state drift — %s. The delegation covered %s at %s into %s; "
               "nothing further runs. Report NEEDS_ATTENTION." % (what, branch, t["commit"][:12], base))
        ship_log(t, msg)
        block(msg)

    def deny(what):
        msg = ("Blocked: the integrator subagent may not %s — outside the YES SHIP delegation "
               "(%s at %s -> %s%s). Report BLOCKED." % (what, branch, t["commit"][:12], base,
                                                        (", release " + rel_branch) if rel_branch else ""))
        ship_log(t, msg)
        block(msg)

    if tool_name in WRITE_TOOLS:
        abspath = resolve_path(tool_input.get("file_path"), cwd) or ""
        allowed_logs = [t.get("log") or ""]
        if t.get("mission_dir"):
            allowed_logs.append(os.path.join(t["mission_dir"], "ship.log"))
        if abspath and os.path.realpath(abspath) in [os.path.realpath(p) for p in allowed_logs if p]:
            return
        if any(seg in abspath for seg in SHIP_GUARD_FILES):
            deny("edit the guard/rails files (%s)" % abspath)
        for root in (repo, iwt, t.get("worktree") or ""):
            if root and under(abspath, root):
                deny("edit files in the repo (%s)" % abspath)
        deny("write %s (only its ship log)" % abspath)

    if tool_name != "Bash" or not SHIP_MUTATING.search(GIT_OPTS_PREFIX.sub("git ", command)):
        return
    cmd = command.strip()
    # Appending to the ticket's own step log (`... >> <log>`) is the one redirect the
    # subagent may use; the part before the redirect must itself be non-mutating.
    m = re.match(r"^(.*?)\s*>>\s*(\S+)\s*$", cmd, re.S)
    if m and os.path.realpath(m.group(2).strip("'\"")) in [
            os.path.realpath(p) for p in (t.get("log") or "",
                                          os.path.join(t["mission_dir"], "ship.log") if t.get("mission_dir") else "") if p]:
        if not SHIP_MUTATING.search(GIT_OPTS_PREFIX.sub("git ", m.group(1))):
            return
        deny("mix a mutating command with a log append ('%s')" % cmd)
    # Exact configured strings: release (base must still be the approved commit),
    # deploy (the release branch — or base when no release step — must be), verify.
    if cmd in (t.get("verify") or []):
        return
    if cmd in (t.get("release") or []):
        if rev(repo, base) != t["commit"]:
            drift("%s is not at the approved commit before release" % base)
        return
    if cmd in (t.get("deploy") or []):
        ref = rel_branch if (t.get("release") and rel_branch) else base
        if rev(repo, ref) != t["commit"]:
            drift("%s is not at the approved commit before deploy" % ref)
        return
    # Generic forms, anchored to the ticket. An optional `cd <integration wt> &&` or
    # `git -C <repo|integration wt>` prefix is allowed; any other compound is not.
    m = re.match(r"^(?:cd\s+(\S+)\s*&&\s*)?git\s+(?:-C\s+(\S+)\s+)?(\S+)\s*(.*)$", cmd)
    if not m:
        deny("run '%s'" % cmd)
    cd_dir, c_dir, sub, rest = m.group(1), m.group(2), m.group(3), m.group(4).strip()
    where = os.path.realpath(c_dir or cd_dir or cwd)
    if any(ch in rest for ch in ";&|`$"):
        deny("chain commands ('%s')" % cmd)
    if sub == "fetch":
        return
    if sub == "merge":
        if rest != "--ff-only %s" % branch:
            deny("merge anything but '--ff-only %s'" % branch)
        if where != os.path.realpath(iwt):
            deny("merge outside the integration checkout %s" % iwt)
        if rev(repo, branch) != t["commit"]:
            drift("%s no longer points at the approved commit" % branch)
        if rev(repo, base) != rev(iwt, "HEAD"):
            drift("%s is not what is checked out in %s" % (base, iwt))
        return
    if sub == "push":
        words = rest.split()
        if any(w.startswith("-") or "+" in w or ":" in w and w.split(":")[0] != w.split(":")[1] for w in words):
            deny("push with options, force, deletes or renames ('%s')" % rest)
        refs = [w.split(":")[0] for w in words[1:]] if words else []
        target = words[0] if words else remote
        if not remote or target != remote:
            deny("push to '%s' (ticket remote: %s)" % (target, remote or "none"))
        for r in refs or [base]:
            if r == base:
                if rev(repo, base) != t["commit"]:
                    drift("%s is not at the approved commit before push" % base)
            elif r == rel_branch and rel_branch:
                if rev(repo, rel_branch) != t["commit"]:
                    drift("%s is not at the approved commit before push" % rel_branch)
            else:
                deny("push ref '%s'" % r)
        return
    deny("run '%s'" % cmd)
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
    # The miss-integrator SUBAGENT (Claude Code stamps `agent_type` on a subagent's
    # tool calls; the parent's carry none): integrator power scoped to the YES SHIP
    # ticket — see ship_check(). It rides the integrator carve-outs below (a repo
    # whose staging IS main/master) and then takes its own, stricter branch.
    is_ship = (event.get("agent_type") or "") == SHIP_AGENT_TYPE
    if is_ship:
        is_integrator = True

    command = tool_input.get("command", "") if tool_name == "Bash" else ""
    if not isinstance(command, str):
        command = ""
    # The role patterns below match `git push`, `git merge`, ... — so `git -C <dir>
    # push` (or `git -c k=v push`) must read the same to them. The raw command is kept
    # for the pieces that care WHERE it acts (foreign_repo_target, ship_check).
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

    # --- ship role: the miss-integrator subagent, bound to its ticket ----------
    if is_ship:
        ship_check(tool_name, tool_input, raw_command, cwd)
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
