"""Doska links: /open resolves a mission, the SessionStart identity block.

stdlib only:  python3 -m unittest tests/test_doska.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "scripts", "doska-project-context.py")
TMP = APP = None


def setUpModule():
    global TMP, APP
    TMP = tempfile.mkdtemp(prefix="miss-doska-")
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_CANVAS_FILE"] = os.path.join(TMP, "state", "canvas.json")
    os.environ["WORKTREES_DIR"] = os.path.join(TMP, "worktrees")
    os.environ["MISSION_ARCHIVES_DIR"] = os.path.join(TMP, "archives")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"
    os.environ.pop("MISSION_TOKEN", None)
    os.environ.pop("MISSION_TLS_CERT", None)
    os.makedirs(os.path.join(TMP, "missions", "ops-one"))
    spec = importlib.util.spec_from_file_location("app_doska", os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def hook(env, stdin='{"session_id": "abc"}', *args):
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("MISS_", "MISSION_"))}
    e["HOME"] = TMP
    e.update(env)
    r = subprocess.run([sys.executable, HOOK, *args], input=stdin, env=e,
                       capture_output=True, text=True)
    return r.stdout


class OpenLink(unittest.TestCase):
    def test_by_name(self):
        self.assertEqual(APP.mission_for_open("", "", "ops-one"), "ops-one")

    def test_unknown_name_falls_through(self):
        self.assertIsNone(APP.mission_for_open("", "", "nope"))
        self.assertIsNone(APP.mission_for_open("", "", "../etc"))

    def test_ops_mission_folder_as_worktree(self):
        wt = os.path.join(TMP, "missions", "ops-one")
        self.assertEqual(APP.mission_for_open(wt, "", "renamed-away"), "ops-one")


class IdentityBlock(unittest.TestCase):
    def test_silent_outside_a_mission(self):
        self.assertEqual(hook({}), "")

    def test_ops_mission_uses_its_folder(self):
        d = os.path.join(TMP, "missions", "ops-one")
        out = hook({"MISSION_DATA_DIR": d, "MISSION_NAME": "ops-one"})
        self.assertIn(f"worktree: {d}\n", out)
        self.assertIn("claude_session_id: abc\n", out)
        self.assertNotIn("project_id:", out)

    def test_project_and_dashboard_from_mission_json(self):
        d = os.path.join(TMP, "missions", "proj-m")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "mission.json"), "w") as fh:
            json.dump({"mode": "ops", "doska_project": "proj-0123456789ab",
                       "dashboard_url": "https://jump.example:4200"}, fh)
        out = hook({"MISSION_DATA_DIR": d, "MISS_WORKTREE": "/wt/x"})
        self.assertIn("worktree: /wt/x\n", out)
        self.assertIn("project_id: proj-0123456789ab\n", out)
        self.assertIn("dashboard_url: https://jump.example:4200\n", out)

    def test_dashboard_url_file_fallback(self):
        os.makedirs(os.path.join(TMP, ".miss-claude"), exist_ok=True)
        with open(os.path.join(TMP, ".miss-claude", "dashboard-url"), "w") as fh:
            fh.write("https://dev.example:4200\n")
        out = hook({"MISSION_DATA_DIR": os.path.join(TMP, "missions", "ops-one")})
        self.assertIn("dashboard_url: https://dev.example:4200\n", out)

    def test_label_is_the_auto_named_title(self):
        d = os.path.join(TMP, "missions", "tidy-wren")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "mission.json"), "w") as fh:
            json.dump({"mode": "ops", "auto_named": True,
                       "title": "fix-payments-status"}, fh)
        out = hook({"MISSION_DATA_DIR": d, "MISSION_NAME": "tidy-wren"})
        self.assertIn("mission: tidy-wren\n", out)
        self.assertIn("label: fix-payments-status\n", out)
        self.assertIn("register_session", out)

    def test_label_falls_back_to_mission_then_worktree(self):
        d = os.path.join(TMP, "missions", "ops-one")
        out = hook({"MISSION_DATA_DIR": d, "MISSION_NAME": "ops-one"})
        self.assertIn("label: ops-one\n", out)
        out = hook({"MISS_WORKTREE": "/wt/blue-fox/"})
        self.assertIn("label: blue-fox\n", out)

    def test_bad_stdin_never_fails(self):
        out = hook({"MISSION_DATA_DIR": "/x"}, stdin="not json")
        self.assertIn("claude_session_id: \n", out)


def write_meta(d, meta):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "mission.json"), "w") as fh:
        json.dump(meta, fh)


def prompt_hook(env):
    return hook(env, '{"session_id": "abc"}', "prompt")


class LabelChange(unittest.TestCase):
    def test_title_set_after_start_is_announced_once(self):
        d = os.path.join(TMP, "missions", "calm-otter")
        write_meta(d, {"mode": "ops", "auto_named": True})
        env = {"MISSION_DATA_DIR": d, "MISSION_NAME": "calm-otter"}
        self.assertIn("label: calm-otter\n", hook(env))
        self.assertEqual(prompt_hook(env), "")
        write_meta(d, {"mode": "ops", "auto_named": True, "title": "ipv6-leak",
                       "doska_project": "proj-0123456789ab"})
        out = prompt_hook(env)
        self.assertIn("== DOSKA LABEL CHANGED ==", out)
        self.assertIn("label: ipv6-leak\n", out)
        self.assertIn("project_id: proj-0123456789ab\n", out)
        self.assertIn(f"worktree: {d}\n", out)
        self.assertIn("call `register_session`", out)
        self.assertNotIn("session_start", out)
        self.assertEqual(prompt_hook(env), "")

    def test_no_project_resends_per_returned_project(self):
        d = os.path.join(TMP, "missions", "dry-lark")
        write_meta(d, {"mode": "dev", "title": "tunnel-churn"})
        out = prompt_hook({"MISSION_DATA_DIR": d, "MISSION_NAME": "dry-lark",
                           "MISS_WORKTREE": "/wt/dry-lark"})
        self.assertIn("worktree: /wt/dry-lark\n", out)
        self.assertIn("label: tunnel-churn\n", out)
        self.assertIn("call `session_start`", out)
        self.assertNotIn("project_id:", out)

    def test_quiet_when_name_never_changed(self):
        d = os.path.join(TMP, "missions", "ops-one")
        self.assertEqual(prompt_hook({"MISSION_DATA_DIR": d, "MISSION_NAME": "ops-one"}), "")
        self.assertEqual(prompt_hook({}), "")
        self.assertEqual(prompt_hook({"MISS_WORKTREE": "/wt/x"}), "")

    def test_session_start_resets_the_announced_label(self):
        d = os.path.join(TMP, "missions", "odd-newt")
        write_meta(d, {"mode": "ops", "title": "first-name"})
        env = {"MISSION_DATA_DIR": d, "MISSION_NAME": "odd-newt"}
        hook(env)
        self.assertEqual(prompt_hook(env), "")

    def test_unwritable_folder_never_fails(self):
        out = prompt_hook({"MISSION_DATA_DIR": "/nonexistent/m", "MISSION_NAME": "m"})
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
