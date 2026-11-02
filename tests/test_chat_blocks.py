"""Chat reply blocks (_chat_blocks): code fences, pipe tables and box /
column-aligned diagrams are cut out of Claude's reply so the chat page can draw
them monospace / as a table; everything else stays prose, untouched."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = None
APP = None
ORIG_HOME = None


def setUpModule():
    global TMP, APP, ORIG_HOME
    TMP = tempfile.mkdtemp(prefix="miss-chatblocks-")
    ORIG_HOME = os.environ.get("HOME")
    os.environ["HOME"] = TMP
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"
    os.makedirs(os.environ["MISSIONS_DIR"])
    spec = importlib.util.spec_from_file_location("app_chatblocks", os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)


def tearDownModule():
    if ORIG_HOME is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = ORIG_HOME
    shutil.rmtree(TMP, ignore_errors=True)


class ChatBlocks(unittest.TestCase):
    def b(self, text):
        return APP._chat_blocks(text)

    def test_plain_prose_is_untouched(self):
        for t in ["Hello there.", "Line one\nLine two\n\n- a bullet  with two spaces",
                  "See https://example.com/x | y", "a | b\n---\nnot a table",
                  "Sentence one.  Sentence two."]:
            self.assertIsNone(self.b(t), t)

    def test_code_fence(self):
        got = self.b("Run this:\n\n```bash\nls  -l\n  echo hi\n```\nDone.")
        self.assertEqual(got, [["text", "Run this:"], ["code", "ls  -l\n  echo hi"],
                               ["text", "Done."]])

    def test_unclosed_fence_runs_to_end(self):
        self.assertEqual(self.b("x\n```\ncut off"), [["text", "x"], ["code", "cut off"]])

    def test_fence_content_is_not_a_table_or_diagram(self):
        got = self.b("```\n| a | b |\n|---|---|\n┌─┐\n```")
        self.assertEqual(got, [["code", "| a | b |\n|---|---|\n┌─┐"]])

    def test_pipe_table(self):
        got = self.b("Results:\n| Name | Size |\n|------|:----:|\n| a | 1 |\n| b \\| c | 2 |\n\nAfter.")
        self.assertEqual(got, [["text", "Results:"],
                               ["table", [["Name", "Size"], ["a", "1"], ["b | c", "2"]]],
                               ["text", "After."]])

    def test_table_without_outer_pipes(self):
        got = self.b("Name | Size\n--- | ---\na | 1")
        self.assertEqual(got, [["table", [["Name", "Size"], ["a", "1"]]]])

    def test_box_diagram_run(self):
        art = "┌──────┐\n│ app  │──► ttyd\n└──┬───┘\n   ▼\n  tmux"
        got = self.b("Layout:\n" + art + "\n\nThat's it.")
        # '   ▼' continues the run (geometric shape); '  tmux' has no diagram char.
        self.assertEqual(got, [["text", "Layout:"],
                               ["pre", "┌──────┐\n│ app  │──► ttyd\n└──┬───┘\n   ▼"],
                               ["text", "  tmux\n\nThat's it."]])

    def test_single_box_line_is_a_diagram(self):
        self.assertEqual(self.b("────────"), [["pre", "────────"]])

    def test_ascii_frame(self):
        got = self.b("+-----+\n| box |\n+-----+")
        self.assertEqual(got, [["pre", "+-----+\n| box |\n+-----+"]])

    def test_column_aligned_needs_two_lines(self):
        self.assertIsNone(self.b("NAME    SIZE    DATE"))
        cols = "NAME    SIZE    DATE\napp.py  300K    today\nREADME  10K     today"
        self.assertEqual(self.b(cols), [["pre", cols]])

    def test_transcript_adds_blocks_to_assistant_only(self):
        f = os.path.join(TMP, "t.jsonl")
        table = "| a | b |\n|---|---|\n| 1 | 2 |"
        with open(f, "w") as fh:
            for d in [
                {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                 "message": {"content": table}},
                {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
                 "message": {"content": [{"type": "text", "text": table}]}},
                {"type": "assistant", "timestamp": "2026-01-01T00:00:02Z",
                 "message": {"content": [{"type": "text", "text": "plain"}]}},
            ]:
                fh.write(json.dumps(d) + "\n")
        msgs = {m["text"] + m["role"]: m for m in APP._transcript_chat(f, 10)}
        self.assertNotIn("blocks", msgs[table + "user"])
        self.assertEqual(msgs[table + "assistant"]["blocks"],
                         [["table", [["a", "b"], ["1", "2"]]]])
        self.assertNotIn("blocks", msgs["plainassistant"])


if __name__ == "__main__":
    unittest.main()
