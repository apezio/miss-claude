"""Auto-naming: a mission spawned with a blank name is a placeholder (`auto_named` in
mission.json) and is renamed after the operator's first prompt (by Claude, else a slug) once its console
has stopped (sweep_auto_names) — and shows that slug as its display title (mission_title)
from the moment the prompt lands, while the console still runs. Covers the slug rules, the first-prompt reader, every
skip rule of the sweep, the flag written by /spawn, and that a rename — by hand or
automatic — settles the name and carries the canvas card over."""
import http.client
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

TMP = None
APP = None


def setUpModule():
    global TMP, APP
    TMP = tempfile.mkdtemp(prefix="miss-autoname-")
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_CANVAS_FILE"] = os.path.join(TMP, "state", "canvas.json")
    os.environ["WORKTREES_DIR"] = os.path.join(TMP, "worktrees")
    os.environ["MISSION_ARCHIVES_DIR"] = os.path.join(TMP, "archives")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"        # no tmux: nothing is running
    os.environ["MISSION_NAME_MODEL"] = ""            # formula names; fake_claude turns Claude on
    os.environ.pop("MISSION_TOKEN", None)
    os.environ.pop("MISSION_TLS_CERT", None)
    os.makedirs(os.path.join(TMP, "missions"))
    spec = importlib.util.spec_from_file_location("app_autoname", os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)
    APP.PROJECTS_DIR = os.path.join(TMP, "projects")
    os.makedirs(APP.PROJECTS_DIR)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def _line(role, content, **extra):
    d = {"type": role, "message": {"role": role, "content": content}}
    d.update(extra)
    return json.dumps(d)


class RandomName(unittest.TestCase):
    def test_every_pair_is_a_safe_name(self):
        for a in APP._NAME_ADJ:
            for n in APP._NAME_NOUN:
                self.assertTrue(APP.safe_name(a + "-" + n))
        self.assertEqual(len(set(APP._NAME_ADJ)), len(APP._NAME_ADJ))
        self.assertEqual(len(set(APP._NAME_NOUN)), len(APP._NAME_NOUN))

    def test_taken_covers_worktrees_and_archives(self):
        # A renamed/archived dev mission leaves its worktree under the random name;
        # drawing that name again would silently attach to the old checkout.
        for d, n in ((APP.MISSIONS_DIR, "m"), (APP.WORKTREES_DIR, "w"), (APP.ARCHIVES_DIR, "a")):
            os.makedirs(os.path.join(d, "amber-otter-" + n))
            self.assertTrue(APP.name_taken("amber-otter-" + n))
        self.assertFalse(APP.name_taken("amber-otter-x"))

    def test_falls_back_to_a_suffix_when_everything_is_taken(self):
        n = APP.random_mission_name(taken=lambda name: True)
        self.assertTrue(APP.safe_name(n))
        self.assertRegex(n, r"^[a-z]+-[a-z]+-\d{3}$")


class Slug(unittest.TestCase):
    def slug(self, text, taken=()):
        return APP.auto_name_from(text, taken=lambda n: n in taken)

    def test_meaningful_words_survive(self):
        self.assertEqual(self.slug("Fix the login page, passwords with spaces break it"),
                         "fix-login-page-passwords-spaces-break")

    def test_caps_words_and_length(self):
        self.assertEqual(self.slug("alpha beta gamma delta epsilon zeta eta theta"),
                         "alpha-beta-gamma-delta-epsilon-zeta")
        long = "supercalifragilistic expialidocious antidisestablishmentarianism words more"
        s = self.slug(long)
        self.assertLessEqual(len(s), APP.AUTO_NAME_MAX_LEN)
        self.assertEqual(s, "supercalifragilistic-expialidocious")

    def test_all_stopwords_falls_back_to_the_words(self):
        self.assertEqual(self.slug("Can you please?"), "can-you-please")

    def test_no_words_is_empty(self):
        self.assertEqual(self.slug("¯\\_(ツ)_/¯"), "")
        self.assertEqual(self.slug(""), "")

    def test_collision_gets_a_counter(self):
        self.assertEqual(self.slug("deploy webapp", taken={"deploy-webapp"}), "deploy-webapp-2")
        self.assertEqual(self.slug("deploy webapp", taken={"deploy-webapp", "deploy-webapp-2"}),
                         "deploy-webapp-3")


