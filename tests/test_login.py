"""Login (password + TOTP): with MISSION_LOGIN=1 nothing is served without a session —
no page, no POST, no console — and with it unset the app behaves as it always has.

The verifier here is the real one (scripts/miss-login.py), run in a thread with a clock
the tests control; the console bridge is a stand-in that answers like ttyd."""
import base64
import http.client
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
HELPER = os.path.join(ROOT, "scripts", "miss-login.py")
PASSWORD = "correct horse battery"
MACHINE = "m" * 43
ENV = ("MISSION_LOGIN", "MISSION_LOGIN_SOCKET", "MISSION_LOGIN_STATE",
       "MISSION_LOGIN_INSECURE_COOKIE", "MISSION_MACHINE_HEADER_FILE",
       "MISSION_CONSOLE_RELAY", "MISSION_LOGIN_TTL_HOURS")
TMP = APP = OFF = ML = VERIFIER = BRIDGE = None
NOW = [1_900_000_000.0]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def serve_unix(path, handle):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv


class Bridge:
    """Answers like ttyd: a page for GET, 101 + echo for a WebSocket upgrade."""

    def __init__(self, path):
        self.heads = []
        self.srv = serve_unix(path, self.handle)

    def handle(self, conn):
        with conn:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head = buf.decode("latin-1")
            self.heads.append(head)
            if "upgrade: websocket" in head.lower():
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                             b"Connection: Upgrade\r\n\r\n")
                while True:
                    data = conn.recv(4096)
                    if not data:
                        return
                    conn.sendall(b"echo:" + data)
            body = b"<html>terminal page</html>"
            conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\n"
                         b"transfer-encoding: chunked\r\n\r\n"
                         + b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))


def setUpModule():
    global TMP, APP, OFF, ML, VERIFIER, BRIDGE
    # /tmp by name: a UNIX socket path is capped at ~108 bytes and $TMPDIR can be long.
    TMP = tempfile.mkdtemp(prefix="ml-", dir="/tmp")
    os.makedirs(os.path.join(TMP, "missions", "probe"))
    with open(os.path.join(TMP, "missions", "probe", "BRIEF.md"), "w", encoding="utf-8") as fh:
        fh.write("# probe\n\nthrowaway test mission\n")
    ML = load("miss_login", HELPER)
    secret = base64.b32encode(b"12345678901234567890").decode()
    cfg = ML.password_fields(PASSWORD)
    cfg["totp"] = secret
    ML.write_private(os.path.join(TMP, "login.json"), cfg)
    VERIFIER = ML.Verifier(os.path.join(TMP, "login.json"), os.path.join(TMP, "state.json"),
                           clock=lambda: NOW[0])
    lock = threading.Lock()

    def answer(conn):
        with conn, lock:
            ML.answer(conn, VERIFIER)
    serve_unix(os.path.join(TMP, "verify.sock"), answer)
    BRIDGE = Bridge(os.path.join(TMP, "ttyd.sock"))
    with open(os.path.join(TMP, "machine-header"), "w", encoding="utf-8") as fh:
        fh.write("X-Miss-Machine: %s\n" % MACHINE)

    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"
    os.environ["MISSION_LABEL"] = "box"
    for k in ("MISSION_TLS_CERT", "MISSION_TOKEN", "CONSOLE_BASE_URL", "APP_BASE_URL") + ENV:
        os.environ.pop(k, None)
    OFF = load("app_login_off", os.path.join(ROOT, "app.py"))
    os.environ.update({
        "MISSION_LOGIN": "1",
        "MISSION_LOGIN_SOCKET": os.path.join(TMP, "verify.sock"),
        "MISSION_LOGIN_STATE": os.path.join(TMP, "state", "sessions.json"),
        "MISSION_LOGIN_INSECURE_COOKIE": "1",
        "MISSION_MACHINE_HEADER_FILE": os.path.join(TMP, "machine-header"),
        "MISSION_CONSOLE_RELAY": os.path.join(TMP, "ttyd.sock"),
    })
    APP = load("app_login_on", os.path.join(ROOT, "app.py"))
    APP.CONSOLE_RELAY_TICK = 0.1


def tearDownModule():
    for k in ENV:
        os.environ.pop(k, None)
    shutil.rmtree(TMP, ignore_errors=True)


