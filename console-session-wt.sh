#!/usr/bin/env bash
# console-session-wt.sh — runs INSIDE the tmux pane when the mission has a dev worktree.
# Like console-session.sh, but the pane's cwd is the mission's git worktree
# (~/missclaude-worktrees/<name>, same name as the mission). It launches a FEATURE WORKER
# via claude-miss, which exports CLAUDE_MISS_ROLE=feature, prints the GREEN/YELLOW
# stoplight, logs the session, and is governed by the prevent-misswork hook. claude-miss
# "Case A" (already inside a worktree) enters and launches with no prompt.
#
# Started by console-launch.sh via `tmux new-session ... console-session-wt.sh`.

export PATH="$HOME/.local/bin:$HOME/bin:$PATH"

# Keep any tmux invoked from inside the pane on the shared socket (see console-launch.sh).
export TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.tmux-console}"

# Disable Claude Code's terminal mouse tracking so xterm.js text selection works (see
# the long note in console-session.sh — this script also never sources ~/.bashrc).
export CLAUDE_CODE_DISABLE_MOUSE=1

here="$(dirname "$(readlink -f "$0")")"
name="$(basename "$PWD")"

# Mission-doc reminder hook (scripts/mission-doc-reminder.py). A DEV console's cwd is the
# WORKTREE, but LOG.md/HANDOFF.md live in the mission data dir — so point MISSION_DATA_DIR
# there by name (not "$PWD"). claude-miss launches claude internally, so the hooks settings
# file is threaded through CLAUDE_MISS_SETTINGS rather than a --settings flag here.
MISSIONS_DIR="${MISSIONS_DIR:-$HOME/missions}"
export MISSION_NAME="$name"
export MISSION_DATA_DIR="$MISSIONS_DIR/$name"
export MISSION_DOC_REMINDER="$here/scripts/mission-doc-reminder.py"
export CLAUDE_MISS_SETTINGS="$here/console-hooks.settings.json"

clear
printf '%s\n' \
  "== Mission ${name} — dev worktree (branch claude/${name}) ==" \
  "FEATURE WORKER: edit code in THIS worktree only. Commit only after YES COMMIT." \
  "Update the mission's LOG/DASHBOARD via the dashboard; say 'ready for integrator' when done." \
  ""

# Launch the feature worker (claude-miss Case A launches directly, no prompt). When it
# exits, drop to a login shell so the tmux session stays alive for reopen — matching
# console-session.sh.
"$here/scripts/claude-miss" || true
exec bash --login -i
