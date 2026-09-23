"""scripts/outline maps app.py: class methods in the default listing, and the
HTTP dispatch in `routes` mode (which exits 1 on a file that has no handler).

stdlib only:  python3 -m unittest tests/test_outline.py
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUTLINE = os.path.join(ROOT, "scripts", "outline")
APP = os.path.join(ROOT, "app.py")


def run(*args):
    return subprocess.run([sys.executable, OUTLINE, *args], cwd=ROOT,
                          capture_output=True, text=True)


class TestOutline(unittest.TestCase):
    def test_methods_listed(self):
        r = run(APP)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(" meth " in ln and " do_GET " in ln
                            for ln in r.stdout.splitlines()), "no `meth  do_GET` row")

    def test_routes(self):
        r = run(APP, "routes")
        self.assertEqual(r.returncode, 0, r.stderr)
        for pat in ("/spawn", "/canvas.json", "/m/<name>/chat"):
            self.assertIn(pat, r.stdout)

    def test_routes_without_handler(self):
        r = run(OUTLINE, "routes")          # the script itself: no HTTP handler
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("0 routes found", r.stderr)


if __name__ == "__main__":
    unittest.main()
