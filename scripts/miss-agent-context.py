#!/usr/bin/env python3
"""SessionStart hook: print this mission's agent role (agents/<role>.md) and, for a
room member, the room's join brief into context. With `prompt` (UserPromptSubmit): the
member's unread room posts (app.room_take_unread), once.

mission.json `agent_role` names the role; the room's spawn picker, `miss-room.py spawn
--as` and the room's ⚙ window set it. SessionStart fires on startup, resume, /clear and
compaction, so the role survives all four. A role changed mid-session is told to the
console by the room at once; this hook only covers the next session start.

`--model-args` instead prints `--model <m> --effort <e>` from the role file's header,
for the launchers (console-session.sh, scripts/claude-miss) to start that session on the
role's model; nothing for no role, no header or an unsafe value. `--cold-cache` prints
the header's `cold_cache:` value ("off" for the coordinator: the launchers then export
COLD_CACHE_GUARD=off, so ~/.claude/cold-cache never holds its prompts), else nothing.

The room brief (app.room_brief, the note a member gets on joining) lets a session that
was cleared, compacted or restarted find the room again. Prints nothing for a mission
with no role outside the room. Stdlib only; exit 0 always.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(REPO, "agents")


def read_role(data_dir):
    """(role, path, header, body) of this mission's role file, or None."""
    try:
        with open(os.path.join(data_dir, "mission.json"), encoding="utf-8") as fh:
            role = json.load(fh).get("agent_role") or ""
    except (OSError, ValueError, AttributeError):
        return None
    if not re.match(r"^[a-z][a-z0-9-]{0,29}$", role):
        return None
    path = os.path.join(AGENTS_DIR, role + ".md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    parts = text.split("---\n", 2)
    if len(parts) == 3 and not parts[0].strip():
        return role, path, parts[1], parts[2]
    return role, path, "", text


def model_args(data_dir):
    found = read_role(data_dir)
    if not found:
        return ""
    head = dict(re.findall(r"^(model|effort):[ \t]*(\S+)[ \t]*$", found[2], re.M))
    # The values land in a `script -c` command string: plain tokens only.
    ok = {k: v for k, v in head.items() if re.match(r"^[A-Za-z0-9._\[\]-]{1,64}$", v)}
    if "model" not in ok:
        return ""
    return " ".join("--%s %s" % (k, ok[k]) for k in ("model", "effort") if k in ok)


def cold_cache(data_dir):
    found = read_role(data_dir)
    m = found and re.search(r"^cold_cache:[ \t]*(off)[ \t]*$", found[2], re.M)
    return m.group(1) if m else ""


def role_block(data_dir):
    found = read_role(data_dir)
    if not found:
        return ""
    role, path, _, body = found
    return ("== AGENT ROLE: %s (%s; the room's ⚙ can change it) ==\n%s\n"
            % (role, path, body.strip()))


def room_block(name):
    try:
        sys.path.insert(0, REPO)
        import app
        room = app.room_read()
        if name not in room["members"]:
            return ""
        return "== ROOM (you are a member; this is your join brief) ==\n%s\n" % app.room_brief(room, name)
    except Exception:          # no room, or an unreadable one: the role still prints
        return ""


def unread_block(name):
    """`prompt` mode (UserPromptSubmit): the room posts that were not typed into this
    console (status posts, copies of results), once each, with the prompt that a typed
    message or the operator starts."""
    try:
        sys.path.insert(0, REPO)
        import app
        if name not in app.room_read()["members"]:
            return ""
        with app.room_edit() as room:
            msgs = app.room_take_unread(room, name)
        return "".join("== ROOM: unread post #%d (not typed into this console) ==\n%s\n"
                       % (m["id"], app.room_delivery_text(m)) for m in msgs)
    except Exception:
        return ""


def main():
    data_dir = os.environ.get("MISSION_DATA_DIR", "")
    name = os.environ.get("MISSION_NAME") or os.path.basename(data_dir.rstrip("/"))
    if sys.argv[1:2] == ["--model-args"]:
        sys.stdout.write(model_args(data_dir) if data_dir else "")
        return 0
    if sys.argv[1:2] == ["--cold-cache"]:
        sys.stdout.write(cold_cache(data_dir) if data_dir else "")
        return 0
    if sys.argv[1:] == ["prompt"]:
        sys.stdout.write(unread_block(name) if name else "")
        return 0
    if data_dir:
        sys.stdout.write(role_block(data_dir))
    if name:
        sys.stdout.write(room_block(name))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
