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
  claude --settings "$hooks_settings" --resume "$MISSION_SESSION_ID" --dangerously-skip-permissions \
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
