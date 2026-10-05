"""File drop: POST /m/<name>/upload stores the raw body under <mission>/uploads/
with a safe, never-overwritten name, and reports whether the console was told."""
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = None
APP = None


def setUpModule():
    global TMP, APP
    TMP = tempfile.mkdtemp(prefix="miss-upload-")
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"        # no console: notify must refuse, not fail
    os.environ["MISSION_TOKEN"] = "sekrit"
    os.environ.pop("MISSION_TLS_CERT", None)
    os.makedirs(os.path.join(TMP, "missions", "probe"))
    spec = importlib.util.spec_from_file_location("app_upload", os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Upload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post(self, path, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def _upload(self, fname, body, mission="probe"):
        return self._post(f"/m/{mission}/upload?token=sekrit&name={fname}", body,
                          {"Content-Type": "application/octet-stream"})

    def test_binary_body_stored_verbatim(self):
        blob = bytes(range(256)) * 4 + b"\xff\xfe not utf-8"
        r, data = self._upload("report.pdf", blob)
        self.assertEqual(r.status, 200, data)
        j = json.loads(data)
        self.assertTrue(j["ok"])
        self.assertEqual(j["path"], "uploads/report.pdf")
        self.assertFalse(j["notified"])               # tmux is /bin/false here
        with open(os.path.join(TMP, "missions", "probe", "uploads", "report.pdf"), "rb") as fh:
            self.assertEqual(fh.read(), blob)

    def test_same_name_never_overwrites(self):
        self._upload("dup.txt", b"one")
        r, data = self._upload("dup.txt", b"two")
        self.assertEqual(json.loads(data)["path"], "uploads/dup-2.txt")
        updir = os.path.join(TMP, "missions", "probe", "uploads")
        with open(os.path.join(updir, "dup.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"one")
        with open(os.path.join(updir, "dup-2.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"two")

    def test_name_is_sanitised(self):
        self.assertEqual(APP.safe_upload_name("../../etc/passwd"), "passwd")
        self.assertEqual(APP.safe_upload_name("C:\\Users\\me\\a b.png"), "a b.png")
        self.assertEqual(APP.safe_upload_name(".env"), "env")
        self.assertEqual(APP.safe_upload_name("we/ird$na;me.tar.gz"), "ird_na_me.tar.gz")
        self.assertEqual(APP.safe_upload_name(""), "upload")
        self.assertEqual(APP.safe_upload_name("..."), "upload")
        r, data = self._upload("..%2F..%2Fescape.txt", b"x")
        self.assertEqual(json.loads(data)["path"], "uploads/escape.txt")
        self.assertFalse(os.path.exists(os.path.join(TMP, "missions", "escape.txt")))

    def test_refusals(self):
        r, data = self._upload("x.txt", b"")
        self.assertEqual(r.status, 400)
        r, data = self._upload("x.txt", b"x", mission="nope")
        self.assertEqual(r.status, 404)
        r, data = self._post("/m/probe/upload?name=x.txt", b"x")   # no token
        self.assertEqual(r.status, 401)
        # Too big is judged on Content-Length before the body is read.
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/m/probe/upload?token=sekrit&name=big.bin")
        c.putheader("Content-Length", str(APP.MAX_UPLOAD + 1))
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 413)
        self.assertFalse(json.loads(r.read())["ok"])
        c.close()
        self.assertFalse(os.path.exists(os.path.join(TMP, "missions", "probe", "uploads", "big.bin")))

    def test_form_routes_still_decode_text(self):
        # The upload branch sits before the form decode; the rest of do_POST is unchanged.
        r, data = self._post("/m/probe/log/append?token=sekrit", b"text=h%C3%A9llo",
                             {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 200, data)
        with open(os.path.join(TMP, "missions", "probe", "LOG.md"), encoding="utf-8") as fh:
            self.assertIn("héllo", fh.read())

    def test_page_has_drop_wiring(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/m/probe/dashboard?token=sekrit")
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertIn("wireDrop()", body)
        self.assertIn(".dropzone", body)
        # Drops over the console iframe arrive by postMessage (console-drop-relay.js).
        self.assertIn("miss-claude:file-drop", body)
        self.assertIn("ev.source !== frame.contentWindow", body)
        # A drag that ends with no dragleave must not leave the overlay over the page.
        self.assertIn('zone.addEventListener("pointermove"', body)


if __name__ == "__main__":
    unittest.main()
