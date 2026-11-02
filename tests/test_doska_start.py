"""POST /doska/start: Doska's cross-origin "+ Start" button. Origin allow/deny
(the CSRF guard), the CORS preflight, the 401-as-JSON for a logged-out browser,
the mission it creates, and the register-prompt kickoff — with the headless
console start and the kickoff thread stubbed out.

stdlib only:  python3 -m unittest tests/test_doska_start.py
"""
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
DOSKA = "https://doska.example"
TMP = APP = None


def setUpModule():
    global TMP, APP
    TMP = tempfile.mkdtemp(prefix="miss-doska-start-")
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_CANVAS_FILE"] = os.path.join(TMP, "state", "canvas.json")
    os.environ["WORKTREES_DIR"] = os.path.join(TMP, "worktrees")
    os.environ["MISSION_ARCHIVES_DIR"] = os.path.join(TMP, "archives")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"
    os.environ["CHAT_EMBED_ORIGINS"] = DOSKA + "/"
    os.environ.pop("MISSION_TOKEN", None)
    os.environ.pop("MISSION_LOGIN", None)
    os.environ.pop("MISSION_TLS_CERT", None)
    os.environ.pop("APP_BASE_URL", None)
    os.makedirs(os.path.join(TMP, "missions"))
    spec = importlib.util.spec_from_file_location("app_doska_start",
                                                  os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)


def tearDownModule():
    os.environ.pop("CHAT_EMBED_ORIGINS", None)
    shutil.rmtree(TMP, ignore_errors=True)


