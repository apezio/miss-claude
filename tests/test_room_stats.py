"""scripts/room-stats.py on a made-up room and transcripts.

    python3 -m unittest tests/test_room_stats.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "room-stats.py")
T0 = 1795400000.0       # 2026-11-22 18:13 PST; every event below is within the hour


def _iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


class RoomStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="room-stats-")
        missions = os.path.join(self.tmp, "missions")
        self.wt = os.path.join(self.tmp, "wt-bob")
        for n, meta in (("coord", {"agent_role": "coordinator"}),
                        ("bob", {"agent_role": "builder", "title": "Bobby", "mode": "dev",
                                 "target": {"kind": "local-repo", "path": self.tmp},
                                 "dev": {"worktree": self.wt}}),
                        ("tess", {"agent_role": "tester"})):
            os.makedirs(os.path.join(missions, n))
            with open(os.path.join(missions, n, "mission.json"), "w") as fh:
                json.dump(meta, fh)
        msgs = [
            (1, 0, "coord", ["bob"], "handoff", "Old task."),
            (2, 60, "bob", ["tess"], "handoff", "Test request: old."),
            (3, 120, "room", [], "event", "Bobby shipped: RESULT: SHIPPED — working at "
                                          "aaaaaaa11111, released."),
            (4, 600, "coord", ["bob"], "handoff", "New task."),
            (5, 700, "bob", ["tess"], "chat", "Thanks."),
            (6, 900, "bob", ["tess"], "chat", "Please test commit bbbbbbb."),
            (7, 1000, "room", [], "event", "Bobby shipped: RESULT: BLOCKED — behind."),
            (8, 1100, "room", [], "event", "Bobby's prompt cache expired, so the cold-cache "
                                           "guard is holding room message #6 for it."),
            (9, 1500, "room", [], "event", "Bobby shipped: RESULT: SHIPPED — working at "
                                           "bbbbbbb22222, released."),
        ]
        room = {"members": ["coord", "bob", "tess"], "coordinator": "coord", "next_id": 10,
                "messages": [{"id": i, "ts": T0 + dt, "from": f, "to": to, "type": k,
                              "text": t, "status": {}} for i, dt, f, to, k, t in msgs]}
        os.makedirs(os.path.join(self.tmp, "room"))
        with open(os.path.join(self.tmp, "room", "room.json"), "w") as fh:
            json.dump(room, fh)
        pdir = os.path.join(self.tmp, "home", ".claude", "projects",
                            "".join(c if c.isalnum() else "-" for c in self.wt))
        os.makedirs(os.path.join(pdir, "s1", "subagents"))
        lines = []
        # (msg id, seconds after T0, cache write); every call: in 1, read 1000, out 10
        for mid, dt, cw in (("m1", 100, 60000),     # first task
                            ("m2", 650, 100),       # second task, after its brief
                            ("m2", 651, 100),       # the same reply again: counted once
                            ("m3", 1200, 70000)):   # a rebuild after the cold hold
            lines.append({"type": "assistant", "timestamp": _iso(T0 + dt), "message": {
                "id": mid, "usage": {"input_tokens": 1, "cache_creation_input_tokens": cw,
                                     "cache_read_input_tokens": 1000, "output_tokens": 10}}})
        with open(os.path.join(pdir, "s1.jsonl"), "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in lines[:3]) + "\n")
        with open(os.path.join(pdir, "s1", "subagents", "a.jsonl"), "w") as fh:
            fh.write(json.dumps(lines[3]) + "\nnot json\n")
        env = dict(os.environ, MISSIONS_DIR=missions, HOME=os.path.join(self.tmp, "home"),
                   MISSION_ROOM_DIR=os.path.join(self.tmp, "room"), TZ="America/Los_Angeles")
        r = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.out = r.stdout.splitlines()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def line(self, needle):
        hits = [l for l in self.out if needle in l]
        self.assertEqual(len(hits), 1, "\n".join(self.out))
        return hits[0]

    def test_ship_lines(self):
        first = self.line("aaaaaaa")
        self.assertIn("Bobby", first)
        self.assertIn("test 1m", first)                 # #2 -> #3
        self.assertIn("from brief #1", first)
        self.assertIn("1 / 60.0k / 1.0k / 10", first)   # m1 only
        second = self.line("bbbbbbb")
        self.assertIn("test 10m", second)               # #6 (not the "Thanks.") -> #9
        self.assertIn("from brief #4", second)
        self.assertIn("2 / 70.1k / 2.0k / 20", second)  # m2 once + m3

    def test_day_line(self):
        day = self.line("2026-11-22  Bobby")
        self.assertIn("rebuilds 2", day)
        self.assertIn("cold holds 1", day)
        self.assertIn("3 / 130.1k / 3.0k / 30", day)


if __name__ == "__main__":
    unittest.main()
