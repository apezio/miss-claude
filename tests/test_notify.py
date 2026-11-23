"""scripts/notify names the mission the way the dashboard does (app.py mission_title):
mission.json's `title` when set — an auto-named mission whose console still runs in its
old-named folder — else $MISSION_NAME. The tap URL keeps the folder name, which is what
/m/<name> resolves. A stub `curl` on PATH records the request; nothing is sent."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NOTIFY = os.path.join(HERE, "..", "scripts", "notify")

FAKE_CURL = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done > "$CURL_LOG"
"""


class NotifyName(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="miss-notify-")
        self.addCleanup(shutil.rmtree, self.tmp)
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        curl = os.path.join(bindir, "curl")
        with open(curl, "w") as fh:
            fh.write(FAKE_CURL)
        os.chmod(curl, 0o755)
        self.conf = os.path.join(self.tmp, "bark.env")
        with open(self.conf, "w") as fh:
            fh.write('BARK_URL="http://127.0.0.1:9/key"\n'
                     'BARK_OPEN_BASE="https://dash.example:4200"\n')
        self.data = os.path.join(self.tmp, "brave-puffin")
        os.makedirs(self.data)
        self.log = os.path.join(self.tmp, "curl.log")
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"],
                        MISS_BARK_CONFIG=self.conf, CURL_LOG=self.log,
                        MISSION_DATA_DIR=self.data, MISSION_NAME="brave-puffin")

    def push(self, meta, event="input", payload=None):
        with open(os.path.join(self.data, "mission.json"), "w") as fh:
            json.dump(meta, fh)
        if os.path.exists(self.log):
            os.remove(self.log)
        if payload is None:
            subprocess.run([NOTIFY, event, "body"], env=self.env, check=True,
                           stdin=subprocess.DEVNULL, timeout=20)
        else:
            subprocess.run([NOTIFY, event], env=self.env, check=True,
                           input=json.dumps(payload).encode(), timeout=20)
        if not os.path.exists(self.log):
            return None
        with open(self.log) as fh:
            args = fh.read().splitlines()
        return dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("http"))

    def test_title_names_the_push_and_url_keeps_folder(self):
        sent = self.push({"notifications": True, "auto_named": True,
                          "title": "dashboard-nav-declutter"})
        self.assertEqual(sent["title"], "⏸ dashboard-nav-declutter needs input")
        self.assertEqual(sent["url"], "https://dash.example:4200/m/brave-puffin/chat")

    def test_no_usable_title_falls_back_to_folder(self):
        for meta in ({"notifications": True},
                     {"notifications": True, "title": "   "},
                     {"notifications": True, "title": 7}):
            with self.subTest(meta=meta):
                sent = self.push(meta, event="done")
                self.assertEqual(sent["title"], "✅ brave-puffin finished")

    def test_silent_toggle_still_read_alongside_title(self):
        sent = self.push({"notifications": True, "notify_sound": False, "title": "new-name"})
        self.assertEqual(sent["sound"], "silence")
        self.assertEqual(sent["title"], "⏸ new-name needs input")

    def test_stop_with_live_background_task_waits(self):
        # Shape recorded from Claude Code 2.1.329's Stop payload.
        running = {"background_tasks": [{"id": "b1xjs49hn", "type": "shell",
                                         "status": "running", "command": "sleep 25"}]}
        self.assertIsNone(self.push({"notifications": True}, "done", running))
        sent = self.push({"notifications": True}, "done", {"background_tasks": []})
        self.assertEqual(sent["title"], "✅ brave-puffin finished")
        self.assertEqual(sent["body"], "mission brave-puffin")
        finished = {"background_tasks": [{"id": "a1", "status": "completed"}]}
        self.assertIsNotNone(self.push({"notifications": True}, "done", finished))

    def test_idle_prompt_is_dropped_but_permission_pushes(self):
        on = {"notifications": True}
        idle = {"notification_type": "idle_prompt",
                "message": "Claude is waiting for your input"}
        self.assertIsNone(self.push(on, "input", idle))
        sent = self.push(on, "input", {"notification_type": "permission_prompt",
                                       "message": "Claude needs your permission to use Bash"})
        self.assertEqual(sent["body"], "Claude needs your permission to use Bash")

    def test_notifications_off_sends_nothing(self):
        self.assertIsNone(self.push({"title": "new-name"}))


if __name__ == "__main__":
    unittest.main()
