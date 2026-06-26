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

name="$(basename "$PWD")"
here="$(dirname "$(readlink -f "$0")")"

# Mission-doc reminder hook (scripts/mission-doc-reminder.py), attached at launch via
# the mission-console-only settings file. The OPS console's cwd IS the mission data
# dir, so MISSION_DATA_DIR is $PWD. Exporting these lets the UserPromptSubmit hook
# gently nudge Claude to keep LOG/DASHBOARD/HANDOFF current (it self-quiets when fresh).
export MISSION_NAME="$name"
export MISSION_DATA_DIR="$PWD"
export MISSION_DOC_REMINDER="$here/scripts/mission-doc-reminder.py"
hooks_settings="$here/console-hooks.settings.json"

clear
printf '%s\n' \
  "== Mission ${name} ==" \
  "Read DASHBOARD.md before acting. Update LOG.md and DASHBOARD.md after meaningful work." \
  "Write HANDOFF.md before stopping. If chat history conflicts with these files, the files win." \
  ""

# Run Claude with permission prompts disabled (this is an auth-gated admin console,
# so tool calls run without interactive approval). Claude keys history off the
# cwd, so --continue resumes the most recent conversation for THIS mission dir — stopping
# a mission and reopening it picks up where it left off. On a brand-new mission with no
# history, `claude --continue` errors ("No conversation found to continue") and exits
# non-zero, so we fall back to a fresh session; without the fallback the pane would drop
# straight to the login shell below and no Claude would start. When Claude exits you drop
# to an interactive login shell in the mission dir; the tmux session stays alive either way.
claude --settings "$hooks_settings" --continue --dangerously-skip-permissions \
  || claude --settings "$hooks_settings" --dangerously-skip-permissions
exec bash --login -i
