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

# A generalized DEV mission tells claude-miss which local repo / base branch / worktrees
# dir it develops via the environment (set by console-launch.sh with `tmux new-session
# -e` from the mission's mission.json). They arrive already-exported from the pane env;
# re-export here only to make the contract explicit. A legacy Claude-Miss dev mission
# sets none, so claude-miss falls back to its defaults (~/mission-dashboard, working).
[ -n "${PRIMARY_REPO:-}" ] && export PRIMARY_REPO
[ -n "${BASE_BRANCH:-}" ] && export BASE_BRANCH
[ -n "${WORKTREES_DIR:-}" ] && export WORKTREES_DIR
# The recorded identity (MISS_* from mission.json via scripts/mission-env.py). The
# pane IS the feature worktree: tell claude-miss so it enters it and never creates
# another one, even if the worktree lives outside WORKTREES_DIR.
export MISS_WORKTREE="${MISS_WORKTREE:-$PWD}"

# Disable Claude Code's terminal mouse tracking so xterm.js text selection works. The
# TUI keeps the alternate screen so PageUp/PageDown page the conversation with the
# prompt pinned (see the long notes in console-session.sh — this script also never
# sources ~/.bashrc).
export CLAUDE_CODE_DISABLE_MOUSE=1

here="$(dirname "$(readlink -f "$0")")"
# The mission name usually equals the worktree's basename, but a RENAMED mission keeps
# its original worktree — console-launch.sh passes the real identity in via
# MISSION_NAME/MISSION_DATA_DIR (tmux -e); the basename is the legacy fallback.
name="${MISSION_NAME:-$(basename "$PWD")}"

# Mission-doc reminder hook (scripts/mission-doc-reminder.py). A DEV console's cwd is the
# WORKTREE, but LOG.md/HANDOFF.md live in the mission data dir — so point MISSION_DATA_DIR
# there by name (not "$PWD"). claude-miss launches claude internally, so the hooks settings
# file is threaded through CLAUDE_MISS_SETTINGS rather than a --settings flag here.
MISSIONS_DIR="${MISSIONS_DIR:-$HOME/missions}"
export MISSION_NAME="$name"
export MISSION_DATA_DIR="${MISSION_DATA_DIR:-$MISSIONS_DIR/$name}"
export MISSION_DOC_REMINDER="$here/scripts/mission-doc-reminder.py"
export MISSION_DOC_POSTACTION="$here/scripts/mission-doc-postaction.py"
export MISSION_DOC_STOP="$here/scripts/mission-doc-stop.py"
# Records which transcript this console is writing (<mission dir>/.console-session), so
# the dashboard's context badge reads THIS session rather than inferring one from the cwd.
export MISSION_CONSOLE_SESSION="$here/scripts/mission-console-session.py"
# Bark push notifier (scripts/notify) for the Notification/Stop hooks — a no-op
# unless bark.env is configured and this mission's 🔔 toggle is on.
export MISSION_NOTIFY="$here/scripts/notify"

# The dev rails, for ANY repo. console-hooks-dev.settings.json = the doc hooks above
# PLUS the prevent-misswork PreToolUse guard ($MISSWORK_HOOK) and the SessionStart role
# rules ($MISS_ROLE_CONTEXT). Historically the guard only reached Claude through the
# mission-dashboard repo's checked-in .claude/settings.json, so a dev mission on any
# OTHER local repo ran --dangerously-skip-permissions with NO rails at all; attaching
# it here closes that hole for every repo. (In a Miss-Claude worktree the guard now
# runs twice — repo settings + this file — which is harmless: it's a read-only check.)
export MISSWORK_HOOK="$here/.claude/hooks/prevent-misswork.py"
export MISS_ROLE_CONTEXT="$here/scripts/miss-role-context.py"
export CLAUDE_MISS_SETTINGS="$here/console-hooks-dev.settings.json"

# CODEX dev worker (mission.json "agent": "codex" -> MISS_AGENT, exported into the
# pane by console-launch.sh). Runs with full powers, no sandbox and NO guard hook:
# codex can't load Claude hooks, and the operator explicitly chose an unguarded
# worker over a sandboxed one (it needs network + git remotes; 2026-09-04). The
# claude-miss machinery and the guard fail-closed check below are Claude-specific,
# so none of it applies. Binary resolution mirrors console-launch.sh's CODEX_RUN
# (PATH first — ~/.local/bin was just prepended — then the npm-under-nvm install).
if [[ "${MISS_AGENT:-}" == "codex" ]]; then
  clear
  cur_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "${MISS_FEATURE_BRANCH:-?}")"
  printf '%s\n' \
    "== Mission ${name} — dev worktree (branch ${cur_branch}; repo ${PRIMARY_REPO:-?}) — codex ==" \
    "FEATURE WORKER: edit code in THIS worktree only." \
    "Update the mission's LOG/DASHBOARD via the dashboard; when the work is verified, ask for YES SHIP." \
    ""
  CX="$(command -v codex 2>/dev/null || ls -1 "$HOME"/.nvm/versions/node/*/bin/codex 2>/dev/null | tail -n 1)"
  if [[ -n "$CX" ]]; then
    "$CX" --dangerously-bypass-approvals-and-sandbox
  else
    echo "[console] codex not found (not on PATH, and nothing under ~/.nvm/versions/node/*/bin)."
  fi
  exec bash --login -i
fi

# FAIL-CLOSED: this console launches Claude with --dangerously-skip-permissions, so
# refuse to start if the guard bundle is missing/broken (mirrors the remote-dev branch
# of console-launch.sh, which refuses when ship-rails can't verify the remote guard).
if ! python3 -c "import ast,json,sys; ast.parse(open(sys.argv[1]).read()); json.load(open(sys.argv[2]))" \
      "$MISSWORK_HOOK" "$CLAUDE_MISS_SETTINGS" 2>/dev/null; then
  echo "Refusing to start the dev console: the guard rails are missing or broken."
  echo "  hook:     $MISSWORK_HOOK"
  echo "  settings: $CLAUDE_MISS_SETTINGS"
  echo "(a dev console must never run --dangerously-skip-permissions with no guard)"
  sleep 8
  exec bash --login -i
fi

clear
cur_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "${MISS_FEATURE_BRANCH:-?}")"
printf '%s\n' \
  "== Mission ${name} — dev worktree (branch ${cur_branch}; repo ${PRIMARY_REPO:-?}) ==" \
  "FEATURE WORKER: edit code in THIS worktree only. Commit only after YES COMMIT." \
  "Update the mission's LOG/DASHBOARD via the dashboard; when the work is verified, ask for YES SHIP." \
  ""

# Launch the feature worker (claude-miss Case A launches directly, no prompt). When it
# exits, drop to a login shell so the tmux session stays alive for reopen — matching
# console-session.sh.
"$here/scripts/claude-miss" || true
exec bash --login -i