class Missions(unittest.TestCase):
    """Fixture: a mission dir with a mission.json and, optionally, a hook marker
    pointing at a transcript under the fake PROJECTS_DIR."""

    def setUp(self):
        for n in os.listdir(os.environ["MISSIONS_DIR"]):
            shutil.rmtree(os.path.join(os.environ["MISSIONS_DIR"], n))
        try:
            os.unlink(APP.CANVAS_FILE)
        except OSError:
            pass

    def mission(self, name, auto=True, lines=None, extra_meta=None):
        d = APP.mission_path(name)
        os.makedirs(d)
        meta = {"mode": "ops", "target": {"kind": "local-dir", "path": os.path.expanduser("~")}}
        if auto:
            meta["auto_named"] = True
        meta.update(extra_meta or {})
        APP.write_mission_meta(name, meta)
        if lines is not None:
            pdir = os.path.join(APP.PROJECTS_DIR, "-home-x")
            os.makedirs(pdir, exist_ok=True)
            f = os.path.join(pdir, name + ".jsonl")
            with open(f, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            with open(os.path.join(d, ".console-session"), "w") as fh:
                json.dump({"transcript_path": f}, fh)
        return d

    # -- first_prompt_text -------------------------------------------------------
    def test_first_prompt_skips_harness_lines_and_tool_results(self):
        self.mission("calm-otter", lines=[
            _line("user", "<local-command-stdout>hi</local-command-stdout>"),
            _line("user", "[Request interrupted by user]"),
            _line("user", "<command-name>/model</command-name>"),
            _line("user", "sidechain junk", isSidechain=True),
            _line("user", "meta junk", isMeta=True),
            _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]),
            _line("assistant", [{"type": "text", "text": "Hello, what shall we do?"}]),
            _line("user", [{"type": "text", "text": "  Rotate the webapp TLS certs  "}]),
            _line("user", "a later prompt"),
        ])
        self.assertEqual(APP.first_prompt_text("calm-otter"), "Rotate the webapp TLS certs")

    def test_first_prompt_without_marker_or_prompt(self):
        self.mission("no-marker")
        self.assertEqual(APP.first_prompt_text("no-marker"), "")
        self.mission("no-prompt", lines=[_line("user", "<hook>stuff</hook>")])
        self.assertEqual(APP.first_prompt_text("no-prompt"), "")

    def test_first_prompt_marker_outside_projects_dir_is_ignored(self):
        d = self.mission("stray")
        f = os.path.join(TMP, "elsewhere.jsonl")
        with open(f, "w") as fh:
            fh.write(_line("user", "secret prompt") + "\n")
        with open(os.path.join(d, ".console-session"), "w") as fh:
            json.dump({"transcript_path": f}, fh)
        self.assertEqual(APP.first_prompt_text("stray"), "")

    # -- sweep_auto_names ----------------------------------------------------------
    def test_sweep_renames_a_stopped_auto_named_mission(self):
        self.mission("brave-otter", lines=[_line("user", "Fix the login page redirect loop")])
        self.assertEqual(APP.sweep_auto_names(running=set()),
                         [("brave-otter", "fix-login-page-redirect-loop")])
        self.assertFalse(os.path.isdir(APP.mission_path("brave-otter")))
        meta = APP.read_mission_meta("fix-login-page-redirect-loop")
        self.assertNotIn("auto_named", meta)
        # The resume key of an ops-at-a-dir mission is pinned to the OLD name.
        self.assertEqual(meta["session_id"], APP.uuid.uuid5(APP.uuid.NAMESPACE_URL, "brave-otter").__str__())
        # Settled: a second sweep leaves it alone.
        self.assertEqual(APP.sweep_auto_names(running=set()), [])

    def test_sweep_skips_running_named_trashed_and_promptless(self):
        self.mission("still-live", lines=[_line("user", "deploy the thing")])
        self.mission("chosen-name", auto=False, lines=[_line("user", "deploy the thing")])
        self.mission("no-flag-legacy", auto=False)
        self.mission("no-prompt-yet")
        d = self.mission("in-the-bin", lines=[_line("user", "deploy the thing")])
        with open(os.path.join(d, APP.TRASH_FILE), "w") as fh:
            json.dump({"due": 9e12, "queued": 0}, fh)
        self.assertEqual(APP.sweep_auto_names(running={"still-live"}), [])
        for n in ("still-live", "chosen-name", "no-flag-legacy", "no-prompt-yet", "in-the-bin"):
            self.assertTrue(os.path.isdir(APP.mission_path(n)), n)
        # Once the console is gone the live one goes through.
        self.assertEqual(APP.sweep_auto_names(running=set()), [("still-live", "deploy-thing")])

    def test_sweep_titles_a_running_mission_and_renames_it_once_stopped(self):
        self.mission("brave-otter", lines=[_line("user", "Fix the login page redirect loop")])
        # Console still running: no rename, but the display title lands at once.
        self.assertEqual(APP.sweep_auto_names(running={"brave-otter"}), [])
        self.assertTrue(os.path.isdir(APP.mission_path("brave-otter")))
        meta = APP.read_mission_meta("brave-otter")
        self.assertEqual(meta["title"], "fix-login-page-redirect-loop")
        self.assertIs(meta["auto_named"], True)
        self.assertEqual(APP.mission_title("brave-otter"), "fix-login-page-redirect-loop")
        self.assertEqual(APP.title_attr("brave-otter"), ' title="~/missions/brave-otter"')
        self.assertIn("fix-login-page-redirect-loop", APP.mission_search_text("brave-otter"))
        self.assertEqual(APP.sweep_auto_names(running={"brave-otter"}), [])
        # Stopped: the directory follows the title (the rename itself — flag and
        # title dropped — is test_sweep_renames_a_stopped_auto_named_mission's).
        self.assertEqual(APP.sweep_auto_names(running=set()),
                         [("brave-otter", "fix-login-page-redirect-loop")])

    def test_title_falls_back_to_the_name_and_hand_rename_drops_it(self):
        self.mission("plain-fox", auto=False)
        self.assertEqual(APP.mission_title("plain-fox"), "plain-fox")
        self.assertEqual(APP.title_attr("plain-fox"), "")
        self.mission("titled-elk", lines=[_line("user", "Rotate the mail certs")])
        self.assertEqual(APP.sweep_auto_names(running={"titled-elk"}), [])
        self.assertEqual(APP.mission_title("titled-elk"), "rotate-mail-certs")
        # The operator renames it by hand while it is titled: the typed name wins,
        # the title goes, and the auto-namer never touches it again.
        self.assertEqual(APP.rename_mission("titled-elk", "certs")[0], "certs")
        meta = APP.read_mission_meta("certs")
        self.assertNotIn("title", meta)
        self.assertNotIn("auto_named", meta)
        self.assertEqual(APP.mission_title("certs"), "certs")
        self.assertEqual(APP.sweep_auto_names(running=set()), [])

    def test_canvas_state_carries_the_title(self):
        self.mission("quiet-yak", lines=[_line("user", "Audit the sudoers files")])
        APP.write_canvas_layout({"cards": {"quiet-yak": {"x": 0, "y": 0, "w": 400, "h": 300}},
                                 "hidden": [], "groups": [], "notes": []})
        self.assertEqual(APP.canvas_state()["missions"]["quiet-yak"]["title"], "quiet-yak")
        APP.sweep_auto_names(running={"quiet-yak"})
        self.assertEqual(APP.canvas_state()["missions"]["quiet-yak"]["title"], "audit-sudoers-files")

    def test_sweep_collision_takes_a_counter(self):
        self.mission("deploy-thing", auto=False)
        self.mission("lucky-heron", lines=[_line("user", "deploy the thing")])
        self.assertEqual(APP.sweep_auto_names(running=set()), [("lucky-heron", "deploy-thing-2")])

    def test_sweep_leaves_a_mission_whose_prompt_slugs_to_itself(self):
        self.mission("deploy-thing", lines=[_line("user", "deploy the thing")])
        self.assertEqual(APP.sweep_auto_names(running=set()), [])
        self.assertTrue(os.path.isdir(APP.mission_path("deploy-thing")))
        self.assertNotIn("auto_named", APP.read_mission_meta("deploy-thing"))

    # -- naming by Claude ----------------------------------------------------------
    def fake_claude(self, script):
        """Point the namer at a stand-in `claude` that logs each call to calls.log."""
        path = os.path.join(TMP, "fake-claude")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\necho call >> %s/calls.log\ncat >/dev/null\n%s\n" % (TMP, script))
        os.chmod(path, 0o755)
        for k, v in (("AUTO_NAME_MODEL", "haiku"), ("AUTO_NAME_CLAUDE", path)):
            self.addCleanup(setattr, APP, k, getattr(APP, k))
            setattr(APP, k, v)
        log = os.path.join(TMP, "calls.log")
        if os.path.exists(log):
            os.unlink(log)
        return lambda: open(log).read().count("\n") if os.path.exists(log) else 0

    def test_claude_names_it_once_and_the_directory_takes_that_name(self):
        calls = self.fake_claude('echo "Admin-Payments-Failed-Status."')
        self.mission("fizzy-heron", lines=[_line("user", "looks like I found a bug on the payments page")])
        self.assertEqual(APP.sweep_auto_names(running={"fizzy-heron"}), [])
        self.assertEqual(APP.mission_title("fizzy-heron"), "admin-payments-failed-status")
        self.assertEqual(APP.sweep_auto_names(running=set()),
                         [("fizzy-heron", "admin-payments-failed-status")])
        self.assertEqual(calls(), 1)

    def test_a_failed_call_falls_back_to_the_formula(self):
        # An error, an empty answer, and prose instead of a name.
        for script in ("exit 1", "true", "echo I need your actual request."):
            calls = self.fake_claude(script)
            self.mission("dull-moth", lines=[_line("user", "Rotate the mail certs")])
            self.assertEqual(APP.sweep_auto_names(running=set()), [("dull-moth", "rotate-mail-certs")])
            self.assertEqual(calls(), 1)
            shutil.rmtree(APP.mission_path("rotate-mail-certs"))

    # -- rename_mission ------------------------------------------------------------
    def test_hand_rename_settles_the_name_and_moves_the_canvas_card(self):
        self.mission("zesty-lynx", lines=[_line("user", "one thing")])
        APP.write_canvas_layout({
            "cards": {"zesty-lynx": {"x": 40, "y": 50, "w": 400, "h": 300, "color": "red"}},
            "groups": [], "hidden": ["zesty-lynx"],
            "notes": [{"id": "n1", "x": 0, "y": 0, "w": 200, "h": 80, "text": "t", "pin": "zesty-lynx"}],
        })
        new, msg = APP.rename_mission("zesty-lynx", "my real name")
        self.assertEqual(new, "my-real-name", msg)
        self.assertNotIn("auto_named", APP.read_mission_meta("my-real-name"))
        self.assertEqual(APP.sweep_auto_names(running=set()), [])
        layout = APP.read_canvas_layout()
        self.assertEqual(sorted(layout["cards"]), ["my-real-name"])
        self.assertEqual((layout["cards"]["my-real-name"]["x"], layout["cards"]["my-real-name"]["color"]),
                         (40, "red"))
        self.assertEqual(layout["hidden"], ["my-real-name"])
        self.assertEqual(layout["notes"][0]["pin"], "my-real-name")

    def test_rename_without_a_card_leaves_the_layout_alone(self):
        self.mission("plain-ibis", auto=False)
        APP.write_canvas_layout({"cards": {"other": {"x": 1, "y": 2, "w": 400, "h": 300}},
                                 "groups": [], "notes": []})
        before = APP.read_canvas_layout()
        self.assertEqual(APP.rename_mission("plain-ibis", "plain-ibis-2")[0], "plain-ibis-2")
        self.assertEqual(APP.read_canvas_layout(), before)


class SpawnFlag(unittest.TestCase):
    """/spawn records auto_named only for a name it generated."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def spawn(self, **fields):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/spawn", body=urllib.parse.urlencode(fields),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        body = r.read().decode("utf-8")
        c.close()
        return r.status, r.getheader("Location") or "", body

    def test_blank_name_is_flagged_and_typed_name_is_not(self):
        st, loc, _ = self.spawn(mode="ops", kind="local-dir", path=TMP)
        self.assertEqual(st, 303)
        name = urllib.parse.unquote(loc.split("/m/")[1].split("/")[0])
        self.assertTrue(APP.safe_name(name))
        self.assertIs(APP.read_mission_meta(name).get("auto_named"), True)

        st, loc, _ = self.spawn(mode="ops", kind="local-dir", path=TMP, name="Chosen Name")
        self.assertEqual(st, 303)
        self.assertIn("/m/Chosen-Name/", loc)
        self.assertNotIn("auto_named", APP.read_mission_meta("Chosen-Name"))


if __name__ == "__main__":
    unittest.main()
