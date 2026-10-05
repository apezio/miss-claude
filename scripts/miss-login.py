#!/usr/bin/python3 -I
"""miss-login — holds the dashboard's login secrets and answers only yes or no.

The dashboard and every console run as the same account, so a secret the dashboard
can read is a secret every console can read. This helper is the other side of that
line: it runs as ROOT, keeps the password hash and the TOTP secret in a root-only
file, and the dashboard asks it over a UNIX socket whether a password + code pair is
right. The answer is one bit. Nothing in the reply says which half was wrong.

setup.sh --login installs a root-owned COPY of this file and runs that; the copy in
the repo is writable by the service account and must never be what root executes.

    miss-login.py enrol     set the password, make a new TOTP secret, show it ONCE
    miss-login.py passwd    change the password, keep the TOTP secret
    miss-login.py status    is a login enrolled, is it on? (prints no secret)
    miss-login.py on        require the login, and restart the dashboard
    miss-login.py off       stop requiring it (the way back in, from SSH), and restart
    miss-login.py serve     answer the dashboard (systemd socket activation, or --listen)

Python standard library only.
"""
import argparse
import base64
import collections
import getpass
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.parse

SECRETS = "/etc/miss-claude/login.json"
STATE = "/var/lib/miss-claude/login-state.json"
# The switch. mission-dashboard.service reads it as an EnvironmentFile; it is root-owned
# so that turning the login off takes root, like everything else here.
ENV_FILE = "/etc/miss-claude/login.env"
DASHBOARD_UNIT = "mission-dashboard.service"

SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
MIN_PASSWORD = 12
TOTP_STEP = 30
TOTP_DIGITS = 6
# Codes one step either side of now are accepted: phone clocks drift.
TOTP_WINDOW = 1

# Backstop for a caller that talks to the socket directly and so never meets the
# dashboard's own per-address limit. Looser than the dashboard's, so in normal use
# the dashboard refuses first and can say why.
MAX_FAILURES = 60
FAILURE_WINDOW = 15 * 60


def scrypt_hash(password, salt, params):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=params["n"],
                          r=params["r"], p=params["p"], dklen=32)


def totp(secret, counter):
    """RFC 6238 (HMAC-SHA1, as every authenticator app expects)."""
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    num = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(num % 10 ** TOTP_DIGITS).zfill(TOTP_DIGITS)


