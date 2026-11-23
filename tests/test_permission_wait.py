"""A console sitting on a permission dialog reads "waiting" (red card, red tab dot,
"stopped — needs approval" on the chat line) instead of "working", and "working"
again once the operator answers. The console's PermissionRequest/Notification hook
(scripts/mission-console-session.py) touches <mission>/.console-permission;
app.py (_permission_wait) trusts it until the transcript moves on or the screen's
spinner returns. Runs the real hook script; the screen is stubbed."""
import http.client
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "scripts", "mission-console-session.py")

TMP = None
APP = None


def setUpModule():
    global TMP, APP
    TMP = tempfile.mkdtemp(prefix="miss-perm-")
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"
    os.environ.pop("MISSION_TOKEN", None)
    os.environ.pop("MISSION_STALE_TURN", None)
    os.makedirs(os.path.join(TMP, "missions", "probe"))
    spec = importlib.util.spec_from_file_location("app_perm", os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def _entry(role, content, stop_reason=None, **extra):
    d = {"type": role, "message": {"role": role, "content": content}}
    if role == "assistant":
        d["message"]["stop_reason"] = stop_reason
    d.update(extra)
    return json.dumps(d)


TOOL_CALL = _entry("assistant", [{"type": "tool_use", "id": "t", "name": "Bash"}],
                   "tool_use", uuid="a1")
TOOL_RESULT = _entry("user", [{"type": "tool_result", "tool_use_id": "t"}], uuid="r1")
DENIED = _entry("user", [{"type": "text", "text": "[Request interrupted by user for tool use]"}],
                uuid="d1")


class PermissionWait(unittest.TestCase):

    def setUp(self):
        self.data = os.path.join(TMP, "missions", "probe")
        self.marker = os.path.join(self.data, ".console-permission")
        self.f = os.path.join(TMP, "transcript.jsonl")
        self.screen = False             # the dialog is up: no spinner
        self._orig = (APP._chat_transcript_file, APP._pane_turn_in_flight)
        APP._chat_transcript_file = lambda name: self.f
        APP._pane_turn_in_flight = lambda name: self.screen

    def tearDown(self):
        APP._chat_transcript_file, APP._pane_turn_in_flight = self._orig
        for p in (self.marker, os.path.join(self.data, ".console-session"), self.f):
            try:
                os.unlink(p)
            except OSError:
                pass

    def write(self, *lines, age=0):
        with open(self.f, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        t = time.time() - age
        os.utime(self.f, (t, t))

    def hook(self, event, **payload):
        payload = dict(payload, hook_event_name=event, transcript_path=self.f, session_id="s")
        subprocess.run([sys.executable, HOOK], input=json.dumps(payload), text=True, check=True,
                       env=dict(os.environ, MISSION_DATA_DIR=self.data), timeout=30,
                       capture_output=True)

    def activity(self):
        return APP.mission_activity_detail("probe")

    def test_dialog_waits_then_approval_works(self):
        self.write(TOOL_CALL, age=10)
        self.assertEqual(self.activity(), ("working", ""))
        self.hook("PermissionRequest", tool_name="Bash")
        state, turn = self.activity()
        self.assertEqual(state, "waiting")
        self.assertTrue(turn.startswith("perm:"))
        # Approved: the tool runs, the spinner is back before any transcript write.
        self.screen = True
        self.assertEqual(self.activity(), ("working", ""))
        # The tool finished: its result moves the transcript past the marker.
        self.screen = False
        self.write(TOOL_CALL, TOOL_RESULT)
        os.utime(self.f, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.activity(), ("working", ""))

    def test_unreadable_screen_keeps_the_wait(self):
        self.write(TOOL_CALL, age=10)
        self.hook("PermissionRequest")
        self.screen = None
        self.assertEqual(self.activity()[0], "waiting")

    def test_denial_is_the_interrupt_wait(self):
        self.write(TOOL_CALL, age=10)
        self.hook("PermissionRequest")
        self.write(TOOL_CALL, TOOL_RESULT, DENIED)
        os.utime(self.f, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.activity(), ("waiting", "d1"))

    def test_old_marker_does_not_touch_a_later_turn(self):
        self.write(TOOL_CALL, age=10)
        self.hook("PermissionRequest")
        old = time.time() - 60
        os.utime(self.marker, (old, old))
        self.write(TOOL_CALL)
        self.assertEqual(self.activity(), ("working", ""))

    def test_late_notification_keeps_the_wait_id(self):
        # The permission_prompt Notification follows ~6s later; re-touching the
        # marker would change the id and re-light an acknowledged wait.
        # Claude Code writes the tool call ~0.1s after the hook ran (seen live).
        self.write(_entry("user", "go"), age=10)
        self.hook("PermissionRequest")
        time.sleep(0.1)
        self.write(TOOL_CALL)
        first = self.activity()
        self.assertEqual(first[0], "waiting")
        time.sleep(0.02)
        self.hook("Notification", notification_type="permission_prompt")
        self.assertEqual(self.activity(), first)

    def test_notification_alone_marks_the_wait(self):
        self.write(TOOL_CALL, age=10)
        self.hook("Notification", notification_type="permission_prompt")
        self.assertEqual(self.activity()[0], "waiting")

    def test_idle_notification_changes_nothing(self):
        self.hook("SessionStart", source="clear")
        session = os.path.join(self.data, ".console-session")
        with open(session) as fh:
            before = fh.read()
        self.hook("Notification", notification_type="idle_prompt")
        self.assertFalse(os.path.exists(self.marker))
        with open(session) as fh:
            self.assertEqual(fh.read(), before)

    def test_hook_prints_nothing(self):
        # Any stdout from a PermissionRequest hook could be read as a decision.
        self.write(TOOL_CALL)
        out = subprocess.run([sys.executable, HOOK], text=True, capture_output=True,
                             input=json.dumps({"hook_event_name": "PermissionRequest",
                                               "transcript_path": self.f}),
                             env=dict(os.environ, MISSION_DATA_DIR=self.data), timeout=30)
        self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_chat_json_says_needs_approval(self):
        self.write(TOOL_CALL, age=10)
        self.hook("PermissionRequest")
        orig = (APP.session_running, APP.chat_messages)
        APP.session_running = lambda name: True
        APP.chat_messages = lambda name, **kw: []
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            c.request("GET", "/m/probe/chat.json", headers={"X-Requested-With": "fetch"})
            data = json.loads(c.getresponse().read())
            c.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            APP.session_running, APP.chat_messages = orig
        self.assertFalse(data["working"])
        self.assertTrue(data["turn"].startswith("perm:"))
        self.assertEqual(data["last"], {"stopped": True, "approval": True})
        self.assertIn('"stopped \\u2014 needs approval"', APP.CHAT_STATUS_JS)

if __name__ == "__main__":
    unittest.main()
