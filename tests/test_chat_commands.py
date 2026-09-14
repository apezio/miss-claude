"""Slash-command autocomplete for the chat page: the scanner behind
/m/<name>/commands.json (built-ins + user/project/plugin skills, frontmatter
descriptions, dedup, codex/remote => empty) and the route itself."""
import http.client
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = None
APP = None
ORIG_HOME = None


def _md(path, desc=None, body="# x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        if desc is not None:
            fh.write("---\nname: n\ndescription: %s\n---\n" % desc)
        fh.write(body)


def setUpModule():
    global TMP, APP, ORIG_HOME
    TMP = tempfile.mkdtemp(prefix="miss-cmds-")
    ORIG_HOME = os.environ.get("HOME")
    os.environ["HOME"] = TMP           # app.py derives ~/.claude from HOME at import
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"
    os.environ.pop("MISSION_TOKEN", None)
    os.environ.pop("MISSION_TLS_CERT", None)
    for m in ("probe", "codexy", "faraway"):
        os.makedirs(os.path.join(TMP, "missions", m))
    with open(os.path.join(TMP, "missions", "codexy", "mission.json"), "w") as fh:
        json.dump({"mode": "console", "agent": "codex", "target": {"kind": "local-dir", "path": ""}}, fh)
    with open(os.path.join(TMP, "missions", "faraway", "mission.json"), "w") as fh:
        json.dump({"mode": "ops", "target": {"kind": "remote", "host": "h", "remote_dir": "/x"}}, fh)
    home = os.path.join(TMP, ".claude")
    _md(os.path.join(home, "skills", "grilling", "SKILL.md"), "Grill the user 'hard'")
    _md(os.path.join(home, "commands", "deploy.md"), "Deploy it")
    _md(os.path.join(home, "commands", "nodesc.md"))
    _md(os.path.join(home, "skills", "Clear", "SKILL.md"), "shadows a built-in")
    plug = os.path.join(TMP, "plug", "1.0")
    _md(os.path.join(plug, "skills", "review", "SKILL.md"), "Plugin review")
    _md(os.path.join(plug, "commands", "fix.md"), "Plugin fix")
    os.makedirs(os.path.join(home, "plugins"))
    with open(os.path.join(home, "plugins", "installed_plugins.json"), "w") as fh:
        json.dump({"plugins": {"pw@market": [{"installPath": plug}], "broken@m": "nope"}}, fh)
    _md(os.path.join(TMP, "missions", "probe", ".claude", "skills", "local", "SKILL.md"), "Project skill")
    spec = importlib.util.spec_from_file_location("app_cmds", os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)


def tearDownModule():
    # Put HOME back: the suite runs in one process and the guard tests read it.
    if ORIG_HOME is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = ORIG_HOME
    shutil.rmtree(TMP, ignore_errors=True)


class Scanner(unittest.TestCase):
    def names(self, cwd=""):
        return {c["name"]: c for c in APP.command_list(cwd)}

    def test_builtins_and_user_entries(self):
        got = self.names()
        self.assertEqual(got["/compact"]["source"], "builtin")
        self.assertEqual(got["/grilling"], {"name": "/grilling", "desc": "Grill the user 'hard'", "source": "user"})
        self.assertEqual(got["/deploy"]["desc"], "Deploy it")
        self.assertEqual(got["/nodesc"]["desc"], "")

    def test_plugins_are_namespaced_and_bad_entries_skipped(self):
        got = self.names()
        self.assertEqual(got["/pw:review"], {"name": "/pw:review", "desc": "Plugin review", "source": "plugin"})
        self.assertEqual(got["/pw:fix"]["source"], "plugin")

    def test_builtin_wins_over_same_named_skill(self):
        got = self.names()
        self.assertEqual(got["/clear"]["source"], "builtin")
        self.assertNotIn("/Clear", got)

    def test_project_skills_only_with_cwd(self):
        self.assertNotIn("/local", self.names())
        got = self.names(os.path.join(TMP, "missions", "probe"))
        self.assertEqual(got["/local"]["source"], "project")

    def test_frontmatter_quotes_and_missing(self):
        p = os.path.join(TMP, "fm.md")
        _md(p, '"quoted one"')
        self.assertEqual(APP._frontmatter_desc(p), "quoted one")
        _md(p, None, "no frontmatter\n")
        self.assertEqual(APP._frontmatter_desc(p), "")
        self.assertEqual(APP._frontmatter_desc(os.path.join(TMP, "missing.md")), "")


class Route(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_commands_json(self):
        st, body = self.get("/m/probe/commands.json")
        self.assertEqual(st, 200)
        names = [c["name"] for c in json.loads(body)["commands"]]
        self.assertIn("/clear", names)
        self.assertIn("/local", names)       # cwd = the mission folder for a plain ops mission
        self.assertIn("/pw:review", names)

    def test_codex_and_remote_are_empty(self):
        for m in ("codexy", "faraway"):
            st, body = self.get("/m/%s/commands.json" % m)
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(body), {"commands": []})

    def test_chat_page_wires_popup(self):
        st, body = self.get("/m/probe/chat")
        self.assertEqual(st, 200)
        self.assertIn(b'id=cmdpop', body)
        self.assertIn(b'/m/probe/commands.json', body)

    def test_chat_page_carries_spawn_wizard(self):
        # The phone view's mission picker leads with "+ Open" (the Spawn wizard's
        # trigger) even with no other console open, and carries the wizard's
        # markup + its CSS; the canvas embed (which has the canvas's own) does not.
        st, body = self.get("/m/probe/chat")
        self.assertEqual(st, 200)
        self.assertIn(b'id=pickbtn', body)
        self.assertIn(b'id=spawn-open', body)
        self.assertIn(b'id=spawn-modal', body)
        self.assertIn(b'.modal-overlay {', body)
        self.assertEqual(body.count(b'id=spawn-open'), 1)
        st, body = self.get("/m/probe/chat?embed=1")
        self.assertEqual(st, 200)
        self.assertNotIn(b'id=spawn-open', body)
        self.assertNotIn(b'id=spawn-modal', body)


if __name__ == "__main__":
    unittest.main()
