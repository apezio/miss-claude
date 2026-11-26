"""The room's history archive: room-history.jsonl beside room.json.

    python3 -m unittest tests/test_room_history.py
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, textwrap, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _load_app():
    spec = importlib.util.spec_from_file_location("app_room_history_test", os.path.join(REPO, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._run_tmux = lambda *a, capture=False: (1, "")   # never the live tmux server
    mod.ROOM_KEEP = 10                                    # small, so posts spill quickly
    return mod


class HistoryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="roomhist-"))
        self.missions = os.path.join(self.tmp, "missions")
        for n in ("coord", "alpha"):
            os.makedirs(os.path.join(self.missions, n))
        self._env = {k: os.environ.get(k) for k in ("MISSIONS_DIR", "MISSION_ROOM_DIR")}
        os.environ["MISSIONS_DIR"] = self.missions
        os.environ["MISSION_ROOM_DIR"] = os.path.join(self.tmp, "room")
        self.app = _load_app()
        with self.app.room_edit() as r:
            for m in ("coord", "alpha"):
                self.app.room_set_member(r, m, True)
            self.app._room_take_seat(r, "coord")
        self.hist = os.path.join(self.tmp, "room", "room-history.jsonl")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, n, text="msg %d"):
        for i in range(n):
            with self.app.room_edit() as r:
                msg, err = self.app.room_post(r, "operator", text % i if "%d" in text else text, ["alpha"])
                self.assertEqual(err, "")

    def ids(self, msgs):
        return [m["id"] for m in msgs]

    def lines(self):
        with open(self.hist, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh.read().splitlines()]


class Append(HistoryBase):
    def test_no_archive_until_room_json_is_full(self):
        self.post(self.app.ROOM_KEEP - len(self.app.room_read()["messages"]))
        self.assertFalse(os.path.exists(self.hist))

    def test_what_leaves_room_json_lands_in_the_archive_once(self):
        self.post(25)
        room = self.app.room_read()
        last = room["next_id"] - 1
        self.assertEqual(self.ids(room["messages"]), list(range(last - 9, last + 1)))
        self.assertEqual(self.ids(self.lines()), list(range(1, last - 9)))   # no overlap, no gap

    def test_archived_message_is_kept_as_it_stood_when_it_left(self):
        self.post(1, "first words")
        first = self.app.room_read()["messages"][-1]
        with self.app.room_edit() as r:           # changed after posting (as acks/answers do)
            r["messages"][-1]["status"]["alpha"] = "acked"
        self.post(self.app.ROOM_KEEP)
        old = [m for m in self.lines() if m["id"] == first["id"]][0]
        self.assertEqual(old["text"], "first words")
        self.assertEqual(old["status"], {"alpha": "acked"})

    def test_same_permissions_as_room_json(self):
        self.post(15)
        room_json = os.path.join(self.tmp, "room", "room.json")
        self.assertEqual(os.stat(self.hist).st_mode & 0o777, os.stat(room_json).st_mode & 0o777)


class Cap(HistoryBase):
    def test_trims_oldest_lines_in_chunks_and_stays_under_the_cap(self):
        self.app.ROOM_HISTORY_MAX_BYTES, self.app.ROOM_HISTORY_TRIM = 6000, 2000
        self.post(10)
        sizes = []
        for _ in range(120):
            self.post(1, "x" * 40 + " %d")
            sizes.append(os.path.getsize(self.hist))
        self.assertTrue(all(s <= 6000 for s in sizes), sizes)
        drops = [a for a, b in zip(sizes, sizes[1:]) if b < a]
        self.assertTrue(drops, sizes)                                   # it did trim…
        self.assertLess(len(drops), len(sizes) // 5, sizes)             # …but not every post
        ids = self.ids(self.lines())
        self.assertGreater(ids[0], 1)                                   # the oldest went
        self.assertEqual(ids, list(range(ids[0], ids[-1] + 1)))         # whole lines, in order
        self.assertEqual(ids[-1] + 1, self.app.room_read()["messages"][0]["id"])


class ReadBack(HistoryBase):
    def test_reads_across_the_room_json_boundary(self):
        self.post(40)
        room = self.app.room_read()
        last = room["next_id"] - 1
        self.assertEqual(self.ids(self.app.room_history(room)), list(range(1, last + 1)))
        self.assertEqual(self.ids(self.app.room_history(room, n=15)), list(range(last - 14, last + 1)))
        self.assertEqual(self.ids(self.app.room_history(room, before=20, n=5)), [15, 16, 17, 18, 19])
        self.assertEqual(self.ids(self.app.room_history(room, before=last - 3, n=3)),
                         [last - 6, last - 5, last - 4])

    def test_older_endpoint_pages_back(self):
        self.app.ROOM_SHOW = 8
        self.post(40)
        room = self.app.room_read()
        page = self.app.room_older(room["messages"][-8]["id"])
        self.assertEqual(len(page["messages"]), 8)
        self.assertTrue(page["more"])
        self.assertEqual(page["titles"]["alpha"], "alpha")
        page = self.app.room_older(5)
        self.assertEqual(self.ids(page["messages"]), [1, 2, 3, 4])
        self.assertFalse(page["more"])

    def test_room_json_poll_is_untouched(self):
        self.post(40)
        room = self.app.room_read()
        self.assertEqual(len(room["messages"]), self.app.ROOM_KEEP)
        self.assertNotIn("history", room)

    def cli(self, *args):
        code = ("import importlib.util, sys; sys.argv = ['miss-room.py'] + %r\n"
                "spec = importlib.util.spec_from_file_location('mr', %r)\n"
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                "sys.modules['app'].ROOM_KEEP = 10\n"
                "sys.exit(mod.main(sys.argv[1:]))\n") % (list(args), os.path.join(REPO, "scripts", "miss-room.py"))
        env = dict(os.environ, MISSION_NAME="", CLAUDE_MISSION="")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [int(x.split()[0][1:]) for x in out.stdout.splitlines() if x.startswith("#")]

    def test_cli_read_reaches_into_the_archive(self):
        self.post(40, "note %d")
        with self.app.room_edit() as r:
            self.app.room_post(r, "operator", "needle in the hay", ["alpha"])
        last = self.app.room_read()["next_id"] - 1
        self.assertEqual(self.cli("read", "30"), list(range(last - 29, last + 1)))
        self.assertEqual(self.cli("read", "--from", "3", "4"), [3, 4, 5, 6])
        self.assertEqual(self.cli("read", "--grep", "NOTE 1"),
                         [m["id"] for m in self.app.room_history(self.app.room_read())
                          if "note 1" in m["text"]][-20:])
        self.assertEqual(self.cli("read", "--grep", "needle"), [last])


class Robust(HistoryBase):
    def test_a_torn_last_line_is_skipped_and_the_next_append_starts_clean(self):
        self.post(20)
        with open(self.hist, "ab") as fh:
            fh.write(b'{"id": 9999, "text": "half a mess')          # a crash mid-write
        room = self.app.room_read()
        self.assertNotIn(9999, self.ids(self.app.room_history(room)))
        self.post(5)
        room = self.app.room_read()
        ids = self.ids(self.app.room_history(room))
        self.assertEqual(ids, list(range(1, room["next_id"])))       # nothing else lost

    def test_concurrent_posts_from_several_processes(self):
        script = textwrap.dedent("""
            import importlib.util, sys
            spec = importlib.util.spec_from_file_location("app_c", %r)
            app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
            app._run_tmux = lambda *a, capture=False: (1, "")
            app.ROOM_KEEP = 10
            for i in range(30):
                with app.room_edit() as r:
                    msg, err = app.room_post(r, "operator", "p%%s-%%d" %% (sys.argv[1], i), ["alpha"])
                    assert not err, err
        """) % os.path.join(REPO, "app.py")
        procs = [subprocess.Popen([sys.executable, "-c", script, str(k)]) for k in range(4)]
        self.assertEqual([p.wait(timeout=120) for p in procs], [0] * 4)
        room = self.app.room_read()
        msgs = self.app.room_history(room)
        self.assertEqual(self.ids(msgs), list(range(1, room["next_id"])))
        texts = [m["text"] for m in msgs if m["from"] == "operator"]
        self.assertEqual(len(texts), 120)
        self.assertEqual(len(set(texts)), 120)
        self.assertEqual(len(self.lines()), room["next_id"] - 1 - self.app.ROOM_KEEP)


if __name__ == "__main__":
    unittest.main()
