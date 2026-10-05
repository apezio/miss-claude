"""scripts/make-console-index.sh: the console page carries BOTH injected scripts, and
its stamp covers the scripts as well as ttyd — so a script change rebuilds the page
(before, only a ttyd upgrade did, and an edited script silently never went live)."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "make-console-index.sh")


@unittest.skipUnless(shutil.which("ttyd"), "ttyd not installed")
class ConsoleIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="miss-cidx-")
        self.out = os.path.join(self.tmp, "ttyd-index.html")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return subprocess.run(["bash", SCRIPT, "--out", self.out],
                              capture_output=True, text=True, timeout=30)

    def test_both_scripts_injected_and_rerun_is_noop(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out, encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("attachCustomWheelEventHandler(function", page)   # wheel fix
        self.assertIn("miss-claude:file-drop", page)                     # drop relay
        # dragover must relay too: Chrome skips dragenter on the drag after one that was
        # dropped on the overlay, so enter-only raised it every other time.
        self.assertIn("addEventListener('dragover', dragging", page)
        self.assertRegex(page, r"<!-- miss-claude console page \(ttyd [^,]+, scripts [0-9a-f]{12}\) -->")
        self.assertLess(page.rindex("miss-claude:file-drop"), page.rindex("</body>"))
        r = self._run()
        self.assertIn("is current", r.stdout)

    def test_old_stamp_is_rebuilt(self):
        # The file live before this change carried the ttyd-only stamp; it must not
        # count as current, or the drop relay would never reach the console.
        with open(self.out, "w", encoding="utf-8") as fh:
            fh.write("<!-- miss-claude console wheel fix (ttyd 1.7.7) --></body>")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wrote", r.stdout)
        with open(self.out, encoding="utf-8") as fh:
            self.assertIn("miss-claude:file-drop", fh.read())


if __name__ == "__main__":
    unittest.main()
