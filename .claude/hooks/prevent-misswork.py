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

HOW A COMMAND IS JUDGED — by what it EXECUTES, not by the words it contains.
The Bash command is parsed (see "command parsing" below) into the programs that
actually land at a command position, together with their own arguments: through
pipes, `;`/`&&`, subshells, `$(...)` / backticks / `<(...)` (which run even inside
double quotes), heredocs fed to a shell or ssh, `bash -c`, `eval`, `sudo`, `env`,
`xargs`, `find -exec`, `ssh host ...`, and so on. The role rules are predicates on
those (program, args) records. Text inside quotes, a heredoc body fed to a
non-shell program (a python script, cat, a commit message), and comments are DATA:
`git commit -m "docs: git push is done by the ship script"` or a python heredoc
that mentions "git push" no longer trips the guard.

FAIL-SAFE: whenever the parse is not confident — unbalanced quotes, a program or
git subcommand that comes from a variable, nesting too deep — the hook falls back
to the older whole-text regex lists (kept below, unchanged). A parse failure can
only ever block MORE, never less.

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


# =============================================================================
# FALLBACK regex lists — whole-text patterns. Used ONLY when the parser below
# raises ParseError. They fire on mentions, which is why they are the fallback
# and not the rule: a parse failure may block more, never less.
# =============================================================================

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
MUTATING_SUBS = {
    "commit", "add", "merge", "push", "pull", "fetch", "rebase", "reset", "checkout", "switch",
    "restore", "stash", "cherry-pick", "revert", "clean", "branch", "tag", "worktree", "remote",
    "am", "apply", "mv", "rm", "update-ref", "symbolic-ref", "clone", "submodule", "notes",
    "replace", "filter-branch", "gc", "prune", "reflog",
}
GIT_MUTATING = re.compile(
    r"\bgit\s+" + GIT_OPTS +
    r"(" + "|".join(sorted(MUTATING_SUBS, key=len, reverse=True)) + r")\b([^;&|\n]*)"
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


def readonly_git_tokens(sub, toks):
    """True when `git <sub> <toks...>` only lists/inspects."""
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


def readonly_git_form(sub, rest):
    """True when `git <sub> <rest>` only lists/inspects (rest = raw text)."""
    return readonly_git_tokens(sub, _tokens(rest))


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


def _target_path(t, cwd):
    t = t.strip("\"'").replace("$HOME", os.path.expanduser("~"))
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(t)))


def _first_foreign(targets, mine):
    for t in targets:
        r = repo_of(t)
        if r is not None and r != mine:
            return r
    return None


def foreign_repo_target(command, cwd):
    """(Regex fallback.) If <command> runs mutating git (fetch/pull included) against
    a repo other than the declared one — via `git -C <other>`, a `cd <other>` earlier
    in the command, or a cwd that is not a checkout of the declared repo — return the
    offending repo path; else None. Read-only forms (`branch --list`, `tag -l`,
    `worktree list`, `stash list`, `remote -v`, ...) are never foreign.
    Unknown/unresolvable => None (never block on a guess)."""
    mine = declared_repo()
    if not mine:
        return None
    mutating = [m for m in GIT_MUTATING.finditer(command)
                if not readonly_git_form(m.group(1), m.group(2))]
    if not mutating:
        return None
    targets = [_target_path(m.group(1), cwd) for m in GIT_C.finditer(command)]
    targets += [_target_path(m.group(1), cwd) for m in CD_TARGET.finditer(command)]
    if not targets:
        targets.append(cwd)
    return _first_foreign(targets, mine)


def foreign_repo_target_parsed(parsed, cwd):
    """Same question, answered from the parsed command: only git that is actually
    EXECUTED counts, and only its own `-C` / a `cd` in the command / the cwd say
    where it acts."""
    mine = declared_repo()
    if not mine:
        return None
    targets = []
    mutating = False
    for rec in parsed.records:
        if rec.prog == "git":
            sub, rest, cpaths = git_sub(rec.args)
            if sub in MUTATING_SUBS and not readonly_git_tokens(sub, rest):
                mutating = True
                targets += [_target_path(p, cwd) for p in cpaths]
    if not mutating:
        return None
    for rec in parsed.records:
        if rec.prog in ("cd", "pushd"):
            pos = [a for a in rec.args if not a.startswith("-")]
            if pos:
                targets.append(_target_path(pos[0], cwd))
    if not targets:
        targets.append(cwd)
    return _first_foreign(targets, mine)


