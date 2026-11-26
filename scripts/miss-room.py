#!/usr/bin/env python3
"""miss-room — post to the Miss Claude room from inside a mission console.

    miss-room.py post [--to NAME[,NAME]|operator] [--type KIND] [--option A ...] "text"
                                                              (text "-" = stdin)
    miss-room.py read [N] [--from ID] [--grep TEXT]   (reaches into room-history.jsonl)
    miss-room.py members
    miss-room.py add NAME | remove NAME                       (coordinator only)
    miss-room.py spawn NAME [--as ROLE] --brief "task" [--repo PATH]   (coordinator only)
    miss-room.py ship NAME                                    (coordinator only)

The sender is this console's mission ($MISSION_NAME). The dashboard's courier types each
message into its target's console once that console is idle (docs/internal/ROOM.md);
--type status is not typed: it comes along with the target's next typed message.
--type question --to operator pins a question on the operator's room page until they answer
(it gets an id like Q12); each --option "…" is a one-tap answer for them. Answered some
other way (in plain chat)? --closes Q12 on any post unpins your own question.
--type ship --commit SHA asks the operator for YES SHIP for that commit (lights their ship
button): post it only when the work is finished and tested, never for "I'll post when it's
ready". A tester's verdict: --type result --verdict pass|fail --commit <the commit tested>;
a pass to the builder lights the operator's ship button, and their tap ships that commit.
`clear NAME [--save]` (coordinator only) clears any agent once idle; --save asks it to
write HANDOFF.md first.
A delivery stopped by the cold-cache guard (the agent's prompt cache expired) is held,
and the coordinator is told: `clear NAME` (bare /clear, then the message again) or
`resume NAME` (type it again: the agent carries on with it, re-reading its context).
--type saved answers the room's save request before a /clear: post it once HANDOFF.md is
written (the /clear waits for it, and for the note to have changed).
`ship NAME` (coordinator only) does what the operator's 🚀 tap on NAME does, while the
operator's "may ship" switch on the room page is on, and only on a tester's PASS that lit
NAME's 🚀. Only the operator flips that switch, on the room page; this script can't.
"""
import argparse
import importlib.util
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import app  # noqa: E402  the room's data model lives in the dashboard module


def _me():
    name = os.environ.get("MISSION_NAME", "")
    return name if app.safe_name(name) else ""


def _coordinator():
    me = _me()
    if me and me == app.room_read()["coordinator"]:
        return me
    print("Only the room's coordinator can do that.")
    return ""


def cmd_post(a):
    me = _me()
    if not me:
        print("Run this inside a mission console ($MISSION_NAME is not set).")
        return 1
    text = sys.stdin.read() if a.text == ["-"] else " ".join(a.text)
    to = [t.strip() for t in (a.to or "").split(",") if t.strip()]
    with app.room_edit() as room:
        if me not in room["members"]:
            print("%s is not in the room. Nothing posted." % me)
            return 1
        msg, err = app.room_post(room, me, text, app.room_resolve(room, to), a.type, a.option,
                                  a.closes, a.verdict, a.commit)
    if err:
        print(err + " Nothing posted.")
        return 1
    print("Posted #%d%s%s." % (msg["id"], (" to " + ", ".join(map(app.room_label, msg["to"])))
                                if msg["to"] else " (shown to the operator)",
                                " as %s, pinned until answered" % msg["qid"] if msg.get("qid") else ""))
    if msg.get("closes"):
        print("Closed %s." % ", ".join(msg["closes"]))
    held = [t for t, st in msg["status"].items() if st == "held"]
    if held:
        print("Held for %s: 5 back-and-forths with no word from the operator. It goes "
              "through once the operator posts. Don't resend." % ", ".join(held))
    return 0


def cmd_read(a):
    """The last N messages; reaches into the archive (app.room_history) past room.json.
    --from ID: the N messages from ID on. --grep TEXT: the last N whose text, sender or
    recipients contain TEXT (any case)."""
    room, n = app.room_read(), a.n or 20
    if a.start is None and a.grep is None:
        msgs = app.room_history(room, n=n)
    else:
        msgs = app.room_history(room)
        if a.grep is not None:
            g = a.grep.lower()
            msgs = [m for m in msgs if g in " ".join([m["text"], m["from"]] + m["to"]).lower()]
        msgs = [m for m in msgs if m["id"] >= a.start][:n] if a.start is not None else msgs[-n:]
    labels = {}                  # room_label reads files (room.json for "operator"): once per name
    label = lambda x: labels[x] if x in labels else labels.setdefault(x, app.room_label(x))
    for m in msgs:
        to = (" -> " + ", ".join(map(label, m["to"]))) if m.get("to") else ""
        text = m["text"].splitlines()[0] if m["type"] == "brief" else m["text"]
        print("#%d %s %s%s (%s): %s" % (m["id"], time.strftime("%H:%M", time.localtime(m["ts"])),
                                       label(m["from"]), to, app.room_kind_label(m), text))
    return 0


def cmd_members(_a):
    room = app.room_read()
    if not room["members"]:
        print("Nobody is in the room.")
    for n in room["members"]:
        title = app.room_label(n)
        role = app.mission_role(n)
        print("%s %s%s%s — %s" % ("*" if n == room["coordinator"] else "-", title,
                                  "" if title == n else " (folder %s)" % n,
                                  " [%s]" % role if role else "",
                                  app.mission_activity(n, app.session_running(n))))
    return 0


def cmd_member(a, on):
    if not _coordinator():
        return 1
    with app.room_edit() as room:
        err = app.room_set_member(room, a.name, on)
    print(err or ("%s %s the room." % (a.name, "joined" if on else "left")))
    return 1 if err else 0