def code(offset=0):
    return ML.totp(b"12345678901234567890", int(NOW[0]) // 30 + offset)


class Base(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = cls.app or APP
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.app.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        # Every test starts a fresh 30 s step away from the last, with clean counters.
        NOW[0] += 300
        VERIFIER.failures.clear()
        APP._login_failures.clear()

    def req(self, method, path, body=None, headers=None, sid=None):
        h = dict(headers or {})
        if sid:
            h["Cookie"] = "ms=" + sid
        if isinstance(body, dict):
            body = "&".join("%s=%s" % (k, v.replace(" ", "+")) for k, v in body.items())
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        c.close()
        return r, data

    def login(self, password=PASSWORD, totp=None, **extra):
        form = {"password": password, "code": code() if totp is None else totp}
        form.update(extra)
        return self.req("POST", "/login", form)

    def assertLoginPage(self, r, body, status=401):
        self.assertEqual(r.status, status)
        self.assertIn("name=password", body)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertEqual(r.getheader("Content-Length"), str(len(body.encode("utf-8"))))
        for leak in ("throwaway", MACHINE, "data-usage-url", "Log out"):
            self.assertNotIn(leak, body)


class NoSession(Base):
    def test_every_get_is_the_login_page(self):
        for path in ("/", "/m/probe", "/m/probe/log", "/m/probe/state", "/canvas",
                     "/canvas.json", "/usage.json", "/index/cards", "/remote", "/ca.crt",
                     "/console/pane.txt?session=mission-probe", "/ttyd/", "/ttyd/token",
                     "/ttyd/ws", "/no/such/page", "/m/probe/console"):
            r, body = self.req("GET", path)
            self.assertLoginPage(r, body)

    def test_every_post_is_refused_and_does_nothing(self):
        for path, form in (("/create", {"name": "intruder"}),
                           ("/m/probe/log/append", {"text": "intruder"}),
                           ("/m/probe/trash", {}), ("/spawn", {"kind": "mission"}),
                           ("/console/key", {"session": "mission-probe", "key": "Enter"}),
                           ("/m/probe/upload?name=x.txt", {"x": "y"})):
            r, body = self.req("POST", path, form)
            self.assertLoginPage(r, body)
        self.assertEqual(os.listdir(os.path.join(TMP, "missions")), ["probe"])
        self.assertEqual(os.listdir(os.path.join(TMP, "missions", "probe")), ["BRIEF.md"])

    def test_made_up_and_stale_cookies(self):
        for sid in ("x", "../../etc/passwd", APP._sid_key("x"), ""):
            r, body = self.req("GET", "/", headers={"Cookie": "ms=" + sid})
            self.assertLoginPage(r, body)
        # The old token is not a side door once the login is on.
        self.assertEqual(APP.TOKEN, "")
        r, body = self.req("GET", "/?token=sekrit", headers={"Cookie": "mt=sekrit"})
        self.assertLoginPage(r, body)

    def test_console_upgrade_never_reaches_the_bridge(self):
        before = len(BRIDGE.heads)
        r, body = self.req("GET", "/ttyd/ws", headers={
            "Connection": "Upgrade", "Upgrade": "websocket",
            "Origin": "http://127.0.0.1:%d" % self.port})
        self.assertLoginPage(r, body)
        self.assertEqual(len(BRIDGE.heads), before)

    def test_pwa_branding_stays_public(self):
        # Chrome and iOS fetch these without cookies; without them there is no install.
        for path in ("/manifest.webmanifest", "/static/icon-192.png"):
            r, _ = self.req("GET", path)
            self.assertEqual(r.status, 200, path)

    def test_login_page_itself(self):
        r, body = self.req("GET", "/login")
        self.assertEqual(r.status, 200)
        self.assertIn("autocomplete=one-time-code", body)
        self.assertIn('rel=manifest', body)


class LoggingIn(Base):
    def test_right_pair_gives_a_session(self):
        r, _ = self.login(next="/m/probe")
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), "/m/probe")
        cookie = r.getheader("Set-Cookie")
        for part in ("HttpOnly", "SameSite=Strict", "Path=/;", "Max-Age=%d" % APP.LOGIN_TTL):
            self.assertIn(part, cookie)
        sid = cookie.split(";")[0].split("=", 1)[1]
        self.assertGreaterEqual(len(sid), 40)
        r, body = self.req("GET", "/", sid=sid)
        self.assertEqual(r.status, 200)
        self.assertIn("Log out", body)
        self.assertIn("probe", body)

    def test_cookie_is_secure_unless_told_otherwise(self):
        h = APP.Handler.__new__(APP.Handler)
        self.assertNotIn("Secure", h._session_cookie("s", 9))
        APP.LOGIN_INSECURE_COOKIE = False
        try:
            self.assertIn("; Secure; ", h._session_cookie("s", 9))
        finally:
            APP.LOGIN_INSECURE_COOKIE = True

    def test_wrong_half_says_nothing_about_which(self):
        r1, b1 = self.login(password="wrong wrong wrong")
        r2, b2 = self.login(totp="000000")
        r3, b3 = self.login(password="", totp="")
        for r, b in ((r1, b1), (r2, b2), (r3, b3)):
            self.assertLoginPage(r, b)
            self.assertIn("Login failed.", b)
        self.assertEqual(b1, b2)

    def test_code_works_once(self):
        used = code()
        self.assertEqual(self.login(totp=used)[0].status, 303)
        r, body = self.login(totp=used)
        self.assertLoginPage(r, body)
        # ...and an older one is no better, though it is inside the clock window.
        r, body = self.login(totp=code(-1))
        self.assertLoginPage(r, body)
        self.assertEqual(self.login(totp=code(1))[0].status, 303)

    def test_code_outside_the_window(self):
        for off in (-2, 2):
            r, body = self.login(totp=code(off))
            self.assertLoginPage(r, body)

    def test_lockout_per_address(self):
        for _ in range(APP.LOGIN_FAILS_PER_ADDRESS):
            self.assertEqual(self.login(totp="000000")[0].status, 401)
        r, body = self.login()                       # right pair, still refused
        self.assertLoginPage(r, body, 429)
        self.assertEqual(VERIFIER.last_counter < int(NOW[0]) // 30, True)

    def test_lockout_for_everyone(self):
        for i in range(APP.LOGIN_FAILS_TOTAL):
            APP.login_failed("10.0.0.%d" % i)
        self.assertTrue(APP.login_locked("192.0.2.1"))
        self.assertLoginPage(*self.login(), status=429)
        # Nothing piles up while locked: refusals are not recorded.
        self.assertLessEqual(len(APP._login_failures), APP.LOGIN_FAILS_TOTAL + 1)

    def test_lockout_ends(self):
        real = time.time
        for _ in range(APP.LOGIN_FAILS_PER_ADDRESS):
            APP.login_failed("127.0.0.1")
        self.assertTrue(APP.login_locked("127.0.0.1"))
        APP.time.time = lambda: real() + APP.LOGIN_FAIL_WINDOW + 1
        try:
            self.assertFalse(APP.login_locked("127.0.0.1"))
        finally:
            APP.time.time = real

    def test_verifier_down_is_a_refusal(self):
        sock = APP.LOGIN_SOCKET
        APP.LOGIN_SOCKET = os.path.join(TMP, "nothing-here.sock")
        try:
            self.assertLoginPage(*self.login())
        finally:
            APP.LOGIN_SOCKET = sock

    def test_next_cannot_leave_the_site(self):
        for bad in ("//evil.example/x", "https://evil.example/", "/\\evil.example",
                    "evil", "", "/login", "/logout", "/a\nb"):
            self.assertEqual(APP._login_next(bad), "/", bad)
        self.assertEqual(APP._login_next("/m/probe/log?x=1"), "/m/probe/log?x=1")

    def test_oversized_login_body(self):
        r, _ = self.req("POST", "/login", "password=" + "a" * 5000)
        self.assertEqual(r.status, 413)


class Sessions(Base):
    def test_expiry(self):
        sid = APP.login_session_new()
        self.assertEqual(self.req("GET", "/", sid=sid)[0].status, 200)
        with APP._login_lock:
            APP._login_sessions[APP._sid_key(sid)] = time.time() - 1
        self.assertLoginPage(*self.req("GET", "/", sid=sid))

    def test_logout(self):
        sid = APP.login_session_new()
        r, _ = self.req("POST", "/logout", sid=sid)
        self.assertEqual(r.status, 303)
        self.assertIn("Max-Age=0", r.getheader("Set-Cookie"))
        self.assertLoginPage(*self.req("GET", "/", sid=sid))

    def test_survives_a_restart_without_storing_the_cookie(self):
        sid = APP.login_session_new()
        with open(APP.LOGIN_STATE, encoding="utf-8") as fh:
            stored = fh.read()
        self.assertNotIn(sid, stored)
        self.assertEqual(os.stat(APP.LOGIN_STATE).st_mode & 0o777, 0o600)
        with APP._login_lock:
            APP._login_sessions = None               # what a restart leaves behind
        self.assertEqual(self.req("GET", "/", sid=sid)[0].status, 200)

    def test_keepalive_does_not_inherit_a_session(self):
        sid = APP.login_session_new()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/", headers={"Cookie": "ms=" + sid})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        c.request("GET", "/")                        # same connection, no cookie
        r = c.getresponse()
        self.assertEqual(r.status, 401)
        self.assertIn("name=password", r.read().decode())
        c.close()


class Machine(Base):
    def test_local_caller_with_the_header(self):
        r, _ = self.req("POST", "/m/probe/log/append", {"text": "from a console"},
                        headers={"X-Miss-Machine": MACHINE})
        self.assertIn(r.status, (200, 303))
        with open(os.path.join(TMP, "missions", "probe", "LOG.md"), encoding="utf-8") as fh:
            self.assertIn("from a console", fh.read())
        os.unlink(os.path.join(TMP, "missions", "probe", "LOG.md"))

    def test_wrong_header(self):
        for value in ("", "m" * 42, "M" * 43):
            r, body = self.req("GET", "/", headers={"X-Miss-Machine": value})
            self.assertLoginPage(r, body)

    def test_header_from_another_host_is_refused(self):
        h = APP.Handler.__new__(APP.Handler)
        h.headers = {"X-Miss-Machine": MACHINE}
        h.client_address = ("127.0.0.1", 1)
        self.assertTrue(h._machine_ok())
        for addr in ("192.0.2.7", "10.0.0.1", "0.0.0.0"):
            h.client_address = (addr, 1)
            self.assertFalse(h._machine_ok(), addr)

    def test_header_never_opens_a_terminal(self):
        before = len(BRIDGE.heads)
        r, _ = self.req("GET", "/ttyd/", headers={"X-Miss-Machine": MACHINE})
        self.assertEqual(r.status, 403)
        self.assertEqual(len(BRIDGE.heads), before)

    def test_short_or_missing_file_means_no_machine_access(self):
        path = os.path.join(TMP, "weak-header")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("X-Miss-Machine: short\n")
        old = APP.MACHINE_HEADER_FILE
        try:
            for p in (path, os.path.join(TMP, "absent")):
                APP.MACHINE_HEADER_FILE = p
                self.assertEqual(APP._read_machine_token(), "")
        finally:
            APP.MACHINE_HEADER_FILE = old

    def test_curl_hint_names_the_file_not_the_secret(self):
        self.assertIn("-H @" + APP.MACHINE_HEADER_FILE, APP.SELF_CURL)
        self.assertNotIn(MACHINE, APP.MISSIONS_CLAUDE_MD)


class Console(Base):
    def upgrade(self, sid, origin=None):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        origin = origin or "http://127.0.0.1:%d" % self.port
        s.sendall(("GET /ttyd/ws HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: Upgrade\r\n"
                   "Upgrade: websocket\r\nOrigin: %s\r\nCookie: ms=%s\r\n"
                   "Sec-WebSocket-Protocol: tty\r\n\r\n" % (self.port, origin, sid)).encode())
        return s

    def head(self, s):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    def test_page_is_relayed_with_a_length(self):
        sid = APP.login_session_new()
        r, body = self.req("GET", "/ttyd/?arg=probe", sid=sid)
        self.assertEqual(r.status, 200)
        self.assertEqual(body, "<html>terminal page</html>")
        self.assertEqual(r.getheader("Content-Length"), str(len(body)))
        self.assertIsNone(r.getheader("Transfer-Encoding"))
        head = BRIDGE.heads[-1]
        self.assertTrue(head.startswith("GET /ttyd/?arg=probe HTTP/1.1\r\n"))
        self.assertNotIn(sid, head)                  # the session stays with the app

    def test_websocket_is_piped_both_ways(self):
        s = self.upgrade(APP.login_session_new())
        self.assertTrue(self.head(s).startswith(b"HTTP/1.1 101"))
        s.sendall(b"keys")
        self.assertEqual(s.recv(4096), b"echo:keys")
        s.close()

    def test_terminal_ends_with_the_session(self):
        sid = APP.login_session_new()
        s = self.upgrade(sid)
        self.assertTrue(self.head(s).startswith(b"HTTP/1.1 101"))
        APP.login_session_drop(sid)
        self.assertEqual(s.recv(4096), b"")          # closed by the app, not by us
        s.close()

    def test_other_site_cannot_open_the_terminal(self):
        before = len(BRIDGE.heads)
        s = self.upgrade(APP.login_session_new(), origin="https://evil.example")
        self.assertTrue(self.head(s).startswith(b"HTTP/1.1 403"))
        s.close()
        self.assertEqual(len(BRIDGE.heads), before)

    def test_bridge_down(self):
        old = APP.CONSOLE_RELAY
        APP.CONSOLE_RELAY = os.path.join(TMP, "nothing-here.sock")
        try:
            r, _ = self.req("GET", "/ttyd/", sid=APP.login_session_new())
            self.assertEqual(r.status, 502)
        finally:
            APP.CONSOLE_RELAY = old

    def test_console_urls_are_same_origin(self):
        self.assertEqual(APP._console_base("box:4200"), "/ttyd")
        r, body = self.req("GET", "/m/probe/dashboard", sid=APP.login_session_new())
        self.assertEqual(r.status, 200)
        self.assertIn('"/ttyd/?arg=probe', body)
        self.assertNotIn(":4201", body)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a certificate")
class ConsoleOverTls(unittest.TestCase):
    """The live boxes serve https: the pipe must not strand bytes that TLS has already
    decrypted, where select() cannot see them."""

    def test_large_frames_both_ways(self):
        pem = os.path.join(TMP, "tls.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-keyout", pem, "-out", pem],
                       check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(pem)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        httpd.daemon_threads = True
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            cctx = ssl.create_default_context(cafile=pem)
            cctx.check_hostname = False
            s = cctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=10))
            s.sendall(("GET /ttyd/ws HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: Upgrade\r\n"
                       "Upgrade: websocket\r\nOrigin: https://127.0.0.1:%d\r\nCookie: ms=%s\r\n"
                       "\r\n" % (port, port, APP.login_session_new())).encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s.recv(4096)
            self.assertTrue(buf.startswith(b"HTTP/1.1 101"), buf[:40])
            got = buf.split(b"\r\n\r\n", 1)[1]
            blob = bytes(range(256)) * 200           # 51200 bytes: several TLS records
            for sent in range(1, 5):
                s.sendall(blob)
                while len(got.replace(b"echo:", b"")) < sent * len(blob):
                    chunk = s.recv(65536)
                    self.assertTrue(chunk, "pipe closed after %d bytes" % len(got))
                    got += chunk
            self.assertEqual(got.replace(b"echo:", b""), blob * 4)
            s.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


class RedirectListener(unittest.TestCase):
    def test_it_serves_nothing_but_the_redirect(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), APP.RedirectHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for method in ("GET", "POST"):
                c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
                c.request(method, "/m/probe", body="x=1" if method == "POST" else None)
                r = c.getresponse()
                body = r.read().decode()
                c.close()
                self.assertEqual(r.status, 301)
                self.assertTrue(r.getheader("Location").startswith("https://"))
                self.assertNotIn("throwaway", body)
        finally:
            httpd.shutdown()
            httpd.server_close()


class FlagOff(Base):
    """MISSION_LOGIN unset: exactly what it was before the login existed."""

    @classmethod
    def setUpClass(cls):
        cls.app = OFF
        super().setUpClass()

    def test_open_as_before(self):
        self.assertFalse(OFF.LOGIN)
        self.assertEqual(OFF.CONSOLE_RELAY, "")
        r, body = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertNotIn("Log out", body)
        self.assertNotIn("/logout", body)

    def test_no_login_routes(self):
        self.assertEqual(self.req("GET", "/login")[0].status, 404)
        self.assertEqual(self.req("POST", "/login", {"password": "x", "code": "1"})[0].status, 404)
        self.assertEqual(self.req("GET", "/ttyd/")[0].status, 404)

    def test_console_is_dialled_directly(self):
        self.assertEqual(OFF._console_base("box:4200"), "http://box:4201")
        self.assertEqual(OFF.SELF_CURL, "curl -s")


class Verifier(unittest.TestCase):
    def test_rfc6238_vectors(self):
        secret = b"12345678901234567890"
        for t, want in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"),
                        (20000000000, "353130")):
            self.assertEqual(ML.totp(secret, t // 30), want)

    def make(self):
        d = tempfile.mkdtemp(dir=TMP)
        cfg = ML.password_fields(PASSWORD)
        cfg["totp"] = base64.b32encode(b"12345678901234567890").decode()
        ML.write_private(os.path.join(d, "login.json"), cfg)
        return ML.Verifier(os.path.join(d, "login.json"), os.path.join(d, "state.json"),
                           clock=lambda: 1_800_000_000.0), d

    def test_own_lockout_and_replay_across_a_restart(self):
        v, d = self.make()
        now = ML.totp(b"12345678901234567890", 1_800_000_000 // 30)
        self.assertTrue(v.check(PASSWORD, now))
        again = ML.Verifier(os.path.join(d, "login.json"), os.path.join(d, "state.json"),
                            clock=lambda: 1_800_000_000.0)
        self.assertFalse(again.check(PASSWORD, now))
        v, _ = self.make()
        for _ in range(ML.MAX_FAILURES):
            self.assertFalse(v.check("nope", "000000"))
        self.assertFalse(v.check(PASSWORD, now))

    def test_not_enrolled_is_a_refusal(self):
        v = ML.Verifier(os.path.join(TMP, "absent.json"), os.path.join(TMP, "s.json"))
        self.assertFalse(v.check(PASSWORD, "000000"))

    def test_cli_end_to_end(self):
        d = tempfile.mkdtemp(dir=TMP)
        base = [sys.executable, HELPER, "--secrets", os.path.join(d, "login.json"),
                "--state", os.path.join(d, "state.json"),
                "--env-file", os.path.join(d, "login.env")]
        r = subprocess.run(base + ["on", "--no-restart"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)         # not enrolled: refuses to lock out
        self.assertFalse(os.path.exists(os.path.join(d, "login.env")))
        r = subprocess.run(base + ["enrol", "--password-stdin"], input="short\n",
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        r = subprocess.run(base + ["enrol", "--password-stdin", "--label", "box"],
                           input=PASSWORD + "\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("otpauth://totp/Miss%20Claude%3Abox?secret=", r.stdout)
        self.assertEqual(os.stat(os.path.join(d, "login.json")).st_mode & 0o777, 0o600)
        with open(os.path.join(d, "login.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertNotIn(PASSWORD, json.dumps(cfg))
        r = subprocess.run(base + ["on", "--no-restart"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(d, "login.env"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "MISSION_LOGIN=1\n")
        r = subprocess.run(base + ["off", "--no-restart"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(d, "login.env")))

        sock = os.path.join(d, "v.sock")
        p = subprocess.Popen(base + ["serve", "--listen", sock])
        try:
            for _ in range(50):
                if os.path.exists(sock):
                    break
                time.sleep(0.1)
            self.assertEqual(os.stat(sock).st_mode & 0o777, 0o600)

            def ask(password, totp):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(sock)
                s.sendall(json.dumps({"password": password, "code": totp}).encode() + b"\n")
                out = json.loads(s.recv(4096))
                s.close()
                return out
            good = ML.totp(base64.b32decode(cfg["totp"]), int(time.time()) // 30)
            self.assertEqual(ask("wrong wrong wrong", good), {"ok": False})
            self.assertEqual(ask(PASSWORD, good), {"ok": True})
            self.assertEqual(ask(PASSWORD, good), {"ok": False})
        finally:
            p.kill()
            p.wait()


if __name__ == "__main__":
    unittest.main()
