#!/usr/bin/env python3
"""room-relay — link this machine's room with a room on another machine.

    room-relay.py            # loop forever (what the Mac's LaunchAgent runs)
    room-relay.py once       # one pass, then exit

Runs on the machine that can reach the other one. Each pass reads the far room.json over
the transport (one ssh connection, kept open by ControlMaster) and the near room here:
- a far message TO the far stand-in, not FROM it, is posted here as the near stand-in to
  this room's coordinator;
- a near message to the near stand-in, not from it, is posted there the same way.
A relayed post is sent BY a stand-in, and only posts TO a stand-in are read, so nothing
relayed comes back. Ship asks and approval phrases are never relayed (only the operator
approves, each in their own room). The last ids relayed sit in a state file, so a restart
sends nothing twice; the first run starts at the newest message (no history).

Settings: env, else the untracked local.env at the repo root (docs/internal/ROOM.md,
"Link two rooms"). The code holds no host or name.
    ROOM_RELAY_HOST        ssh host alias of the far machine
    ROOM_RELAY_NEAR        the near stand-in: the far coordinator's seat in this room
    ROOM_RELAY_FAR         the far stand-in: this coordinator's seat in the far room
    ROOM_RELAY_FAR_REPO    the far Miss Claude checkout (~/ allowed)
    ROOM_RELAY_TRANSPORT   optional: the command that runs a far shell command given as its
                           last argument (default: ssh with ControlMaster to the host)
    ROOM_RELAY_STATE       optional: state file (default ~/.miss-claude/room-relay.json)
    ROOM_RELAY_EVERY       optional: seconds between passes (default 7)
"""
import json
import os
import shlex
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import app  # noqa: E402  the room's data model lives in the dashboard module

KINDS = ("chat", "status", "question")      # kept as they are; any other kind goes as chat
TIMEOUT = 60


def setting(key, default=""):
    return os.environ.get(key) or app._local_env(key) or default


def log(msg):
    print("%s room-relay: %s" % (time.strftime("%F %T"), msg), flush=True)


def far_path(p):
    """A shell word for `p` that still expands a leading ~/ on the far machine."""
    return '"$HOME"/' + shlex.quote(p[2:]) if p.startswith("~/") else shlex.quote(p)


class Relay:
    def __init__(self):
        self.host = setting("ROOM_RELAY_HOST")
        self.near = setting("ROOM_RELAY_NEAR")
        self.far = setting("ROOM_RELAY_FAR")
        self.far_repo = setting("ROOM_RELAY_FAR_REPO")
        transport = setting("ROOM_RELAY_TRANSPORT")
        self.transport = shlex.split(transport) if transport else [
            "ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=auto",
            "-o", "ControlPath=~/.ssh/miss-relay-%C", "-o", "ControlPersist=10m", self.host]
        self.state_file = setting("ROOM_RELAY_STATE", os.path.expanduser("~/.miss-claude/room-relay.json"))
        missing = [k for k, v in (("ROOM_RELAY_NEAR", self.near), ("ROOM_RELAY_FAR", self.far),
                                  ("ROOM_RELAY_FAR_REPO", self.far_repo)) if not v]
        if not transport and not self.host:
            missing.insert(0, "ROOM_RELAY_HOST")
        if missing:
            raise SystemExit("room-relay: not configured: set %s in local.env" % ", ".join(missing))
        if not (app.safe_name(self.near) and app.safe_name(self.far)):
            raise SystemExit("room-relay: stand-in names must be plain mission names")

    def remote(self, cmd, stdin=""):
        p = subprocess.run(self.transport + [cmd], input=stdin, capture_output=True, text=True,
                           timeout=TIMEOUT)
        if p.returncode:
            raise RuntimeError((p.stderr or p.stdout).strip()[-300:] or "exit %d" % p.returncode)
        return p.stdout

    def far_room(self):
        room = json.loads(self.remote('cat "${MISSION_ROOM_DIR:-$HOME/.miss-claude/room}/room.json"'))
        room.setdefault("messages", [])
        return room

    def far_post(self, kind, text):
        out = self.remote("MISSION_NAME=%s python3 %s/scripts/miss-room.py post --type %s -"
                          % (shlex.quote(self.far), far_path(self.far_repo), kind), text)
        if not out.startswith("Posted"):
            raise RuntimeError(out.strip())

    def near_post(self, kind, text):
        with app.room_edit() as room:
            if self.near not in room["members"]:
                raise RuntimeError("%s is not in the room" % self.near)
            _msg, err = app.room_post(room, self.near, text, [], kind)
        if err:
            raise RuntimeError(err)

    def ensure_near_member(self):
        if self.near in app.room_read()["members"]:
            return
        os.makedirs(app.mission_path(self.near), exist_ok=True)
        with app.room_edit() as room:
            err = app.room_set_member(room, self.near, True)
        if err:
            raise SystemExit("room-relay: could not add %s: %s" % (self.near, err))
        log("added %s to this room" % self.near)

    def load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_state(self, state):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        app.write_text_atomic(self.state_file, json.dumps(state) + "\n")

    @staticmethod
    def label(room, fallback):
        name = room.get("name") or ""
        return name if name and name != "Room" else fallback

    def wanted(self, m, standin):
        """True for a post to `standin` that may cross; skips are logged."""
        if standin not in (m.get("to") or []) or m.get("from") in (standin, "room"):
            return False
        if m.get("type") == "ship" or app.ROOM_APPROVAL_RE.search(m.get("text") or ""):
            log("not relayed: #%d (ship ask or approval phrase)" % m["id"])
            return False
        return True

    def cross(self, state, key, room, standin, post, label, local):
        """Relay `room`'s new posts to `standin` with `post`; the state advances per post,
        so a failure resends only what never got through."""
        msgs = room["messages"]
        top = max((m["id"] for m in msgs), default=0)
        if key not in state or state[key] > top:     # first run, or the room was reset
            state[key] = top
            self.save_state(state)
            return
        for m in msgs:
            if m["id"] <= state[key]:
                continue
            if self.wanted(m, standin):
                kind = m["type"] if m["type"] in KINDS else "chat"
                who = m["from"]
                if who == "operator":
                    who = room.get("operator_name") or "operator"
                elif local:
                    who = app.room_label(who)
                text = "[via %s] %s: %s" % (label, who, m["text"])
                post(kind, text)
                log("relayed #%d from %s" % (m["id"], label))
            state[key] = m["id"]
            self.save_state(state)

    def once(self):
        state = self.load_state()
        far = self.far_room()
        near = app.room_read()
        near_label = self.label(near, socket.gethostname().split(".")[0])
        far_label = self.label(far, self.host or "far room")
        self.cross(state, "far_last", far, self.far, self.near_post, far_label, False)
        self.cross(state, "near_last", near, self.near, self.far_post, near_label, True)


def main(argv):
    relay = Relay()
    relay.ensure_near_member()
    if argv[1:] == ["once"]:
        relay.once()
        return 0
    every = float(setting("ROOM_RELAY_EVERY", "7"))
    log("linking this room with %s every %gs" % (relay.host or "the transport", every))
    while True:
        try:
            relay.once()
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
            log("pass failed, retrying: %s" % e)
        time.sleep(every)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
