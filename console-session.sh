#!/usr/bin/env bash
# console-session.sh — runs INSIDE the tmux pane as the mission session's command
# (started by console-launch.sh via `tmux new-session ... console-session.sh`).
#
# The pane's cwd is the mission directory (set with tmux's -c), so we derive the
# mission name from it. Pinning PATH guarantees `claude` resolves no matter how the
# tmux server was first started (systemd's env can be minimal).

export PATH="$HOME/.local/bin:$HOME/bin:$PATH"

# Keep any tmux invoked from inside the pane on the shared socket (see console-launch.sh).
export TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.tmux-console}"

# Disable Claude Code's terminal mouse tracking. Without this, Claude's TUI turns on
# mouse reporting, so the browser/ttyd (xterm.js) terminal forwards drags to the app
# instead of doing a native text selection — breaking highlight-to-copy. This must be
# set HERE: the script is run directly as the tmux session command, so it never sources
# ~/.bashrc, and tmux doesn't propagate custom env vars into new panes.
export CLAUDE_CODE_DISABLE_MOUSE=1

# Claude's TUI keeps the ALTERNATE screen (i.e. we deliberately do NOT set
# CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN). Scrolling the console is then Claude's job,
# not the terminal's, and that is the behaviour we want: PageUp/PageDown page the
# conversation while the prompt box and status line stay pinned to the bottom of the
# screen. Putting the TUI in the normal buffer instead (tried, reverted) does fill
# tmux's scrollback, but scrolling it moves the WHOLE viewport — the prompt scrolls
# out of sight and the history is littered with duplicate half-drawn frames.
# The cost is that tmux's own history stays empty here, so anything that wants the
# conversation must ask Claude to page (app.py's ▲/▼ buttons send PageUp/PageDown)
# rather than read `capture-pane` scrollback.

# MISSION_NAME / MISSION_DATA_DIR are usually derived from the pane's cwd (the mission
# folder). For an ops mission whose console works in a CHOSEN local dir (cwd != mission
# folder), console-launch.sh passes them in via `tmux new-session -e`, so honor those
# when set and only fall back to $PWD otherwise — the docs always live in the mission dir.
name="${MISSION_NAME:-$(basename "$PWD")}"
here="$(dirname "$(readlink -f "$0")")"

# Mission-doc reminder hook (scripts/mission-doc-reminder.py), attached at launch via
# the mission-console-only settings file. Exporting these lets the UserPromptSubmit hook
# gently nudge Claude to keep LOG/DASHBOARD/HANDOFF current (it self-quiets when fresh).
export MISSION_NAME="$name"
export MISSION_DATA_DIR="${MISSION_DATA_DIR:-$PWD}"
export MISSION_DOC_REMINDER="$here/scripts/mission-doc-reminder.py"
export MISSION_DOC_POSTACTION="$here/scripts/mission-doc-postaction.py"
export MISSION_DOC_STOP="$here/scripts/mission-doc-stop.py"
# Records which transcript this console is writing (<mission dir>/.console-session), so
# the dashboard's context badge reads THIS session rather than inferring one from the cwd.
export MISSION_CONSOLE_SESSION="$here/scripts/mission-console-session.py"
hooks_settings="$here/console-hooks.settings.json"

clear
printf '%s\n' \
  "== Mission ${name} ==" \
  "Read DASHBOARD.md before acting. Update LOG.md and DASHBOARD.md after meaningful work." \
  "Write HANDOFF.md before stopping. If chat history conflicts with these files, the files win." \
  "Started in the mission dir: also read $HOME/CLAUDE.md and the fleet MEMORY.md." \
  ""

# Run Claude with permission prompts disabled (this is a firewall- + auth-gated admin
# console, so tool calls run without interactive approval). When Claude exits you drop to
# an interactive login shell in the mission dir; the tmux session stays alive either way.
if [[ -n "${MISSION_SESSION_ID:-}" ]]; then
  # Shared/local-dir ops console: the cwd is a dir whose Claude history is NOT unique to
  # this mission (e.g. the user's home), so --continue would grab an unrelated conversation.
  # console-launch.sh passed a deterministic per-mission UUID; resume THIS mission's own
  # conversation (--resume), and on first open — when it doesn't exist yet — CREATE it with
  # that exact id (--session-id). A different mission in the same dir uses a different id, so
  # the conversations stay independent. Mirrors the remote/local console resume pattern.
  #
  # ...but that pinned uuid is only the conversation this console STARTED from. A /clear
  # opens a NEW session file mid-process and abandons the old one, so on the NEXT open
  # --resume <pinned> drops the operator into the conversation from before the clear
  # (verified: one console would have resumed a 277k thread from the previous day, five
  # forks back). The console already writes its live transcript to <mission dir>/.console-session
  # for the context badge (mission-console-session.py) — that marker is the same answer
  # this needs, so prefer the id it records. Fail-safe chain, each step falling back to
  # exactly today's behaviour: live session -> pinned uuid -> create the pinned uuid.
  resume_id="$MISSION_SESSION_ID"
  live_id=$(python3 - "$MISSION_DATA_DIR/.console-session" <<'PY' 2>/dev/null || true
import json, re, sys
try:
    rec = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit
sid = rec.get("session_id") if isinstance(rec, dict) else None
if isinstance(sid, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", sid):
    print(sid)
PY
)
  [[ -n "$live_id" ]] && resume_id="$live_id"
  claude --settings "$hooks_settings" --resume "$resume_id" --dangerously-skip-permissions \
    || claude --settings "$hooks_settings" --resume "$MISSION_SESSION_ID" --dangerously-skip-permissions \
    || claude --settings "$hooks_settings" --session-id "$MISSION_SESSION_ID" --dangerously-skip-permissions
else
  # Normal mission: the cwd is the mission's own folder (unique), so Claude keys history off
  # that cwd and --continue resumes the most recent conversation for THIS mission — stopping
  # a mission and reopening it picks up where it left off. On a brand-new mission with no
  # history, `claude --continue` errors ("No conversation found to continue") and exits
  # non-zero, so we fall back to a fresh session; without the fallback the pane would drop
  # straight to the login shell below and no Claude would start.
  claude --settings "$hooks_settings" --continue --dangerously-skip-permissions \
    || claude --settings "$hooks_settings" --dangerously-skip-permissions
fi
exec bash --login -i