# =============================================================================
# Command parsing: what does this Bash command EXECUTE?
# =============================================================================
#
# parse_command(text) -> Parsed(records, redirects), where every record is one
# program that lands at a command position (prog = basename, args = its own
# arguments, sudo = it runs under sudo/doas). Wrappers (sudo, env, nohup, timeout,
# xargs, bash -c, eval, ssh, find -exec, ...) are unwrapped so the program they run
# is a record of its own. Pieces that run text as shell — `$(...)`, backticks,
# `<(...)`, heredocs / here-strings fed to a shell or ssh, `bash -c`, `eval`,
# `su -c`, `ssh host cmd` — are parsed recursively as commands. Everything else
# (quoted strings, heredocs fed to other programs, comments) is data.
#
# Anything the parser cannot judge with confidence raises ParseError, and the
# caller falls back to the regex lists above (fail-safe).

class ParseError(Exception):
    """The command could not be parsed confidently; use the regex fallback."""


class Rec(object):
    __slots__ = ("prog", "args", "sudo")

    def __init__(self, prog, args, sudo=False):
        self.prog, self.args, self.sudo = prog, list(args), sudo

    def __repr__(self):
        return "Rec(%r, %r%s)" % (self.prog, self.args, ", sudo" if self.sudo else "")


class Parsed(object):
    def __init__(self):
        self.records = []     # Rec, in order of appearance
        self.redirects = []   # (op, target) for every simple command


SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "mksh"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "in",
            "case", "esac", "select", "function", "time", "!", "{", "}", "[[", "]]", "coproc"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
PLACEHOLDER = "__MISS_"
MAX_DEPTH = 6
SEPARATOR_CHARS = set(";|&()")
REDIRECT_CHARS = set("<>&|")
HEREDOC_TAG = re.compile(r"""(-?)[ \t]*(?:'([^']*)'|"([^"]*)"|\\?([^\s;|&<>()'"]+))""")

# Wrapper programs: (options that take an argument, positional args to skip before
# the wrapped command, skip VAR=val assignments too).
WRAPPERS = {
    "env": ({"-u", "-C", "--unset", "--chdir"}, 0, True),
    "nohup": (set(), 0, False),
    "exec": ({"-a"}, 0, False),
    "command": (set(), 0, False),
    "builtin": (set(), 0, False),
    "nice": ({"-n", "--adjustment"}, 0, False),
    "setsid": (set(), 0, False),
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0, False),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1, False),
    "ionice": ({"-c", "-n", "-p", "--class", "--classdata", "--pid"}, 0, False),
    "chrt": ({"-p", "--pid"}, 1, False),
    "unbuffer": (set(), 0, False),
    "chroot": ({"--userspec", "--groups"}, 1, False),
    "systemd-run": ({"-p", "-E", "-u", "-M", "-H", "--unit", "--property", "--setenv",
                     "--machine", "--host", "--description", "--slice", "--on-active",
                     "--on-boot", "--on-calendar", "--working-directory", "--uid", "--gid",
                     "--nice", "--service-type"}, 0, False),
    "xargs": ({"-I", "-i", "-n", "-L", "-l", "-P", "-s", "-d", "-a", "-E", "-e",
               "--replace", "--max-args", "--max-lines", "--max-procs", "--max-chars",
               "--delimiter", "--arg-file", "--eof"}, 0, False),
}
SUDO_ARG_OPTS = {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U", "--user", "--group",
                 "--close-from", "--chdir", "--host", "--prompt", "--role", "--type",
                 "--command-timeout", "--other-user"}
SSH_ARG_OPTS = {"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m",
                "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w", "-P"}


def _dynamic(word):
    """A word whose value the parser can't know (a variable, an expansion)."""
    return "$" in word or PLACEHOLDER in word or "`" in word


