#!/usr/bin/env bash
# console-session-int.sh — runs INSIDE the tmux pane of an INTEGRATOR dev mission.
# The pane's cwd is the checkout that holds the repo's integration branch (recorded in
# the mission's mission.json as dev.integration_worktree; console-launch.sh exports it
# as INTEGRATION_WORKTREE together with PRIMARY_REPO / BASE_BRANCH). It launches
# claude-miss-integrator, which refuses to run anywhere but that checkout on that
# branch — so WHICH repo this session integrates is decided by the mission's recorded
# identity, never by the pane's cwd or by the dashboard's own default repo.
#
# Started by console-launch.sh via `tmux new-session ... console-session-int.sh`.

export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
export TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.tmux-console}"
export CLAUDE_CODE_DISABLE_MOUSE=1

here="$(dirname "$(readlink -f "$0")")"
name="${MISSION_NAME:-$(basename "$PWD")}"

# The identity contract (see console-launch.sh). Refuse rather than guess: an
# integrator with no recorded repo would fall back to Miss Claude's own checkout.
if [[ -z "${PRIMARY_REPO:-}" || -z "${INTEGRATION_WORKTREE:-}" ]]; then
  echo "Refusing to start the integrator console: PRIMARY_REPO / INTEGRATION_WORKTREE not set."
  echo "(this pane must be started by console-launch.sh from a mission whose mission.json"
  echo " records dev.role = integrator, dev.repo and dev.integration_worktree)"
  sleep 8
  exec bash --login -i
fi
export PRIMARY_REPO BASE_BRANCH INTEGRATION_WORKTREE WORKTREES_DIR
export MISSIONS_DIR="${MISSIONS_DIR:-$HOME/missions}"
export MISSION_NAME="$name"
export MISSION_DATA_DIR="${MISSION_DATA_DIR:-$MISSIONS_DIR/$name}"

clear
printf '%s\n' \
  "== Mission ${name} — INTEGRATOR for ${PRIMARY_REPO} (staging '${BASE_BRANCH:-working}') ==" \
  "Runs in ${INTEGRATION_WORKTREE}. Fast-forward reviewed claude/* branches only after YES INTEGRATE." \
  "Never write feature code here; log each integrate/release/deploy via the dashboard." \
  ""

# claude-miss-integrator wires the doc hooks + rails itself (settings file, guard,
# role context) from MISSION_NAME / the env above. Drop to a shell when it exits so the
# tmux session stays alive for reopen — matching console-session.sh.
"$here/scripts/claude-miss-integrator" "$name" || true
exec bash --login -i
