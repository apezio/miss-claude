"""Chat bubble order and paste unwrapping (_transcript_chat).

Two things the phone chat view used to get wrong, both reported as "the reply
lands above the prompt I sent":
  * a message typed while Claude was working is queued, and its transcript line
    is written only when the turn picks it up — so it sat below replies that
    were newer than it;
  * a long message (dictation) arrives as a real paste and is recorded wrapped
    in <pasted_content> tags, which made it start with '<' and be dropped as
    "not typed chat" — leaving the page's optimistic bubble pinned to the
    bottom for good.
"""
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
    TMP = tempfile.mkdtemp(prefix="miss-chatorder-")
    ORIG_HOME = os.environ.get("HOME")
    os.environ["HOME"] = TMP
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"
    os.makedirs(os.environ["MISSIONS_DIR"])
    spec = importlib.util.spec_from_file_location("app_chatorder", os.path.join(HERE, "..", "app.py"))
    APP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(APP)


def tearDownModule():
    if ORIG_HOME is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = ORIG_HOME
    shutil.rmtree(TMP, ignore_errors=True)


def _write(lines):
    f = os.path.join(TMP, "t.jsonl")
    with open(f, "w") as fh:
        for d in lines:
            fh.write(json.dumps(d) + "\n")
    return f


def _user(ts, text):
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


def _assistant(ts, text):
    return {"type": "assistant", "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]}}


def _queued(ts, prompt, written=None):
    return {"type": "attachment", "timestamp": written or ts,
            "attachment": {"type": "queued_command", "prompt": prompt, "timestamp": ts}}


def _chat(lines, limit=40):
    """The bubbles as the page draws them: oldest first."""
    return list(reversed(APP._transcript_chat(_write(lines), limit)))


PASTED = '\n\n<pasted_content id="8c9f">\nfix the ordering please\n</pasted_content id="8c9f">\n'


class Unwrap(unittest.TestCase):
    def test_pasted_user_message_is_a_bubble(self):
        msgs = _chat([_user("2026-09-20T10:00:00.000Z", PASTED)])
        self.assertEqual([(m["role"], m["text"]) for m in msgs],
                         [("user", "fix the ordering please")])

    def test_text_around_the_paste_is_kept(self):
        msgs = _chat([_user("2026-09-20T10:00:00.000Z", "look: " + PASTED.strip())])
        self.assertEqual(msgs[0]["text"], "look: fix the ordering please")

    def test_harness_prompts_are_still_dropped(self):
        msgs = _chat([
            _user("2026-09-20T10:00:00.000Z", "<task-notification>done</task-notification>"),
            _user("2026-09-20T10:00:01.000Z", "[Request interrupted by user]"),
            _queued("2026-09-20T10:00:02.000Z", "<task-notification>x</task-notification>"),
            _assistant("2026-09-20T10:00:03.000Z", "ok"),
        ])
        self.assertEqual([(m["role"], m["text"]) for m in msgs], [("assistant", "ok")])


class Order(unittest.TestCase):
    def test_queued_message_sits_where_it_was_typed(self):
        # Typed at :05 while the turn was running; Claude Code writes the line
        # only at :30, after two replies that are newer than it.
        msgs = _chat([
            _user("2026-09-20T10:00:00.000Z", "go"),
            _assistant("2026-09-20T10:00:10.000Z", "first reply"),
            _assistant("2026-09-20T10:00:20.000Z", "second reply"),
            _queued("2026-09-20T10:00:05.000Z", "wait, stop",
                    written="2026-09-20T10:00:30.000Z"),
        ])
        self.assertEqual([m["text"] for m in msgs],
                         ["go", "wait, stop", "first reply", "second reply"])

    def test_pasted_queued_message_is_ordered_too(self):
        msgs = _chat([
            _assistant("2026-09-20T10:00:10.000Z", "working on it"),
            _queued("2026-09-20T10:00:01.000Z", PASTED, written="2026-09-20T10:00:11.000Z"),
        ])
        self.assertEqual([(m["role"], m["text"]) for m in msgs],
                         [("user", "fix the ordering please"), ("assistant", "working on it")])

    def test_undated_entry_stays_with_its_neighbour(self):
        msgs = _chat([
            _user("2026-09-20T10:00:00.000Z", "go"),
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "no stamp"}]}},
            _assistant("2026-09-20T10:00:20.000Z", "later"),
        ])
        self.assertEqual([m["text"] for m in msgs], ["go", "no stamp", "later"])

    def test_already_ordered_transcript_is_untouched(self):
        msgs = _chat([
            _user("2026-09-20T10:00:00.000Z", "one"),
            _assistant("2026-09-20T10:00:01.000Z", "two"),
            _user("2026-09-20T10:00:02.000Z", "three"),
            _assistant("2026-09-20T10:00:03.000Z", "four"),
        ])
        self.assertEqual([m["text"] for m in msgs], ["one", "two", "three", "four"])


if __name__ == "__main__":
    unittest.main()