class Route(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.started, self.kicked = [], []
        self._orig = (APP.start_console_headless, APP.start_doska_kickoff)
        APP.start_console_headless = lambda name: self.started.append(name) or ""
        APP.start_doska_kickoff = lambda name, since: self.kicked.append(name)

    def tearDown(self):
        APP.start_console_headless, APP.start_doska_kickoff = self._orig

    def req(self, method, origin=DOSKA, **fields):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Host": "jump.example:4200"}
        if origin:
            headers["Origin"] = origin
        body = urllib.parse.urlencode(fields) if method == "POST" else None
        c.request(method, "/doska/start", body=body, headers=headers)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, (json.loads(data) if data else None)

    def meta(self, name):
        with open(os.path.join(TMP, "missions", name, "mission.json")) as fh:
            return json.load(fh)

    def test_creates_auto_named_mission_and_starts_it(self):
        st, h, d = self.req("POST", doska_project="proj-0123abcd")
        self.assertEqual(st, 200, d)
        self.assertEqual(h["access-control-allow-origin"], DOSKA)
        self.assertEqual(h["access-control-allow-credentials"], "true")
        self.assertIn("Origin", h["vary"])
        self.assertEqual((d["ok"], d["started"], d["msg"]), (True, True, ""))
        name = d["name"]
        self.assertEqual(d["open_url"], "http://jump.example:4200/m/%s/dashboard" % name)
        self.assertEqual(self.started, [name])
        self.assertEqual(self.kicked, [name])
        m = self.meta(name)
        self.assertEqual(m["mode"], "ops")
        self.assertEqual(m["target"], {"kind": "local-dir",
                                       "path": os.path.realpath(os.path.expanduser("~"))})
        self.assertIs(m["auto_named"], True)
        self.assertEqual(m["doska_project"], "proj-0123abcd")
        self.assertEqual(m["dashboard_url"], "http://jump.example:4200")

    def test_given_name_is_not_auto_named(self):
        st, _, d = self.req("POST", doska_project="proj-0123abcd", name="my  doska job")
        self.assertEqual((st, d["name"]), (200, "my-doska-job"))
        self.assertNotIn("auto_named", self.meta("my-doska-job"))
        st, _, d = self.req("POST", doska_project="proj-0123abcd", name="my doska job")
        self.assertEqual((st, d["ok"]), (409, False))

    def test_start_failure_is_reported_and_no_kickoff(self):
        APP.start_console_headless = lambda name: "console did not start: boom"
        st, _, d = self.req("POST", doska_project="proj-0123abcd")
        self.assertEqual((st, d["ok"], d["started"]), (200, True, False))
        self.assertIn("boom", d["msg"])
        self.assertEqual(self.kicked, [])

    def test_other_or_missing_origin_is_403_without_cors(self):
        for origin in ("https://evil.example", "https://doska.example.evil", None):
            st, h, d = self.req("POST", origin=origin, doska_project="proj-0123abcd")
            self.assertEqual(st, 403, origin)
            self.assertFalse(d["ok"])
            self.assertNotIn("access-control-allow-origin", h)
        self.assertEqual(self.started, [])

    def test_bad_project_id(self):
        for bad in ("", "proj-XYZ", "proj-12345", "proj-0123abcd/../x"):
            st, h, d = self.req("POST", doska_project=bad)
            self.assertEqual(st, 400, bad)
            self.assertFalse(d["ok"])
            self.assertEqual(h["access-control-allow-origin"], DOSKA)
        self.assertEqual(self.started, [])

    def test_not_logged_in_is_401_json_with_cors(self):
        orig = APP.TOKEN
        APP.TOKEN = "sekrit"
        try:
            st, h, d = self.req("POST", doska_project="proj-0123abcd")
        finally:
            APP.TOKEN = orig
        self.assertEqual(st, 401)
        self.assertEqual((d["ok"], d["login"]), (False, True))
        self.assertIn("Log in to http://jump.example:4200 first.", d["msg"])
        self.assertEqual(h["access-control-allow-origin"], DOSKA)
        self.assertEqual(h["access-control-allow-credentials"], "true")
        self.assertEqual(self.started, [])

    def test_other_routes_keep_html_auth(self):
        orig = APP.TOKEN
        APP.TOKEN = "sekrit"
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("POST", "/spawn", body="mode=ops", headers={"Origin": DOSKA})
            r = c.getresponse()
            r.read()
            c.close()
        finally:
            APP.TOKEN = orig
        self.assertEqual(r.status, 401)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_preflight(self):
        st, h, _ = self.req("OPTIONS")
        self.assertEqual(st, 204)
        self.assertEqual(h["access-control-allow-origin"], DOSKA)
        self.assertEqual(h["access-control-allow-credentials"], "true")
        self.assertIn("POST", h["access-control-allow-methods"])
        self.assertIn("Content-Type", h["access-control-allow-headers"])
        st, h, _ = self.req("OPTIONS", origin="https://evil.example")
        self.assertEqual(st, 403)
        self.assertNotIn("access-control-allow-origin", h)


class Kickoff(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._orig = (APP._console_marker_mtime, APP._pane_turn_in_flight, APP.console_send,
                      APP.DOSKA_READY_TIMEOUT, APP.DOSKA_READY_SETTLE)
        APP.console_send = lambda *a, **k: self.sent.append((a, k)) or (True, "sent")
        APP.DOSKA_READY_SETTLE = 0

    def tearDown(self):
        (APP._console_marker_mtime, APP._pane_turn_in_flight, APP.console_send,
         APP.DOSKA_READY_TIMEOUT, APP.DOSKA_READY_SETTLE) = self._orig

    def test_sends_prompt_once_ready(self):
        APP._console_marker_mtime = lambda name: 5.0
        APP._pane_turn_in_flight = lambda name: False
        self.assertTrue(APP.doska_kickoff("m1", 0.0))
        (args, kw), = self.sent
        self.assertEqual(args, (APP.SESSION_PREFIX + "m1", "text", APP.DOSKA_REGISTER_PROMPT))
        self.assertEqual(kw, {"submit": True})
        self.assertTrue(APP.DOSKA_REGISTER_PROMPT.startswith("[doska] "))

    def test_times_out_without_sending(self):
        APP.DOSKA_READY_TIMEOUT = 0.01
        APP._console_marker_mtime = lambda name: 0.0      # SessionStart never fired
        APP._pane_turn_in_flight = lambda name: False
        self.assertFalse(APP.doska_kickoff("m2", 0.0))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
