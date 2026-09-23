#!/usr/bin/env python3
"""claude-trust-dir.py — pre-accept Claude Code's "do you trust this folder?" dialog.

    python3 scripts/claude-trust-dir.py <dir>

A mission console runs Claude in a directory it has usually never seen (a new
~/missions/<name>/, or a Local-dir target), so Claude opens on the folder-trust
dialog. In the raw terminal that is merely a keypress; in the chat view nothing
shows it, so the console just sits there waiting. Dev consoles never hit it
because their worktree belongs to an already-trusted repo. This marks <dir> as
trusted up front — the same `hasTrustDialogAccepted` flag answering "Yes" writes.

The flag lives in Claude Code's global config, $CLAUDE_CONFIG_DIR/.claude.json
(else ~/.claude.json), under projects[<absolute physical path>]. The file is only
rewritten when the flag is missing, and then atomically (temp file + rename, as
Claude Code itself does). Nothing here fails loudly: any error just leaves the
dialog to be answered by hand. Standard library only.
"""

import json
import os
import sys


def config_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(base, ".claude.json") if base else os.path.expanduser("~/.claude.json")


def trust(path):
    path = os.path.realpath(path)
    cfg = config_path()
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        return
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return
    entry = projects.setdefault(path, {})
    if not isinstance(entry, dict) or entry.get("hasTrustDialogAccepted") is True:
        return
    entry["hasTrustDialogAccepted"] = True
    tmp = f"{cfg}.tmp.miss-trust.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp, os.stat(cfg).st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, cfg)


def main():
    if len(sys.argv) != 2:
        return
    try:
        trust(sys.argv[1])
    except Exception:
        pass


if __name__ == "__main__":
    main()