def write_private(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


class Verifier:
    def __init__(self, secrets_path, state_path, clock=time.time):
        self.secrets_path = secrets_path
        self.state_path = state_path
        self.clock = clock
        self.failures = collections.deque()
        state = read_json(state_path) or {}
        self.last_counter = int(state.get("last_counter", 0))

    def locked(self):
        horizon = self.clock() - FAILURE_WINDOW
        while self.failures and self.failures[0] < horizon:
            self.failures.popleft()
        return len(self.failures) >= MAX_FAILURES

    def check(self, password, code):
        """True only when BOTH halves are right and the code has not been used.

        Both halves are always computed, and every candidate code is compared, so how
        long a refusal takes says nothing about which half failed."""
        if self.locked():
            return False
        # Read per request: an enrol or passwd takes effect without a restart.
        cfg = read_json(self.secrets_path)
        if not cfg:
            return False
        try:
            salt = bytes.fromhex(cfg["salt"])
            want = bytes.fromhex(cfg["hash"])
            secret = base64.b32decode(cfg["totp"])
            params = cfg["scrypt"]
            got = scrypt_hash(password, salt, params)
        except (KeyError, TypeError, ValueError):
            return False
        pw_ok = hmac.compare_digest(got, want)

        code = "".join(code.split())
        now = int(self.clock()) // TOTP_STEP
        matched = 0
        for counter in range(now - TOTP_WINDOW, now + TOTP_WINDOW + 1):
            if hmac.compare_digest(totp(secret, counter).encode(), code.encode()):
                matched = counter
        # A code is good once. Without this, anyone who sees one typed (or reads it
        # off the wire of a broken TLS setup) has until the window closes to reuse it.
        code_ok = matched > self.last_counter

        if pw_ok and code_ok:
            self.last_counter = matched
            try:
                write_private(self.state_path, {"last_counter": matched})
            except OSError:
                # Refuse rather than accept a code we cannot mark as used.
                return False
            return True
        self.failures.append(self.clock())
        return False


def answer(conn, verifier):
    conn.settimeout(3)
    buf = b""
    while b"\n" not in buf and len(buf) < 4096:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    ok = False
    try:
        req = json.loads(buf.decode("utf-8"))
        password, code = req["password"], req["code"]
        if isinstance(password, str) and isinstance(code, str):
            ok = verifier.check(password, code)
    except (ValueError, KeyError, TypeError):
        pass
    conn.sendall(json.dumps({"ok": ok}).encode() + b"\n")


def listening_socket(path):
    """The socket systemd handed us, else one we make at `path` (tests, dev)."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        return socket.socket(fileno=3)
    if not path:
        sys.exit("miss-login: not socket-activated and no --listen given")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    old = os.umask(0o177)
    try:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
    finally:
        os.umask(old)
    srv.listen(8)
    return srv


def cmd_serve(args):
    verifier = Verifier(args.secrets, args.state)
    srv = listening_socket(args.listen)
    # One caller at a time, on purpose: the password hash costs ~0.1 s, which makes
    # the queue itself a throttle.
    while True:
        conn, _ = srv.accept()
        try:
            answer(conn, verifier)
        except OSError:
            pass
        finally:
            conn.close()


def read_password(args):
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("New dashboard password: ")
        if getpass.getpass("Again: ") != pw:
            sys.exit("miss-login: the two passwords differ — nothing changed")
    if len(pw) < MIN_PASSWORD:
        sys.exit("miss-login: password too short (minimum %d characters) — nothing changed"
                 % MIN_PASSWORD)
    return pw


def password_fields(pw):
    salt = secrets.token_bytes(16)
    return {"scrypt": SCRYPT, "salt": salt.hex(), "hash": scrypt_hash(pw, salt, SCRYPT).hex()}


def cmd_enrol(args):
    cfg = password_fields(read_password(args))
    cfg["totp"] = base64.b32encode(secrets.token_bytes(20)).decode()
    write_private(args.secrets, cfg)
    label = urllib.parse.quote("Miss Claude:%s" % args.label)
    uri = ("otpauth://totp/%s?secret=%s&issuer=Miss%%20Claude&algorithm=SHA1&digits=%d&period=%d"
           % (label, cfg["totp"], TOTP_DIGITS, TOTP_STEP))
    print("Enrolled. Add this to your authenticator app now — it is not shown again:\n")
    print("  secret: %s" % cfg["totp"])
    print("  uri:    %s\n" % uri)
    print("Any earlier authenticator entry for this host no longer works.")


def cmd_passwd(args):
    cfg = read_json(args.secrets)
    if not cfg or "totp" not in cfg:
        sys.exit("miss-login: nothing enrolled yet — run: miss-login.py enrol")
    cfg.update(password_fields(read_password(args)))
    write_private(args.secrets, cfg)
    print("Password changed. The authenticator entry is unchanged.")


def cmd_status(args):
    try:
        st = os.stat(args.secrets)
    except FileNotFoundError:
        print("not enrolled (%s is missing)" % args.secrets)
        return 1
    except PermissionError:
        print("cannot read %s — run as root" % args.secrets)
        return 1
    ok = st.st_uid == 0 and not st.st_mode & 0o077
    print("enrolled; %s is owned by uid %d, mode %o%s"
          % (args.secrets, st.st_uid, st.st_mode & 0o777,
             "" if ok else "  <-- WARNING: should be root-owned, mode 600"))
    print("login is %s (%s)" % ("ON" if login_is_on(args.env_file) else "OFF", args.env_file))
    return 0 if ok else 1


def login_is_on(env_file):
    try:
        with open(env_file, encoding="utf-8") as fh:
            return any(line.strip() == "MISSION_LOGIN=1" for line in fh)
    except OSError:
        return False


def restart_dashboard(args):
    if args.no_restart:
        print("Not restarted (--no-restart). It takes effect on the next restart of %s."
              % DASHBOARD_UNIT)
        return 0
    rc = subprocess.run(["systemctl", "restart", DASHBOARD_UNIT]).returncode
    print("Restarted %s." % DASHBOARD_UNIT if rc == 0 else
          "FAILED to restart %s — run: systemctl restart %s" % (DASHBOARD_UNIT, DASHBOARD_UNIT))
    return rc


def cmd_on(args):
    cfg = read_json(args.secrets)
    if not cfg or "totp" not in cfg or "hash" not in cfg:
        sys.exit("miss-login: nothing enrolled — turning the login on now would lock "
                 "everyone out. Run: miss-login.py enrol")
    os.makedirs(os.path.dirname(args.env_file), mode=0o755, exist_ok=True)
    tmp = "%s.%d.tmp" % (args.env_file, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("MISSION_LOGIN=1\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, args.env_file)
    print("Login is ON. Keep this SSH session open until you have logged in from a "
          "browser;\nthe way back is: miss-login.py off")
    return restart_dashboard(args)


def cmd_off(args):
    try:
        os.unlink(args.env_file)
    except FileNotFoundError:
        pass
    print("Login is OFF: the dashboard and console are open to whoever the firewall lets in.")
    return restart_dashboard(args)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--secrets", default=SECRETS, help="default %s" % SECRETS)
    ap.add_argument("--state", default=STATE, help="default %s" % STATE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("enrol", "passwd"):
        p = sub.add_parser(name)
        p.add_argument("--password-stdin", action="store_true",
                       help="read the password from stdin instead of prompting")
        if name == "enrol":
            p.add_argument("--label", default=socket.gethostname().split(".")[0],
                           help="name shown in the authenticator app")
    ap.add_argument("--env-file", default=ENV_FILE, help="default %s" % ENV_FILE)
    sub.add_parser("status")
    for name in ("on", "off"):
        sub.add_parser(name).add_argument("--no-restart", action="store_true")
    p = sub.add_parser("serve")
    p.add_argument("--listen", default="", help="UNIX socket path (without systemd)")
    args = ap.parse_args()
    return {"enrol": cmd_enrol, "passwd": cmd_passwd, "status": cmd_status,
            "on": cmd_on, "off": cmd_off, "serve": cmd_serve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
