"""Director mode (the experiment): scripts/miss-director.py + miss-director-context.py.

Two things must hold. (1) The context hook is SILENT for every ordinary mission — it only
speaks for a `.director` marker or a child's SPEC.md, so no existing console changes.
(2) The CLI creates a child through the dashboard's own route, gives it
SPEC/STATUS/RESULT, and its board classifies children off STATUS.md alone.

    python3 -m unittest tests/test_director.py
"""
import importlib.util, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOOK = os.path.join(REPO, "scripts", "miss-director-context.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class DirectorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="director-")
        self._saved_env = {k: os.environ.get(k) for k in
                           ("MISSIONS_DIR", "MISSION_SELF_URL", "MISSION_TLS_CA")}
        os.environ["MISSIONS_DIR"] = self.tmp
        os.environ.pop("MISSION_SELF_URL", None)
        os.environ.pop("MISSION_TLS_CA", None)
        # miss-director.py does `import app`, so bind that name to an app whose
        # MISSIONS_DIR is the fixture before loading it.
        self._saved = {k: sys.modules.get(k) for k in ("app", "miss_director")}
        self.app = _load("app", os.path.join(REPO, "app.py"))
        self.md = _load("miss_director", os.path.join(REPO, "scripts", "miss-director.py"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        # MISSIONS_DIR points at a directory that no longer exists — restore it rather
        # than leave the trap for whatever test module loads app.py next.
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def mission(self, name, **files):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        for fn, body in files.items():
            with open(os.path.join(d, fn), "w", encoding="utf-8") as fh:
                fh.write(body)
        return d


class ContextHookTest(DirectorBase):
    """The hook must be invisible to every mission that isn't part of the experiment."""

    def run_hook(self, data_dir):
        env = dict(os.environ, MISSION_DATA_DIR=data_dir)
        r = subprocess.run([sys.executable, HOOK], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        return r.stdout

    def test_silent_for_an_ordinary_mission(self):
        self.assertEqual(self.run_hook(self.mission("plain", **{"LOG.md": "x"})), "")

    def test_silent_when_the_dir_does_not_exist(self):
        self.assertEqual(self.run_hook(os.path.join(self.tmp, "nope")), "")

    def test_director_block_for_the_marker(self):
        out = self.run_hook(self.mission("boss", **{".director": "on"}))
        self.assertIn("DIRECTOR MODE", out)
        self.assertIn("miss-director.py", out)
        self.assertNotIn("STOP RULE", out)

    def test_child_block_for_a_spec(self):
        out = self.run_hook(self.mission("kid", **{"SPEC.md": "# goal"}))
        self.assertIn("SPEC & STOP RULE", out)
        self.assertIn("YES SHIP", out)          # shipping stays the operator's call
        self.assertIn("Never ship without it", out)
        self.assertNotIn("DIRECTOR MODE", out)

    def test_no_leftover_copy_of_the_old_no_relay_rule(self):
        """The relay is a rule change, and a stale copy of the old rule is the failure
        mode: a Director that reads "never ship anything" will not relay, and a child
        that reads "the Director never approves for you" may refuse a real approval."""
        boss = self.run_hook(self.mission("boss2", **{".director": "on"}))
        kid = self.run_hook(self.mission("kid2", **{"SPEC.md": "# goal"}))
        for text in (boss, kid):
            self.assertNotIn("never ship anything", text)
            self.assertNotIn("Shipping safety is unchanged", text)
            self.assertNotIn("The Director never approves for you", text)
        self.assertIn("ship <child-name>", boss)       # the relay is offered
        self.assertIn("not READY", boss)               # and its one hard gate

    def test_marker_wins_over_a_spec(self):
        out = self.run_hook(self.mission("both", **{".director": "on", "SPEC.md": "x"}))
        self.assertIn("DIRECTOR MODE", out)
        self.assertNotIn("SPEC & STOP RULE", out)


class BoardTest(DirectorBase):
    def setUp(self):
        super().setUp()
        self.md.app.session_running = lambda name: name != "dark"

    def board(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.md.main(["board"])
        return buf.getvalue()

    def test_only_children_appear(self):
        self.mission("plain", **{"LOG.md": "x"})
        self.mission("boss", **{".director": "on", "SPEC.md": "the director's own spec"})
        self.mission("kid", **{"SPEC.md": "s", "STATUS.md": "RUNNING\nbuilding it"})
        out = self.board()
        self.assertIn("- kid — building it", out)
        self.assertNotIn("plain", out)
        self.assertNotIn("boss", out)

    def test_buckets_and_notes(self):
        self.mission("a", **{"SPEC.md": "s", "STATUS.md": "RUNNING\nbuilding"})
        self.mission("b", **{"SPEC.md": "s", "STATUS.md": "READY\nverified"})
        self.mission("c", **{"SPEC.md": "s", "STATUS.md": "NEEDS ME\none decision"})
        self.mission("d", **{"SPEC.md": "s", "STATUS.md": "BLOCKED\nno network"})
        self.mission("dark", **{"SPEC.md": "s", "STATUS.md": "RUNNING\nstarted"})
        out = self.board()
        self.assertIn("- a — building", out)
        self.assertIn("READY\n- b — verified — needs the operator's YES SHIP", out)
        self.assertIn("NEEDS ME\n- c — one decision", out)
        self.assertIn("BLOCKED\n- d — no network", out)
        self.assertLess(out.index("RUNNING"), out.index("- a — building"))
        self.assertIn("[console not running]", out)          # dark
        self.assertLess(out.index("READY"), out.index("NEEDS ME"))

    def test_missing_or_unknown_status_reads_as_running(self):
        self.mission("nostatus", **{"SPEC.md": "s"})
        self.mission("weird", **{"SPEC.md": "s", "STATUS.md": "almost done maybe"})
        out = self.board()
        self.assertIn("- nostatus — (no status written yet)", out)
        self.assertIn("- weird — almost done maybe", out)
        self.assertNotIn("READY", out)

    def test_detail_is_the_first_paragraph_only(self):
        # The spawn template keeps a reminder under a blank line; it is not status.
        self.mission("t", **{"SPEC.md": "s", "STATUS.md": self.md.STATUS_TEMPLATE})
        out = self.board()
        self.assertIn("- t — Just launched; reading SPEC.md.", out)
        self.assertNotIn("keep it to the status", out)

    def test_status_token_may_carry_its_detail_on_one_line(self):
        # Models drift from "exactly one of"; reading "READY - done" as RUNNING would
        # hide a finished child from the operator.
        self.mission("r", **{"SPEC.md": "s", "STATUS.md": "READY - all criteria pass"})
        out = self.board()
        self.assertIn("READY\n- r — all criteria pass", out)

    def test_empty_board(self):
        self.assertIn("No Director children yet.", self.board())


class EndpointTest(DirectorBase):
    """How the dashboard is dialed. Guessing it from this process's own env is wrong
    inside a console: claude-console.service exports MISSION_SELF_URL (https) but not
    MISSION_TLS_CERT, so app.SELF_URL would say http and the connection is reset."""

    def test_exported_env_wins(self):
        os.environ["MISSION_SELF_URL"] = "https://127.0.0.1:4200/"
        os.environ["MISSION_TLS_CA"] = "/ca.crt"
        self.assertEqual(self.md.dashboard_endpoint(),
                         ("https://127.0.0.1:4200", "/ca.crt"))

    def test_falls_back_to_the_file_app_py_publishes(self):
        with open(os.path.join(self.tmp, ".dashboard-url"), "w", encoding="utf-8") as fh:
            fh.write('{"base": "https://127.0.0.1:4200", "ca": "/ca.crt"}\n')
        self.assertEqual(self.md.dashboard_endpoint(),
                         ("https://127.0.0.1:4200", "/ca.crt"))

    def test_falls_back_to_this_apps_url_when_nothing_says_otherwise(self):
        base, _ca = self.md.dashboard_endpoint()
        self.assertEqual(base, self.app.SELF_URL)

    def test_an_explicit_url_beats_every_source(self):
        os.environ["MISSION_SELF_URL"] = "https://127.0.0.1:4200"
        url, _ca = self.md._base_url("http://127.0.0.1:4209")
        self.assertEqual(url, "http://127.0.0.1:4209")

    def test_a_broken_dashboard_url_file_is_ignored(self):
        with open(os.path.join(self.tmp, ".dashboard-url"), "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertEqual(self.md.dashboard_endpoint()[0], self.app.SELF_URL)


class SpawnTest(DirectorBase):
    def setUp(self):
        super().setUp()
        self.posts = []

        def fake_post(url, fields, ca=""):
            self.posts.append((url, fields))
            name = fields.get("name")
            os.makedirs(os.path.join(self.tmp, name), exist_ok=True)   # what the route does
            return 302, "/m/%s/dashboard" % name, ""
        self.md._post = fake_post
        self.md.app.session_running = lambda name: False   # no real tmux in a test
        self.spec = os.path.join(self.tmp, "spec.md")
        with open(self.spec, "w", encoding="utf-8") as fh:
            fh.write("# Goal\n- [ ] it works\n")

    def run_cli(self, argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.md.main(argv)
        return rc, buf.getvalue()

    def test_ops_child_uses_create_and_gets_its_three_files(self):
        rc, out = self.run_cli(["spawn", "kid", "--spec-file", self.spec, "--no-start"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0][0].endswith("/create"))
        d = os.path.join(self.tmp, "kid")
        self.assertIn("it works", self.app.read_text(os.path.join(d, "SPEC.md")))
        self.assertTrue(self.app.read_text(os.path.join(d, "STATUS.md")).startswith("RUNNING"))
        self.assertTrue(os.path.isfile(os.path.join(d, "RESULT.md")))

    def test_dev_child_posts_a_feature_spawn(self):
        rc, out = self.run_cli(["spawn", "devkid", "--mode", "dev", "--repo", "/tmp/r",
                                "--spec-file", self.spec, "--no-start"])
        self.assertEqual(rc, 0, out)
        url, fields = self.posts[0]
        self.assertTrue(url.endswith("/spawn"))
        self.assertEqual(fields["mode"], "dev")
        self.assertEqual(fields["kind"], "local-repo")
        self.assertEqual(fields["role"], "feature")
        self.assertEqual(fields["path"], "/tmp/r")

    def test_dev_child_needs_a_repo(self):
        rc, out = self.run_cli(["spawn", "x", "--mode", "dev", "--spec-file", self.spec])
        self.assertEqual(rc, 1)
        self.assertIn("--repo", out)
        self.assertEqual(self.posts, [])

    def test_refuses_an_existing_mission(self):
        self.mission("taken")
        rc, out = self.run_cli(["spawn", "taken", "--spec-file", self.spec, "--no-start"])
        self.assertEqual(rc, 1)
        self.assertIn("already exists", out)
        self.assertEqual(self.posts, [])

    def test_refuses_an_empty_spec(self):
        empty = os.path.join(self.tmp, "empty.md")
        open(empty, "w").close()
        rc, out = self.run_cli(["spawn", "kid", "--spec-file", empty, "--no-start"])
        self.assertEqual(rc, 1)
        self.assertIn("Empty spec", out)
        self.assertEqual(self.posts, [])

    def test_a_refusal_from_the_dashboard_is_not_success(self):
        # /create and /spawn answer a refusal with the index page, status 200. The
        # markup is render_index's own: the class attribute is UNQUOTED.
        self.md._post = lambda url, fields, ca="": (
            200, "", '<div class=notice>Mission "kid" already exists.</div>')
        rc, out = self.run_cli(["spawn", "kid", "--spec-file", self.spec, "--no-start"])
        self.assertEqual(rc, 1)
        self.assertIn('Mission "kid" already exists.', out)   # the reason, not "(no reason given)"
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "kid", "SPEC.md")))

    def test_an_unreachable_dashboard_is_reported_not_raised(self):
        self.md._post = lambda url, fields, ca="": (0, "", "[Errno 104] Connection reset by peer")
        rc, out = self.run_cli(["spawn", "kid", "--spec-file", self.spec, "--no-start"])
        self.assertEqual(rc, 1)
        self.assertIn("Could not reach the dashboard", out)
        self.assertIn("Connection reset", out)

    def test_the_route_gets_to_name_the_mission(self):
        # /create slugifies further than safe_name does ("kid-" -> "kid"); the three
        # files must follow the mission that actually exists.
        def slugifying_post(url, fields, ca=""):
            os.makedirs(os.path.join(self.tmp, "kid"), exist_ok=True)
            return 302, "/m/kid/dashboard", ""
        self.md._post = slugifying_post
        rc, out = self.run_cli(["spawn", "kid-", "--spec-file", self.spec, "--no-start"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "kid", "SPEC.md")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "kid-")))

    def test_a_redirect_to_a_missing_mission_writes_nothing(self):
        self.md._post = lambda url, fields, ca="": (302, "/m/ghost/dashboard", "")
        rc, out = self.run_cli(["spawn", "ghost", "--spec-file", self.spec, "--no-start"])
        self.assertEqual(rc, 1)
        self.assertIn("no such mission exists", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ghost")))

    def test_init_marks_an_existing_mission_without_creating_one(self):
        self.mission("boss")
        rc, out = self.run_cli(["init", "boss", "--no-start"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "boss", ".director")))
        self.assertEqual(self.posts, [])          # it already existed

    def test_init_creates_the_mission_when_it_does_not_exist(self):
        # One command is the whole toggle: create, mark, start.
        rc, out = self.run_cli(["init", "boss", "--no-start"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.posts[0][0].endswith("/create"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "boss", ".director")))
        self.assertIn("created mission boss", out)

    def test_init_starts_the_console_and_types_nothing(self):
        started = []
        self.md._start_console = lambda name: started.append(name) or ""
        sent = []
        self.md.app.console_send = lambda *a, **k: sent.append(a) or (True, "sent")
        rc, out = self.run_cli(["init", "boss"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(started, ["boss"])
        self.assertEqual(sent, [])                # the operator talks to it, not us
        self.assertIn("console is running", out)

    def test_init_on_a_live_console_asks_for_a_restart(self):
        self.mission("boss")
        self.md.app.session_running = lambda name: True
        rc, out = self.run_cli(["init", "boss"])
        self.assertEqual(rc, 0, out)
        self.assertIn("restart it", out)          # SessionStart already happened

    def test_init_refuses_a_bad_name(self):
        rc, out = self.run_cli(["init", "../evil"])
        self.assertEqual(rc, 1)
        self.assertIn("Invalid mission name", out)
        self.assertEqual(self.posts, [])


class ShipTest(DirectorBase):
    """`ship` is a RELAY with hard gates. Each gate is a separate test because each one
    protects a different mistake: shipping a mission still working, shipping a mission
    the operator never named, and losing the audit trail."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.md.app.console_send = lambda *a, **k: (
            self.sent.append((a, k)) or (True, "sent"))
        self.md.app.session_running = lambda name: True
        os.environ["MISSION_NAME"] = "boss"
        self.mission("boss", **{".director": "on"})

    def tearDown(self):
        os.environ.pop("MISSION_NAME", None)
        super().tearDown()

    def run_cli(self, argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.md.main(argv)
        return rc, buf.getvalue()

    def log(self, name="boss"):
        return self.app.read_text(os.path.join(self.tmp, name, "LOG.md"))

    def test_relays_into_a_ready_child_and_logs_it(self):
        self.mission("kid", **{"SPEC.md": "s", "STATUS.md": "READY\nverified"})
        rc, out = self.run_cli(["ship", "kid"])
        self.assertEqual(rc, 0, out)
        (session, action, text), kw = self.sent[0]
        self.assertEqual(session, self.app.SESSION_PREFIX + "kid")
        self.assertEqual((action, text), ("text", "YES SHIP"))
        self.assertTrue(kw["submit"])
        self.assertIn("Relayed YES SHIP into kid", out)
        self.assertIn('operator said YES SHIP for child "kid"', self.log())

    def test_a_child_that_is_not_ready_is_refused(self):
        for status in ("RUNNING\nstill working", "NEEDS ME\na decision",
                       "BLOCKED\nno network"):
            name = "kid" + status[:3].strip().lower()
            self.mission(name, **{"SPEC.md": "s", "STATUS.md": status})
            rc, out = self.run_cli(["ship", name])
            self.assertEqual(rc, 1, out)
            self.assertIn("not READY", out)
            self.assertEqual(self.sent, [])
        self.assertNotIn("YES SHIP", self.log())

    def test_an_unknown_name_is_refused(self):
        rc, out = self.run_cli(["ship", "ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("No mission", out)
        self.assertEqual(self.sent, [])

    def test_a_bad_name_is_refused(self):
        rc, out = self.run_cli(["ship", "../evil"])
        self.assertEqual(rc, 1)
        self.assertIn("Invalid mission name", out)
        self.assertEqual(self.sent, [])

    def test_a_plain_mission_and_the_director_itself_are_refused(self):
        self.mission("plain", **{"LOG.md": "x", "STATUS.md": "READY"})
        rc, out = self.run_cli(["ship", "plain"])
        self.assertEqual(rc, 1)
        self.assertIn("not a Director child", out)
        rc, out = self.run_cli(["ship", "boss"])
        self.assertEqual(rc, 1)
        self.assertIn("is the Director", out)
        self.assertEqual(self.sent, [])

    def test_a_dead_console_is_reported_and_still_logged(self):
        self.mission("kid", **{"SPEC.md": "s", "STATUS.md": "READY"})
        self.md.app.console_send = lambda *a, **k: (False, "That console is not running.")
        rc, out = self.run_cli(["ship", "kid"])
        self.assertEqual(rc, 1)
        self.assertIn("Could not send", out)
        self.assertIn("NOT sent", self.log())     # the attempt is still auditable

    def test_the_director_is_found_when_the_console_env_is_absent(self):
        os.environ.pop("MISSION_NAME", None)
        self.mission("kid", **{"SPEC.md": "s", "STATUS.md": "READY"})
        rc, out = self.run_cli(["ship", "kid"])
        self.assertEqual(rc, 0, out)
        self.assertIn("YES SHIP", self.log())     # the one marked mission

    def test_two_directors_need_an_explicit_one(self):
        os.environ.pop("MISSION_NAME", None)
        self.mission("boss2", **{".director": "on"})
        self.mission("kid", **{"SPEC.md": "s", "STATUS.md": "READY"})
        rc, out = self.run_cli(["ship", "kid"])
        self.assertEqual(rc, 1)
        self.assertIn("More than one Director", out)
        self.assertEqual(self.sent, [])
        rc, out = self.run_cli(["ship", "kid", "--director", "boss2"])
        self.assertEqual(rc, 0, out)
        self.assertIn("YES SHIP", self.log("boss2"))

    def test_ship_takes_exactly_one_child(self):
        # No wildcard, no list: argparse itself is the guard against "ship everything".
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit):
                self.md.main(["ship", "a", "b"])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
