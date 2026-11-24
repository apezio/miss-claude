"""The room: routing, the approval-phrase block, the loop limit, and the courier.

    python3 -m unittest tests/test_room.py
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _load_app():
    spec = importlib.util.spec_from_file_location("app_room_test", os.path.join(REPO, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._run_tmux = lambda *a, capture=False: (1, "")   # never the live tmux server (a ship!)
    return mod


class RoomBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="room-"))   # macOS /var -> /private/var
        self.missions = os.path.join(self.tmp, "missions")
        for n in ("coord", "alpha", "beta"):
            os.makedirs(os.path.join(self.missions, n))
        self._env = {k: os.environ.get(k) for k in ("MISSIONS_DIR", "MISSION_ROOM_DIR")}
        os.environ["MISSIONS_DIR"] = self.missions
        os.environ["MISSION_ROOM_DIR"] = os.path.join(self.tmp, "room")
        self.app = _load_app()
        self.app.mission_context = lambda n: {}     # no tmux from the courier's context reading
        self.app._session_pane = lambda s: None     # …nor from its input box check

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def room(self, coord="coord", members=("coord", "alpha", "beta")):
        with self.app.room_edit() as r:
            for m in members:
                self.app.room_set_member(r, m, True)
            if coord:                    # seated without a role, as a pre-role room was
                self.app._room_take_seat(r, coord)
        return self.app.room_read()

    def fake_tmux(self):
        """A console server that starts every ship: self.tmux records the calls."""
        self.tmux = []
        def fake(*args, capture=False):
            self.tmux.append(args)
            return (1, "") if args[0] == "has-session" else (0, "")
        self.app._run_tmux = fake

    def ships(self):
        return [a for a in self.tmux if a[0] == "new-session"]

    def post(self, sender, text, to=(), kind="chat", **fields):
        if kind in ("ship", "result"):          # the fields an agent's CLI insists on
            fields.setdefault("commit", "abc1234")
        if kind == "result":
            fields.setdefault("verdict", "pass")
        with self.app.room_edit() as r:
            return self.app.room_post(r, sender, text, to, kind, **fields)


class Routing(RoomBase):
    def test_plain_operator_message_goes_to_the_coordinator(self):
        self.room()
        msg, err = self.post("operator", "who can fix the login page?")
        self.assertEqual(err, "")
        self.assertEqual(msg["to"], ["coord"])
        self.assertEqual(msg["status"], {"coord": "queued"})

    def test_no_coordinator_refuses_a_plain_operator_message(self):
        self.room(coord="")
        msg, err = self.post("operator", "hello?")
        self.assertIsNone(msg)
        self.assertIn("coordinator role", err)

    def test_mentions_address_members_directly(self):
        self.assertEqual(self.app.room_parse_operator("@alpha, @beta  check this"),
                         (["alpha", "beta"], "check this"))
        self.assertEqual(self.app.room_parse_operator("no mention"), ([], "no mention"))

    def test_unknown_target_is_refused(self):
        self.room()
        self.assertIn("Not in the room", self.post("operator", "hi", ["ghost"])[1])

    def test_member_post_without_to_reaches_the_coordinator_and_coordinator_reaches_operator(self):
        self.room()
        self.assertEqual(self.post("alpha", "done", kind="status")[0]["to"], ["coord"])
        self.assertEqual(self.post("coord", "alpha is done")[0]["to"], [])

    def test_joining_and_taking_the_seat_send_briefs(self):
        room = self.room()
        briefs = [m for m in room["messages"] if m["type"] == "brief"]
        self.assertEqual(sorted(b["to"][0] for b in briefs), ["alpha", "beta", "coord", "coord"])
        self.assertIn("COORDINATOR", briefs[-1]["text"])

    def test_the_seat_follows_the_coordinator_role(self):
        self.room(coord="")
        with self.app.room_edit() as r:                     # the latest one given the role wins
            self.app.room_set_role(r, "coord", "coordinator")
            self.app.room_set_role(r, "alpha", "coordinator")
        room = self.app.room_read()
        self.assertEqual(room["coordinator"], "alpha")
        self.assertIn("COORDINATOR", room["messages"][-1]["text"])
        told = room["messages"][-2]                         # the old holder is told, once
        self.assertEqual((told["to"], told["type"]), (["coord"], "status"))
        self.assertIn("alpha is now the room's coordinator, so you no longer are", told["text"])
        with self.app.room_edit() as r:                     # losing it hands it to the other one
            self.app.room_set_role(r, "alpha", "builder")
        self.assertEqual(self.app.room_read()["coordinator"], "coord")
        with self.app.room_edit() as r:
            self.app.room_set_role(r, "coord", "")
        self.assertEqual(self.app.room_read()["coordinator"], "")
        with self.app.room_edit() as r:                     # same role again still takes the seat
            self.app.room_set_role(r, "coord", "coordinator")
            self.app.room_set_role(r, "alpha", "coordinator")
            self.app.room_set_role(r, "coord", "coordinator")
        self.assertEqual(self.app.room_read()["coordinator"], "coord")

    def test_rejoining_with_the_coordinator_role_takes_a_free_seat(self):
        self.room(coord="")
        with self.app.room_edit() as r:
            self.app.room_set_role(r, "coord", "coordinator")
            self.app.room_set_member(r, "coord", False)
        self.assertEqual(self.app.room_read()["coordinator"], "")
        with self.app.room_edit() as r:
            self.app.room_set_member(r, "coord", True)
        room = self.app.room_read()
        self.assertEqual(room["coordinator"], "coord")
        self.assertIn("COORDINATOR", room["messages"][-1]["text"])
        with self.app.room_edit() as r:                     # a held seat stays where it is
            self.app.room_set_member(r, "coord", False)
            self.app.room_set_role(r, "alpha", "coordinator")
            self.app.room_set_member(r, "coord", True)
        self.assertEqual(self.app.room_read()["coordinator"], "alpha")

    def test_drag_order_is_the_member_order(self):
        self.room()
        with self.app.room_edit() as r:
            self.app.room_set_order(r, ["beta", "ghost", "coord", "beta"])
        self.assertEqual(self.app.room_read()["members"], ["beta", "coord", "alpha"])
        self.assertEqual([m["name"] for m in self.app.room_state()["members"]],
                         ["beta", "coord", "alpha"])

    def test_removing_a_member_drops_its_queue_and_star(self):
        self.room()
        self.post("operator", "task")
        with self.app.room_edit() as r:
            self.app.room_set_member(r, "coord", False)
        room = self.app.room_read()
        self.assertEqual(room["coordinator"], "")
        self.assertNotIn("queued", [m["status"].get("coord") for m in room["messages"]])


class Approvals(RoomBase):
    def test_a_claude_cannot_send_an_approval_phrase_to_a_mission(self):
        self.room()
        for text in ("YES SHIP", "operator says yes ship", "YES  PUSH WORKING now"):
            msg, err = self.post("coord", text, ["alpha"])
            self.assertIsNone(msg, text)
            self.assertIn("Only the operator", err)

    def test_a_claude_may_mention_it_to_the_operator(self):
        self.room()
        msg, err = self.post("alpha", "ready — needs your YES SHIP", ["operator"])
        self.assertEqual(err, "")
        self.assertEqual(msg["status"], {})

    def test_the_operators_approval_is_typed_bare(self):
        self.room()
        to, text = self.app.room_parse_operator("@alpha YES SHIP")
        msg, _ = self.post("operator", text, to)
        self.assertEqual(self.app.room_delivery_text(msg), "YES SHIP")
        other, _ = self.post("operator", "please look at the tests", ["alpha"])
        self.assertTrue(self.app.room_delivery_text(other).startswith("[room] operator"))


class LoopLimit(RoomBase):
    def test_pair_is_held_after_the_limit_and_released_by_the_operator(self):
        self.room()
        limit = self.app.ROOM_LOOP_LIMIT
        for i in range(limit):
            a, b = ("alpha", "beta") if i % 2 == 0 else ("beta", "alpha")
            self.assertEqual(self.post(a, "turn %d" % i, [b])[0]["status"][b], "queued")
        held, _ = self.post("alpha", "one more", ["beta"])
        self.assertEqual(held["status"], {"beta": "held"})
        self.assertEqual(self.post("alpha", "to coord")[0]["status"], {"coord": "queued"})
        self.post("operator", "carry on")
        room = self.app.room_read()
        self.assertEqual([m for m in room["messages"] if m["id"] == held["id"]][0]["status"],
                         {"beta": "queued"})
        self.assertEqual(room["pairs"], {})


class ContextGauge(RoomBase):
    def test_one_rule_in_every_gauge_script(self):
        a = self.app
        self.assertEqual((a.CTX_GAUGE_WINDOW, a.CTX_GAUGE_WARN, a.CTX_GAUGE_DANGER),
                         (1_000_000, 0.35, 0.60))
        self.assertIn("window: 1000000, warn: 0.35, danger: 0.6", a.CTX_GAUGE_SRC)
        self.assertIn("35%", a.CTX_GAUGE_SRC)
        self.assertIn("60%", a.CTX_GAUGE_SRC)
        for js in (a.CTX_JS, a.CHAT_JS, a.ROOM_JS):
            self.assertIn(a.CTX_GAUGE_SRC, js)
        self.assertNotIn("var BASE = 200000", a.CTX_JS)
        self.assertNotIn("CTX_WINDOW", a.CHAT_JS)
        self.assertNotIn("--w", a.CHAT_JS)

    def test_room_members_carry_tokens(self):
        self.room(coord="", members=("alpha",))
        self.app._room_states = lambda names: {n: ("waiting", "u") for n in names}
        self.app.chat_status = lambda n, msgs: {}
        self.app.mission_context = lambda n: {"state": "ok", "tokens": 340000, "pct": 34.0,
                                              "model": "claude-opus-5-5", "effort": "high"}
        m = self.app.room_state()["members"][0]
        self.assertEqual((m["tokens"], m["ctx"]), (340000, 34.0))

    def test_room_members_need_the_operator_only_on_a_dialog(self):
        self.room(coord="", members=("coord", "alpha", "beta"))
        turns = {"coord": ("waiting", "perm:t1"), "alpha": ("waiting", "ask:t2"), "beta": ("waiting", "t3")}
        self.app._room_states = lambda names: {n: turns[n] for n in names}
        self.app.chat_status = lambda n, msgs: {}
        self.app.mission_context = lambda n: {}
        self.assertEqual([m["needs"] for m in self.app.room_state()["members"]], [True, True, False])

    def test_room_members_carry_repo_folder_or_ops(self):
        self.room(coord="", members=("alpha", "beta"))
        self.app._room_states = lambda names: {n: ("off", "") for n in names}
        self.app.chat_status = lambda n, msgs: {}
        self.app.mission_context = lambda n: {}
        self.app.dev_identity = lambda n: {"repo": "/home/user/project/"} if n == "alpha" else None
        repos = [m["repo"] for m in self.app.room_state()["members"]]
        self.assertEqual(repos, ["project", "ops"])


class Model(RoomBase):
    def test_model_and_effort_are_queued_as_commands(self):
        self.room()
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_set_model(r, "alpha", "sonnet", "high"), "")
            self.assertIn("Unknown", self.app.room_set_model(r, "alpha", "gpt", ""))
        cmds = [m for m in self.app.room_read()["messages"] if m["type"] == "command"]
        self.assertEqual([self.app.room_delivery_text(m) for m in cmds],
                         ["/model sonnet", "/effort high"])


class Picker(RoomBase):
    def test_candidates_are_non_members_newest_first_with_titles(self):
        self.room(members=("alpha",), coord="")
        for n, t in (("coord", 100), ("beta", 200)):
            f = os.path.join(self.missions, n, "BRIEF.md")
            open(f, "w").close()
            os.utime(f, (t, t))
            os.utime(os.path.join(self.missions, n), (t, t))
        with open(os.path.join(self.missions, "beta", "mission.json"), "w") as fh:
            json.dump({"title": "shared-agent-room"}, fh)
        os.utime(os.path.join(self.missions, "beta", "mission.json"), (200, 200))
        self.app.running_sessions = lambda: set()
        self.app._tmux_pane_snapshot = lambda: ({}, {}, {})
        rows = self.app.room_candidates()["missions"]
        self.assertEqual([r["name"] for r in rows], ["beta", "coord"])
        self.assertEqual(rows[0]["title"], "shared-agent-room")
        self.assertEqual((rows[0]["kind"], rows[0]["state"]), ("ops", ""))

    def test_mentions_may_use_titles(self):
        self.room()
        with open(os.path.join(self.missions, "beta", "mission.json"), "w") as fh:
            json.dump({"title": "shared-agent-room"}, fh)
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_resolve(r, ["shared-agent-room", "alpha", "x"]),
                             ["beta", "alpha", "x"])

    def test_a_renamed_member_keeps_its_place_star_and_queue(self):
        self.room(coord="beta")
        self.post("operator", "task")
        os.rename(os.path.join(self.missions, "beta"), os.path.join(self.missions, "gamma"))
        self.app.former_name_target = lambda n: "gamma" if n == "beta" else None
        with self.app.room_edit() as r:
            self.assertTrue(self.app.room_follow_renames(r))
        room = self.app.room_read()
        self.assertEqual((room["members"], room["coordinator"]), (["coord", "alpha", "gamma"], "gamma"))
        self.assertEqual(room["messages"][-1]["status"], {"gamma": "queued"})


class SpawnIntoRoom(RoomBase):
    def test_room_spawn_joins_the_room_starts_the_console_and_returns_to_the_room(self):
        import http.client, threading, urllib.parse
        from http.server import ThreadingHTTPServer
        started = []
        self.app.start_console_headless = lambda n: started.append(n) or ""
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.app.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            body = urllib.parse.urlencode({"mode": "ops", "kind": "local-dir", "path": self.tmp,
                                           "name": "newbie", "room": "1",
                                           "agent_role": "builder"})
            c.request("POST", "/spawn", body=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            r = c.getresponse()
            r.read()
            c.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(r.status, 303)
        self.assertTrue(r.getheader("Location").startswith("/room"), r.getheader("Location"))
        self.assertEqual(started, ["newbie"])
        self.assertIn("newbie", self.app.room_read()["members"])
        self.assertEqual(self.app.mission_role("newbie"), "builder")
        brief = self.app.room_read()["messages"][-1]
        self.assertIn("Your agent role: builder", brief["text"])


class OperatorName(RoomBase):
    def test_the_operator_shows_by_its_room_setting_and_answers_to_it(self):
        self.room()
        self.assertEqual(self.app.room_label("operator"), "operator")    # the default
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_set_operator_name(r, "Jon"), "")
            self.assertEqual(self.app.room_resolve(r, ["Jon", "jon", "alpha"]),
                             ["operator", "operator", "alpha"])
        self.assertEqual(self.app.room_read()["operator_name"], "Jon")
        self.assertEqual(self.app.room_label("operator"), "Jon")
        self.assertEqual(self.app.room_state()["titles"]["operator"], "Jon")
        msg, _ = self.post("operator", "status?", ["alpha"])
        self.assertTrue(self.app.room_delivery_text(msg).startswith("[room] Jon → you"))
        used = self.app.mission_names_in_use()
        self.assertTrue(self.app.title_taken(used, "JON", "alpha"))
        with self.app.room_edit() as r:
            self.assertIn("keeps", self.app.room_set_title(r, "alpha", "jon"))
            self.assertIn("taken", self.app.room_set_operator_name(r, "beta"))
            self.assertIn("Invalid", self.app.room_set_operator_name(r, "a/b"))
            self.assertIn("taken", self.app.room_set_operator_name(r, "room"))
        self.assertEqual(self.app.room_read()["operator_name"], "Jon")


class RoomName(RoomBase):
    def test_the_room_name_is_saved_and_shown_in_the_header_and_tab_title(self):
        self.room()
        self.assertEqual(self.app.room_read()["name"], "Room")           # the default
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_set_name(r, "  Ops   <war> room "), "")
            self.assertIn("60", self.app.room_set_name(r, "x" * 61))
        self.assertEqual(self.app.room_state()["name"], "Ops <war> room")
        page = self.app.render_room_page()
        self.assertIn("<title>Ops &lt;war&gt; room</title>", page)
        self.assertIn("<span class=name>💬 Ops &lt;war&gt; room</span>", page)
        with self.app.room_edit() as r:                                # blank = back to the default
            self.app.room_set_name(r, " ")
        self.assertEqual(self.app.room_read()["name"], "Room")


class Roles(RoomBase):
    def meta(self, name):
        with open(os.path.join(self.missions, name, "mission.json")) as fh:
            return json.load(fh)

    def test_the_repo_ships_six_roles_with_descriptions(self):
        defs = self.app.agent_defs()
        self.assertEqual(sorted(defs), ["builder", "coordinator", "researcher", "reviewer", "shipper", "tester"])
        for d in defs.values():
            self.assertTrue(d.get("description"))
            self.assertIn(d.get("model"), self.app.ROOM_MODELS)

    def test_changing_a_role_records_it_and_tells_the_console_at_once(self):
        self.room()
        with open(os.path.join(self.missions, "alpha", "mission.json"), "w") as fh:
            json.dump({"title": "keep me"}, fh)
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_set_role(r, "alpha", "reviewer"), "")
        self.assertEqual(self.meta("alpha"), {"title": "keep me", "agent_role": "reviewer"})
        msg = self.app.room_read()["messages"][-1]
        self.assertEqual((msg["from"], msg["to"], msg["type"], msg["status"]),
                         ("room", ["alpha"], "role", {"alpha": "queued"}))
        typed = self.app.room_delivery_text(msg)
        self.assertIn("Your role is now reviewer: read ", typed)
        self.assertIn(os.path.join("agents", "reviewer.md"), typed)
        self.assertEqual(self.app.room_state()["members"][1]["role"], "reviewer")
        with self.app.room_edit() as r:    # same role again: nothing new is sent
            self.app.room_set_role(r, "alpha", "reviewer")
            self.assertEqual(self.app.room_set_role(r, "alpha", ""), "")
        self.assertEqual(self.meta("alpha"), {"title": "keep me"})
        msgs = self.app.room_read()["messages"]
        self.assertIn("role is cleared", msgs[-1]["text"])
        self.assertEqual(msgs[-2]["id"], msg["id"])

    def test_feed_roles_follow_the_sender_and_every_role_has_a_colour(self):
        self.room()
        self.post("alpha", "done")
        self.assertEqual(self.app.room_state()["roles"], {})
        with self.app.room_edit() as r:
            self.app.room_set_role(r, "alpha", "builder")
        self.assertEqual(self.app.room_state()["roles"], {"alpha": "builder"})
        css = self.app.agent_role_css()
        for role in self.app.agent_defs():
            self.assertIn(".role.r-%s{" % role, css)
            self.assertIn(".chip.r-%s{border-left-color:" % role, css)
        self.assertEqual(self.app.AGENT_ROLE_COLORS["coordinator"][0], "#2563eb")

    def test_unknown_role_or_non_member_is_refused(self):
        self.room(members=("coord", "alpha"))
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_set_role(r, "alpha", "wizard"), "Unknown role.")
            self.assertEqual(self.app.room_set_role(r, "beta", "builder"), "Not in the room.")
        self.assertFalse(os.path.exists(os.path.join(self.missions, "alpha", "mission.json")))

    def test_briefs_and_roles_say_the_operator_reads_the_room(self):
        self.room()
        brief = [m for m in self.app.room_read()["messages"] if m["to"] == ["alpha"]][-1]
        self.assertIn("The operator reads this room, rarely your console", brief["text"])
        for d in self.app.agent_defs().values():   # said once, in the brief
            with open(d["path"]) as fh:
                self.assertNotIn("## Talking to the operator", fh.read())

    def test_coordinator_brief_lists_member_roles(self):
        with open(os.path.join(self.missions, "alpha", "mission.json"), "w") as fh:
            json.dump({"agent_role": "tester"}, fh)
        self.room()
        brief = [m for m in self.app.room_read()["messages"] if m["to"] == ["coord"]][-1]
        self.assertIn("alpha [tester]", brief["text"])
        self.assertIn("--as ROLE", brief["text"])

    def test_session_start_hook_prints_the_role_body_only_when_set(self):
        hook = os.path.join(REPO, "scripts", "miss-agent-context.py")
        run = lambda d: subprocess.run([sys.executable, hook], capture_output=True, text=True,
                                       env=dict(os.environ, MISSION_DATA_DIR=d), timeout=30)
        alpha = os.path.join(self.missions, "alpha")
        self.assertEqual(run(alpha).stdout, "")
        with open(os.path.join(alpha, "mission.json"), "w") as fh:
            json.dump({"agent_role": "researcher"}, fh)
        r = run(alpha)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("== AGENT ROLE: researcher"), r.stdout[:80])
        self.assertIn("## Job", r.stdout)
        self.assertNotIn("description:", r.stdout)
        with open(os.path.join(alpha, "mission.json"), "w") as fh:
            json.dump({"agent_role": "../etc/passwd"}, fh)
        self.assertEqual(run(alpha).stdout, "")

    def test_session_start_hook_adds_the_join_brief_for_room_members(self):
        hook = os.path.join(REPO, "scripts", "miss-agent-context.py")
        run = lambda n: subprocess.run(
            [sys.executable, hook], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, MISSION_DATA_DIR=os.path.join(self.missions, n), MISSION_NAME=n))
        self.room(members=("coord", "alpha"))
        with open(os.path.join(self.missions, "alpha", "mission.json"), "w") as fh:
            json.dump({"agent_role": "builder"}, fh)
        out = run("alpha").stdout
        self.assertTrue(out.startswith("== AGENT ROLE: builder"), out[:80])
        self.assertIn("== ROOM (you are a member", out)
        self.assertIn("[room] You have joined", out)
        self.assertIn("COORDINATOR", run("coord").stdout)
        self.assertEqual(run("beta").stdout, "")      # not a member, no role


class Name(RoomBase):
    def meta(self, n):
        return self.app.read_mission_meta(n) or {}

    def say(self, text):                 # as the room page's Send does: @names first
        with self.app.room_edit() as r:
            to, text = self.app.room_parse_operator(text)
            return self.app.room_post(r, "operator", text, self.app.room_resolve(r, to))

    def test_a_name_is_a_title_alias_that_settles_auto_naming(self):
        self.room()
        self.app.write_mission_meta("alpha", {"auto_named": True, "title": "first-guess"})
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_set_title(r, "alpha", "  login  fixer "), "")
        self.assertEqual(self.meta("alpha"), {"title": "login-fixer"})
        self.assertTrue(os.path.isdir(os.path.join(self.missions, "alpha")))  # folder stays
        self.assertEqual(self.app.mission_title("alpha"), "login-fixer")
        told = self.app.room_read()["messages"][-1]
        self.assertEqual((told["from"], told["to"]), ("room", ["alpha"]))
        self.assertIn('"login-fixer"', told["text"])
        msg, err = self.say("@login-fixer hello")              # @ follows the name
        self.assertEqual((err, msg["to"]), ("", ["alpha"]))

    def test_duplicates_and_reserved_names_are_refused(self):
        self.room()
        self.app.write_mission_meta("beta", {"title": "ship-confirmation"})
        with self.app.room_edit() as r:
            self.assertIn("taken by beta", self.app.room_set_title(r, "alpha", "Ship-Confirmation"))
            self.assertIn("another mission's folder", self.app.room_set_title(r, "alpha", "coord"))
            self.assertIn("for itself", self.app.room_set_title(r, "alpha", "operator"))
            self.assertIn("Invalid", self.app.room_set_title(r, "alpha", "a/b"))
            self.assertEqual(self.app.room_set_title(r, "beta", "ship-confirmation"), "")
            self.assertEqual(self.app.room_set_title(r, "beta", "beta"), "")  # back to the folder
        self.assertEqual(self.meta("beta"), {})
        self.assertNotIn("alpha", [m["to"][0] for m in self.app.room_read()["messages"]
                                   if m["type"] == "status"])

    def test_a_title_two_members_share_resolves_to_neither(self):
        self.room()
        for n in ("coord", "beta"):
            self.app.write_mission_meta(n, {"title": "ship-confirmation"})
        r = self.app.room_read()
        self.assertEqual(self.app.room_resolve(r, ["ship-confirmation", "beta"]),
                         ["ship-confirmation", "beta"])
        self.assertIn("Not in the room", self.say("@ship-confirmation hi")[1])


class ShipButton(RoomBase):
    def test_page_wires_yes_ship_through_the_room_post(self):
        page = self.app.render_room_page()
        self.assertIn("function shipBtn", page)
        self.assertIn("post(ROOM.post, {text: '@' + m.name + ' YES SHIP'})", page)
        self.assertNotIn("confirm('Send YES SHIP", page)   # one tap, like the mission page

    def test_agents_still_cannot_send_it_through_the_room(self):
        self.room()
        self.assertIn("Only the operator", self.post("alpha", "YES SHIP", ["beta"])[1])

    def test_room_js_has_no_stale_model_helper_calls(self):
        import re
        self.assertIsNone(re.search(r"[^\w.]model\(", self.app.ROOM_JS))  # modelName() only

    def asks(self):
        return self.app.room_ship_asks(self.app.room_read())

    def test_only_a_ship_post_lights_it_and_later_posts_keep_it_lit(self):
        self.room()
        self.post("alpha", "I'll post when it's ready to ship")
        self.assertEqual(self.asks(), [])
        self.post("alpha", "Ready: room chips show the model.", ["operator"], "ship")
        self.post("alpha", "A note for the coordinator.")
        self.assertEqual(self.asks(), ["alpha"])

    def test_a_yes_ship_dims_it_until_a_newer_ask(self):
        self.room()
        self.fake_tmux()
        self.post("alpha", "Ready.", (), "ship")
        self.app.room_ship_sent("mission-alpha")       # the button / mission page sent it
        self.assertEqual(self.asks(), [])
        self.app.room_ship_sent("mission-nobody")      # not a member: nothing written
        self.post("alpha", "Fixed the review note — ready again.", (), "ship")
        self.assertEqual(self.asks(), ["alpha"])
        self.post("operator", "YES SHIP", ["alpha"])    # typed in the room as @alpha YES SHIP
        self.assertEqual(self.asks(), [])

    def test_its_ship_result_dims_it(self):
        self.room(coord="", members=("alpha",))
        self.post("alpha", "Ready.", (), "ship")
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app._room_ship_results = lambda n, seen: (["RESULT: SHIPPED"], 1)
        self.app.room_tick()
        self.assertEqual(self.asks(), [])

    def test_ship_results_are_marked_for_the_operator(self):
        self.room(coord="", members=("alpha", "beta"))
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app._room_ship_results = lambda n, seen: (
            ["RESULT: SHIPPED"] if n == "alpha" else ["RESULT: BLOCKED — conflict"], 1)
        self.app.room_tick()
        res = {m["text"]: m.get("result") for m in self.app.room_read()["messages"]
               if "shipped:" in m["text"]}
        self.assertEqual(sorted(res.values()), ["BLOCKED", "SHIPPED"])
        self.assertIn('m.result === \'SHIPPED\'', self.app.render_room_page())

    def test_a_running_ship_badges_its_builder_until_its_result_or_its_session_ends(self):
        self.room(coord="", members=("alpha", "beta"))
        self.fake_tmux()
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        for n in ("alpha", "beta"):
            self.post(n, "Ready.", ["operator"], "ship", commit="c0ffee1")
            self.app.room_ship_sent("mission-" + n)
        start = self.app.room_read()["messages"][-1]
        self.assertEqual((start["result"], start["text"][:26]), ("SHIPPING", "beta is shipping c0ffee1 ("))
        shipping = lambda: [m["name"] for m in self.app.room_state()["members"] if m["shipping"]]
        self.assertEqual(shipping(), ["alpha", "beta"])
        alive = {"=ship-alpha", "=ship-beta"}
        self.app._run_tmux = lambda *a, capture=False: (
            (0 if a[-1] in alive else 1, "") if a[0] == "has-session" else (0, ""))
        self.app._room_ship_results = lambda n, seen: ([], 0)
        self.app.room_tick()
        self.assertEqual(shipping(), ["alpha", "beta"])        # a reload mid-ship still shows it
        self.app._room_ship_results = lambda n, seen: (["RESULT: BLOCKED — x"] if n == "alpha" else [], 1)
        self.app.room_tick()
        self.assertEqual(shipping(), ["beta"])
        alive.clear()                                          # no RESULT, session gone
        self.app._room_ship_results = lambda n, seen: ([], 1)
        self.app.room_tick()
        self.assertEqual(shipping(), [])

    def test_a_yes_ship_before_any_ask_is_a_pre_approval_until_its_result(self):
        self.room(coord="", members=("alpha", "beta"))
        self.fake_tmux()
        pre = lambda: self.app.room_state()["preapproved"]
        self.post("beta", "Ready.", (), "ship")
        self.app.room_ship_sent("mission-beta")         # ships its ask: not a pre-approval
        self.app.room_ship_sent("mission-alpha")        # grey button, mid-task
        self.assertEqual(pre(), ["alpha"])
        self.post("operator", "YES SHIP", ["beta"])      # its ship already started: nothing
        self.assertEqual(pre(), ["alpha"])
        self.post("operator", "Next task.", ["beta"], "handoff")
        self.post("operator", "YES SHIP", ["beta"])
        self.assertEqual(pre(), ["alpha", "beta"])
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app._room_ship_results = lambda n, seen: \
            ((["RESULT: SHIPPED"], 1) if n == "alpha" else ([], 0))
        self.app.room_tick()
        self.assertEqual(pre(), ["beta"])

    def test_pre_approval_reaches_the_builder_and_the_button(self):
        self.room()
        with open(os.path.join(REPO, "agents", "builder.md")) as fh:
            self.assertIn("pre-approves your current task", fh.read())
        self.assertIn("Pre-approved: its current task ships when done", self.app.render_room_page())

    def notes(self, n):
        return [m["text"] for m in self.app.room_read()["messages"]
                if m["type"] == "approval" and m["to"] == [n]]

    def test_a_result_and_a_ship_ask_carry_their_fields(self):
        self.room(coord="", members=("alpha", "beta"))
        for fields, kind, why in (({"verdict": "", "commit": ""}, "result", "needs --verdict"),
                                  ({"verdict": "maybe"}, "result", "needs --verdict"),
                                  ({"commit": ""}, "ship", "needs --commit"),
                                  ({"commit": "HEAD"}, "ship", "Bad commit"),
                                  ({"verdict": "pass"}, "chat", "only with --type result")):
            msg, err = self.post("alpha", "x", ["beta"], kind, **fields)
            self.assertIsNone(msg)
            self.assertIn(why, err)
        msg, err = self.post("alpha", "All 3 steps.", ["beta"], "result", verdict="PASS",
                             commit="ABCDEF12")
        self.assertEqual((msg["verdict"], msg["commit"]), ("pass", "abcdef12"))
        self.assertIn("(result PASS abcdef1):", self.app.room_delivery_text(msg))

    def test_the_rocket_ships_that_ask_and_its_commit_for_the_agent(self):
        self.room(coord="", members=("alpha",))
        self.fake_tmux()
        ask, _ = self.post("alpha", "Ready.", ["operator"], "ship", commit="f1cad4304dce")
        self.assertEqual(self.app.room_state()["askcommits"], {"alpha": "f1cad4304dce"})
        self.app.room_ship_sent("mission-alpha")
        self.assertEqual(self.notes("alpha"), [])            # the builder needs nothing
        last = self.app.room_read()["messages"][-1]
        self.assertEqual((last["type"], last["to"], last["status"]), ("event", [], {}))
        self.assertEqual(last["text"], "alpha is shipping "
                         "f1cad4304dce (room #%d): miss-ship.py runs in tmux session ship-alpha."
                         % ask["id"])
        self.assertIn("f1cad4304dce", self.ships()[0])
        self.assertEqual(self.app.room_state()["preapproved"], [])

    def test_a_pre_approval_binds_to_the_latest_task_brief_and_a_new_one_drops_it(self):
        self.room(coord="coord", members=("coord", "alpha"))
        self.post("coord", "Task one.", ["alpha"], "handoff")
        two, _ = self.post("coord", "Task two.", ["alpha"], "handoff")
        self.app.room_ship_sent("mission-alpha")
        self.assertIn("current task (room #%d)" % two["id"], self.notes("alpha")[-1])
        self.assertEqual(self.app.room_read()["pretask"], {"alpha": two["id"]})
        brief = self.app.room_brief(self.app.room_read(), "alpha")   # what a /clear re-reads
        self.assertIn("pre-approves your current task (room #%d)" % two["id"], brief)
        self.post("coord", "Something else.", ["alpha"], "chat")
        self.assertEqual(self.app.room_state()["preapproved"], ["alpha"])
        self.post("coord", "Task three.", ["alpha"], "handoff")
        self.assertEqual(self.app.room_state()["preapproved"], [])
        self.assertNotIn("operator's YES SHIP pre-approves", self.app.room_brief(self.app.room_read(), "alpha"))

    def test_the_old_per_agent_flag_is_dropped(self):
        self.room(coord="", members=("alpha",))
        with self.app.room_edit() as r:
            r["preapproved"] = {"alpha": True}
        self.assertNotIn("preapproved", self.app.room_read())
        self.assertEqual(self.app.room_state()["preapproved"], [])


class ShipOnPass(RoomBase):
    """A tester's PASS lights the 🚀; the operator's tap ships the passed commit with no
    Claude: miss-ship.py runs in a tmux session on the console server."""
    def setUp(self):
        super().setUp()
        self.room(coord="coord", members=("coord", "alpha", "beta"))
        self.fake_tmux()
        self.post("alpha", "Test claude/alpha at 1a2b3c4: the chip shows X.", ["beta"], "handoff")

    def test_a_pass_lights_it_and_the_tap_runs_the_script_for_that_commit(self):
        ps, _ = self.post("beta", "PASS: X shows on all 3 chips.", ["alpha", "coord"], "result",
                          commit="1a2b3c4")
        self.assertEqual(self.app.room_ship_asks(self.app.room_read()), ["alpha"])
        self.assertEqual(self.app.room_state()["askcommits"]["alpha"], "1a2b3c4")
        with self.app.room_edit() as r:
            tap, err = self.app.room_operator_post(r, "@alpha YES SHIP")
        self.assertEqual((err, tap["status"]), ("", {}))     # the phrase is typed nowhere
        (args,) = self.ships()
        self.assertEqual(args[:4], ("new-session", "-d", "-s", "ship-alpha"))
        argv = list(args[args.index("--approval") - 1:])
        self.assertEqual(argv[1:], ["--approval", "YES SHIP", "--commit", "1a2b3c4", "--request",
                                    "Test claude/alpha at 1a2b3c4: the chip shows X.",
                                    "--tests", "PASS: X shows on all 3 chips."])
        self.assertIn("MISSION_DATA_DIR=" + os.path.join(self.missions, "alpha"), args)
        self.assertNotIn("alpha", self.app.room_ship_asks(self.app.room_read()))
        self.assertEqual(ps["status"], {"alpha": "queued", "coord": "queued"})
        self.assertIn("alpha is shipping 1a2b3c4 (room #%d)" % ps["id"],
                      self.app.room_read()["messages"][-1]["text"])

    def test_dry_run_env_adds_the_flag(self):
        self.post("beta", "PASS.", ["alpha"], "result")
        os.environ["MISSION_SHIP_DRY_RUN"] = "1"
        try:
            self.post("operator", "YES SHIP", ["alpha"])
        finally:
            del os.environ["MISSION_SHIP_DRY_RUN"]
        self.assertEqual(self.ships()[0][-1], "--dry-run")

    def test_a_later_fail_unlights_it(self):
        self.post("beta", "PASS.", ["alpha"], "result")
        self.post("beta", "FAIL: broke on reload.", ["alpha"], "result", verdict="fail")
        self.assertNotIn("alpha", self.app.room_ship_asks(self.app.room_read()))

    def test_no_console_server_keeps_it_lit_and_tells_the_operator(self):
        self.app._run_tmux = lambda *a, capture=False: (1, "")
        self.post("beta", "PASS.", ["alpha"], "result")
        self.post("operator", "YES SHIP", ["alpha"])
        self.assertIn("alpha", self.app.room_ship_asks(self.app.room_read()))
        last = self.app.room_read()["messages"][-1]
        self.assertEqual(last["to"], ["operator"])
        self.assertIn("no console server", last["text"])

    def test_a_pre_approved_task_ships_when_its_pass_arrives(self):
        self.post("operator", "YES SHIP", ["alpha"])          # early: no PASS yet
        self.assertEqual(self.app.room_state()["preapproved"], ["alpha"])
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app._room_ship_results = lambda n, seen: ([], 0)
        self.app.room_tick()
        self.assertEqual(self.ships(), [])
        self.post("beta", "PASS.", ["alpha"], "result")       # the tester's CLI: no ship there
        self.assertEqual(self.ships(), [])
        self.app.room_tick()
        self.assertEqual(len(self.ships()), 1)
        self.app.room_tick()
        self.assertEqual(len(self.ships()), 1)                 # once, though still pre-approved

    def test_no_agent_is_told_to_ship_and_the_phrase_is_typed_nowhere(self):
        self.post("alpha", "Ready.", ["operator"], "ship")       # an ask, no PASS
        tap, _ = self.post("operator", "YES SHIP", ["alpha"])
        self.assertEqual((tap["status"], len(self.ships())), ({}, 1))
        early, _ = self.post("operator", "YES SHIP", ["beta"])  # a pre-approval
        self.assertEqual(early["status"], {})
        notes = [m["text"] for m in self.app.room_read()["messages"] if m["type"] == "approval"]
        self.assertEqual(len(notes), 1)                          # only the pre-approval's
        self.assertIn("ship script yourself", notes[0])
        self.assertNotIn("--commit", notes[0])

    def test_repeated_approvals_start_one_ship(self):
        self.post("operator", "YES SHIP", ["alpha"])            # pre-approval
        self.post("beta", "PASS.", ["alpha"], "result")
        self.post("operator", "YES SHIP", ["alpha"])            # a tap before the courier
        self.post("operator", "YES SHIP", ["alpha"])            # and a second tap
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app._room_ship_results = lambda n, seen: ([], 0)
        self.app.room_tick()                                     # the pre-approval's turn
        self.assertEqual(len(self.ships()), 1)
        self.assertIn("already started a ship", self.app.room_read()["messages"][-1]["text"])
        self.app._room_ship_results = lambda n, seen: \
            ((["RESULT: SHIPPED"], 1) if n == "alpha" else ([], 0))
        self.app.room_tick()                                     # it shipped; a tap after it
        self.post("operator", "YES SHIP", ["alpha"])
        self.assertEqual((len(self.ships()), self.app.room_state()["preapproved"]), (1, []))

    def test_after_a_blocked_the_same_tasks_next_pass_ships(self):
        self.post("beta", "PASS.", ["alpha"], "result")
        self.post("operator", "YES SHIP", ["alpha"])
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app._room_ship_results = lambda n, seen: \
            ((["RESULT: BLOCKED — behind working"], 1) if n == "alpha" else ([], 0))
        self.app.room_tick()
        self.app._room_ship_results = lambda n, seen: ([], 0)
        self.post("operator", "YES SHIP", ["alpha"])            # reopened: a pre-approval again
        self.assertEqual(self.app.room_state()["preapproved"], ["alpha"])
        self.post("beta", "PASS after the rebase.", ["alpha"], "result", commit="9f8e7d6")
        self.app.room_tick()
        self.assertEqual(len(self.ships()), 2)
        self.assertIn("9f8e7d6", self.ships()[1])

    def test_a_blocked_keeps_the_pre_approval_and_needs_attention_drops_it(self):
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        for result, kept in (("RESULT: BLOCKED — behind working", True),
                             ("RESULT: NEEDS_ATTENTION — x", False)):
            self.post("operator", "Task: %s." % result, ["alpha"], "handoff")
            self.post("operator", "YES SHIP", ["alpha"])         # pre-approval
            self.post("beta", "PASS.", ["alpha"], "result")
            self.app._room_ship_results = lambda n, seen: ([], 0)
            self.app.room_tick()                                 # ships it
            self.app._room_ship_results = lambda n, seen, r=result: \
                (([r], 1) if n == "alpha" else ([], 0))
            self.app.room_tick()
            self.assertEqual(self.app.room_state()["preapproved"] == ["alpha"], kept, result)
        self.app._room_ship_results = lambda n, seen: ([], 0)
        self.post("operator", "Task: blocked again.", ["alpha"], "handoff")
        self.post("operator", "YES SHIP", ["alpha"])
        self.app._room_ship_results = lambda n, seen: \
            ((["RESULT: BLOCKED — behind working"], 1) if n == "alpha" else ([], 0))
        self.post("beta", "PASS.", ["alpha"], "result")
        self.app.room_tick()                                     # ships; a BLOCKED comes back
        self.app._room_ship_results = lambda n, seen: ([], 0)
        self.post("beta", "PASS after the rebase.", ["alpha"], "result", commit="9f8e7d6")
        self.app.room_tick()                                     # no second tap needed
        self.assertIn("9f8e7d6", self.ships()[-1])

    def test_a_new_pass_after_a_blocked_lights_it_for_a_tap(self):
        self.post("beta", "PASS.", ["alpha"], "result")
        self.post("operator", "YES SHIP", ["alpha"])
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app._room_ship_results = lambda n, seen: \
            ((["RESULT: NEEDS_ATTENTION — x"], 1) if n == "alpha" else ([], 0))
        self.app.room_tick()
        self.post("beta", "PASS after the fix.", ["alpha"], "result", commit="9f8e7d6")
        self.assertIn("alpha", self.app.room_ship_asks(self.app.room_read()))
        self.post("operator", "YES SHIP", ["alpha"])
        self.assertEqual(len(self.ships()), 2)

    def test_a_running_ship_session_blocks_another(self):
        self.post("beta", "PASS.", ["alpha"], "result")
        self.app._run_tmux = lambda *a, capture=False: (0, "")   # ship-alpha still exists
        self.post("operator", "YES SHIP", ["alpha"])
        self.assertIn("ship-alpha is still running", self.app.room_read()["messages"][-1]["text"])

    def test_a_failed_ship_goes_to_its_builder_and_the_coordinator(self):
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app._room_ship_results = lambda n, seen: \
            ((["RESULT: BLOCKED — behind working"], 1) if n == "alpha" else
             (["RESULT: SHIPPED"], 1) if n == "beta" else ([], 0))
        self.app.room_tick()
        to = dict((m["text"].split(" shipped")[0], m["to"])
                  for m in self.app.room_read()["messages"] if " shipped: " in m["text"])
        self.assertEqual(to, {"alpha": ["alpha", "coord"], "beta": []})
        st = [m["status"] for m in self.app.room_read()["messages"] if " shipped: " in m["text"]]
        self.assertEqual(st, [{"coord": "queued"}, {"alpha": "queued", "coord": "queued"}])

    def test_a_copy_of_a_pass_ships_nothing(self):
        self.post("beta", "PASS.", ["alpha", "coord"], "result")
        self.post("operator", "YES SHIP", ["coord"])                 # the coordinator's copy
        self.assertEqual(self.ships(), [])
        self.assertIn("coordinator never ships", self.app.room_read()["messages"][-1]["text"])
        self.post("beta", "PASS of nothing asked.", ["coord"], "result")
        self.assertEqual(self.app.room_ship_asks(self.app.room_read()), ["alpha"])

    def test_yes_ship_on_a_members_page_is_the_rocket_and_is_not_typed(self):
        self.post("beta", "PASS.", ["alpha"], "result")
        self.assertTrue(self.app.room_ship_sent("mission-alpha"))
        self.assertEqual(len(self.ships()), 1)
        self.assertFalse(self.app.room_ship_sent("mission-nobody"))
        src = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
        at = src.index('if path == "/console/key":')
        self.assertLess(src.index("room_ship_sent(session)", at), src.index("console_send(", at))


class CoordinatorShips(RoomBase):
    """The operator's "<coordinator> may ship" switch: while it is on, `miss-room.py ship
    NAME` from the coordinator does what the operator's 🚀 tap does, on a lit PASS only."""
    def setUp(self):
        super().setUp()
        self.room(coord="coord", members=("coord", "alpha", "beta"))
        self.fake_tmux()
        self.post("alpha", "Test claude/alpha at 1a2b3c4: the chip shows X.", ["beta"], "handoff")

    def flip(self):
        with self.app.room_edit() as r:
            return self.app.room_set_coord_ship(r)

    def ship(self, sender="coord", name="alpha"):
        with self.app.room_edit() as r:
            return self.app.room_coord_ship(r, sender, name)

    def run_script(self, *args, mission="coord"):
        env = dict(os.environ, MISSION_NAME=mission)
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "miss-room.py"), *args],
                           capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_off_by_default_and_the_coordinator_is_refused(self):
        self.post("beta", "PASS.", ["alpha"], "result", commit="1a2b3c4")
        before = self.app.room_read()
        self.assertFalse(before["coord_ship"])
        self.assertIn("switch is off", self.ship())
        self.assertEqual((self.ships(), self.app.room_read()), ([], before))   # nothing changed
        rc, out = self.run_script("ship", "alpha")
        self.assertEqual(rc, 1, out)
        self.assertIn("Nothing shipped: the operator's \"coord may ship\" switch is off.", out)
        self.assertEqual(self.app.room_ship_asks(self.app.room_read()), ["alpha"])   # still lit

    def test_on_it_ships_a_lit_pass_exactly_like_the_tap(self):
        self.flip()
        told = self.app.room_read()["messages"][-1]
        self.assertEqual((told["from"], told["to"], told["status"]), ("room", ["coord"], {"coord": "queued"}))
        self.assertIn("ON", told["text"])
        ps, _ = self.post("beta", "PASS: X shows on all 3 chips.", ["alpha", "coord"], "result",
                          commit="1a2b3c4")
        self.assertEqual(self.ship(), "")
        (args,) = self.ships()
        self.assertEqual(args[:4], ("new-session", "-d", "-s", "ship-alpha"))
        argv = list(args[args.index("--approval") - 1:])
        self.assertEqual(argv[1:], ["--approval", "YES SHIP", "--commit", "1a2b3c4", "--request",
                                    "Test claude/alpha at 1a2b3c4: the chip shows X.",
                                    "--tests", "PASS: X shows on all 3 chips."])
        room = self.app.room_read()
        event, record = room["messages"][-2:]
        self.assertEqual((event["from"], event["result"]), ("room", "SHIPPING"))
        self.assertEqual((record["from"], record["to"], record["type"], record["status"]),
                         ("coord", ["operator"], "delegated", {}))
        self.assertIn("alpha's 1a2b3c4 (beta's PASS, room #%d) on the operator's delegation"
                      % ps["id"], record["text"])
        self.assertFalse([m for m in room["messages"] if m["from"] == "operator"])
        self.assertNotIn("alpha", self.app.room_ship_asks(room))
        self.assertIn("alpha", room["shipping"])
        self.assertIn("no tester's PASS", self.ship())   # unlit now: a second call ships nothing
        self.assertEqual(len(self.ships()), 1)

    def test_on_it_refuses_a_member_with_no_lit_pass(self):
        self.flip()
        for setup in (lambda: None,
                      lambda: self.post("alpha", "ready", ["operator"], "ship"),    # an ask alone
                      lambda: self.post("beta", "FAIL.", ["alpha"], "result", verdict="fail")):
            setup()
            self.assertIn("no tester's PASS has lit alpha's 🚀", self.ship())
        self.assertIn("no tester's PASS", self.ship(name="coord"))
        self.assertIn("no tester's PASS", self.ship(name="nobody"))
        rc, out = self.run_script("ship", "alpha")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.ships(), [])

    def test_a_non_coordinator_is_always_refused(self):
        self.post("beta", "PASS.", ["alpha"], "result", commit="1a2b3c4")
        for on in (False, True):
            if on:
                self.flip()
            for who in ("alpha", "beta", "operator", ""):
                self.assertEqual(self.ship(sender=who), "only the room's coordinator can ship")
            rc, out = self.run_script("ship", "alpha", mission="beta")
            self.assertEqual(rc, 1, out)
            self.assertIn("only the room's coordinator", out)
        self.assertEqual(self.ships(), [])

    def test_agents_cannot_flip_it(self):
        rc, out = self.run_script("coord_ship")              # miss-room.py has no such command
        self.assertNotEqual(rc, 0)
        for text in ("coord_ship on", "/room/settings coord_ship"):
            self.post("coord", text, ["alpha"])
        self.assertFalse(self.app.room_read()["coord_ship"])
        self.assertEqual(self.flip(), "")                      # the room page's switch
        self.assertTrue(self.app.room_read()["coord_ship"])
        self.flip()
        self.assertFalse(self.app.room_read()["coord_ship"])
        self.assertIn("OFF", self.app.room_read()["messages"][-1]["text"])

    def test_the_room_page_shows_the_switch_and_the_settings_route_flips_it(self):
        import http.client, threading, urllib.parse
        from http.server import ThreadingHTTPServer
        page = self.app.render_room_page()
        self.assertIn("id=tg-cship type=button aria-pressed=false", page)
        self.assertIn("coord may ship</span>", page)
        self.assertIn("post(ROOM.settings, {key: 'coord_ship'})", page)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.app.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            c.request("POST", "/room/settings", body=urllib.parse.urlencode({"key": "coord_ship"}),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            r = c.getresponse()
            body = json.loads(r.read())
            c.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual((r.status, body["ok"]), (200, True))
        self.assertTrue(self.app.room_state()["coord_ship"])
        self.assertIn("id=tg-cship type=button aria-pressed=true", self.app.render_room_page())

    def test_the_coordinators_brief_names_the_command(self):
        brief = self.app.room_brief(self.app.room_read(), "coord")
        self.assertIn("miss-room.py ship NAME", brief)
        self.assertIn("switch is on (it is off;", brief)


class Unread(RoomBase):
    """Status posts and copies of a result are not typed: the target's next prompt brings
    them along, once (scripts/miss-agent-context.py prompt, a UserPromptSubmit hook)."""
    def setUp(self):
        super().setUp()
        self.room()
        self.typed = []
        self.app.console_send = lambda s, a, t="", submit=False: (self.typed.append(t) or (True, ""))
        self.app._room_states = lambda names: {n: ("waiting", "u") for n in names}
        self.app._room_ship_results = lambda n, seen: ([], 0)
        self.app._chat_transcript_file = lambda n: None
        self.app.ROOM_SETTLE = 0
        with self.app.room_edit() as r:          # the join briefs are not what is tested
            for m in r["messages"]:
                m["status"] = dict((t, "acked") for t in m["status"])

    def hook(self, n):
        return subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "miss-agent-context.py"), "prompt"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, MISSION_DATA_DIR=os.path.join(self.missions, n),
                     MISSION_NAME=n)).stdout

    def test_a_status_post_types_nothing_and_the_next_prompt_brings_it_once(self):
        one, _ = self.post("alpha", "Sent the test to beta.", kind="status")
        two, _ = self.post("beta", "Holding.", ["coord"], "status")
        self.assertEqual((one["status"], two["status"]), ({"coord": "unread"}, {"coord": "unread"}))
        self.app.room_tick()
        self.assertEqual(self.typed, [])
        self.post("operator", "Next task?")          # typed as before
        self.app.room_tick()
        self.assertEqual(len(self.typed), 1)
        out = self.hook("coord")
        self.assertIn("unread post #%d" % one["id"], out)
        self.assertIn("Sent the test to beta.", out)
        self.assertLess(out.index("Sent the test"), out.index("Holding."))
        self.assertEqual(self.hook("coord"), "")      # once
        st = dict((m["id"], m["status"]) for m in self.app.room_read()["messages"])
        self.assertEqual((st[one["id"]], st[two["id"]]), ({"coord": "acked"}, {"coord": "acked"}))
        self.assertEqual(self.hook("alpha"), "")

    def test_handoffs_questions_results_to_the_builder_and_room_notices_are_typed(self):
        self.post("alpha", "Test claude/alpha at 1a2b3c4.", ["beta"], "handoff")
        q, _ = self.post("alpha", "Which port?", ["beta"], "question")
        res, _ = self.post("beta", "PASS.", ["alpha", "coord"], "result")
        self.assertEqual(q["status"], {"beta": "queued"})
        self.assertEqual(res["status"], {"alpha": "queued", "coord": "queued"})
        lone, _ = self.post("beta", "FAIL.", ["coord"], "result", verdict="fail")
        self.assertEqual(lone["status"], {"coord": "queued"})   # no test request: as today
        with self.app.room_edit() as r:
            self.app.room_set_role(r, "alpha", "coordinator")   # the room's own status note
        told = [m for m in self.app.room_read()["messages"] if m["type"] == "status"][-1]
        self.assertEqual(told["status"], {"coord": "queued"})



class CoordinatorNews(RoomBase):
    """A PASS that lights a 🚀 pushes and chimes once; every result and ship RESULT is
    typed into the coordinator's console once; the coordinator is never cold-held."""
    def setUp(self):
        super().setUp()
        self.room()
        self.typed, self.pushes = [], []
        self.app.room_push = lambda title, body, sound=True, kind="": \
            self.pushes.append((title, kind))
        self.app.console_send = lambda s, a, t="", submit=False: (
            a == "text" and self.typed.append((s[len("mission-"):], t)) or (True, ""))
        self.app._room_states = lambda names: {n: ("waiting", "u") for n in names}
        self.app._room_ship_results = lambda n, seen: ([], 0)
        self.app._chat_transcript_file = lambda n: None
        self.app.ROOM_SETTLE = 0
        with self.app.room_edit() as r:
            r["push"] = True
            for m in r["messages"]:
                m["status"] = dict((t, "acked") for t in m["status"])
        self.app.room_tick()                              # first look: nothing replayed
        self.post("alpha", "Test claude/alpha at 1a2b3c4.", ["beta"], "handoff")

    def ticks(self, n=4):
        for _ in range(n):
            self.app.room_tick()
            with self.app.room_edit() as r:               # each typed delivery taken in
                for m in r["messages"]:
                    m["status"] = dict((t, "acked" if s == "sent" else s)
                                       for t, s in m["status"].items())

    def to_coord(self, needle):
        return [t for who, t in self.typed if who == "coord" and needle in t]

    def test_a_pass_pushes_and_chimes_once_and_a_fail_does_neither(self):
        fail, _ = self.post("beta", "FAIL: broke on reload.", ["alpha"], "result", verdict="fail")
        self.ticks()
        self.assertEqual((self.pushes, fail.get("ask")), ([], None))
        ps, _ = self.post("beta", "PASS: all chips.", ["alpha"], "result")
        self.ticks()
        self.assertEqual(self.pushes, [("🚀 alpha asks to ship", "ship")])
        shown = [m for m in self.app.room_state()["messages"] if m.get("ask")]
        self.assertEqual([(m["id"], m["ask"]) for m in shown], [(ps["id"], "alpha")])  # the chime
        self.assertIn("(m.type === 'ship' || m.ask)", self.app.ROOM_JS)
        self.ticks()
        self.assertEqual(len(self.pushes), 1)             # the same PASS never again

    def test_a_pre_approved_pass_ships_without_asking(self):
        with self.app.room_edit() as r:
            r["pretask"]["alpha"] = 0
        ps, _ = self.post("beta", "PASS.", ["alpha"], "result")
        self.assertNotIn("ask", ps)

    def test_a_result_reaches_the_coordinator_once_addressed_to_it_or_not(self):
        self.post("beta", "PASS one.", ["alpha"], "result")
        self.post("beta", "FAIL two.", ["alpha", "coord"], "result", verdict="fail")
        self.ticks()
        self.assertEqual((len(self.to_coord("PASS one.")), len(self.to_coord("FAIL two."))), (1, 1))
        self.assertEqual(len([1 for who, t in self.typed if who == "alpha" and "PASS one." in t]), 1)

    def test_a_ship_result_reaches_the_coordinator_once(self):
        lines = {"alpha": ["RESULT: SHIPPED"], "beta": ["RESULT: BLOCKED — behind"]}
        self.app._room_ship_results = lambda n, seen: (lines.pop(n, []), 1)
        self.ticks()
        self.assertEqual(len(self.to_coord("alpha shipped: RESULT: SHIPPED")), 1)
        self.assertEqual(len(self.to_coord("beta shipped: RESULT: BLOCKED")), 1)

    def test_the_coordinator_is_never_cold_held(self):
        self.post("operator", "hello coord")
        self.post("operator", "hello alpha", ["alpha"])
        self.app.room_tick()
        held = [(m["id"], t) for m in self.app.room_read()["messages"]
                for t, s in m["status"].items() if s == "sent"]
        self.app.room_cold_held = lambda room: held
        self.app.room_tick()
        st = dict((m["text"], m["status"]) for m in self.app.room_read()["messages"])
        self.assertEqual((st["hello coord"]["coord"], st["hello alpha"]["alpha"]), ("queued", "cold"))
        self.app.room_cold_held = lambda room: []
        self.typed.clear()
        self.app.room_tick()
        self.assertEqual([who for who, t in self.typed if "hello coord" in t], ["coord"])
        mid = next(m["id"] for m in self.app.room_read()["messages"] if m["text"] == "hello coord")
        self.assertFalse([m for m in self.app.room_read()["messages"]
                          if "holding room message #%d " % mid in m["text"]])


class ShipAgent(RoomBase):
    """A blocked ship goes to the room's ship agent (role shipper), not the builder; its
    rebased commit ships on the tester's PASS with no new tap; SHIPPED clears both."""
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.missions, "gamma"))
        with open(os.path.join(self.missions, "gamma", "mission.json"), "w") as fh:
            json.dump({"agent_role": "shipper"}, fh)
        self.room(coord="coord", members=("coord", "alpha", "beta", "gamma"))
        self.fake_tmux()
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.pos = 0
        self.result("")
        self.post("alpha", "Test claude/alpha at 1a2b3c4.", ["beta"], "handoff")

    def result(self, line):
        self.pos += 1
        self.app._room_ship_results = lambda n, seen, p=self.pos: \
            (([line], p) if line and n == "alpha" else ([], 0))

    def test_blocked_goes_to_the_ship_agent_and_its_fix_ships_untapped(self):
        self.post("beta", "PASS.", ["alpha", "coord"], "result", commit="1a2b3c4")
        self.post("operator", "YES SHIP", ["alpha"])
        self.result("RESULT: BLOCKED — the rebase failed (conflicts in: docs/x.md)")
        self.app.room_tick()
        fix = self.app.room_read()["messages"][-1]
        self.assertEqual((fix["type"], fix["for"], fix["to"]), ("handoff", "alpha", ["gamma", "coord"]))
        self.assertEqual(fix["status"], {"gamma": "queued", "coord": "queued"})
        self.assertIn("old commit 1a2b3c4", fix["text"])
        self.assertFalse([m for m in self.app.room_read()["messages"]
                          if "alpha" in m["to"] and m["id"] > fix["id"] - 2 and m["from"] == "room"])
        self.result("")
        self.post("gamma", "Re-test claude/alpha: 1a2b3c4 is now 9f8e7d6.", ["beta"], "handoff")
        ps, _ = self.post("beta", "PASS on 9f8e7d6.", ["gamma"], "result", commit="9f8e7d6")
        self.assertEqual(self.app.room_ship_asks(self.app.room_read()), ["alpha"])
        self.app.room_tick()                                     # no tap: it ships
        self.assertEqual(len(self.ships()), 2)
        self.assertIn("9f8e7d6", self.ships()[1])
        self.result("RESULT: SHIPPED — deployed")
        self.app.room_tick()
        clears = [m["to"] for m in self.app.room_read()["messages"] if m.get("reset") == "clear"]
        self.assertIn(["alpha"], clears)
        self.assertIn(["gamma"], clears)

    def test_a_clean_ship_clears_only_the_builder(self):
        self.post("beta", "PASS.", ["alpha"], "result")
        self.post("operator", "YES SHIP", ["alpha"])
        self.result("RESULT: SHIPPED — deployed")
        self.app.room_tick()
        clears = [m["to"] for m in self.app.room_read()["messages"] if m.get("reset") == "clear"]
        self.assertEqual(clears, [["alpha"]])


class ShipResultsAcrossRestarts(RoomBase):
    """Every ship restarts the dashboard ~2 s before it writes RESULT to ship.log: the
    courier's read position lives in room.json, so the fresh process posts it, once."""
    def setUp(self):
        super().setUp()
        self.room(coord="", members=("alpha",))
        self.log = os.path.join(self.missions, "alpha", "ship.log")

    def restart(self):
        self.app = _load_app()
        self.app.mission_context = lambda n: {}
        self.app._room_states = lambda names: {n: ("working", "") for n in names}

    def append(self, text):
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(text)

    def shipped(self):
        return [m["text"] for m in self.app.room_read()["messages"] if "shipped:" in m["text"]]

    def test_a_result_written_across_a_restart_posts_once(self):
        self.append("deploy: restarting\nRESULT: SHIPPED (an old one)\n")
        self.restart()
        self.app.room_tick()                              # first look: old ships not replayed
        self.app.room_ship_sent("mission-alpha")          # a pre-approval, cleared by a RESULT
        self.assertEqual(self.app.room_state()["preapproved"], ["alpha"])
        self.append("verify: ok\n")
        self.app.room_tick()
        self.restart()                                    # the ship restarts the dashboard...
        self.append("RESULT: SHIPPED\n")                  # ...then writes its RESULT
        self.app.room_tick()
        self.assertEqual(self.shipped(), ["alpha shipped: RESULT: SHIPPED"])
        self.assertEqual(self.app.room_state()["preapproved"], [])
        self.restart()
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(len(self.shipped()), 1)          # never again

    def test_a_first_ship_log_counts_and_a_removed_member_starts_over(self):
        self.restart()
        self.app.room_tick()                              # no ship.log yet: position 0
        self.append("RESULT: SHIPPED\n")
        self.app.room_tick()
        self.assertEqual(len(self.shipped()), 1)
        with self.app.room_edit() as r:
            self.app.room_set_member(r, "alpha", False)
        self.assertEqual(self.app.room_read()["shiplog"], {})
        self.append("RESULT: SHIPPED (while out of the room)\n")
        with self.app.room_edit() as r:
            self.app.room_set_member(r, "alpha", True)
        self.app.room_tick()
        self.assertEqual(len(self.shipped()), 1)


class Questions(RoomBase):
    def say(self, text):
        with self.app.room_edit() as r:
            return self.app.room_operator_post(r, text)

    def test_a_question_to_the_operator_is_pinned_until_answered(self):
        self.room()
        with self.app.room_edit() as r:
            q, err = self.app.room_post(r, "alpha", "Dev instance or header?", ["operator"],
                                        "question", ["Use a dev instance", "  Header   is OK "])
        self.assertEqual(err, "")
        self.assertEqual((q["qid"], q["options"]), ("Q%d" % q["id"], ["Use a dev instance", "Header is OK"]))
        open_q = self.app.room_open_questions(self.app.room_read())
        self.assertEqual([x["qid"] for x in open_q], [q["qid"]])
        ans, err = self.say("@%s Use a dev instance" % q["qid"].lower())
        self.assertEqual(err, "")
        self.assertEqual((ans["to"], ans["type"], ans["re"]), (["alpha"], "answer", [q["qid"]]))
        self.assertIn("(answer to %s)" % q["qid"], self.app.room_delivery_text(ans))
        self.assertEqual(self.app.room_open_questions(self.app.room_read()), [])
        self.assertIn("not an open question", self.say("@%s again?" % q["qid"])[1])

    def test_only_member_questions_to_the_operator_get_an_id(self):
        self.room()
        self.assertNotIn("qid", self.post("alpha", "Which branch?", ["beta"], "question")[0])
        self.assertNotIn("qid", self.post("alpha", "Plain note", ["operator"])[0])
        with self.app.room_edit() as r:
            self.assertIn("only go with --type question",
                          self.app.room_post(r, "alpha", "x", ["operator"], "chat", ["a"])[1])
            self.assertIn("At most", self.app.room_post(r, "alpha", "x", ["operator"], "question",
                                                        list("abcdefg"))[1])
        self.assertIn("Q99 is not an open question", self.say("@Q99 hi")[1])


class CloseQuestion(RoomBase):
    def ask(self, sender="alpha"):
        with self.app.room_edit() as r:
            return self.app.room_post(r, sender, "Which port?", ["operator"], "question")[0]["qid"]

    def open_ids(self):
        return [q["qid"] for q in self.app.room_open_questions(self.app.room_read())]

    def test_the_asker_closes_its_own_question_with_any_post(self):
        self.room()
        q, other = self.ask(), self.ask("beta")
        with self.app.room_edit() as r:
            msg, err = self.app.room_post(r, "alpha", "Jon said 4209 in chat.", ["coord"],
                                          closes=[q.lower()])
        self.assertEqual((err, msg["closes"], msg["to"]), ("", [q], ["coord"]))
        self.assertEqual(self.open_ids(), [other])

    def test_nobody_else_closes_it_and_a_refusal_posts_nothing(self):
        self.room()
        q = self.ask()
        before = self.app.room_read()["next_id"]
        with self.app.room_edit() as r:
            self.assertIn("%s is not an open question of yours" % q,
                          self.app.room_post(r, "beta", "done", closes=[q])[1])
            self.assertIn("Q999 is not an open question of yours",
                          self.app.room_post(r, "alpha", "done", closes=["Q999"])[1])
        self.assertEqual((self.app.room_read()["next_id"], self.open_ids()), (before, [q]))

    def test_the_operators_x_unpins_it_with_an_event(self):
        self.room()
        q = self.ask()
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_close_question(r, q), "")
            self.assertIn("is not an open question", self.app.room_close_question(r, q))
        room = self.app.room_read()
        self.assertEqual(self.app.room_open_questions(room), [])
        self.assertEqual((room["messages"][-1]["type"], room["messages"][-1]["text"]),
                         ("event", "operator closed %s." % q))

    def test_cli_closes_and_brief_and_page_offer_it(self):
        self.room()
        q = self.ask()
        env = dict(os.environ, MISSION_NAME="alpha")
        out = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "miss-room.py"), "post",
                              "--to", "coord", "--closes", q, "answered in chat"], env=env,
                             capture_output=True, text=True).stdout
        self.assertIn("Closed %s." % q, out)
        self.assertEqual(self.open_ids(), [])
        self.assertIn("--closes Q<n>", self.app.room_brief(self.app.room_read(), "alpha"))
        page = self.app.render_room_page()
        self.assertIn('"close": "/room/close', page)
        self.assertIn("post(ROOM.close, {name: q.qid})", page)


class AgentTalk(RoomBase):
    def test_agent_to_agent_talk_folds_and_a_header_toggle_hides_it(self):
        page = self.app.render_room_page()
        self.assertIn("A2A_KEEP = {ship: 1, question: 1, answer: 1, status: 1, result: 1}", page)
        self.assertIn("m.from !== 'operator' && m.from !== 'room' && m.to.length > 0", page)
        self.assertIn("m.to.indexOf('operator') < 0", page)
        self.assertIn("if (agentTalk(m)) { msgsEl.appendChild(folded(m)); return; }", page)
        self.assertIn("id=tg-a2a", page)
        self.assertIn("localStorage.setItem('room-a2a'", page)
        self.assertIn("body.noa2a .a2a { display:none; }", page)


class ContextAsk(RoomBase):
    """Past ROOM_CTX_ASK the room tells the coordinator in a plain note, once per crossing;
    nothing is pinned for the operator."""
    def setUp(self):
        super().setUp()
        self.room()

    def ask(self, seat="coord", **tokens):
        with self.app.room_edit() as r:
            r["coordinator"] = seat
            self.app.room_ctx_asks(r, tokens)
        room = self.app.room_read()
        self.assertEqual(self.app.room_open_questions(room), [])
        return [m for m in room["messages"] if m["from"] == "room" and "context" in m["text"]]

    def test_once_per_crossing_as_a_note_to_the_coordinator(self):
        self.assertEqual(self.app.ROOM_CTX_ASK, 200_000)
        self.assertEqual(self.ask(alpha=199999, beta=None), [])
        notes = self.ask(alpha=312000)
        self.assertEqual([(m["to"], m["type"], m["status"]) for m in notes],
                         [(["coord"], "chat", {"coord": "queued"})])
        self.assertEqual(notes[0]["text"], "[room] FYI: alpha is at 312k context. A clear helps "
                                           "if it is between tasks; ask operator before any compact.")
        self.assertEqual(self.app.room_delivery_text(notes[0]), notes[0]["text"])
        self.assertEqual(len(self.ask(alpha=350000)), 1)          # still the same crossing
        self.assertEqual(len(self.ask(alpha=None)), 1)            # console off: unchanged
        self.assertEqual(len(self.ask(alpha=40000)), 1)           # dropped: nothing posted
        self.assertEqual(len(self.ask(alpha=201000)), 2)          # climbed back: told again

    def test_the_coordinator_itself_and_no_coordinator(self):
        self.assertEqual(self.ask(coord=401000)[0]["to"], ["coord"])
        self.assertEqual(self.ask(seat="", beta=250000)[-1]["to"], [])   # shown in the room only

    def test_a_removed_member_is_forgotten(self):
        self.ask(beta=400000)
        with self.app.room_edit() as r:
            self.app.room_set_member(r, "beta", False)
        self.assertEqual(len(self.ask(beta=400000)), 1)
        self.assertNotIn("beta", self.app.room_read()["ctx_asked"])

    def test_the_courier_reads_context_at_most_every_ctx_every(self):
        reads = []
        self.app._room_states = lambda names: {n: ("working" if n == "alpha" else "off", "")
                                               for n in names}
        self.app.console_send = lambda *a, **k: (True, "")
        self.app._room_ctx = lambda n: reads.append(n) or ("", "", 31.2, 312000)
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(reads, ["alpha"])                        # off consoles are not read
        self.assertEqual(self.app.room_read()["ctx_asked"], {"alpha": True})


class ShipSound(RoomBase):
    def test_each_new_ship_ask_pushes_once_with_the_ship_sound(self):
        pushes = []
        self.app.room_push = lambda title, body, sound=True, kind="": \
            pushes.append((title, sound, kind))
        self.app._room_states = lambda names: {n: ("working", "") for n in names}
        self.app.console_send = lambda *a, **k: (True, "")
        self.room()
        with self.app.room_edit() as r:
            r["push"] = True
        self.app.room_tick()                              # first look: nothing replayed
        self.post("alpha", "Ready.", ["coord"], "ship")   # not --to operator: still pushed
        self.post("beta", "a note to the coordinator", ["coord"])
        self.post("alpha", "for you", ["operator"])
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(pushes, [("🚀 alpha asks to ship", True, "ship"), ("💬 alpha", True, "")])
        with self.app.room_edit() as r:
            r["sound"] = False
        self.post("beta", "Ready too.", (), "ship")
        self.app.room_tick()
        self.assertEqual(pushes[-1], ("🚀 beta asks to ship", False, "ship"))
        n = len(pushes)
        with self.app.room_edit() as r:
            r["sound"] = True
            self.app.room_post(r, "beta", "Which port?", ["operator"], "question")
            self.app.room_ctx_asks(r, {"alpha": 312000})  # the coordinator's note: no push
        self.app.room_tick()
        self.assertEqual(pushes[n:], [("❓ beta asks", True, "question")])

    def test_ship_and_question_push_sounds_are_never_the_message_sound(self):
        import urllib.parse
        sent = []
        self.app.urllib.request.urlopen = lambda url, data, timeout: \
            sent.append(dict(urllib.parse.parse_qsl(data.decode()))) or open(os.devnull)
        self.app.BARK_CONF = os.path.join(self.tmp, "bark.env")
        for bark_sound, want in (("bell", "fanfare"), ("fanfare", "telegraph")):
            with open(self.app.BARK_CONF, "w") as fh:
                fh.write("BARK_URL=http://bark.invalid/k\nBARK_SOUND=%s\n" % bark_sound)
            self.app.room_push("t", "b", True, "ship")
            self.assertEqual(sent[-1]["sound"], want)
        self.app.room_push("t", "b", True)
        self.assertEqual(sent[-1]["sound"], "fanfare")    # the ordinary BARK_SOUND
        self.app.room_push("t", "b", False, "ship")
        self.assertEqual(sent[-1]["sound"], "silence")
        for bark_sound, want in (("fanfare", "anticipate"), ("anticipate", "bell")):
            with open(self.app.BARK_CONF, "w") as fh:
                fh.write("BARK_URL=http://bark.invalid/k\nBARK_SOUND=%s\n" % bark_sound)
            self.app.room_push("t", "b", True, "question")
            self.assertEqual(sent[-1]["sound"], want)
        self.assertFalse(set(self.app.ROOM_PUSH_SOUNDS["ship"]) & set(self.app.ROOM_PUSH_SOUNDS["question"]))

    def test_the_page_sounds_only_for_new_ship_posts_and_new_questions(self):
        page = self.app.render_room_page()
        self.assertIn("m.type === 'ship'", page)
        self.assertIn("s.sound && shipSeen !== null && top > shipSeen) alertSound(playChime)", page)
        self.assertIn("s.sound && askSeen !== null && ask > askSeen) alertSound(playAsk)", page)
        self.assertIn("id=soundnote hidden>🔇 Tap anywhere to turn on sounds", page)

    def test_question_badges_are_yellow_not_the_page_green(self):
        css = self.app.ROOM_CSS
        badge = css[css.index(".qbadge {"):]
        badge = badge[:badge.index("}")]
        self.assertIn("#f5c400", badge)
        self.assertNotIn("var(--accent)", badge)


class Courier(RoomBase):
    def setUp(self):
        super().setUp()
        self.states = {}
        self.typed = []
        self.app._room_states = lambda names: {n: self.states.get(n, ("off", "")) for n in names}
        self.app.console_send = lambda s, a, t, submit=False: (self.typed.append((s, t)) or (True, ""))
        self.app.ROOM_SETTLE = 0

    def test_types_only_into_idle_consoles_one_message_each(self):
        self.room()
        self.states = {"coord": ("waiting", "u1"), "alpha": ("working", ""),
                       "beta": ("waiting", "perm:1.0")}
        self.app.room_tick()
        self.assertEqual([s for s, _ in self.typed], ["mission-coord"])
        self.app.room_tick()                          # its second brief, next tick
        self.assertEqual([s for s, _ in self.typed], ["mission-coord", "mission-coord"])
        room = self.app.room_read()
        self.assertEqual([m["status"] for m in room["messages"] if "alpha" in m["status"]],
                         [{"alpha": "queued"}])

    def test_events_and_labels_use_titles(self):
        self.room(members=("alpha",), coord="")
        with open(os.path.join(self.missions, "alpha", "mission.json"), "w") as fh:
            json.dump({"title": "brave-otter"}, fh)
        self.states = {"alpha": ("working", "")}
        self.app.room_tick()
        self.states = {"alpha": ("waiting", "perm:2.0")}
        self.app.room_tick()
        events = [m["text"] for m in self.app.room_read()["messages"] if m["type"] == "event"]
        self.assertEqual(events, ["brave-otter is waiting for a permission answer"])
        msg, _ = self.post("alpha", "hello")
        self.assertTrue(self.app.room_delivery_text(msg).startswith("[room] brave-otter → you"))

    def test_busy_consoles_get_messages_but_dialogs_and_starting_consoles_do_not(self):
        self.room(coord="", members=("alpha", "beta", "coord"))
        self.app._chat_transcript_file = lambda n: None if n == "coord" else "/t/%s.jsonl" % n
        self.states = {"alpha": ("working", ""), "beta": ("waiting", "ask:u9"),
                       "coord": ("working", "")}
        self.app.room_tick()
        self.assertEqual([s for s, _ in self.typed], ["mission-alpha"])
        self.assertFalse(self.app.room_can_type("x", "waiting", "perm:1.0"))
        self.assertFalse(self.app.room_can_type("x", "waiting", "stale:u1"))
        self.assertFalse(self.app.room_can_type("x", "off", ""))

    def test_a_reply_acknowledges_what_was_delivered(self):
        self.room()
        msg, _ = self.post("operator", "status please")
        self.states = {"coord": ("waiting", "u1")}
        self.app.room_tick()
        self.app.room_tick()                          # coord had two briefs queued first
        self.app.room_tick()
        status = lambda: [m for m in self.app.room_read()["messages"] if m["id"] == msg["id"]][0]["status"]
        self.assertEqual(status(), {"coord": "sent"})
        self.post("coord", "on it")
        self.assertEqual(status(), {"coord": "acked"})

    def test_a_finished_turn_posts_no_event(self):
        self.room(members=("alpha",), coord="")
        self.states = {"alpha": ("working", "")}
        self.app.room_tick()
        self.states = {"alpha": ("waiting", "u2")}
        self.app.room_tick()
        self.assertEqual([m for m in self.app.room_read()["messages"] if m["type"] == "event"], [])

    def test_delivery_note_asks_for_no_ack(self):
        self.room()
        msg, _ = self.post("alpha", "done")
        self.assertNotIn("acknowledgement", self.app.room_delivery_text(msg))
        brief = [m for m in self.app.room_read()["messages"] if m["to"] == ["alpha"]][0]
        self.assertIn('never a bare "Ack"', brief["text"])

    def test_console_taking_a_message_in_marks_it_seen(self):
        self.room(members=("alpha",), coord="")
        tr = os.path.join(self.tmp, "alpha.jsonl")
        open(tr, "w").close()
        os.utime(tr, (1, 1))
        self.app._chat_transcript_file = lambda n: tr
        msg, _ = self.post("operator", "check this", ["alpha"])
        self.states = {"alpha": ("waiting", "u1")}
        status = lambda: [m for m in self.app.room_read()["messages"] if m["id"] == msg["id"]][0]["status"]
        for _ in range(3):                            # its brief first, then this
            self.app.room_tick()
        self.assertEqual(status(), {"alpha": "sent"})
        self.app.room_tick()                          # transcript untouched: still only sent
        self.assertEqual(status(), {"alpha": "sent"})
        future = __import__("time").time() + 5
        os.utime(tr, (future, future))                # the console wrote its next step
        self.app.room_tick()
        self.assertEqual(status(), {"alpha": "acked"})


class Script(RoomBase):
    def run_script(self, *args, mission="alpha"):
        env = dict(os.environ, MISSION_NAME=mission)
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "miss-room.py"), *args],
                           capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout

    def test_post_and_read(self):
        self.room()
        rc, out = self.run_script("post", "--type", "question", "which branch?")
        self.assertEqual(rc, 0, out)
        self.assertIn("to coord", out)
        rc, out = self.run_script("read", "1")
        self.assertIn("alpha -> coord (question): which branch?", out)

    def test_titles_are_shown_and_accepted(self):
        self.room()
        with open(os.path.join(self.missions, "beta", "mission.json"), "w") as fh:
            json.dump({"title": "shared-agent-room"}, fh)
        rc, out = self.run_script("post", "--to", "shared-agent-room", "hi")
        self.assertEqual(rc, 0, out)
        self.assertIn("to shared-agent-room", out)
        self.assertEqual(self.app.room_read()["messages"][-1]["to"], ["beta"])
        rc, out = self.run_script("read", "1", mission="beta")
        self.assertIn("alpha -> shared-agent-room (chat): hi", out)
        rc, out = self.run_script("members")
        self.assertIn("- shared-agent-room (folder beta)", out)

    def test_to_the_operators_display_name(self):
        self.room()
        with self.app.room_edit() as r:
            self.app.room_set_operator_name(r, "Jon")
        rc, out = self.run_script("post", "--to", "Jon", "done")
        self.assertEqual(rc, 0, out)
        self.assertIn("to Jon", out)
        self.assertEqual(self.app.room_read()["messages"][-1]["to"], ["operator"])

    def test_members_show_roles(self):
        self.room()
        with open(os.path.join(self.missions, "beta", "mission.json"), "w") as fh:
            json.dump({"agent_role": "builder"}, fh)
        rc, out = self.run_script("members")
        self.assertIn("- beta [builder]", out)
        rc, out = self.run_script("spawn", "x", "--as", "wizard", "--brief", "b", mission="coord")
        self.assertEqual(rc, 1)
        self.assertIn("Unknown role wizard", out)

    def test_refusals(self):
        self.room()
        self.assertEqual(self.run_script("post", "--to", "beta", "YES SHIP")[0], 1)
        rc, out = self.run_script("add", "beta")
        self.assertEqual(rc, 1)
        self.assertIn("coordinator", out)
        with open(os.path.join(self.tmp, "room", "room.json")) as fh:
            posts = [m for m in json.load(fh)["messages"] if m["from"] != "room"]
        self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()


class CompactAndClear(RoomBase):
    def setUp(self):
        super().setUp()
        self.states = {}
        self.typed = []
        self.app._room_states = lambda names: {n: self.states.get(n, ("off", "")) for n in names}
        self.app.console_send = lambda s, a, t, submit=False: (self.typed.append(t) or (True, ""))
        self.app._chat_transcript_file = lambda n: "/t/%s.jsonl" % n
        self.app.ROOM_SETTLE = 0
        self.room(members=("alpha",), coord="")
        self.states = {"alpha": ("waiting", "u1")}
        self.app.room_tick()                          # its join brief
        self.typed.clear()
        self.note = os.path.join(self.missions, "alpha", "HANDOFF.md")

    def act(self, fn):
        with self.app.room_edit() as r:
            if fn == "room_clear":                    # the context alert's Save & clear
                return self.app.room_clear(r, "alpha", save=True)
            return getattr(self.app, fn)(r, "alpha")

    def test_the_bubbles_clear_waits_only_for_idle_and_saves_nothing(self):
        self.states = {"alpha": ("working", "")}
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_clear(r, "alpha"), "")
        self.app.room_tick()
        self.assertEqual(self.typed, [])
        self.states = {"alpha": ("waiting", "u2")}
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(self.typed, ["/clear"])      # no save request, no follow-up

    def touch(self, age):
        open(self.note, "a").close()
        t = __import__("time").time() + age
        os.utime(self.note, (t, t))

    def test_compact_waits_for_idle_and_refuses_a_second(self):
        self.states = {"alpha": ("working", "")}
        self.assertEqual(self.act("room_compact"), "")
        self.assertIn("already has", self.act("room_clear"))
        self.app.room_tick()
        self.assertEqual(self.typed, [])              # busy: a slash command waits
        self.states = {"alpha": ("waiting", "perm:1")}
        self.app.room_tick()
        self.assertEqual(self.typed, [])
        self.states = {"alpha": ("waiting", "u2")}
        self.app.room_tick()
        self.assertEqual(self.typed, ["/compact"])
        self.assertEqual(self.act("room_compact"), "")  # done: a new one is allowed
        with self.app.room_edit() as r:
            self.assertEqual(self.app.room_compact(r, "beta"), "Not in the room.")

    def test_clear_needs_saved_post_newer_note_and_idle(self):
        self.touch(-60)                               # an old note
        self.assertEqual(self.act("room_clear"), "")
        self.app.room_tick()
        self.assertIn("--type saved", self.typed[0])
        self.assertIn(self.note, self.typed[0])
        self.app.room_tick()
        self.assertEqual(len(self.typed), 1)          # no saved post yet
        self.touch(5)
        self.app.room_tick()
        self.assertEqual(len(self.typed), 1)          # note written, still no saved post
        self.post("alpha", "State saved.", kind="saved")
        self.states = {"alpha": ("working", "")}
        self.app.room_tick()
        self.assertEqual(len(self.typed), 1)          # not idle
        self.states = {"alpha": ("waiting", "u3")}
        self.app.room_tick()
        self.assertEqual(self.typed[1], "/clear")
        self.app.room_tick()                          # the fresh session is told to read it
        self.assertIn("You were just cleared", self.typed[2])
        self.assertIn(self.note, self.typed[2])

    def test_unchanged_note_drops_the_clear_and_tells_the_operator(self):
        self.touch(-60)
        self.act("room_clear")
        self.app.room_tick()
        self.post("alpha", "State saved.", kind="saved")
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(len(self.typed), 1)          # only the save request
        room = self.app.room_read()
        clear = [m for m in room["messages"] if m["text"] == "/clear"][0]
        self.assertEqual(clear["status"], {"alpha": "dropped"})
        alert = room["messages"][-1]
        self.assertEqual(alert["to"], ["operator"])
        self.assertIn("has not changed", alert["text"])
        self.assertEqual(self.act("room_clear"), "")  # it can be asked again

    def test_no_saved_post_in_time_drops_the_clear(self):
        self.act("room_clear")
        self.app.room_tick()
        self.app.ROOM_SAVE_WAIT = -1                  # sent_at is rounded, maybe up
        self.app.room_tick()
        self.assertEqual(len(self.typed), 1)
        self.assertIn("did not post", self.app.room_read()["messages"][-1]["text"])

    def test_page_wires_both_icons_by_double_click(self):
        page = self.app.render_room_page()
        self.assertIn("resetBtn(m, 'compact'), resetBtn(m, 'clear')", page)
        self.assertIn("b.ondblclick", page)
        self.assertIn('"/room/clear', page)


class ColdStart(RoomBase):
    BOOTING = "\n Claude Code v2.1\n ▐▛███▜▌   Opus · /tmp/zz\n\n  ⎿ SessionStart:startup hook running…\n"
    READY = ("\n Claude Code v2.1\n\n" + "─" * 80 + "\n❯ \n" + "─" * 80 +
             "\n  ⏵⏵ auto mode on (shift+tab to cycle)\n")

    def test_the_input_box_is_what_counts_as_ready(self):
        ready = self.app._pane_text_prompt_ready
        self.assertFalse(ready(self.BOOTING))
        self.assertFalse(ready(""))
        self.assertTrue(ready(self.READY))
        self.assertTrue(ready(self.READY.replace("❯ \n", "❯ [room] hello\n  more text\n")))

    def test_a_fresh_console_gets_nothing_until_its_prompt_shows(self):
        screen = {"t": self.BOOTING}
        self.app._session_pane = lambda s: "%1"
        self.app._pane_in_copy_mode = lambda p: False
        self.app._run_tmux = lambda *a, **k: (0, screen["t"])
        self.assertFalse(self.app.room_can_type("alpha", "waiting", "start:s1"))
        screen["t"] = self.READY
        self.assertTrue(self.app.room_can_type("alpha", "waiting", "start:s1"))
        screen["t"] = self.BOOTING                    # other idle turns never read the screen
        self.assertTrue(self.app.room_can_type("alpha", "waiting", "u1"))


class TesterResultAndColdCache(RoomBase):
    def setUp(self):
        super().setUp()
        self.states, self.typed = {}, []
        self.app._room_states = lambda names: {n: self.states.get(n, ("off", "")) for n in names}
        self.app.console_send = lambda s, a, t="", submit=False: (   # alpha's console only
            s == "mission-alpha" and self.typed.append(t if a == "text" else "<" + a + ">")
            or (True, ""))
        self.app._chat_transcript_file = lambda n: "/t/%s.jsonl" % n
        self.app.ROOM_SETTLE = 0
        self.room()
        for n in ("coord", "alpha", "beta"):
            self.states[n] = ("waiting", "u1")
        for _ in range(3):
            self.app.room_tick()                      # the join briefs
        self.typed.clear()

    def role(self, n, role):
        with open(os.path.join(self.missions, n, "mission.json"), "w") as fh:
            json.dump({"agent_role": role}, fh)

    def test_a_testers_result_clears_it_once_idle_and_a_builders_does_not(self):
        self.role("alpha", "tester")
        self.role("beta", "builder")
        self.states["alpha"] = ("working", "")
        self.post("alpha", "PASS, all checks.", ["beta"], "result")
        self.post("beta", "Built it.", ["alpha"], "result")
        clears = [m for m in self.app.room_read()["messages"] if m["text"] == "/clear"]
        self.assertEqual([m["to"] for m in clears], [["alpha"]])
        self.app.room_tick()
        self.assertNotIn("/clear", self.typed)          # still working
        self.states["alpha"] = ("waiting", "u2")
        self.app.room_tick()
        self.assertIn("/clear", self.typed)

    def test_a_testers_result_waits_while_another_test_request_is_open(self):
        self.role("alpha", "tester")
        clears = lambda: [m for m in self.app.room_read()["messages"] if m["text"] == "/clear"]
        self.post("beta", "Test ship 1.", ["alpha"], "handoff")
        self.post("coord", "Test ship 2.", ["alpha"], "handoff")
        self.post("alpha", "PASS ship 1.", ["beta"], "result")
        self.assertEqual(clears(), [])                  # coord's request still waits
        self.post("alpha", "Got it, testing.", ["coord"])
        self.assertEqual(clears(), [])
        self.post("alpha", "PASS ship 2.", ["coord"], "result")
        self.assertEqual([m["to"] for m in clears()], [["alpha"]])

    def test_requests_from_before_the_testers_last_clear_do_not_count(self):
        self.role("alpha", "tester")
        self.post("beta", "An old request, never answered.", ["alpha"], "handoff")
        with self.app.room_edit() as r:
            self.app.room_clear(r, "alpha")
            r["messages"][-1]["status"]["alpha"] = "sent"   # that /clear went through
        self.post("coord", "Test ship 2.", ["alpha"], "handoff")
        self.post("alpha", "PASS ship 2.", ["coord"], "result")
        clears = [m for m in self.app.room_read()["messages"] if m["text"] == "/clear"]
        self.assertEqual(len(clears), 2)

    def blocked(self, n, prompt):
        rec = {"session_id": "s-" + n, "cwd": os.path.join(self.missions, n)}
        with open(os.path.join(self.missions, n, ".console-session"), "w") as fh:
            json.dump(rec, fh)
        d = os.path.join(self.missions, n, "tmp", "cold-cache")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "s-%s.blocked" % n), "w") as fh:
            json.dump({"after": "msg_1", "prompt": prompt}, fh)

    def held_message(self):
        msg, _ = self.post("operator", "Please check the build.", ["alpha"])
        self.app.room_tick()
        self.blocked("alpha", "\n<pasted_content>\n" + self.app.room_delivery_text(msg))
        second, _ = self.post("operator", "And a second thing.", ["alpha"])
        self.app.room_tick()
        self.assertEqual(self.status(second["id"]), {"alpha": "queued"})   # never onto the box
        return msg

    def status(self, mid):
        return [m for m in self.app.room_read()["messages"] if m["id"] == mid][0]["status"]

    def test_a_held_delivery_is_marked_told_to_the_coordinator_and_freezes_the_console(self):
        msg = self.held_message()
        self.assertEqual(self.status(msg["id"]), {"alpha": "cold"})
        note = self.app.room_read()["messages"][-1]
        self.assertEqual(note["to"], ["coord"])
        self.assertIn("clear alpha", note["text"])
        self.assertIn("resume alpha", note["text"])
        self.typed.clear()
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(self.typed, [])                # the second message waits
        n = len(self.app.room_read()["messages"])
        self.app.room_tick()
        self.assertEqual(len(self.app.room_read()["messages"]), n)   # told once

    def test_an_old_block_is_not_this_delivery(self):
        self.blocked("alpha", "[room] operator → you (chat):\nPlease check the build.")
        os.utime(os.path.join(self.missions, "alpha", "tmp", "cold-cache", "s-alpha.blocked"), (1, 1))
        msg, _ = self.post("operator", "Please check the build.", ["alpha"])
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(self.status(msg["id"]), {"alpha": "sent"})

    def go(self, how):
        with self.app.room_edit() as r:
            return self.app.room_cold_go(r, "alpha", how)

    def test_clear_wipes_the_box_clears_then_sends_the_held_messages_again(self):
        msg = self.held_message()
        self.typed.clear()
        self.assertEqual(self.go("clear"), "")
        self.assertIn("Nothing of", self.go("resume"))
        self.assertEqual(self.status(msg["id"]), {"alpha": "resent"})
        self.app.room_tick()
        self.assertEqual(self.typed, ["<clearline>", "/clear"])
        with open(os.path.join(self.missions, "alpha", ".console-session"), "w") as fh:
            json.dump({"session_id": "s-fresh", "cwd": os.path.join(self.missions, "alpha")}, fh)
        for _ in range(3):                            # the fresh session: no block of its own
            self.app.room_tick()
        self.assertIn("Please check the build.", self.typed[2])
        self.assertIn("And a second thing.", self.typed[3])

    def test_resume_wipes_the_box_and_types_the_held_message_again(self):
        msg = self.held_message()
        self.typed.clear()
        self.assertEqual(self.go("resume"), "")
        self.assertEqual(self.status(msg["id"]), {"alpha": "queued"})
        self.app.room_tick()
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(self.typed[0], "<clearline>")
        self.assertIn("Please check the build.", self.typed[1])
        self.assertIn("And a second thing.", self.typed[2])
        self.assertEqual(self.status(msg["id"]), {"alpha": "sent"})   # not held again

    def test_only_the_coordinator_runs_clear_and_resume(self):
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "miss-room.py"),
                            "resume", "alpha"], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, MISSION_NAME="beta"))
        self.assertIn("Only the room's coordinator", r.stdout)
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "miss-room.py"),
                            "resume", "alpha"], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, MISSION_NAME="coord"))
        self.assertIn("Nothing of alpha's is held", r.stdout)

    def test_clear_with_nothing_held_clears_the_agent_anyway(self):
        run = lambda *a: subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "miss-room.py"), "clear", *a],
            capture_output=True, text=True, timeout=30, env=dict(os.environ, MISSION_NAME="coord"))
        self.assertIn("will save its state", run("alpha", "--save").stdout)
        self.assertIn("already has a /compact or /clear waiting", run("alpha").stdout)
        self.assertIn("will get a bare /clear once idle.", run("beta").stdout)
        msgs = self.app.room_read()["messages"]
        self.assertEqual([(m["type"], m["to"]) for m in msgs if m["type"] in ("save", "command")],
                         [("save", ["alpha"]), ("command", ["alpha"]), ("command", ["beta"])])

    def test_a_turn_in_the_console_after_the_hold_releases_it(self):
        # Jon pressed Enter on the held text in Connie's console (the guard lets the
        # prompt after a block through): no clear/resume ever came, yet chats must flow.
        msg = self.held_message()
        tr = os.path.join(self.missions, "alpha", "t.jsonl")
        self.app._chat_transcript_file = lambda n: tr
        blocked = os.path.join(self.missions, "alpha", "tmp", "cold-cache", "s-alpha.blocked")
        at = os.path.getmtime(blocked)
        stamp = lambda e: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(e))
        with open(tr, "w") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": stamp(at - 60)}) + "\n")
            fh.write(json.dumps({"type": "system", "timestamp": stamp(at)}) + "\n")
        self.typed.clear()
        self.app.room_tick()
        self.assertEqual(self.typed, [])                # the block alone is not a turn
        with open(tr, "a") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": stamp(at + 5)}) + "\n")
        os.utime(tr, (at + 5, at + 5))
        self.app.room_tick()
        self.assertEqual(self.status(msg["id"]), {"alpha": "acked"})
        self.assertIn("And a second thing.", self.typed[0])

    def test_a_held_delivery_back_in_its_box_gets_no_extra_enter(self):
        msg = self.held_message()
        self.app._pane_input = lambda n: "[Pasted text #2 +%d lines]" % (
            self.app.room_delivery_text(msg).count("\n"))
        self.typed.clear()
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(self.typed, [])
        self.assertEqual(self.status(msg["id"]), {"alpha": "cold"})


class StuckInTheBox(RoomBase):
    # A real screen: the submitted message above, a queued one's hint in the box.
    SCREEN = ("❯ [room] Connie → you (chat):\n  Say only the word PONG.\n"
              "  ⎿  Queued · ctrl+x ctrl+s to send now\n· Perambulating… (26s)\n" + "─" * 80 +
              "\n❯ Press up to edit queued messages\n" + "─" * 80 +
              "\n  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt\n")

    def setUp(self):
        super().setUp()
        self.typed, self.box = [], ""
        self.app._room_states = lambda names: {n: ("waiting", "u1") for n in names}
        self.app.console_send = lambda s, a, t="", submit=False: (   # alpha's console only
            s == "mission-alpha" and self.typed.append(t if a == "text" else "<" + a + ">")
            or (True, ""))
        self.app._chat_transcript_file = lambda n: "/t/%s.jsonl" % n
        self.app._pane_input = lambda n: self.box if n == "alpha" else None
        self.app.ROOM_SETTLE = 0
        self.room()
        for _ in range(3):
            self.app.room_tick()                      # the join briefs

    status = TesterResultAndColdCache.status

    def test_the_input_box_text_is_read_off_the_screen(self):
        read = self.app._pane_text_input
        self.assertEqual(read(self.SCREEN), "Press up to edit queued messages")
        self.assertEqual(read(self.SCREEN.replace("Press up to edit queued messages",
                                                  "[Pasted text #1 +3 lines]")),
                         "[Pasted text #1 +3 lines]")
        self.assertEqual(read(ColdStart.READY), "")
        self.assertIsNone(read(ColdStart.BOOTING))

    def test_a_delivery_left_in_its_box_gets_enter_once_then_is_stuck_and_told(self):
        msg, _ = self.post("operator", "Please check the build.", ["alpha"])
        self.app.room_tick()
        self.box = "[Pasted text #3 +%d lines]" % self.app.room_delivery_text(msg).count("\n")
        second, _ = self.post("operator", "And a second thing.", ["alpha"])
        self.typed.clear()
        self.app.room_tick()
        self.assertEqual(self.typed, ["<enter>"])       # once more, nothing new typed
        self.app.room_tick()
        self.assertEqual(self.typed, ["<enter>"])
        self.assertEqual(self.status(msg["id"]), {"alpha": "stuck"})
        self.assertEqual(self.status(second["id"]), {"alpha": "queued"})
        note = self.app.room_read()["messages"][-1]
        self.assertEqual(note["to"], ["coord"])
        self.assertIn("#%d is stuck in alpha's input box" % msg["id"], note["text"])
        n = len(self.app.room_read()["messages"])
        self.app.room_tick()
        self.assertEqual(len(self.app.room_read()["messages"]), n)   # told once
        self.box = ""                                   # someone sent or cleared it
        self.app.room_tick()
        self.assertIn("And a second thing.", self.typed[-1])

    def test_a_delivery_that_left_its_box_is_not_pressed_again(self):
        msg, _ = self.post("operator", "Please check the build.", ["alpha"])
        self.app.room_tick()
        self.box = "Press up to edit queued messages"
        self.typed.clear()
        self.app.room_tick()
        self.app.room_tick()
        self.assertEqual(self.typed, [])
        self.assertEqual(self.status(msg["id"]), {"alpha": "sent"})

    def test_a_busy_transcript_does_not_ack_what_is_still_in_the_box(self):
        tr = os.path.join(self.tmp, "alpha.jsonl")
        open(tr, "w").close()
        self.app._chat_transcript_file = lambda n: tr
        msg, _ = self.post("operator", "Please check the build.", ["alpha"])
        self.app.room_tick()
        self.box = "[Pasted text #1 +%d lines]" % self.app.room_delivery_text(msg).count("\n")
        future = __import__("time").time() + 5
        os.utime(tr, (future, future))                # a busy turn's next tool step
        self.app.room_tick()
        self.assertEqual(self.status(msg["id"]), {"alpha": "sent"})
        self.box = ""
        self.app.room_tick()
        self.assertEqual(self.status(msg["id"]), {"alpha": "acked"})
