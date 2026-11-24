"""scripts/room-relay.py: two rooms linked once each way, no echo, no duplicates after a
restart, ship asks and approval phrases stay home. The far room is reached through a
local shell transport (ROOM_RELAY_TRANSPORT), the same miss-room.py path ssh would run.

    python3 -m unittest tests/test_room_relay.py
"""
import importlib.util, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
KEYS = ("MISSIONS_DIR", "MISSION_ROOM_DIR", "ROOM_RELAY_HOST", "ROOM_RELAY_NEAR", "ROOM_RELAY_FAR",
        "ROOM_RELAY_FAR_REPO", "ROOM_RELAY_TRANSPORT", "ROOM_RELAY_STATE")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RelayTest(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in KEYS}
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="relay-"))
        far_m, far_r = os.path.join(self.tmp, "far-missions"), os.path.join(self.tmp, "far-room")
        near_m, near_r = os.path.join(self.tmp, "near-missions"), os.path.join(self.tmp, "near-room")
        for d in ("fcoord", "cat"):
            os.makedirs(os.path.join(far_m, d))
        os.makedirs(os.path.join(near_m, "ncoord"))

        os.environ.update(MISSIONS_DIR=far_m, MISSION_ROOM_DIR=far_r)
        self.far = _load("app_relay_far", os.path.join(REPO, "app.py"))
        self.far._run_tmux = lambda *a, capture=False: (1, "")
        with self.far.room_edit() as r:
            r["name"] = "Server"
            for m in ("fcoord", "cat"):
                self.far.room_set_member(r, m, True)
            self.far._room_take_seat(r, "fcoord")

        os.environ.update(
            MISSIONS_DIR=near_m, MISSION_ROOM_DIR=near_r, ROOM_RELAY_NEAR="connie",
            ROOM_RELAY_FAR="cat", ROOM_RELAY_FAR_REPO=REPO,
            ROOM_RELAY_STATE=os.path.join(self.tmp, "state.json"),
            ROOM_RELAY_TRANSPORT="env MISSIONS_DIR=%s MISSION_ROOM_DIR=%s bash -c" % (far_m, far_r))
        os.environ.pop("ROOM_RELAY_HOST", None)
        sys.modules.pop("app", None)            # the relay's own `import app` reads the near env
        self.mod = _load("room_relay_test", os.path.join(REPO, "scripts", "room-relay.py"))
        self.near = self.mod.app
        self.near._run_tmux = lambda *a, capture=False: (1, "")
        with self.near.room_edit() as r:
            r["name"] = "Mac"
            self.near.room_set_member(r, "ncoord", True)
            self.near._room_take_seat(r, "ncoord")
        self.relay = self.mod.Relay()
        self.relay.ensure_near_member()
        self.relay.once()                       # first pass: baseline, nothing old crosses

    def tearDown(self):
        sys.modules.pop("app", None)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, app, sender, text, to, kind="chat", **kw):
        with app.room_edit() as r:
            msg, err = app.room_post(r, sender, text, to, kind, **kw)
        self.assertEqual(err, "")
        return msg

    def relayed(self, app, sender):
        return [m for m in app.room_read()["messages"] if m["from"] == sender]

    def test_near_standin_added(self):
        self.assertIn("connie", self.near.room_read()["members"])

    def test_round_trip_once_no_echo_no_restart_dupes(self):
        self.post(self.far, "fcoord", "hello Cat", ["cat"])
        self.relay.once()
        self.relay.once()
        got = self.relayed(self.near, "connie")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["to"], ["ncoord"])
        self.assertEqual(got[0]["text"], "[via Server] fcoord: hello Cat")

        self.post(self.near, "ncoord", "hi back", ["connie"])
        self.relay.once()
        self.relay.once()
        back = self.relayed(self.far, "cat")
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["to"], ["fcoord"])
        self.assertEqual(back[0]["text"], "[via Mac] ncoord: hi back")

        restarted = self.mod.Relay()
        restarted.once()
        self.assertEqual(len(self.relayed(self.near, "connie")), 1)
        self.assertEqual(len(self.relayed(self.far, "cat")), 1)

    def test_ship_asks_and_approval_phrases_stay_home(self):
        self.post(self.far, "fcoord", "please ship", ["cat"], "ship", commit="abc1234")
        self.post(self.far, "operator", "YES SHIP", ["cat"])
        self.post(self.near, "operator", "yes deploy", ["connie"])
        self.relay.once()
        self.assertEqual(self.relayed(self.near, "connie"), [])
        self.assertEqual(self.relayed(self.far, "cat"), [])

    def test_failed_far_post_is_retried_not_skipped(self):
        self.post(self.near, "ncoord", "first", ["connie"])
        real = self.relay.far_post
        def down(kind, text):
            raise RuntimeError("far machine unreachable")
        self.relay.far_post = down
        self.assertRaises(RuntimeError, self.relay.once)
        self.relay.far_post = real
        self.relay.once()
        self.assertEqual([m["text"] for m in self.relayed(self.far, "cat")], ["[via Mac] ncoord: first"])


if __name__ == "__main__":
    unittest.main()
