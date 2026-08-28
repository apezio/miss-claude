#!/usr/bin/env python3
"""claude-session-args.py — decide a console's resume flags BEFORE launching Claude.

    args=$(python3 scripts/claude-session-args.py <cwd> [session-id])
    claude $args --dangerously-skip-permissions

Prints the session flag(s) for `claude`, space-separated, on ONE line (possibly
empty). It exists because the console launchers used to CHAIN invocations
(`claude --resume X || claude --resume Y || claude --session-id Y`): each failing
attempt still showed the interactive "do you trust this folder?" dialog and exited
before persisting the answer, so opening one mission asked the operator to trust
the same directory up to three times and printed "No conversation found with
session ID" twice. Resolving the same decision from the filesystem means `claude`
runs exactly once.

Claude Code stores a conversation at
`<config>/projects/<slug>/<session-id>.jsonl`, where <config> is $CLAUDE_CONFIG_DIR
(else ~/.claude) and <slug> is the absolute PHYSICAL cwd (symlinks resolved, as the
shell's `pwd -P` reports it — that is what Claude Code slugs) with every
non-alphanumeric character replaced by `-`. That is the whole lookup:

  session id given, transcript exists   ->  --resume <id>
  session id given, no transcript       ->  --session-id <id>   (create it with that id)
  no session id, cwd has any *.jsonl    ->  --continue
  no session id, no history             ->  (nothing — a fresh session)

An id that is not a uuid ([0-9a-fA-F-]{36}) is ignored, i.e. treated as "no session
id". Nothing here ever fails loudly: any unexpected error prints nothing and exits
0, which means "fresh session" — the safe answer, and the callers keep a plain
`claude` fallback anyway. Standard library only.
"""

import os
import re
import sys

UUID_RE = re.compile(r"[0-9a-fA-F-]{36}\Z")


def project_dir(cwd):
    """The transcript directory Claude Code uses for `cwd`."""
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    # realpath, not abspath: a symlinked component would slug differently from the
    # path Claude Code records and every probe would miss.
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    return os.path.join(config, "projects", slug)


def session_args(cwd, sid=None):
    proj = project_dir(cwd)
    if sid and UUID_RE.match(sid):
        if os.path.isfile(os.path.join(proj, sid + ".jsonl")):
            return ["--resume", sid]
        return ["--session-id", sid]
    try:
        has_history = any(n.endswith(".jsonl") for n in os.listdir(proj))
    except OSError:
        has_history = False
    return ["--continue"] if has_history else []


def main(argv):
    if not argv:
        return 0
    print(" ".join(session_args(argv[0], argv[1] if len(argv) > 1 else None)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(0)
