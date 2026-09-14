"""Canvas dashboard (/canvas): the per-card activity state read off a transcript
(mission_activity), the layout store's validation (clean_canvas_layout), and the
routes — /canvas, /canvas.json, /canvas/layout, the embedded chat page, and the
JSON flavour of /spawn the canvas uses (canvas=1) with the headless console start
stubbed out."""
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

TMP = None
APP = None


def setUpModule():
    global TMP, APP
    TMP = tempfile.mkdtemp(prefix="miss-canvas-")
    os.environ["MISSIONS_DIR"] = os.path.join(TMP, "missions")
    os.environ["MISSION_CANVAS_FILE"] = os.path.join(TMP, "state", "canvas.json")
    os.environ["MISSION_PORT"] = "0"
    os.environ["MISSION_TMUX"] = "/bin/false"        # no tmux: nothing is running
    os.environ.pop("MISSION_TOKEN", None)
    os.environ.pop("MISSION_TLS_CERT", None)
    os.makedirs(os.path.join(TMP, "missions", "probe"))
    os.makedirs(os.path.join(TMP, "missions", "other"))
    spec = importlib.util.spec_from_file_location("app_canvas", os.path.join(HERE, "..", "app.py"))
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


class Activity(unittest.TestCase):
    """mission_activity reads the newest conversational entry of the live transcript."""

    def setUp(self):
        self.f = os.path.join(TMP, "transcript.jsonl")
        # Point the transcript locator straight at our file.
        self._orig = APP._chat_transcript_file
        APP._chat_transcript_file = lambda name: self.f

    def tearDown(self):
        APP._chat_transcript_file = self._orig

    def _write(self, *lines):
        with open(self.f, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_not_live_is_off(self):
        self.assertEqual(APP.mission_activity("probe", live=False), "off")

    def test_end_turn_is_waiting(self):
        self._write(_entry("user", "do it"),
                    _entry("assistant", [{"type": "text", "text": "done"}], "end_turn"))
        self.assertEqual(APP.mission_activity("probe"), "waiting")

    def test_wait_id_names_the_turn(self):
        # The wait id is the transcript uuid of the entry that ended the turn, so
        # an acknowledged wait survives a reload but the next turn's wait is new.
        self._write(_entry("user", "do it"),
                    _entry("assistant", [{"type": "text", "text": "done"}], "end_turn", uuid="u1"))
        self.assertEqual(APP.mission_activity_detail("probe"), ("waiting", "u1"))
        self._write(_entry("user", "more"),
                    _entry("assistant", [{"type": "text", "text": "ok"}], "end_turn", uuid="u2"))
        self.assertEqual(APP.mission_activity_detail("probe"), ("waiting", "u2"))
        self._write(_entry("user", "more"),
                    _entry("assistant", [{"type": "tool_use"}], "tool_use", uuid="u3"))
        self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
        self.assertEqual(APP.mission_activity_detail("probe", live=False), ("off", ""))

    def test_tool_use_is_working(self):
        self._write(_entry("assistant", [{"type": "tool_use", "id": "x"}], "tool_use"))
        self.assertEqual(APP.mission_activity("probe"), "working")

    def test_mid_stream_is_working(self):
        self._write(_entry("assistant", [{"type": "thinking"}], None))
        self.assertEqual(APP.mission_activity("probe"), "working")

    def test_user_prompt_is_working(self):
        self._write(_entry("assistant", [{"type": "text", "text": "done"}], "end_turn"),
                    _entry("user", "now this"))
        self.assertEqual(APP.mission_activity("probe"), "working")

    def test_tool_result_is_working(self):
        self._write(_entry("user", [{"type": "tool_result", "content": "ok"}]))
        self.assertEqual(APP.mission_activity("probe"), "working")

    def test_interrupt_is_waiting(self):
        self._write(_entry("user", "[Request interrupted by user]"))
        self.assertEqual(APP.mission_activity("probe"), "waiting")

    def test_prompt_with_idle_screen_is_waiting(self):
        # An Esc before the first assistant entry writes nothing: the transcript
        # ends on the prompt. The console screen breaks the tie.
        self._write(_entry("assistant", [{"type": "text", "text": "done"}], "end_turn"),
                    _entry("user", "YES SHIP", uuid="p1"))
        orig = APP._pane_turn_in_flight
        try:
            APP._pane_turn_in_flight = lambda name: False
            self.assertEqual(APP.mission_activity_detail("probe"), ("waiting", "p1"))
            APP._pane_turn_in_flight = lambda name: True
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
            APP._pane_turn_in_flight = lambda name: None      # unreadable: unchanged
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
            # An interrupt notice / a reply in flight never asks the screen.
            APP._pane_turn_in_flight = lambda name: 1 / 0
            self._write(_entry("user", "[Request interrupted by user]", uuid="i1"))
            self.assertEqual(APP.mission_activity_detail("probe"), ("waiting", "i1"))
            self._write(_entry("user", "go"), _entry("assistant", [], "tool_use"))
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
            # Nor does a tool_result: Claude streams its reply after the last
            # tool with that entry newest, and the spinner line can be off the
            # screen then (a message queued mid-turn redraws it) — the card
            # went red seconds before the reply landed.
            self._write(_entry("user", "go"), _entry("assistant", [], "tool_use"),
                        _entry("user", [{"type": "tool_result", "content": "ok"}]))
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
        finally:
            APP._pane_turn_in_flight = orig

    def test_turn_elapsed(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = (now - datetime.timedelta(seconds=95)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        msgs = [{"role": "user", "text": "a", "ts": ts},
                {"role": "assistant", "text": "b", "ts": ts}]
        e = APP.turn_elapsed(msgs)
        self.assertTrue(94 <= e <= 97, e)
        self.assertIsNone(APP.turn_elapsed([]))
        self.assertIsNone(APP.turn_elapsed([{"role": "user", "text": "a", "ts": ""}]))
        self.assertIsNone(APP.turn_elapsed([{"role": "user", "text": "a", "ts": "garbage"}]))
        # No fraction is fine too; a future stamp (clock skew) clamps to 0.
        self.assertEqual(APP.turn_elapsed([{"role": "user", "ts": "2999-01-01T00:00:00Z"}]), 0)

    def test_last_turn(self):
        rec = json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 29245,
                          "timestamp": "2026-09-09T03:50:11.734Z"})
        self._write(_entry("user", "go", uuid="u1", timestamp="2026-09-09T03:49:42.000Z"),
                    _entry("assistant", [{"type": "text", "text": "ok"}], "end_turn",
                           uuid="a1", parentUuid="u1", timestamp="2026-09-09T03:50:11.000Z"),
                    rec)
        self.assertEqual(APP.last_turn("probe"),
                         {"done_ms": 29245, "ended": "2026-09-09T03:50:11.734Z"})
        # No record (older transcript): assistant time minus its prompt's.
        self._write(_entry("user", "go", uuid="u1", timestamp="2026-09-09T03:49:42.000Z"),
                    _entry("assistant", [{"type": "text", "text": "ok"}], "end_turn",
                           uuid="a1", parentUuid="u1", timestamp="2026-09-09T03:50:11.000Z"))
        self.assertEqual(APP.last_turn("probe"),
                         {"done_ms": 29000, "ended": "2026-09-09T03:50:11.000Z"})
        # Interrupted: the newest entry is a notice / a bare prompt.
        self._write(_entry("assistant", [{"type": "text", "text": "ok"}], "end_turn"), rec,
                    _entry("user", "[Request interrupted by user]"))
        self.assertEqual(APP.last_turn("probe"), {"stopped": True})
        self._write(_entry("assistant", [{"type": "text", "text": "ok"}], "end_turn"), rec,
                    _entry("user", "YES SHIP"))
        self.assertEqual(APP.last_turn("probe"), {"stopped": True})
        # Nothing finished yet.
        self._write(_entry("assistant", [{"type": "tool_use", "name": "Bash", "input": {}}], "tool_use"))
        self.assertIsNone(APP.last_turn("probe"))
        self._write()
        self.assertIsNone(APP.last_turn("probe"))

    def test_fresh_console_with_idle_screen_is_waiting(self):
        APP._chat_transcript_file = lambda name: None
        orig = APP._pane_turn_in_flight
        try:
            APP._pane_turn_in_flight = lambda name: False
            st, turn = APP.mission_activity_detail("probe")
            self.assertEqual(st, "waiting"); self.assertTrue(turn.startswith("start:"))
            APP._pane_turn_in_flight = lambda name: True
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
            APP._pane_turn_in_flight = lambda name: None
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
        finally:
            APP._pane_turn_in_flight = orig

    def test_pane_screen_classifier(self):
        busy_narrow = "\u2733 Crystallizing\u2026 (thought for 7s)\n  \u2714 Update installed\n\u2500\u2500\u2500\n\u276f \n\u2500\u2500\u2500\n  \u23f5\u23f5 bypass permissions on\n"
        busy_wide = "\u273b Cogitating\u2026 (12s \u00b7 \u2191 1.2k tokens \u00b7 esc to interrupt)\n\u276f \n"
        idle = "  NEXT STEP:\n  None\n\n\u273b Saut\u00e9ed for 1m 28s \u00b7 done 8:29 PM\n\n\u2500\u2500\u2500\n\u276f YES SHIP\n\u2500\u2500\u2500\n  \u23f5\u23f5 bypass permissions on\n"
        self.assertTrue(APP._pane_text_in_flight(busy_narrow))
        self.assertTrue(APP._pane_text_in_flight(busy_wide))
        self.assertFalse(APP._pane_text_in_flight(idle))
        self.assertFalse(APP._pane_text_in_flight(""))
        # Only the bottom of the screen counts: old spinner text scrolled up top is not a turn.
        stale = "\u273b Thinking\u2026 (3s)\n" + "line\n" * 30 + "\u276f \n"
        self.assertFalse(APP._pane_text_in_flight(stale))
        # No pane (tests run with no tmux): unknown, not a verdict.
        self.assertIsNone(APP._pane_turn_in_flight("probe"))

    def test_clear_command_record_is_waiting(self):
        # /clear writes its slash-command record into the NEW transcript at
        # once, as ordinary user entries — the console then sits idle at its
        # prompt. Newest entry is the command record (the caveat is isMeta).
        self._write(_entry("assistant", [{"type": "text", "text": "done"}], "end_turn"),
                    _entry("user", "<local-command-caveat>Caveat: …</local-command-caveat>",
                           isMeta=True),
                    _entry("user", "<command-name>/clear</command-name>\n"
                           "            <command-message>clear</command-message>\n"
                           "            <command-args></command-args>", uuid="cmd-1"))
        self.assertEqual(APP.mission_activity_detail("probe"), ("waiting", "cmd-1"))
        # the next real prompt ends the state
        self._write(_entry("user", "now this"))
        self.assertEqual(APP.mission_activity("probe"), "working")

    def test_cleared_console_no_file_is_waiting(self):
        # Belt-and-braces for the case the transcript really is absent while
        # the marker says the session came from a /clear — waiting, not the
        # "just started" working that no-transcript otherwise means.
        APP._chat_transcript_file = lambda name: None
        orig = APP._console_session_record
        try:
            APP._console_session_record = lambda name: {
                "transcript_path": "/x/y.jsonl", "session_id": "sid-1",
                "event": "SessionStart", "source": "clear"}
            self.assertEqual(APP.mission_activity_detail("probe"),
                             ("waiting", "clear:sid-1"))
            # startup/resume — and old markers with no source — still read working
            APP._console_session_record = lambda name: {
                "event": "SessionStart", "source": "startup"}
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
            APP._console_session_record = lambda name: {"event": "SessionStart"}
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
            # the next prompt's UserPromptSubmit rewrite ends the cleared state
            APP._console_session_record = lambda name: {
                "event": "UserPromptSubmit", "source": None}
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
            APP._console_session_record = lambda name: None
            self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
        finally:
            APP._console_session_record = orig

    def test_meta_and_sidechain_skipped(self):
        self._write(_entry("assistant", [{"type": "text", "text": "done"}], "end_turn"),
                    _entry("user", "aside", isSidechain=True),
                    _entry("user", "ctx", isMeta=True),
                    '{"type": "attachment"}', "not json")
        self.assertEqual(APP.mission_activity("probe"), "waiting")

    def test_chat_shows_slash_commands_as_typed(self):
        # A slash command is recorded wrapped in <command-*> tags; the chat shows
        # it as "/name args" so the page's optimistic bubble reconciles. Other
        # '<'/'[' user lines (command output, hook context) stay hidden.
        self._write(_entry("user", "<command-name>/clear</command-name>\n"
                                   "  <command-message>clear</command-message>\n"
                                   "  <command-args></command-args>"),
                    _entry("user", "<local-command-stdout>ok</local-command-stdout>"),
                    _entry("user", "<command-name>/model</command-name>"
                                   "<command-args>opus</command-args>"),
                    # Skill/custom commands record <command-message> FIRST.
                    _entry("user", "<command-message>grill-me</command-message>\n"
                                   "<command-name>/grill-me</command-name>"),
                    _entry("user", "<command-message>prd-to-issues</command-message>\n"
                                   "<command-name>/prd-to-issues</command-name>\n"
                                   "<command-args>docs/PRD.txt</command-args>"),
                    _entry("user", "[Request interrupted by user]"),
                    _entry("assistant", [{"type": "text", "text": "done"}], "end_turn"))
        msgs = APP.chat_messages("probe")
        self.assertEqual([(m["role"], m["text"]) for m in msgs],
                         [("user", "/clear"), ("user", "/model opus"),
                          ("user", "/grill-me"), ("user", "/prd-to-issues docs/PRD.txt"),
                          ("assistant", "done")])
        self.assertEqual(APP._slash_command_text("<command-args>x</command-args>"), "")

    def test_chat_shows_messages_queued_mid_turn(self):
        # Sent while Claude was working: recorded as a queued_command attachment,
        # never as a user message — still the operator's bubble, in order.
        self._write(_entry("user", "first"),
                    _entry("assistant", [{"type": "tool_use", "id": "x"}], "tool_use"),
                    json.dumps({"type": "attachment", "attachment": {
                        "type": "queued_command", "prompt": " also this ",
                        "timestamp": "2026-09-08T23:23:21.397Z"}}),
                    json.dumps({"type": "attachment", "attachment": {"type": "other"}}),
                    _entry("user", [{"type": "tool_result", "content": "ok"}]),
                    _entry("assistant", [{"type": "text", "text": "done"}], "end_turn"))
        msgs = APP.chat_messages("probe")
        self.assertEqual([(m["role"], m["text"]) for m in msgs],
                         [("user", "first"), ("user", "also this"), ("assistant", "done")])
        self.assertEqual(msgs[1]["ts"], "2026-09-08T23:23:21.397Z")
        # It does not change what the console is doing.
        self.assertEqual(APP.mission_activity("probe"), "waiting")

    ASK = {"type": "tool_use", "id": "ask1", "name": "AskUserQuestion", "input": {"questions": [
        {"question": "Which colour?", "header": "Colour", "multiSelect": False,
         "options": [{"label": "Red", "description": "warm"}, {"label": "Blue"}]},
        {"question": "Which size?", "header": "Size", "multiSelect": True,
         "options": [{"label": "Small", "description": "s"}, {"label": "Large", "description": "l"}]},
        {"question": "no options", "options": []}]}}

    def test_question_to_operator_is_waiting(self):
        # An AskUserQuestion call is a tool_use whose "result" is the operator's
        # answer: unanswered it is their move (red), answered it is working.
        self._write(_entry("user", "go"),
                    _entry("assistant", [self.ASK], "tool_use", uuid="q1"))
        self.assertEqual(APP.mission_activity_detail("probe"), ("waiting", "q1"))
        self._write(_entry("user", "go"),
                    _entry("assistant", [self.ASK], "tool_use", uuid="q1"),
                    _entry("user", [{"type": "tool_result", "tool_use_id": "ask1",
                                     "content": "Your questions have been answered: ok"}]))
        self.assertEqual(APP.mission_activity_detail("probe"), ("working", ""))
        self._write(_entry("assistant", [{"type": "tool_use", "id": "p", "name": "ExitPlanMode",
                                          "input": {}}], "tool_use", uuid="p1"))
        self.assertEqual(APP.mission_activity_detail("probe"), ("waiting", "p1"))

    def test_chat_shows_question_and_answer(self):
        self._write(_entry("user", "go"),
                    _entry("assistant", [{"type": "text", "text": "One thing first."}, self.ASK],
                           "tool_use"))
        msgs = APP.chat_messages("probe")
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "assistant"])
        q = msgs[2]
        self.assertTrue(q["open"])
        self.assertEqual([x["header"] for x in q["ask"]], ["Colour", "Size"])   # empty one dropped
        self.assertEqual(q["ask"][0]["options"], [{"label": "Red", "description": "warm"},
                                                  {"label": "Blue", "description": ""}])
        self.assertTrue(q["ask"][1]["multiSelect"])
        self.assertIn("Colour: Which colour?\n  1. Red \u2014 warm\n  2. Blue", q["text"])
        # Answered: the structured answers become the operator's bubble, in the
        # exact shape the page's optimistic bubble uses ("Header: label" lines,
        # one question -> just the label), and the question is no longer open.
        self._write(_entry("user", "go"),
                    _entry("assistant", [self.ASK], "tool_use"),
                    _entry("user", [{"type": "tool_result", "tool_use_id": "ask1",
                                     "content": "Your questions have been answered: ..."}],
                           toolUseResult={"questions": self.ASK["input"]["questions"],
                                          "answers": {"Which colour?": "Blue",
                                                      "Which size?": "Small, Large"}}),
                    _entry("assistant", [{"type": "text", "text": "Blue it is."}], "end_turn"))
        msgs = APP.chat_messages("probe")
        self.assertEqual([(m["role"], m.get("open")) for m in msgs],
                         [("user", None), ("assistant", False), ("user", None), ("assistant", None)])
        self.assertEqual(msgs[2]["text"], "Colour: Blue\nSize: Small, Large")
        # One question, or no structured record: the label / the result text.
        self.assertEqual(APP._ask_answer_text(
            {"toolUseResult": {"questions": [], "answers": {"Q?": "Red"}}}, []), "Red")
        self.assertEqual(APP._ask_answer_text({}, [{"type": "tool_result", "content":
            "Your questions have been answered: \"Q?\"=\"Red\". "
            "You can now continue with these answers in mind."}]), '"Q?"="Red".')
        self.assertEqual(APP._ask_answer_text({}, [{"type": "tool_result", "content": "ok"}]), "")

    PLAN = {"type": "tool_use", "id": "plan1", "name": "ExitPlanMode",
            "input": {"plan": "# Fix it\n\n## Steps\n1. edit\n2. test", "planFilePath": "/x.md"}}
    APPROVED = ("User has approved your plan. You can now start coding. Start with updating "
                "your todo list if applicable\n\nYour plan has been saved to: /x.md\n\n"
                "## Approved Plan:\n# Fix it")
    REJECTED = ("The user doesn't want to proceed with this tool use. The tool use was rejected "
                "(eg. if it was a file edit, the new_string was NOT written to the file). STOP "
                "what you are doing and wait for the user to tell you how to proceed.")

    def test_chat_shows_plan_and_verdict(self):
        # An ExitPlanMode call is Claude's plan presented for approval: the
        # same bubble shape as a question (the plan as its text, the TUI's
        # three choices as options), flagged `plan` so the page presses the
        # digit alone. The beep fired for it but the chat showed nothing.
        self._write(_entry("user", "go"),
                    _entry("assistant", [{"type": "text", "text": "Here is the plan."}, self.PLAN],
                           "tool_use"))
        msgs = APP.chat_messages("probe")
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "assistant"])
        q = msgs[2]
        self.assertTrue(q["open"] and q["plan"])
        self.assertEqual(len(q["ask"]), 1)
        self.assertEqual(q["ask"][0]["question"], self.PLAN["input"]["plan"])
        self.assertFalse(q["ask"][0]["multiSelect"])
        self.assertEqual([o["label"] for o in q["ask"][0]["options"]],
                         [o["label"] for o in APP.PLAN_OPTIONS])
        self.assertIn("Here is Claude's plan: # Fix it", q["text"])
        self.assertIn("2. Yes, manually approve edits", q["text"])
        # Approved: the verdict is the operator's bubble right after the plan,
        # in the words the page's optimistic bubble uses; the plan closes.
        self._write(_entry("user", "go"),
                    _entry("assistant", [self.PLAN], "tool_use"),
                    _entry("user", [{"type": "tool_result", "tool_use_id": "plan1",
                                     "content": self.APPROVED}],
                           toolUseResult={"plan": "# Fix it"}),
                    _entry("assistant", [{"type": "text", "text": "On it."}], "end_turn"))
        msgs = APP.chat_messages("probe")
        self.assertEqual([(m["role"], m.get("open"), m["text"][:13]) for m in msgs],
                         [("user", None, "go"), ("assistant", False, "Ready to code"),
                          ("user", None, "Approved plan"), ("assistant", None, "On it.")])
        # Rejected, with and without typed feedback.
        self._write(_entry("user", "go"),
                    _entry("assistant", [self.PLAN], "tool_use"),
                    _entry("user", [{"type": "tool_result", "tool_use_id": "plan1",
                                     "content": self.REJECTED + "\n\nUse a queue instead."}]))
        msgs = APP.chat_messages("probe")
        self.assertEqual(msgs[2]["text"], "Plan not approved \u2014 Use a queue instead.")
        self.assertFalse(msgs[1]["open"])
        self.assertEqual(APP._plan_answer_text(self.REJECTED), "Plan not approved")
        self.assertEqual(APP._plan_answer_text("File created successfully at: /x"), "")
        # No plan text in the call: still a bubble, pointing at the console.
        self._write(_entry("user", "go"),
                    _entry("assistant", [{"type": "tool_use", "id": "p2", "name": "ExitPlanMode",
                                          "input": {}}], "tool_use"))
        msgs = APP.chat_messages("probe")
        self.assertTrue(msgs[1]["plan"] and msgs[1]["open"])
        self.assertIn("see the console", msgs[1]["ask"][0]["question"])

    def test_end_turn_with_pending_background_agent_is_working(self):
        self._write(_entry("assistant", [{"type": "text", "text": "waiting on the agent"}], "end_turn"),
                    json.dumps({"type": "system", "subtype": "turn_duration",
                                "pendingBackgroundAgentCount": 1}))
        self.assertEqual(APP.mission_activity("probe"), "working")
        self._write(_entry("assistant", [{"type": "text", "text": "done"}], "end_turn"),
                    json.dumps({"type": "system", "subtype": "turn_duration",
                                "pendingBackgroundAgentCount": 0}))
        self.assertEqual(APP.mission_activity("probe"), "waiting")
        # An older turn's pending count must not leak onto a newer, clean end_turn.
        self._write(_entry("assistant", [{"type": "text", "text": "spawned"}], "end_turn"),
                    json.dumps({"type": "system", "subtype": "turn_duration",
                                "pendingBackgroundAgentCount": 1}),
                    _entry("user", "<task-notification>agent done</task-notification>"),
                    _entry("assistant", [{"type": "text", "text": "all done"}], "end_turn"),
                    json.dumps({"type": "system", "subtype": "turn_duration",
                                "pendingBackgroundAgentCount": 0}))
        self.assertEqual(APP.mission_activity("probe"), "waiting")

    def test_no_transcript_is_working(self):
        APP._chat_transcript_file = lambda name: None
        self.assertEqual(APP.mission_activity("probe"), "working")


