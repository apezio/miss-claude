#!/usr/bin/env python3
"""miss-director — the Director's four commands (EXPERIMENT; see docs/DIRECTOR.md).

Director mode is an optional layer on top of ordinary Miss Claude. It adds no route, no
mode, no role and no daemon: a DIRECTOR is a normal ops mission carrying a `.director`
marker, a CHILD is a normal mission carrying a SPEC.md, and everything below drives the
dashboard's EXISTING create/spawn routes and tmux helpers by importing app.py (the same
trick scripts/summarize-missions.py uses).

  init  <mission>   turn Director mode on: create the mission if it does not exist,
                    write the .director marker, start its console
  spawn <name> ...  create a normal child mission, give it SPEC/STATUS/RESULT, start its
                    console headlessly and type the kickoff prompt
  board             the RUNNING / READY / NEEDS ME / BLOCKED board, read off the
                    children's STATUS.md files (never their transcripts)
  ship  <child>     relay the operator's YES SHIP into that ONE child's console

Nothing here edits code or decides anything: `spawn` writes markdown and starts a console,
`board` only reads, and `ship` types a phrase the operator has just said, into a console
the operator named. `ship` is a RELAY, not an approval: it refuses a child whose STATUS.md
is not READY, takes exactly one child (no wildcard, no "ship everything"), and records the
approval in the Director mission's LOG.md. What the child then verifies before it acts on
the phrase is entirely unchanged — this moves who types it, not what is checked.

Remove the feature by deleting this file, scripts/miss-director-context.py and the few
wiring lines listed in docs/DIRECTOR.md; child missions keep working as ordinary missions.
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import app  # the dashboard module — mission paths, tmux helpers, stdlib-only

MARKER = ".director"
START_TIMEOUT = 30    # s to wait for the child's tmux session to exist
READY_TIMEOUT = 90    # s to wait for the agent itself before typing the kickoff prompt
READY_SETTLE = 2.0    # s for the TUI to finish its first draw before we paste into it
# "The agent is up" is read from the marker the console's own SessionStart hook writes
# (scripts/mission-console-session.py -> <mission dir>/.console-session) — the same signal
# the dashboard's context badge trusts. Scraping the pane for TUI glyphs was tried first
# and is wrong: the input box is drawn differently across Claude versions. On timeout the
# kickoff is NOT typed and spawn says so — a half-typed prompt is worse than none.
READY_MARKER = ".console-session"

STATUSES = ("RUNNING", "READY", "NEEDS ME", "BLOCKED")

# The operator's phrase, relayed verbatim. It is a constant here for the same
# reason it is one in miss-ship.py: a paraphrase is not an approval.
APPROVAL = "YES SHIP"

STATUS_TEMPLATE = """\
RUNNING
Just launched; reading SPEC.md.

_First line is the status: RUNNING / READY / NEEDS ME / BLOCKED. The Director reads this
file, so keep it to the status plus at most three short lines._
"""

RESULT_TEMPLATE = """\
_Not finished yet. When every acceptance criterion in SPEC.md passes, replace this with
what was done, what was verified, and anything left for the operator._
"""

KICKOFF = (
    "Read SPEC.md in this mission's folder ({data_dir}) and work only toward its "
    "acceptance criteria. Log anything unrelated in LOG.md instead of fixing it. Keep "
    "STATUS.md current (first line RUNNING / READY / NEEDS ME / BLOCKED). When every "
    "criterion passes, write RESULT.md, set STATUS.md to READY and stop."
)


# ---------------------------------------------------------------------------
# talking to the dashboard (its own routes, over its own loopback URL)
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the 302 instead of following it — the 302 IS the success signal.

    /create and /spawn answer a REFUSAL (name taken, bad path, git failure) by rendering
    the index page with a notice, status 200. Following redirects would make both look
    alike, so a Director must check for the redirect itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, fields, ca=""):
    """POST a form to the dashboard. -> (status, location, body).

    status 0 = the dashboard could not be reached at all (stopped, wrong scheme, bad
    CA). Transport failures are values here, not tracebacks: the Director is a model
    reading stdout, and "Connection reset by peer" up a stack trace tells it nothing."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    ctx = None
    if url.startswith("https:"):
        # The dashboard's cert comes from the local CA in ~/.miss-claude/tls (see
        # scripts/make-certs.sh); point at it exactly like SELF_CURL's --cacert does.
        ctx = ssl.create_default_context(cafile=ca or None)
    opener = urllib.request.build_opener(_NoRedirect,
                                         urllib.request.HTTPSHandler(context=ctx))
    try:
        r = opener.open(req, timeout=120)
    except urllib.error.HTTPError as e:      # 4xx/5xx come back as an exception
        return e.code, e.headers.get("Location", ""), e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, "", str(e)
    return r.status, r.headers.get("Location", ""), r.read().decode("utf-8", "replace")