def cmd_cold(a):
    """clear / resume: the coordinator's answer to a delivery the cold-cache guard holds.
    `clear` with nothing held clears the agent anyway (room_clear, --save first)."""
    if not _coordinator():
        return 1
    done = "%s will get the held message again and carry on with it." % a.name
    with app.room_edit() as room:
        names = app.room_resolve(room, [a.name])
        held = names and any(m.get("status", {}).get(names[0]) == "cold" for m in room["messages"])
        if not names:
            err = "Not in the room: %s" % a.name
        elif a.cmd == "clear" and not held:
            err = app.room_clear(room, names[0], a.save)
            done = ("%s will save its state, then get a bare /clear once idle." if a.save
                    else "%s will get a bare /clear once idle.") % a.name
        else:
            err = app.room_cold_go(room, names[0], a.cmd)
            if a.cmd == "clear":
                done = "%s will get a bare /clear once idle, then the held message again." % a.name
    print(err or done)
    return 1 if err else 0


def cmd_ship(a):
    me = _me()
    with app.room_edit() as room:
        name = app.room_resolve(room, [a.name])[0]
        err = app.room_coord_ship(room, me, name)
    if err:
        print("Nothing shipped: %s." % err)
        return 1
    print("Shipping %s: miss-ship.py runs in tmux session ship-%s; its RESULT comes to the "
          "room. The feed shows it as your ship on the operator's delegation." % (a.name, name))
    return 0


def cmd_spawn(a):
    me = _coordinator()
    if not me:
        return 1
    if not app.safe_name(a.name):
        print("Invalid mission name: %s" % a.name)
        return 1
    if os.path.exists(app.mission_path(a.name)):
        print('Mission "%s" already exists — use `add` instead.' % a.name)
        return 1
    if a.role and a.role not in app.agent_defs():
        print("Unknown role %s (roles: %s)." % (a.role, ", ".join(app.agent_defs()) or "none"))
        return 1
    if app.ROOM_APPROVAL_RE.search(a.brief):
        print("The brief may not contain an approval phrase. Nothing created.")
        return 1
    # The Director's creator posts to the dashboard's own /create or /spawn routes.
    spec = importlib.util.spec_from_file_location(
        "miss_director", os.path.join(REPO, "scripts", "miss-director.py"))
    md = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(md)
    name, err = md._create_mission(a.name, "dev" if a.repo else "ops", a.repo or "")
    if err:
        print(err)
        return 1
    if a.role:                       # before the console starts: its hook prints the role
        meta = app.read_mission_meta(name) or {}
        meta["agent_role"] = a.role
        app.write_mission_meta(name, meta)
    started = app.start_console_headless(name)
    with app.room_edit() as room:
        app.room_set_member(room, name, True)
        app.room_post(room, me, a.brief, [name], "handoff")
    print("Created %s; it is in the room and gets your brief once its console is idle.%s"
          % (name, (" Console: " + started) if started else ""))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="miss-room", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("post", help="post a message")
    pp.add_argument("--to", help="member name(s) or title(s), comma-separated, or 'operator'")
    pp.add_argument("--type", default="chat", help="question, handoff (a task brief: the agent's current task), "
                         "status, chat, ship (finished, tested work: ask for YES SHIP; needs --commit), "
                         "result (a tester's verdict, needs --verdict and --commit: the room then clears the tester), "
                         "or saved (HANDOFF.md written, before a /clear)")
    pp.add_argument("--option", action="append", default=[],
                    help="with --type question: a one-tap answer for the operator (repeatable)")
    pp.add_argument("--closes", action="append", default=[], metavar="QID",
                    help="unpin your own open question (e.g. Q12), answered some other way")
    pp.add_argument("--verdict", default="", choices=("", "pass", "fail"),
                    help="with --type result: pass or fail")
    pp.add_argument("--commit", default="",
                    help="with --type result: the commit tested; with --type ship: the commit to ship")
    pp.add_argument("text", nargs="+")
    pr = sub.add_parser("read", help="the last N messages (default 20), older ones included")
    pr.add_argument("n", nargs="?", type=int, default=0)
    pr.add_argument("--from", dest="start", type=int, metavar="ID",
                    help="the N messages from message ID on")
    pr.add_argument("--grep", metavar="TEXT", help="only messages containing TEXT (any case)")
    sub.add_parser("members", help="who is in the room")
    for verb in ("add", "remove"):
        sub.add_parser(verb).add_argument("name")
    pc = sub.add_parser("clear", help="bare /clear once idle; held by the cold-cache guard: "
                        "then the held message again")
    pc.add_argument("name")
    pc.add_argument("--save", action="store_true",
                    help="nothing held: ask it to write HANDOFF.md first")
    sub.add_parser("resume", help="held by the cold-cache guard: type it again, carry on"
                   ).add_argument("name")
    sub.add_parser("ship", help="ship what a tester's PASS lit, while the operator's switch "
                   "lets the coordinator (coordinator only)").add_argument("name")
    ps = sub.add_parser("spawn", help="start a new mission in the room")
    ps.add_argument("name")
    ps.add_argument("--brief", required=True)
    ps.add_argument("--repo", help="git repo path: makes it a dev mission")
    ps.add_argument("--as", dest="role", help="agent role: agents/<ROLE>.md")
    a = p.parse_args(argv)
    if a.cmd == "post":
        return cmd_post(a)
    if a.cmd == "read":
        return cmd_read(a)
    if a.cmd == "members":
        return cmd_members(a)
    if a.cmd in ("add", "remove"):
        return cmd_member(a, a.cmd == "add")
    if a.cmd in ("clear", "resume"):
        return cmd_cold(a)
    if a.cmd == "ship":
        return cmd_ship(a)
    return cmd_spawn(a)


if __name__ == "__main__":
    sys.exit(main())