class Doing(unittest.TestCase):
    """turn_doing names the in-flight turn's newest tool call for the chat
    page's "Working…" line — and only the CURRENT turn's."""

    def setUp(self):
        self.f = os.path.join(TMP, "doing.jsonl")
        self._orig = APP._chat_transcript_file
        APP._chat_transcript_file = lambda name: self.f

    def tearDown(self):
        APP._chat_transcript_file = self._orig

    def _write(self, *lines):
        with open(self.f, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _tool(self, name, tin, **extra):
        return _entry("assistant", [{"type": "tool_use", "name": name, "input": tin}],
                      "tool_use", **extra)

    def test_bash_uses_its_description(self):
        self._write(_entry("user", "go"),
                    self._tool("Bash", {"command": "x", "description": "Run the full test suite"}))
        self.assertEqual(APP.turn_doing("probe"), "Run the full test suite")

    def test_file_tools_name_the_file(self):
        self._write(_entry("user", "go"),
                    self._tool("Edit", {"file_path": "/home/x/repo/app.py"}))
        self.assertEqual(APP.turn_doing("probe"), "Editing app.py")
        self._write(_entry("user", "go"), self._tool("Read", {"file_path": "/a/b/notes.md"}))
        self.assertEqual(APP.turn_doing("probe"), "Reading notes.md")
        self._write(_entry("user", "go"), self._tool("Write", {"file_path": "/a/b/new.txt"}))
        self.assertEqual(APP.turn_doing("probe"), "Writing new.txt")

    def test_tool_result_keeps_naming_its_call(self):
        # While Claude streams the reply after its last tool, the tool_result
        # sits newest — the line keeps naming that call, never goes blank.
        self._write(_entry("user", "go"),
                    self._tool("Bash", {"description": "Restart nothing"}),
                    _entry("user", [{"type": "tool_result", "content": "ok"}]))
        self.assertEqual(APP.turn_doing("probe"), "Restart nothing")

    def test_no_tool_yet_is_none(self):
        # Thinking / streaming before the first tool: the plain spinner is right.
        self._write(_entry("user", "go"))
        self.assertIsNone(APP.turn_doing("probe"))
        self._write(_entry("user", "go"),
                    _entry("assistant", [{"type": "text", "text": "Let me look."}], None))
        self.assertIsNone(APP.turn_doing("probe"))

    def test_older_turns_tools_never_leak(self):
        self._write(_entry("user", "first"),
                    self._tool("Bash", {"description": "Old work"}),
                    _entry("assistant", [{"type": "text", "text": "done"}], "end_turn"),
                    _entry("user", "second"))
        self.assertIsNone(APP.turn_doing("probe"))

    def test_parallel_calls_and_sidechains(self):
        self._write(_entry("user", "go"),
                    _entry("assistant",
                           [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a/a.py"}},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}}],
                           "tool_use"),
                    self._tool("Bash", {"description": "Subagent detour"}, isSidechain=True))
        self.assertEqual(APP.turn_doing("probe"), "Reading b.py")

    def test_unknown_and_mcp_tools(self):
        self._write(_entry("user", "go"), self._tool("Frobnicate", {}))
        self.assertEqual(APP.turn_doing("probe"), "Using Frobnicate")
        self._write(_entry("user", "go"),
                    self._tool("mcp__plugin_playwright_playwright__browser_click", {}))
        self.assertEqual(APP.turn_doing("probe"), "Using browser click")

    def test_caps_a_runaway_description(self):
        self._write(_entry("user", "go"), self._tool("Bash", {"description": "x" * 500}))
        self.assertEqual(len(APP.turn_doing("probe")), 90)

    def test_no_transcript_is_none(self):
        APP._chat_transcript_file = lambda name: None
        self.assertIsNone(APP.turn_doing("probe"))


class Layout(unittest.TestCase):
    def test_clean_drops_bad_entries_keeps_good(self):
        raw = {
            "cards": {
                "probe": {"x": "10", "y": 20, "w": 300, "h": 300, "color": "blue"},
                "bad name/": {"x": 1, "y": 1, "w": 300, "h": 300},
                "neg": {"x": -5, "y": 1, "w": 10, "h": 10, "color": "nope"},
                "junk": "not a dict",
            },
            "groups": [
                {"id": "g1", "x": 0, "y": 0, "w": 500, "h": 400, "label": "x" * 200, "color": "nope"},
                {"id": "g1", "x": 0, "y": 0, "w": 500, "h": 400},          # duplicate id
                {"id": "bad id!", "x": 0, "y": 0, "w": 500, "h": 400},
                {"id": "g2", "x": 0, "y": 0},                              # no size
            ],
            "hidden": ["zzz", "bad/name", 3, "zzz"],
        }
        out = APP.clean_canvas_layout(raw)
        self.assertEqual(sorted(out["cards"]), ["neg", "probe"])
        self.assertEqual(out["cards"]["probe"],
                         {"x": 10, "y": 20, "w": 300, "h": 300, "color": "blue", "ding": True})
        neg = out["cards"]["neg"]
        self.assertTrue(neg["ding"])            # absent ⇒ the card dings
        self.assertEqual((neg["x"], neg["w"], neg["h"], neg["color"]),
                         (0, APP.CANVAS_MIN_W, APP.CANVAS_MIN_H, "none"))
        self.assertEqual([g["id"] for g in out["groups"]], ["g1"])
        self.assertEqual(len(out["groups"][0]["label"]), 80)
        self.assertEqual(out["groups"][0]["color"], "none")
        self.assertEqual(out["hidden"], ["zzz"])

    def test_card_bell_only_explicit_false_silences(self):
        raw = {"cards": {
            "quiet": {"x": 0, "y": 0, "w": 300, "h": 300, "ding": False},
            "str0": {"x": 0, "y": 0, "w": 300, "h": 300, "ding": "0"},
            "none": {"x": 0, "y": 0, "w": 300, "h": 300, "ding": None},
        }}
        out = APP.clean_canvas_layout(raw)["cards"]
        self.assertEqual([out[n]["ding"] for n in ("quiet", "str0", "none")], [False, True, True])

    def test_garbage_is_empty_layout(self):
        for raw in (None, [], "x", {"cards": [], "groups": {}, "hidden": "no"}):
            self.assertEqual(APP.clean_canvas_layout(raw),
                             {"cards": {}, "groups": [], "notes": [], "hidden": [],
                              "state_colors": dict(APP.CANVAS_STATE_DEFAULTS)})

    def test_missing_file_reads_empty(self):
        self.assertEqual(APP.read_canvas_layout(),
                         {"cards": {}, "groups": [], "notes": [], "hidden": [],
                          "state_colors": dict(APP.CANVAS_STATE_DEFAULTS)})

    def test_state_colors_cleaned(self):
        # Valid picks survive; "none", junk and a missing key fall back per state.
        out = APP.clean_canvas_layout(
            {"state_colors": {"working": "blue", "waiting": "none", "junk": "red"}})
        self.assertEqual(out["state_colors"],
                         {"working": "blue", "waiting": "red", "off": "gray"})
        out = APP.clean_canvas_layout({"state_colors": "nope"})
        self.assertEqual(out["state_colors"], dict(APP.CANVAS_STATE_DEFAULTS))

    def test_notes_cleaned(self):
        raw = {"notes": [
            {"id": "n1", "x": 5, "y": 5, "w": 200, "h": 80, "text": "x" * 5000,
             "color": "yellow", "size": "xl"},
            {"id": "n1", "x": 0, "y": 0, "w": 200, "h": 80},               # duplicate id
            {"id": "n2", "x": 0, "y": 0, "w": 1, "h": 1, "size": "huge", "text": 7},
            {"id": "bad id!", "x": 0, "y": 0, "w": 200, "h": 80},
            "junk",
            {"id": "n3", "x": 0, "y": 0, "w": 200, "h": 80, "pin": "probe"},
            {"id": "n4", "x": 0, "y": 0, "w": 200, "h": 80, "pin": "../etc"},
            {"id": "n5", "x": 0, "y": 0, "w": 200, "h": 80, "pin": 7},
        ]}
        out = APP.clean_canvas_layout(raw)
        self.assertEqual([n["id"] for n in out["notes"]], ["n1", "n2", "n3", "n4", "n5"])
        n1, n2, n3, n4, n5 = out["notes"]
        # `pin` (the card a note is docked under) survives only as a valid mission name.
        self.assertEqual(n3["pin"], "probe")
        self.assertNotIn("pin", n1); self.assertNotIn("pin", n4); self.assertNotIn("pin", n5)
        self.assertEqual((len(n1["text"]), n1["color"], n1["size"]),
                         (APP.CANVAS_NOTE_MAX_TEXT, "yellow", "xl"))
        self.assertEqual((n2["w"], n2["h"], n2["size"], n2["text"], n2["color"]),
                         (APP.CANVAS_NOTE_MIN_W, APP.CANVAS_NOTE_MIN_H, "m", "", "none"))
        # An old layout file with no notes key still reads cleanly.
        self.assertEqual(APP.clean_canvas_layout({"cards": {}, "groups": []})["notes"], [])


class RepoLabel(unittest.TestCase):
    """mission_repo_label: the repo a dev mission develops, else the console's dir."""

    def _meta(self, name, meta):
        d = os.path.join(TMP, "missions", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "mission.json"), "w") as fh:
            json.dump(meta, fh)
        self.addCleanup(shutil.rmtree, d, True)

    def test_local_dev_is_repo_basename(self):
        self._meta("rl-dev", {"mode": "dev", "target": {"kind": "local-repo", "path": "/srv/heron"},
                              "dev": {"repo": "/srv/heron", "worktree": "/srv/wt/rl-dev"}})
        self.assertEqual(APP.mission_repo_label("rl-dev"), "heron")

    def test_remote_dev_is_host_prefixed(self):
        self._meta("rl-rdev", {"mode": "dev", "target": {"kind": "remote-repo", "host": "web1",
                                                          "remote_dir": "/opt/site"},
                               "dev": {"repo": "/opt/site", "worktree": "/opt/wt", "host": "web1"}})
        self.assertEqual(APP.mission_repo_label("rl-rdev"), "web1:site")

    def test_ops_is_working_dir_basename(self):
        self._meta("rl-ops", {"mode": "ops", "target": {"kind": "local-dir", "path": "/var/www/blog/"}})
        self.assertEqual(APP.mission_repo_label("rl-ops"), "blog")

    def test_own_folder_says_nothing(self):
        self.assertEqual(APP.mission_repo_label("probe"), "")

    def test_canvas_state_carries_repo(self):
        self._meta("rl-dev2", {"mode": "dev", "target": {"kind": "local-repo", "path": "/srv/heron"},
                               "dev": {"repo": "/srv/heron", "worktree": "/srv/wt/rl-dev2"}})
        APP.write_canvas_layout({"cards": {"rl-dev2": {"x": 0, "y": 0, "w": 400, "h": 300}},
                                 "groups": [], "notes": []})
        st = APP.canvas_state()
        self.assertEqual(st["missions"]["rl-dev2"]["repo"], "heron")


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method, path, body=None, ctype=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-Requested-With": "fetch"}
        if ctype:
            headers["Content-Type"] = ctype
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        c.close()
        return r.status, data

    def get(self, path):
        return self._req("GET", path)

    def post_json(self, path, obj):
        return self._req("POST", path, json.dumps(obj), "application/json")

    def post_form(self, route, **fields):
        return self._req("POST", route, urllib.parse.urlencode(fields),
                         "application/x-www-form-urlencoded")

    def test_canvas_page(self):
        st, body = self.get("/canvas")
        self.assertEqual(st, 200)
        for needle in ("id=world", "id=spawn-open", "id=cmenu", "canvas-wrap", "var INITIAL =",
                       # zoom: the bar controls and the JS that drives them
                       "id=canvas-zoom", "id=zoom-pct", "id=zoom-fit", "function setZoom(",
                       "function zoomAt(", "function bindFrameZoom(", 'VIEW_KEY = "canvas-view"',
                       # a hidden tab keeps polling (slower) so the ding fires in the
                       # background; the per-card context polls stay gated
                       "HIDDEN_MS = 15000", "setInterval(tick, POLL_MS)",
                       "if (!document.hidden) pollCtx();"):
            self.assertIn(needle, body)
        self.assertNotIn("function poll(){\n    if (document.hidden) return;\n    fetch(CANVAS_URL", body)

    def test_masthead_link_everywhere(self):
        st, body = self.get("/")
        self.assertEqual(st, 200)
        self.assertIn('href="/canvas"', body)

    def test_embed_hides_chat_header(self):
        st, plain = self.get("/m/probe/chat")
        self.assertEqual(st, 200)
        self.assertIn("<header", plain)
        st, embed = self.get("/m/probe/chat?embed=1")
        self.assertEqual(st, 200)
        self.assertNotIn("<header", embed)
        self.assertIn("id=sendform", embed)

    def test_chat_draft_and_focus_seams(self):
        # Unsent text is kept per console; the embedded page reports focus to
        # the canvas, which drops the red highlight (but keeps the state).
        st, embed = self.get("/m/probe/chat?embed=1")
        self.assertEqual(st, 200)
        for needle in ('var MISSION = "probe"', "chat-draft:", "chat-history:", '"chat-focus"',
                       '"chat-stop"'):
            self.assertIn(needle, embed)
        st, canvas = self.get("/canvas")
        self.assertEqual(st, 200)
        for needle in ('d.type !== "chat-focus"', "state-waiting.acked",
                       'var ACK_KEY = "canvas-acked"', "acked[n] === turns[n]",
                       'd.type === "chat-stop"', "var preAck = {}",
                       "!stopped &&",
                       "body.resizing iframe { pointer-events:none; }",
                       '"New note"', "function editNote",
                       '"Add note"', "function newPinnedNote", "function snapPinned",
                       '"Unpin (free note)"'):
            self.assertIn(needle, canvas)

    def test_chat_enter_sends_and_yes_ship_acks(self):
        st, embed = self.get("/m/probe/chat?embed=1")
        self.assertEqual(st, 200)
        for needle in ('ev.key !== "Enter" || ev.shiftKey', "function ackFocus",
                       "m.text.indexOf(p) !== -1"):
            self.assertIn(needle, embed)

    def test_note_drop_sends_to_chat(self):
        # Dragging notes onto a card sends their text to that card's chat: the
        # canvas restores every dragged note to its origin (a send, not a move)
        # and postMessages chat-send into the card's iframe; the embedded chat
        # page turns that into a normal typed send (bubble + /console/key).
        st, canvas = self.get("/canvas")
        self.assertEqual(st, 200)
        for needle in ("drag.noteSend = moving.every",
                       "function cardAt", "function dropNotesOn",
                       'd.kind === "move" && d.over',
                       'postMessage({type: "chat-send", text: t}',
                       ".ccard.droptarget",
                       # A pinned note starts a send-only drag (never a move) and
                       # snapPinned leaves notes mid-drag under the pointer.
                       "origin: [{x: pr.x, y: pr.y}], noteSend: true",
                       "drag.moving.indexOf(el) >= 0) return;"):
            self.assertIn(needle, canvas)
        st, embed = self.get("/m/probe/chat?embed=1")
        self.assertEqual(st, 200)
        self.assertIn('ev.data.type !== "chat-send"', embed)
        self.assertIn("if (t) sendText(t, false);", embed)

    def test_model_badge_menu_and_dictation(self):
        # The titlebar model badge is a menu of CANVAS_MODELS that types
        # `/model <id>` into the console; the chat page carries the shared 🎤.
        st, canvas = self.get("/canvas")
        self.assertEqual(st, 200)
        self.assertIn("var MODELS = " + json.dumps(APP.CANVAS_MODELS) + ";", canvas)
        for needle in ('"/model " + m', "function modelMenu", 'className = "caret"',
                       'closest("a, button, input, textarea, .badge.model")',
                       'setAttribute("allow", "microphone")'):
            self.assertIn(needle, canvas)
        self.assertTrue(APP.CANVAS_MODELS)
        st, embed = self.get("/m/probe/chat?embed=1")
        self.assertEqual(st, 200)
        for needle in ("id=micbtn", "window.attachDictation = function",
                       'attachDictation(document.getElementById("micbtn")'):
            self.assertIn(needle, embed)

    def test_layout_round_trip(self):
        layout = {"cards": {"probe": {"x": 10, "y": 20, "w": 300, "h": 300, "color": "teal"},
                            "nope!": {"x": 0, "y": 0, "w": 300, "h": 300}},
                  "groups": [{"id": "g1", "x": 5, "y": 5, "w": 600, "h": 500,
                              "label": "Ops", "color": "red"}],
                  "notes": [{"id": "n1", "x": 8, "y": 8, "w": 240, "h": 90,
                             "text": "call ops\nat 5", "color": "none", "size": "l"}],
                  "hidden": ["other"]}
        st, body = self.post_json("/canvas/layout", layout)
        self.assertEqual(st, 200)
        saved = json.loads(body)["layout"]
        self.assertEqual(sorted(saved["cards"]), ["probe"])
        self.assertEqual(saved["notes"], layout["notes"])
        st, body = self.get("/canvas.json")
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertEqual(d["layout"], saved)
        # A card on the canvas gets a state even with nothing running.
        self.assertEqual(d["missions"]["probe"], {"state": "off", "turn": "", "running": False, "live": False, "repo": "", "title": "probe"})
        self.assertNotIn("other", d["missions"])
        self.assertEqual(sorted(d["all"]), ["other", "probe"])

    def test_chat_json_running_without_transcript(self):
        orig = APP.session_running
        APP.session_running = lambda name: True
        try:
            st, body = self.get("/m/probe/chat.json")
        finally:
            APP.session_running = orig
        self.assertEqual((st, json.loads(body)), (200, {"running": True, "working": True, "doing": None, "elapsed": None, "last": None, "msgs": []}))
        st, body = self.get("/m/probe/chat.json")
        self.assertEqual(json.loads(body)["running"], False)

    def test_layout_bad_json(self):
        st, body = self._req("POST", "/canvas/layout", "{not json", "application/json")
        self.assertEqual(st, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_spawn_canvas_creates_and_starts(self):
        calls = []
        orig = APP.start_console_headless
        APP.start_console_headless = lambda name: calls.append(name) or ""
        try:
            st, body = self.post_form("/spawn", canvas="1", mode="ops", kind="local-dir",
                                      path=TMP, name="from canvas")
        finally:
            APP.start_console_headless = orig
        self.assertEqual(st, 200, body)
        d = json.loads(body)
        self.assertEqual((d["ok"], d["name"], d["started"]), (True, "from-canvas", True))
        self.assertEqual(calls, ["from-canvas"])
        self.assertTrue(os.path.isfile(os.path.join(TMP, "missions", "from-canvas", "mission.json")))

    def test_spawn_canvas_reports_start_failure(self):
        orig = APP.start_console_headless
        APP.start_console_headless = lambda name: "console did not start: boom"
        try:
            st, body = self.post_form("/spawn", canvas="1", mode="ops", kind="local-dir",
                                      path=TMP, name="halfway")
        finally:
            APP.start_console_headless = orig
        d = json.loads(body)
        self.assertEqual((st, d["ok"], d["started"]), (200, True, False))
        self.assertIn("boom", d["msg"])

    def test_spawn_canvas_errors_are_json(self):
        st, body = self.post_form("/spawn", canvas="1", mode="ops", kind="local-dir",
                                  path="/definitely/not/here", name="x")
        self.assertEqual(st, 400)
        d = json.loads(body)
        self.assertFalse(d["ok"])
        self.assertIn("No such directory", d["msg"])

    def test_spawn_canvas_console_mode_redirects_in_json(self):
        st, body = self.post_form("/spawn", canvas="1", mode="console", kind="local-dir",
                                  path=TMP)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertIn("redirect", d)

    def test_console_start_route(self):
        orig = APP.start_console_headless
        calls = []
        APP.start_console_headless = lambda name: calls.append(name) or ""
        try:
            st, body = self._req("POST", "/m/probe/console/start")
            APP.start_console_headless = lambda name: "console did not start: nope"
            st2, body2 = self._req("POST", "/m/probe/console/start")
            st3, _ = self._req("POST", "/m/missing/console/start")
        finally:
            APP.start_console_headless = orig
        self.assertEqual((st, json.loads(body)), (200, {"ok": True, "msg": ""}))
        self.assertEqual(calls, ["probe"])
        self.assertEqual((st2, json.loads(body2)["ok"]), (200, False))
        self.assertIn("nope", json.loads(body2)["msg"])
        self.assertEqual(st3, 404)

    def test_spawn_without_canvas_still_redirects(self):
        st, body = self.post_form("/spawn", mode="ops", kind="local-dir", path=TMP, name="plain")
        self.assertEqual(st, 303)


if __name__ == "__main__":
    unittest.main()
