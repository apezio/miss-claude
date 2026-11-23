#!/usr/bin/env python3
"""room-stats — does the room save work? Read-only, from logs that already exist.

    room-stats.py [--day YYYY-MM-DD]      (default: every day in the room feed)

Per shipped task (a room "X shipped: RESULT: SHIPPED" event), one line: the builder, the
commit, minutes from its first test request to the ship, and its tokens from its task brief
(its latest handoff) to the ship. Then per agent per day: tokens, cache rebuilds (a turn
that writes more than REBUILD tokens of cache: a full context re-read) and cold-cache holds.

Sources: room.json (MISSION_ROOM_DIR) and each member's Claude transcripts
(~/.claude/projects/<munged console cwd>/**/*.jsonl, message.usage). Writes nothing.
"""
import argparse
import datetime
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import app  # noqa: E402  room data, mission titles/roles and console cwds live there

REBUILD = 50000
SHIPPED_RE = re.compile(r"^(.+?) shipped: RESULT: SHIPPED\b.*?working at ([0-9a-f]{7,40})")
COLD_RE = re.compile(r"^(.+?)'s prompt cache expired, so the cold-cache guard is holding")
TEST_RE = re.compile(r"\btest", re.I)
KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def day(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def label_map(room):
    """Shown label -> member key. Events name a member by its title at the time, so a
    member renamed since is matched only by its current title or folder name."""
    out = {}
    for n in room["members"]:
        out[n] = n
        out[app.mission_title(n)] = n
    return out


def ships(room, labels, testers):
    """[(ts, member, commit, start, start ts, first test request msg or None)].

    A task starts after the builder's previous ship, or its ship ask before the one
    this ship answers (not every ship was announced in the room, and tasks also come
    as plain chat); its brief is the latest handoff to it after that, if any. An ask
    that only replaces an earlier one cuts the task short there."""
    msgs, out = room["messages"], []
    for i, m in enumerate(msgs):
        hit = m["from"] == "room" and m["type"] == "event" and SHIPPED_RE.match(m["text"])
        who = hit and labels.get(hit.group(1))
        if not who:
            continue
        before = msgs[:i]
        marks = [("ship", b) for b in before if b["from"] == "room" and b["type"] == "event"
                 and labels.get((SHIPPED_RE.match(b["text"]) or [None, None])[1]) == who]
        asks = [b for b in before if b["from"] == who and b["type"] == "ship"]
        marks += [("ask", b) for b in asks[-2:-1]]
        kind, mark = (max(marks, key=lambda x: x[1]["ts"]) if marks
                      else ("feed", dict(msgs[0], ts=msgs[0]["ts"] - 1)))
        brief = next((b for b in reversed(before) if b["type"] == "handoff" and who in b["to"]
                      and b["from"] not in testers and b["ts"] > mark["ts"]), None)
        if brief:
            kind, mark = "brief", brief
        test = next((t for t in before if t["from"] == who and t["ts"] > mark["ts"]
                     and any(x in testers for x in t["to"])
                     and (t["type"] == "handoff" or TEST_RE.search(t["text"]))), None)
        out.append((m["ts"], who, hit.group(2)[:7], "%s #%d" % (kind, mark["id"]),
                    mark["ts"], test))
    return out


def cold_holds(room, labels):
    """{(member, day): holds} from the room's cold-cache events."""
    out = {}
    for m in room["messages"]:
        hit = m["from"] == "room" and COLD_RE.match(m["text"])
        if hit:
            key = (labels.get(hit.group(1), hit.group(1)), day(m["ts"]))
            out[key] = out.get(key, 0) + 1
    return out


def turns(pdir, since):
    """[(ts, usage)] for every API call in the transcripts under `pdir` (subagents too),
    one per message id: a reply split into several entries repeats its usage."""
    seen = {}
    for root, _dirs, files in os.walk(pdir):
        for f in files:
            path = os.path.join(root, f)
            if not f.endswith(".jsonl") or os.path.getmtime(path) < since:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                        msg, ts = e["message"], e["timestamp"]
                        usage = msg["usage"]
                    except (ValueError, KeyError, TypeError):
                        continue
                    t = datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=datetime.timezone.utc).timestamp()
                    seen[(path, msg.get("id") or e.get("uuid"))] = (t, usage)
    return [v for v in seen.values() if v[0] >= since]


def total(us):
    return [sum(u.get(k) or 0 for _t, u in us) for k in KEYS]


def fmt(n):
    for div, unit in ((1e6, "M"), (1e3, "k")):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return str(n)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", help="only this day (YYYY-MM-DD, local time)")
    a = ap.parse_args()
    room = app.room_read()
    if not room["messages"]:
        print("The room feed is empty.")
        return 0
    labels = label_map(room)
    testers = set(n for n in room["members"] if app.mission_role(n) == "tester")
    t0 = room["messages"][0]["ts"]
    calls = {}
    for n in room["members"]:
        cwd, remote = app._console_cwd_guess(n)
        calls[n] = [] if remote or not cwd else turns(app._project_dir_for_cwd(cwd), t0)

    print("Ships (test = first test request -> ship; tokens = task start -> ship:"
          " input / cache-write / cache-read / output)")
    for ts, who, commit, start, start_ts, test in ships(room, labels, testers):
        if a.day and day(ts) != a.day:
            continue
        tok = total([c for c in calls[who] if start_ts <= c[0] <= ts])
        print("  %s  %-14s %s  test %-5s  from %-10s  %s" % (
            datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
            app.mission_title(who)[:14], commit,
            "%dm" % ((ts - test["ts"]) / 60) if test else "-",
            start, " / ".join(fmt(x) for x in tok)))

    print("\nPer agent per day (tokens: total = input / cache-write / cache-read / output;"
          " rebuilds = turns writing > %dk cache)" % (REBUILD // 1000))
    cold = cold_holds(room, labels)
    days = sorted(set(day(c[0]) for cs in calls.values() for c in cs)
                  | set(d for _n, d in cold))
    for d in days:
        if a.day and d != a.day:
            continue
        for n in room["members"]:
            us = [c for c in calls[n] if day(c[0]) == d]
            holds = cold.get((n, d), 0)
            if not us and not holds:
                continue
            tok = total(us)
            rebuilds = sum(1 for _t, u in us if (u.get(KEYS[1]) or 0) > REBUILD)
            print("  %s  %-14s %7s = %s   rebuilds %d   cold holds %d" % (
                d, app.mission_title(n)[:14], fmt(sum(tok)),
                " / ".join(fmt(x) for x in tok), rebuilds, holds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
