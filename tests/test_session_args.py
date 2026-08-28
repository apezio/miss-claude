#!/usr/bin/env python3
"""Tests for scripts/claude-session-args.py — the console's pre-launch resume decision.

    python3 -m unittest tests/test_session_args.py -v      (from the repo root)

Standard library only. Builds a fake CLAUDE_CONFIG_DIR in a tmpdir with the project
dirs / transcripts Claude Code would have written (slug = the absolute cwd with every
non-alphanumeric character replaced by `-`) and runs the helper as a subprocess, the
way console-session.sh / console-launch.sh call it.

Proves the four cases the console depends on: a known session id resumes, an unknown
one is created with that exact id, a cwd with history continues, an empty one starts
fresh — plus that a bogus id is ignored and nothing ever fails loudly.
"""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HELPER = os.path.join(ROOT, "scripts", "claude-session-args.py")
SID = "c52fc4e4-48c8-51e5-afe2-7251ebb46c58"
OTHER = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


class SessionArgsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="miss-sessargs-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, "config")
        os.makedirs(self.config)

    def project_dir(self, cwd):
        import re
        # realpath, like the helper — /tmp is a symlink on some hosts.
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        d = os.path.join(self.config, "projects", slug)
        os.makedirs(d, exist_ok=True)
        return d

    def transcript(self, cwd, sid):
        with open(os.path.join(self.project_dir(cwd), sid + ".jsonl"), "w") as fh:
            fh.write("{}\n")

    def run_helper(self, *args, **kw):
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = kw.get("config", self.config)
        r = subprocess.run([sys.executable, HELPER, *args], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_existing_id_resumes(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        self.transcript(cwd, SID)
        self.assertEqual(self.run_helper(cwd, SID), "--resume " + SID)

    def test_missing_id_is_created(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        self.transcript(cwd, OTHER)          # a different conversation in the same dir
        self.assertEqual(self.run_helper(cwd, SID), "--session-id " + SID)

    def test_id_from_another_dir_does_not_count(self):
        cwd = os.path.join(self.tmp, "work")
        other_cwd = os.path.join(self.tmp, "elsewhere")
        os.makedirs(cwd)
        os.makedirs(other_cwd)
        self.transcript(other_cwd, SID)
        self.assertEqual(self.run_helper(cwd, SID), "--session-id " + SID)

    def test_history_without_id_continues(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        self.transcript(cwd, SID)
        self.assertEqual(self.run_helper(cwd), "--continue")

    def test_no_history_is_fresh(self):
        cwd = os.path.join(self.tmp, "empty")
        os.makedirs(cwd)
        self.assertEqual(self.run_helper(cwd), "")          # project dir absent
        self.project_dir(cwd)
        self.assertEqual(self.run_helper(cwd), "")          # present but empty

    def test_bogus_id_is_ignored(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        # Not a uuid: falls back to the no-id rules (here: history => --continue).
        self.assertEqual(self.run_helper(cwd, "not-a-uuid"), "")
        self.transcript(cwd, SID)
        self.assertEqual(self.run_helper(cwd, "not-a-uuid"), "--continue")
        # ...and something shell-nasty is likewise just not a uuid.
        self.assertEqual(self.run_helper(cwd, "$(touch pwned); rm -rf /"), "--continue")

    def test_never_fails_loudly(self):
        # No args, and an unreadable/nonexistent config dir: exit 0, no flags.
        self.assertEqual(self.run_helper(), "")
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd, exist_ok=True)
        self.assertEqual(
            self.run_helper(cwd, config=os.path.join(self.tmp, "nope")), "")

    def test_slug_matches_claude_code_layout(self):
        # Verified empirically: every non-alphanumeric char becomes "-".
        cwd = os.path.join(self.tmp, "a.b_c")
        os.makedirs(cwd)
        self.transcript(cwd, SID)
        self.assertTrue(os.path.isdir(os.path.join(
            self.config, "projects",
            __import__("re").sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd)))))
        self.assertEqual(self.run_helper(cwd, SID), "--resume " + SID)


if __name__ == "__main__":
    unittest.main()
