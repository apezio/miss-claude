"""PWA installability: the manifest and icons are served (unauthenticated, since
Chrome fetches them without cookies), the manifest describes a standalone app
rooted at the app base, and every page shell links it."""
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
    TMP = tempfile.mkdtemp(prefix="miss-pwa-")
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"
    os.environ["MISSION_TOKEN"] = "sekrit"           # the strict case: token-gated
    os.environ["MISSION_LABEL"] = "box"
    os.environ.pop("MISSION_TLS_CERT", None)
    os.makedirs(os.path.join(TMP, "missions", "probe"))
    spec = importlib.util.spec_from_file_location("app_pwa", os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Pwa(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_manifest_public_and_standalone(self):
        r, body = self._get("/manifest.webmanifest")        # no token on purpose
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/manifest+json")
        self.assertEqual(r.getheader("Content-Length"), str(len(body)))
        m = json.loads(body)
        self.assertEqual(m["display"], "standalone")
        self.assertEqual(m["start_url"], "/")
        self.assertEqual(m["scope"], "/")
        self.assertEqual(m["short_name"], "Miss Claude")
        self.assertIn("box", m["name"])
        sizes = {i["sizes"] for i in m["icons"]}
        self.assertEqual(sizes, {"192x192", "512x512"})
        for icon in m["icons"]:
            self.assertEqual(icon["type"], "image/png")
            self.assertTrue(icon["src"].startswith("/static/icon-"))

    def test_icons_public_png(self):
        for name in ("icon-192.png", "icon-512.png"):
            r, body = self._get(f"/static/{name}")
            self.assertEqual(r.status, 200, name)
            self.assertEqual(r.getheader("Content-Type"), "image/png")
            self.assertTrue(body.startswith(b"\x89PNG"), name)
            self.assertEqual(r.getheader("Content-Length"), str(len(body)))

    def test_other_static_stays_gated(self):
        r, _ = self._get("/static/nope.png")
        self.assertEqual(r.status, 401)
        r, _ = self._get("/static/../app.py")
        self.assertEqual(r.status, 401)

    def test_pages_link_manifest(self):
        for path in ("/?token=sekrit", "/m/probe/chat?token=sekrit", "/canvas?token=sekrit"):
            r, body = self._get(path)
            self.assertEqual(r.status, 200, path)
            self.assertIn(b'<link rel=manifest href="/manifest.webmanifest">', body, path)
            self.assertIn(b'<meta name=theme-color content="#2f6f4f">', body, path)

    def test_app_base_prefixes_everything(self):
        old = APP.APP_BASE
        APP.APP_BASE = "/miss"
        try:
            m = APP.pwa_manifest()
            self.assertEqual(m["start_url"], "/miss/")
            self.assertEqual(m["scope"], "/miss/")
            self.assertEqual(m["icons"][0]["src"], "/miss/static/icon-192.png")
            self.assertIn('href="/miss/manifest.webmanifest"', APP.pwa_head())
        finally:
            APP.APP_BASE = old


if __name__ == "__main__":
    unittest.main()