def _skip_opts(args, with_arg, positional=0, assignments=False):
    """Drop a wrapper's own options (and optionally VAR=val), then <positional>
    leading positional args; return what's left = the wrapped command."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("-") and len(a) > 1:
            base = a.split("=", 1)[0]
            if base in with_arg and "=" not in a:
                i += 2
            else:
                i += 1
            continue
        if assignments and ASSIGN.match(a):
            i += 1
            continue
        break
    rest = args[i:]
    return rest[positional:]


class _Parser(object):
    def __init__(self):
        self.out = Parsed()
        self.n = 0
        self.heredocs = {}   # placeholder -> (body, quoted_tag)

    # ---- pass 1: quote-aware scan -----------------------------------------
    def _ph(self, kind):
        self.n += 1
        return "%s%s_%d__" % (PLACEHOLDER, kind, self.n)

    def _scan(self, s, i, nested, lifted, depth):
        """Scan s from i. Newlines outside quotes become ';' (a new line is a new
        command), backslash-newline is joined, comments are dropped, `$(..)` /
        backticks / `<(..)` / `>(..)` are lifted out into <lifted> (each is its own
        command, and they run even inside double quotes), `$((..))` is skipped,
        heredocs are replaced by a placeholder word with their body kept aside.
        nested=True: stop at the ')' that closes the current substitution.
        Returns (processed_text, index_after)."""
        out = []
        pending = []          # heredocs whose bodies start at the next newline
        sq = dq = False
        depth_paren = 0
        n = len(s)
        word_start = True
        while i < n:
            c = s[i]
            if sq:
                if c == "'":
                    sq = False
                out.append(c)
                i += 1
                continue
            if c == "\\":
                if i + 1 < n and s[i + 1] == "\n":
                    i += 2                         # line continuation
                    continue
                out.append(s[i:i + 2])
                i += 2
                word_start = False
                continue
            if dq:
                if c == '"':
                    dq = False
                    out.append(c)
                    i += 1
                elif s.startswith("$(", i) and not s.startswith("$((", i):
                    i = self._subst(s, i + 2, lifted, depth, out)
                elif c == "`":
                    i = self._backtick(s, i + 1, lifted, depth, out)
                else:
                    out.append(c)
                    i += 1
                continue
            # --- unquoted ---
            if c == "'":
                sq = True
                out.append(c)
                i += 1
                word_start = False
            elif c == '"':
                dq = True
                out.append(c)
                i += 1
                word_start = False
            elif c == "#" and word_start:
                while i < n and s[i] != "\n":
                    i += 1
            elif s.startswith("$((", i):
                # arithmetic: no command of its own, but a $(..) inside still runs
                body, i = self._scan(s, i + 3, True, lifted, depth)
                if not s.startswith(")", i):
                    raise ParseError("unbalanced $((")
                i += 1
                out.append(self._ph("ARITH"))
                word_start = False
            elif s.startswith("$(", i):
                i = self._subst(s, i + 2, lifted, depth, out)
                word_start = False
            elif c == "`":
                i = self._backtick(s, i + 1, lifted, depth, out)
                word_start = False
            elif (c == "<" or c == ">") and s.startswith(c + "(", i):
                i = self._subst(s, i + 2, lifted, depth, out)   # process substitution
                out.append(" ")
                word_start = True
            elif s.startswith("<<<", i):
                out.append("<<<")                  # here-string: its word is data
                i += 3
                word_start = True
            elif s.startswith("<<", i):
                if nested:
                    raise ParseError("heredoc inside a substitution")
                m = HEREDOC_TAG.match(s, i + 2)
                if not m:
                    raise ParseError("heredoc without a tag")
                strip_tabs = m.group(1) == "-"
                tag = m.group(2) if m.group(2) is not None else \
                    m.group(3) if m.group(3) is not None else m.group(4)
                # 'TAG' / "TAG" / \TAG => the body is literal; bare TAG => expansions live
                quoted = m.group(4) is None or s[m.start(4) - 1] == "\\"
                ph = self._ph("HEREDOC")
                pending.append((ph, tag, strip_tabs, quoted))
                out.append(" " + ph + " ")
                i = m.end()
                word_start = True
            elif c == "(":
                depth_paren += 1
                out.append(c)
                i += 1
                word_start = True
            elif c == ")":
                if nested and depth_paren == 0:
                    if pending:
                        raise ParseError("heredoc inside a substitution")
                    return "".join(out), i + 1
                depth_paren -= 1
                out.append(c)
                i += 1
                word_start = True
            elif c == "\n":
                if pending:
                    i = self._bodies(s, i + 1, pending, lifted, depth)
                    pending = []
                else:
                    i += 1
                out.append(";")
                word_start = True
            else:
                out.append(c)
                i += 1
                word_start = c in " \t" or c in SEPARATOR_CHARS
        if sq or dq:
            raise ParseError("unbalanced quote")
        if nested:
            raise ParseError("unbalanced substitution")
        if pending:                       # heredoc at EOF with no newline: empty body
            for ph, _tag, _st, quoted in pending:
                self.heredocs[ph] = ("", quoted)
        return "".join(out), i

    def _subst(self, s, i, lifted, depth, out):
        body, i = self._scan(s, i, True, lifted, depth)
        lifted.append(body)
        out.append(self._ph("SUBST"))
        return i

    def _backtick(self, s, i, lifted, depth, out):
        j = s.find("`", i)
        if j < 0:
            raise ParseError("unbalanced backtick")
        body = s[i:j].replace("\\`", "`")
        inner = []
        body, _ = self._scan(body, 0, False, inner, depth)
        lifted.append(body)
        lifted.extend(inner)
        out.append(self._ph("SUBST"))
        return j + 1

    def _bodies(self, s, i, pending, lifted, depth):
        """Consume the heredoc bodies that start at s[i:] (one per pending heredoc,
        in order). Returns the index after the last terminator line."""
        for ph, tag, strip_tabs, quoted in pending:
            lines = []
            while True:
                if i >= len(s):
                    break                              # unterminated: body to EOF
                j = s.find("\n", i)
                line = s[i:] if j < 0 else s[i:j]
                i = len(s) if j < 0 else j + 1
                probe = line.lstrip("\t") if strip_tabs else line
                if probe == tag:
                    break
                lines.append(line)
            body = "\n".join(lines) + ("\n" if lines else "")
            self.heredocs[ph] = (body, quoted)
            if not quoted:
                # an unquoted tag keeps expansions live: $(..) / backticks in the body
                # run whatever program reads the heredoc.
                self._lift_expansions(body, lifted, depth)
        return i

    def _lift_expansions(self, body, lifted, depth):
        i = 0
        n = len(body)
        while i < n:
            if body.startswith("\\", i):
                i += 2
            elif body.startswith("$((", i):
                _b, i = self._scan(body, i + 3, True, lifted, depth)
                i += 1 if body.startswith(")", i) else 0
            elif body.startswith("$(", i):
                i = self._subst(body, i + 2, lifted, depth, [])
            elif body[i] == "`":
                i = self._backtick(body, i + 1, lifted, depth, [])
            else:
                i += 1

    # ---- pass 2: tokens -> simple commands -> records ---------------------
    def parse_text(self, text, depth, sudo=False):
        if depth > MAX_DEPTH:
            raise ParseError("nesting too deep")
        lifted = []
        processed, _ = self._scan(text, 0, False, lifted, depth)
        self._commands(processed, depth, sudo)
        for body in lifted:
            self._commands(body, depth + 1, sudo)

    def _commands(self, processed, depth, sudo):
        lex = shlex.shlex(processed, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        try:
            tokens = list(lex)
        except ValueError as e:
            raise ParseError(str(e))
        words, redirects = [], []
        for tok in tokens + [";"]:
            if tok and set(tok) <= SEPARATOR_CHARS:
                if words or redirects:
                    self.resolve(words, redirects, depth, sudo)
                words, redirects = [], []
            elif tok and set(tok) <= REDIRECT_CHARS and ("<" in tok or ">" in tok):
                redirects.append([tok, None])     # a redirect operator (never quoted text)
            elif redirects and redirects[-1][1] is None and redirects[-1][0] != "":
                redirects[-1][1] = tok
            else:
                words.append(tok)
        # (trailing ';' flushed the last command)

    def resolve(self, words, redirects, depth, sudo):
        if depth > MAX_DEPTH:
            raise ParseError("nesting too deep")
        self.out.redirects.extend((op, tgt) for op, tgt in redirects)
        words = list(words)
        while words and (ASSIGN.match(words[0]) or words[0] in KEYWORDS):
            words.pop(0)
        if not words:
            return
        if _dynamic(words[0]):
            raise ParseError("program comes from an expansion: %s" % words[0])
        prog = os.path.basename(words[0])
        args = words[1:]
        self.out.records.append(Rec(prog, args, sudo))

        if prog in ("sudo", "doas"):
            self.resolve(_skip_opts(args, SUDO_ARG_OPTS), redirects, depth + 1, True)
        elif prog == "su":
            for k, a in enumerate(args):
                if a in ("-c", "--command") and k + 1 < len(args):
                    self.parse_text(args[k + 1], depth + 1, sudo)
                elif a.startswith("--command="):
                    self.parse_text(a.split("=", 1)[1], depth + 1, sudo)
        elif prog in SHELLS:
            self._shell(args, depth, sudo)
        elif prog == "eval":
            self.parse_text(" ".join(args), depth + 1, sudo)
        elif prog in WRAPPERS:
            with_arg, positional, assignments = WRAPPERS[prog]
            if prog == "env":
                for k, a in enumerate(args):
                    if a in ("-S", "--split-string") and k + 1 < len(args):
                        self.parse_text(args[k + 1], depth + 1, sudo)
                    elif a.startswith("--split-string="):
                        self.parse_text(a.split("=", 1)[1], depth + 1, sudo)
            rest = _skip_opts(args, with_arg, positional, assignments)
            if rest:
                self.resolve(rest, redirects, depth + 1, sudo)
        elif prog in ("source", "."):
            if args:
                self.out.records.append(Rec(os.path.basename(args[0]), args[1:], sudo))
        elif prog == "ssh":
            self._ssh(args, depth, sudo)
        elif prog == "find":
            self._find(args, depth, sudo)
        elif prog in ("watch", "flock", "script"):
            self._runs_string(prog, args, depth, sudo)
        elif prog in ("tmux", "screen"):
            # both run their positional arguments through a shell; fail-safe: parse each
            for a in args:
                if not a.startswith("-") and (" " in a or ";" in a):
                    self.parse_text(a, depth + 1, sudo)
        elif prog == "trap":
            pos = [a for a in args if not a.startswith("-")]
            if pos and pos[0] not in ("", "-"):
                self.parse_text(pos[0], depth + 1, sudo)

        # anything fed on stdin to a shell / ssh / eval-ish program is executed
        if prog in SHELLS or prog in ("ssh", "su", "sudo", "doas", "eval"):
            self._stdin_scripts(args, redirects, depth, sudo)

    def _stdin_scripts(self, args, redirects, depth, sudo):
        for a in args:
            if a in self.heredocs:
                self.parse_text(self.heredocs[a][0], depth + 1, sudo)
        for op, tgt in redirects:
            if op == "<<<" and tgt is not None:
                self.parse_text(tgt, depth + 1, sudo)

    def _shell(self, args, depth, sudo):
        i = 0
        cmdstr = None
        while i < len(args):
            a = args[i]
            if a == "--":
                i += 1
                break
            if (a.startswith("-") and len(a) > 1) or (a.startswith("+") and len(a) > 1):
                if a in ("-o", "+o", "-O", "+O"):
                    i += 2
                    continue
                if not a.startswith("--") and "c" in a[1:]:
                    if i + 1 < len(args):
                        cmdstr = args[i + 1]
                    i = len(args)          # the rest are $0, $1, ...
                    break
                i += 1
                continue
            break
        if cmdstr is not None:
            self.parse_text(cmdstr, depth + 1, sudo)
            return
        positional = args[i:]
        if positional and positional[0] != "-" and positional[0] not in self.heredocs:
            if _dynamic(positional[0]):
                raise ParseError("script comes from an expansion")
            self.out.records.append(Rec(os.path.basename(positional[0]), positional[1:], sudo))

    def _ssh(self, args, depth, sudo):
        rest = _skip_opts(args, SSH_ARG_OPTS)
        if not rest:
            return
        remote = [a for a in rest[1:] if a not in self.heredocs]   # rest[0] = host
        if remote:
            # ssh joins its arguments with spaces and the remote shell re-parses them —
            # so the joined text is exactly what runs there.
            self.parse_text(" ".join(remote), depth + 1, sudo)

    def _find(self, args, depth, sudo):
        i = 0
        while i < len(args):
            if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
                j = i + 1
                cmd = []
                while j < len(args) and args[j] not in (";", "+"):
                    cmd.append(args[j])
                    j += 1
                if cmd:
                    self.resolve(cmd, [], depth + 1, sudo)
                i = j + 1
            else:
                i += 1

    def _runs_string(self, prog, args, depth, sudo):
        if prog == "watch":
            rest = _skip_opts(args, {"-n", "-d", "--interval", "--differences"})
            if rest:
                self.parse_text(" ".join(rest), depth + 1, sudo)
        elif prog == "flock":
            for k, a in enumerate(args):
                if a in ("-c", "--command") and k + 1 < len(args):
                    self.parse_text(args[k + 1], depth + 1, sudo)
                    return
            rest = _skip_opts(args, {"-w", "-E", "--timeout", "--conflict-exit-code"}, 1)
            if rest:
                self.resolve(rest, [], depth + 1, sudo)
        elif prog == "script":
            for k, a in enumerate(args):
                if a in ("-c", "--command") and k + 1 < len(args):
                    self.parse_text(args[k + 1], depth + 1, sudo)
                elif a.startswith("--command="):
                    self.parse_text(a.split("=", 1)[1], depth + 1, sudo)


def parse_command(text):
    """Parse a Bash tool command into the programs it executes. Raises ParseError
    when not confident (the caller then uses the regex fallback)."""
    p = _Parser()
    p.parse_text(text, 0)
    return p.out


# ---- git argument helpers ----------------------------------------------------
GIT_GLOBAL_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
                       "--super-prefix", "--exec-path"}


def git_sub(args):
    """(subcommand, its args, the -C paths) of a git invocation, skipping git's own
    global options. A subcommand from an expansion raises ParseError."""
    i = 0
    cpaths = []
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a in GIT_GLOBAL_WITH_ARG:
            if a == "-C" and i + 1 < len(args):
                cpaths.append(args[i + 1])
            i += 2
        elif a.startswith("-C") and len(a) > 2 and not a.startswith("--"):
            cpaths.append(a[2:])
            i += 1
        elif a.startswith("-") and len(a) > 1:
            i += 1                 # -c<x>, --git-dir=<x>, --no-pager, -p, ...
        else:
            break
    if i >= len(args):
        return None, [], cpaths
    sub = args[i]
    rest = args[i + 1:]
    if _dynamic(sub) or any(_dynamic(a) and (a.startswith("-") or a.startswith("$")
                                             or PLACEHOLDER in a) for a in rest):
        raise ParseError("git argument comes from an expansion")
    return sub, rest, cpaths


def _has_flag(rest, flags):
    for a in rest:
        if a == "--":
            break
        if a.split("=", 1)[0] in flags:
            return True
    return False


def _short_cluster_has(rest, letters):
    for a in rest:
        if a == "--":
            break
        if a.startswith("-") and not a.startswith("--") and len(a) > 1 \
                and any(ch in letters for ch in a[1:]):
            return True
    return False


SYSTEMCTL_STATE = {"start", "stop", "restart", "reload", "enable", "disable"}


def _systemctl_state(args):
    return any(a in SYSTEMCTL_STATE for a in args if not a.startswith("-"))


def _pip_change(rec):
    if re.match(r"^pip\d*$", rec.prog):
        pos = [a for a in rec.args if not a.startswith("-")]
    elif re.match(r"^python\d*(\.\d+)?$", rec.prog) and "-m" in rec.args \
            and rec.args[rec.args.index("-m") + 1:rec.args.index("-m") + 2] == ["pip"]:
        pos = [a for a in rec.args[rec.args.index("-m") + 2:] if not a.startswith("-")]
    else:
        return False
    return bool(pos) and pos[0] in ("install", "uninstall")


def _redirect_writes(op, target):
    """True for a redirect that writes a file (not an fd dup like 2>&1 / >&2)."""
    if ">" not in op:
        return False
    if op == ">&" and target is not None and (target.isdigit() or target == "-"):
        return False                       # 2>&1, >&2, >&-
    return True


# ---- role predicates over the parsed records ----------------------------------
def master_violation(parsed):
    for op, tgt in parsed.redirects:
        if _redirect_writes(op, tgt):
            return "redirect append (>>)" if op == ">>" else "redirect write (>)"
    for rec in parsed.records:
        p = rec.prog
        if p in ("sudo", "doas") or rec.sudo:
            return "sudo"
        if p == "git":
            sub, rest, _ = git_sub(rec.args)
            if sub in ("commit", "push", "add", "reset", "rebase", "merge", "cherry-pick",
                       "revert", "stash", "clean", "restore"):
                return "git " + sub
            if sub == "checkout" and not _has_flag(rest, {"-b", "-B", "--orphan"}):
                return "git checkout (non -b)"
            if sub == "switch" and not _has_flag(rest, {"-c", "-C", "--create",
                                                        "--force-create", "--orphan"}):
                return "git switch (non -c)"
        if _pip_change(rec):
            return "pip package change"
        if p in ("rm", "mv", "cp", "chmod", "chown", "tee", "wget"):
            return p
        if p == "curl" and (_has_flag(rec.args, {"-o", "-O", "--output", "--remote-name",
                                                 "--remote-name-all", "--output-dir"})
                            or _short_cluster_has(rec.args, "oO")):
            return "curl -o (write file)"
        if p == "systemctl" and _systemctl_state(rec.args):
            return "systemctl state change"
    return None


def feature_violation(parsed):
    for rec in parsed.records:
        p = rec.prog
        if p in ("sudo", "doas") or rec.sudo:
            return "sudo"
        if p == "systemctl" and _systemctl_state(rec.args):
            return "systemctl state change (deploy)"
        if p != "git":
            continue
        sub, rest, _ = git_sub(rec.args)
        if sub == "push":
            return "git push"
        if sub == "merge":
            return "git merge"
        if sub == "worktree" and (not rest or rest[0] != "list"):
            return "git worktree"
        if sub == "branch" and (_has_flag(rest, {"--delete", "--move"})
                                or _short_cluster_has(rest, "dDmM")):
            return "git branch delete/rename"
        if sub == "switch" and not _has_flag(rest, {"-c", "-C", "--create", "--force-create",
                                                    "--orphan"}):
            return "git switch (leaving your branch)"
        if sub == "checkout" and not _has_flag(rest, {"-b", "-B", "--orphan"}) \
                and "--" not in rest:
            return "git checkout (leaving your branch)"
    return None


def integrator_violation(parsed):
    """Returns (kind, label) or None; kind in force-push / rebase / merge."""
    for rec in parsed.records:
        if rec.prog != "git":
            continue
        sub, rest, _ = git_sub(rec.args)
        if sub == "push" and (_has_flag(rest, {"--force", "--force-with-lease",
                                               "--force-if-includes"})
                              or any(a.startswith("--force-with-lease=") for a in rest)
                              or _short_cluster_has(rest, "f")
                              or any(a.startswith("+") for a in rest)):
            return "force-push"
        if sub == "rebase":
            return "rebase"
        if sub == "merge" and "--ff-only" not in rest \
                and not _has_flag(rest, {"--abort", "--continue", "--quit"}):
            return "merge"
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
    # The fallback role patterns match `git push`, `git merge`, ... — so `git -C <dir>
    # push` (or `git -c k=v push`) must read the same to them. The raw command is kept
    # for the pieces that care WHERE it acts (foreign_repo_target, is_ship_script).
    raw_command = command
    command = GIT_OPTS_PREFIX.sub("git ", command)

    # Parse what the command executes. parsed=None => the regex fallback lists are
    # used for every check below (fail-safe: never less strict than before).
    parsed = None
    if tool_name == "Bash" and raw_command:
        try:
            parsed = parse_command(raw_command)
        except ParseError:
            parsed = None
        except Exception:
            parsed = None

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
            try:
                label = master_violation(parsed) if parsed is not None \
                    else match(MASTER_BASH_PATTERNS, command)
            except ParseError:
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
        try:
            other = foreign_repo_target_parsed(parsed, cwd) if parsed is not None \
                else foreign_repo_target(raw_command, cwd)
        except ParseError:
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
            kind = None
            if parsed is not None:
                try:
                    kind = integrator_violation(parsed)
                except ParseError:
                    parsed = None
            if parsed is None:
                if INTEGRATOR_FORCE_PUSH.search(command):
                    kind = "force-push"
                elif INTEGRATOR_REBASE.search(command):
                    kind = "rebase"
                elif GIT_MERGE.search(command) and not MERGE_FF_ONLY.search(command) \
                        and not MERGE_NON_OP.search(command):
                    kind = "merge"
            if kind == "force-push":
                block(
                    "Blocked: the integrator never force-pushes.\n"
                    f"Command: {command}\n"
                    "Fix: use a plain fast-forward push. If a branch won't "
                    "fast-forward, its feature worker must rebase it first."
                )
            if kind == "rebase":
                block(
                    "Blocked: the integrator does not rebase. Integration is "
                    "fast-forward-only.\n"
                    f"Command: {command}\n"
                    "Fix: if a branch isn't current with working, tell its "
                    "feature worker to rebase after you approve with YES REBASE."
                )
            if kind == "merge":
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
        label = None
        if parsed is not None:
            try:
                label = feature_violation(parsed)
            except ParseError:
                parsed = None
        if parsed is None:
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
