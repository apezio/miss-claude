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
                  "Sentence one.  Sentence two.", "Wow! nice\n2 > 1 here",
                  "!important\n>not a quote"]:
            self.assertIsNone(self.b(t), t)

    def test_quote(self):
        got = self.b("Here is a draft:\n\n> Hi Lee,\n>\n> Thanks,\n> Jon\n\nDone.")
        self.assertEqual(got, [["text", "Here is a draft:"],
                               ["quote", "Hi Lee,\n\nThanks,\nJon"], ["text", "Done."]])

    def test_each_command_is_its_own_block(self):
        got = self.b("NEXT STEP:\n! scripts/status\n! sudo systemctl status x \\\n"
                     "  --no-pager\nthen wait")
        self.assertEqual(got, [["text", "NEXT STEP:"], ["cmd", "! scripts/status"],
                               ["cmd", "! sudo systemctl status x \\\n--no-pager"],
                               ["text", "then wait"]])

    def test_fence_content_is_not_a_quote_or_command(self):
        self.assertEqual(self.b("```\n> q\n! cmd\n```"), [["code", "> q\n! cmd"]])

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


class CopyButtonPages(unittest.TestCase):
    """The console's copy buttons (scripts/console-copy-buttons.js) need the page that
    frames the terminal to answer with reply blocks, and the frame's clipboard
    permission; the chat view draws its own."""

    @classmethod
    def setUpClass(cls):
        os.makedirs(os.path.join(os.environ["MISSIONS_DIR"], "probe"), exist_ok=True)

    def test_framing_pages_answer_after_defining_the_responder(self):
        for page in (APP.render_mission_page("probe", "localhost"),
                     APP.render_canvas_page("localhost"),
                     APP.render_card_page("probe", "localhost")):
            self.assertLess(page.index("function serveCopySrc"),
                            page.index("serveCopySrc(function(src)"))

    def test_console_frames_may_write_the_clipboard(self):
        self.assertIn('title="Claude console" allow="clipboard-write"',
                      APP.render_mission_page("probe", "localhost"))
        self.assertIn('allow="microphone; clipboard-write"',
                      APP.render_card_page("probe", "localhost"))
        self.assertIn('"microphone; clipboard-write"', APP.render_canvas_page("localhost"))

    def test_chat_view_draws_copy_buttons(self):
        page = APP.render_chat_page("probe")
        self.assertIn("function copyBtn", page)
        self.assertIn('kind === "quote"', page)


if __name__ == "__main__":
    unittest.main()