def _notice(body):
    """The one-line refusal the dashboard rendered into its index page, if we can find it.

    render_index emits the attribute UNQUOTED (`<div class=notice>`), so the pattern
    must not assume quotes. A refusal raised through _error() (HTTP 400) carries no
    notice div at all — the caller prints the status code alongside this."""
    m = re.search(r"""class=["']?notice["']?[^>]*>(.*?)<""", body, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else "(no reason given)"


def dashboard_endpoint():
    """(base_url, ca_path) for the running dashboard. Never guessed.

    Precedence: the console's exported MISSION_SELF_URL/MISSION_TLS_CA first (claude-console.service
    sets both), then the .dashboard-url file app.py rewrites on every start, and only
    then this process's own app.SELF_URL. That last one is a poor guess from inside a
    console: SELF_URL is computed from MISSION_TLS_CERT, which the CONSOLE service does
    not set, so an https dashboard would be dialed on http and merely reset the
    connection."""
    base = os.environ.get("MISSION_SELF_URL", "").strip().rstrip("/")
    ca = os.environ.get("MISSION_TLS_CA", "").strip()
    if not base:
        try:
            with open(os.path.join(app.MISSIONS_DIR, ".dashboard-url"),
                      encoding="utf-8") as fh:
                d = json.load(fh)
            base = str(d.get("base", "")).strip().rstrip("/")
            ca = ca or str(d.get("ca", "")).strip()
        except (OSError, ValueError, AttributeError):
            pass
    if not base:
        base, ca = app.SELF_URL, (ca or app.TLS_CA)
    return base, ca


def _base_url(arg):
    """(base_url_with_token, ca). An explicit --url/MISS_DIRECTOR_URL wins over both."""
    base, ca = dashboard_endpoint()
    url = (arg or os.environ.get("MISS_DIRECTOR_URL", "") or base).rstrip("/")
    return url + ("?token=" + urllib.parse.quote(app.TOKEN) if app.TOKEN else ""), ca


def _url(base, path):
    """Join a route onto the base URL, keeping any ?token= the base carries."""
    u, _, q = base.partition("?")
    return u.rstrip("/") + path + (("?" + q) if q else "")


# ---------------------------------------------------------------------------
# reading children
# ---------------------------------------------------------------------------
def is_director(name):
    return os.path.exists(app.mission_path(name, MARKER))


def is_child(name):
    return (os.path.isfile(app.mission_path(name, "SPEC.md"))
            and not is_director(name))


def child_status(name):
    """(status, detail) from a child's STATUS.md — its first line is the status.

    Only the FIRST paragraph is read: a child (or the initial template) may keep notes
    or a reminder below a blank line, and the board is one line per child."""
    lines = []
    for raw in app.read_text(app.mission_path(name, "STATUS.md")).splitlines():
        s = raw.strip()
        if not s:
            if lines:
                break
            continue
        if s.startswith("_"):      # italic guidance, not status
            continue
        lines.append(s)
    if not lines:
        return "RUNNING", "(no status written yet)"
    # A prefix match, not equality: a child writing "READY - all criteria pass" on one
    # line is common, and misreading that as RUNNING is the one error that matters —
    # a finished child would never surface for the operator's YES SHIP.
    first = lines[0].strip("*# ")
    status = next((s for s in STATUSES if first.upper().startswith(s)), "")
    detail = first[len(status):].strip(" -:—") if status else ""
    rest = " ".join(([detail] if detail else []) + lines[1:]) if status \
        else " ".join(lines)   # no recognizable token: show the text, call it RUNNING
    return (status or "RUNNING"), (rest[:100] or "-")


def director_mission(explicit=""):
    """(name, error) for the Director mission whose LOG.md an approval is written to.

    Never guessed loosely: the console's own MISSION_NAME first (a `ship` is run FROM a
    Director console), then the single marked mission if there is exactly one. Two
    Directors, or none, is an error rather than a coin flip — an approval that lands in
    the wrong mission's log is worse than one that refuses to be relayed at all."""
    if explicit:
        if not app.safe_name(explicit) or not is_director(explicit):
            return "", 'No Director mission called "%s" (no %s marker).' % (explicit,
                                                                           MARKER)
        return explicit, ""
    env = os.environ.get("MISSION_NAME", "").strip()
    if env and app.safe_name(env) and is_director(env):
        return env, ""
    marked = [n for n, _m in app.list_missions() if is_director(n)]
    if len(marked) == 1:
        return marked[0], ""
    if not marked:
        return "", ("No Director mission found (no mission carries a %s marker), so the "
                    "approval could not be logged. Run this from the Director console, "
                    "or pass --director <name>." % MARKER)
    return "", ("More than one Director mission (%s) — pass --director <name> so the "
                "approval is logged in the right one." % ", ".join(sorted(marked)))


def children():
    """[(name, status, detail, live)] for every Director child, newest activity first."""
    out = []
    for name, _mtime in app.list_missions():
        if not is_child(name):
            continue
        status, detail = child_status(name)
        out.append((name, status, detail, app.session_running(name)))
    return out


# ---------------------------------------------------------------------------
# creating a mission through the dashboard's own routes
# ---------------------------------------------------------------------------
def _create_mission(name, mode="ops", repo="", base_branch="", url=""):
    """Create a normal mission via /create (ops) or /spawn (dev). -> (name, error).

    The returned name is the one the ROUTE settled on: both slugify further than
    safe_name does (they collapse "--" and strip edge dashes), so "kid-" becomes the
    mission "kid", and writing files at the requested spelling would throw and leave a
    real mission half-built."""
    base, ca = _base_url(url)
    if mode == "ops":
        # /create's plain ops mission: scaffold + a mission.json whose console runs in
        # the mission's own folder. Exactly what the dashboard's own button makes.
        status, loc, body = _post(_url(base, "/create"), {"name": name}, ca)
    else:
        status, loc, body = _post(_url(base, "/spawn"), {
            "mode": "dev", "kind": "local-repo", "name": name,
            "path": repo, "base": base_branch or "", "role": "feature",
        }, ca)
    if status == 0:
        return name, ("Could not reach the dashboard at %s: %s"
                      % (base.partition("?")[0], body))
    if status not in (301, 302, 303) or not loc:
        return name, ("Dashboard refused to create %s (HTTP %d): %s"
                      % (name, status, _notice(body)))
    created = loc.rstrip("/").split("/")[-2] if "/m/" in loc else name
    created = urllib.parse.unquote(created)
    if created != name:
        print("note: the dashboard named it %s (not %s)" % (created, name))
        name = created
    if not app.safe_name(name) or not os.path.isdir(app.mission_path(name)):
        return name, ("Dashboard redirected to %s but no such mission exists — "
                      "nothing written." % loc)
    return name, ""


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_init(args):
    """Turn Director mode on: create the mission if needed, mark it, start its console.

    One command, because the marker only takes effect at SessionStart — a mission you
    have to create, mark and then restart by hand is three steps to say "yes"."""
    name = args.mission
    if not app.safe_name(name):
        print("Invalid mission name: %s" % name)
        return 1
    existed = os.path.isdir(app.mission_path(name))
    if not existed:
        name, err = _create_mission(name, url=args.url)
        if err:
            print(err)
            return 1
        print("created mission %s" % name)
    app.write_text_atomic(app.mission_path(name, MARKER),
                          "Director mode (scripts/miss-director.py). Delete to turn off.\n")
    if app.session_running(name):
        # Its Claude started before the marker existed, and the block is injected at
        # SessionStart — so this one really does need the restart.
        print("%s is now the Director, but its console is already running: restart it "
              "(the ✕ on its index card) to load the Director context." % name)
        return 0
    if args.no_start:
        print("%s is now the Director. Open it to start its console." % name)
        return 0
    err = _start_console(name)
    if err:
        print("%s is now the Director, but its console did not start (%s) — open its "
              "page instead." % (name, err))
        return 0
    print("%s is now the Director and its console is running. Open it and tell it what "
          "you want." % name)
    return 0


def _start_console(name):
    """Start the child's console headlessly. -> "" on success, else an error line.

    Uses the SAME launcher the browser uses (console-launch.sh), with MISS_NO_ATTACH=1 so
    it creates the detached tmux session and returns instead of exec'ing `tmux attach`."""
    env = dict(os.environ, MISS_NO_ATTACH="1")
    env.setdefault("TMUX_TMPDIR", os.path.expanduser("~/.tmux-console"))
    try:
        with open(os.devnull) as devnull:
            r = subprocess.run([os.path.join(REPO, "console-launch.sh"), name],
                               stdin=devnull, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               timeout=START_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return "could not run console-launch.sh: %s" % e
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if app.session_running(name):
            return ""
        time.sleep(0.5)
    return "console did not start: %s" % (r.stdout or "").strip()[:300]


def _marker_mtime(name):
    try:
        return os.path.getmtime(app.mission_path(name, READY_MARKER))
    except OSError:
        return 0.0


def _wait_ready(name, since):
    """True once the child's agent has started this session (bounded); False on timeout.

    `since` is the marker's mtime from before the launch, so a console being RE-started in
    a mission that already has a marker waits for the new session, not the old file."""
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if _marker_mtime(name) > since:
            time.sleep(READY_SETTLE)
            return True
        time.sleep(1.0)
    return False


def cmd_spawn(args):
    name = args.name
    if not app.safe_name(name):
        print("Invalid mission name: %s" % name)
        return 1
    if os.path.exists(app.mission_path(name)):
        print('Mission "%s" already exists.' % name)
        return 1
    if args.mode == "dev" and not args.repo:
        print("A dev child needs --repo <path to the git repo>.")
        return 1
    spec = sys.stdin.read() if args.spec_file == "-" else app.read_text(args.spec_file)
    if not spec.strip():
        print("Empty spec (%s). A child without acceptance criteria has no stop rule."
              % args.spec_file)
        return 1

    name, err = _create_mission(name, args.mode, args.repo, args.base, args.url)
    if err:
        print(err)
        return 1

    app.write_text_atomic(app.mission_path(name, "SPEC.md"), spec.rstrip("\n") + "\n")
    app.write_text_atomic(app.mission_path(name, "STATUS.md"), STATUS_TEMPLATE)
    app.write_text_atomic(app.mission_path(name, "RESULT.md"), RESULT_TEMPLATE)
    print("created %s (%s) with SPEC.md / STATUS.md / RESULT.md" % (name, args.mode))
    if args.no_start:
        return 0

    # The mission now EXISTS. Everything below can fail without undoing that, so these
    # paths warn and still exit 0 — a Director that reads only the exit status must not
    # conclude "spawn failed" and try again into "already exists".
    marker_was = _marker_mtime(name)
    err = _start_console(name)
    if err:
        print("  console: %s — the mission exists; open its page to start it by hand "
              "(do not re-spawn)." % err)
        return 0
    if not _wait_ready(name, marker_was):
        print("  console started but its agent is not ready yet; kickoff NOT typed. "
              "Open %s and paste the prompt yourself (do not re-spawn)." % name)
        return 0
    ok, msg = app.console_send(app.SESSION_PREFIX + name, "text",
                               KICKOFF.format(data_dir=app.mission_path(name)),
                               submit=True)
    print("  console started; kickoff %s" % ("typed" if ok else "NOT typed (%s)" % msg))
    return 0


def cmd_board(args):
    rows = children()
    if not rows:
        print("No Director children yet.")
        return 0
    buckets = {s: [] for s in STATUSES}
    for name, status, detail, live in rows:
        note = detail
        if status == "RUNNING" and not live:
            note = (note + " [console not running]").strip()
        if status == "READY":
            note = (note + " — needs the operator's YES SHIP").strip() \
                if "YES SHIP" not in note else note
        buckets[status].append("- %s — %s" % (name, note))
    out = []
    for status in ("RUNNING", "READY", "NEEDS ME", "BLOCKED"):
        if buckets[status]:
            out.append(status + "\n" + "\n".join(buckets[status]))
    print("\n\n".join(out))
    return 0


def cmd_ship(args):
    """Relay the operator's YES SHIP into ONE named child's console.

    This is the one place the Director acts on a child rather than reading it, so every
    gate is here and every gate is narrow:

    * the operator names the child — there is no wildcard and no "all READY", because
      the approval the operator gave was for a mission they were talking about;
    * the child must be a real child (a SPEC.md, not the Director itself) and its own
      STATUS.md must say READY — the Director must never hand an approval to a mission
      that is still working;
    * its console must be live, since the phrase is typed into that console;
    * and the approval is written to the Director's LOG.md, so there is a record of who
      approved what, and when.

    What it does NOT do is decide. `ship` is only ever correct immediately after the
    operator has said YES SHIP for this child; the child's own pre-ship verification
    (miss-ship.py's checks, the guard hook, its branch scope) is untouched."""
    name = args.child
    if not app.safe_name(name):
        print("Invalid mission name: %s" % name)
        return 1
    if not os.path.isdir(app.mission_path(name)):
        print('No mission called "%s". Nothing sent.' % name)
        return 1
    if is_director(name):
        print('"%s" is the Director, not a child. Nothing sent.' % name)
        return 1
    if not is_child(name):
        print('"%s" is not a Director child (no SPEC.md). Nothing sent.' % name)
        return 1

    status, detail = child_status(name)
    if status != "READY":
        print('Refused: "%s" is %s, not READY%s. A child ships only after it has '
              "verified its own acceptance criteria and said READY. Nothing sent."
              % (name, status, (" (%s)" % detail) if detail and detail != "-" else ""))
        return 1

    director, err = director_mission(args.director)
    if err:
        print(err + " Nothing sent.")
        return 1

    ok, msg = app.console_send(app.SESSION_PREFIX + name, "text", APPROVAL, submit=True)
    app.append_log_entry(director,
                         'operator said %s for child "%s" — relayed into its console (%s)'
                         % (APPROVAL, name, "sent" if ok else "NOT sent: %s" % msg))
    if not ok:
        print("Could not send %s to %s: %s (logged in %s/LOG.md)."
              % (APPROVAL, name, msg, director))
        return 1
    print("Relayed %s into %s's console. Logged in %s/LOG.md. The child now runs its "
          "own ship flow — watch it with `board`; do not send it anything else."
          % (APPROVAL, name, director))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="miss-director", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="turn Director mode on for a mission (creating it "
                                     "if it does not exist yet)")
    pi.add_argument("mission")
    pi.add_argument("--no-start", action="store_true",
                    help="do not start its console; open the mission page instead")
    pi.add_argument("--url", default="", help="dashboard base URL (default: this app's)")
    pi.set_defaults(fn=cmd_init)

    ps = sub.add_parser("spawn", help="create a child mission and start it")
    ps.add_argument("name")
    ps.add_argument("--mode", choices=("ops", "dev"), default="ops")
    ps.add_argument("--repo", default="", help="git repo for a dev child (required)")
    ps.add_argument("--base", default="", help="base branch (blank = auto-detect)")
    ps.add_argument("--spec-file", required=True,
                    help="file holding the spec + acceptance checklist ('-' = stdin)")
    ps.add_argument("--no-start", action="store_true",
                    help="create the mission but do not start its console")
    ps.add_argument("--url", default="", help="dashboard base URL (default: this app's)")
    ps.set_defaults(fn=cmd_spawn)

    pb = sub.add_parser("board", help="the children's status board")
    pb.set_defaults(fn=cmd_board)

    psh = sub.add_parser(
        "ship", help="relay the operator's YES SHIP into ONE named READY child's console",
        description="Type the operator's YES SHIP into that child's console. Use it ONLY "
                    "when the operator has just told you to ship that named child: it is "
                    "a relay, not a decision. It refuses a child that is not READY, takes "
                    "exactly one child (no wildcard, no 'ship everything'), logs the "
                    "approval in the Director mission's LOG.md, and changes nothing about "
                    "what the child verifies before it acts on the phrase.")
    psh.add_argument("child", help="the ONE child the operator named")
    psh.add_argument("--director", default="",
                     help="Director mission whose LOG.md records it "
                          "(default: this console's mission)")
    psh.set_defaults(fn=cmd_ship)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
