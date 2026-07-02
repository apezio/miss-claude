#!/usr/bin/env bash
# dev-run.sh — run the WHOLE dev stack in one command: the dashboard (app.py) AND the
# ttyd console bridge. Starting only app.py is the classic trap — the dashboard comes
# up fine but every Console pane is a dead iframe — so this launcher refuses to let
# you make that mistake: both processes start together and both stop together (trap).
#
# This is the DEV path only. The real deployment stays systemd (setup.sh installs
# mission-dashboard.service + claude-console.service); nothing here touches those.
#
# Defaults are dev-safe: everything binds 127.0.0.1, a random ttyd basic-auth
# password is generated per run (and printed), and missions live in the normal
# $MISSIONS_DIR (override with a temp dir for a throwaway instance, e.g.
#   MISSIONS_DIR=$(mktemp -d) ./dev-run.sh
# ). Overridable knobs (same env names as app.py / the systemd units):
#   MISSION_HOST / MISSION_PORT   dashboard bind + port   (default 127.0.0.1:4200)
#   CONSOLE_TTYD_PORT             console bridge port     (default 4201)
#   MISSIONS_DIR                  mission data dir        (default ~/missions)
#   DEV_CREDENTIAL                user:pass for ttyd      (default: generated)
#
# Part of the Mission Dashboard (see app.py / README.md).
set -uo pipefail

here="$(dirname "$(readlink -f "$0")")"

MISSION_HOST="${MISSION_HOST:-127.0.0.1}"
MISSION_PORT="${MISSION_PORT:-4200}"
CONSOLE_TTYD_PORT="${CONSOLE_TTYD_PORT:-4201}"
MISSIONS_DIR="${MISSIONS_DIR:-$HOME/missions}"
# Shared tmux socket dir — must match what console-launch.sh uses so the dashboard's
# live/kill logic sees the console's tmux sessions (same contract as the systemd units).
export TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.tmux-console}"

# --- preflight: fail LOUD on missing tools, before anything starts -----------------
# The console path needs all three at runtime; better one clear error now than a
# dashboard that "works" until the first console tab spins on a broken launcher.
missing=0
need() {  # need <cmd> <install hint>
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "MISSING: $1 — $2" >&2
    missing=1
  fi
}
# claude installs to ~/.local/bin, which a non-login shell may not have on PATH yet;
# add it (and ~/bin) the same way console-launch.sh does before checking.
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
need python3 "install with: sudo dnf install python3"
need tmux    "install with: sudo dnf install tmux"
need ttyd    "install with: sudo dnf install epel-release && sudo dnf install ttyd (see setup.sh/README)"
need claude  "install with: curl -fsSL https://claude.ai/install.sh | bash"
if (( missing )); then
  echo "Aborting: install the missing tool(s) above, then re-run $0." >&2
  exit 1
fi

mkdir -p "$MISSIONS_DIR" "$TMUX_TMPDIR"

# --- ttyd credential: generated per run unless the caller pins one -----------------
# ttyd wants user:pass; the password is random so a dev instance never ships the
# CHANGE-ME template value. Printed below because the browser will prompt for it
# when the dashboard iframes the console.
cred_user="$USER"
if [[ -n "${DEV_CREDENTIAL:-}" ]]; then
  credential="$DEV_CREDENTIAL"
else
  # /dev/urandom -> alnum only: shell-, URL- and basic-auth-safe. No new deps.
  cred_pass="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 20)"
  credential="$cred_user:$cred_pass"
fi

# --- start both, stop both ----------------------------------------------------------
# The EXIT trap is the whole point: Ctrl-C (or either process dying) tears down BOTH,
# so there is never a half-running stack. NOTE: tmux sessions the console spawned are
# persistence by design and are NOT killed — reattach by re-running this script, or
# clean up with: TMUX_TMPDIR=~/.tmux-console tmux kill-server
app_pid=""; ttyd_pid=""
cleanup() {
  trap - EXIT INT TERM
  for pid in "$app_pid" "$ttyd_pid"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
  echo
  echo "[dev-run] dashboard + console bridge stopped."
}
trap cleanup EXIT INT TERM

MISSION_HOST="$MISSION_HOST" MISSION_PORT="$MISSION_PORT" MISSIONS_DIR="$MISSIONS_DIR" \
  CONSOLE_TTYD_PORT="$CONSOLE_TTYD_PORT" \
  python3 "$here/app.py" &
app_pid=$!

# Same flags as claude-console.service, minus the 0.0.0.0 bind (dev = localhost).
ttyd --port "$CONSOLE_TTYD_PORT" --interface "$MISSION_HOST" --writable --url-arg \
  --credential "$credential" \
  --client-option fontSize=14 --client-option "titleFixed=Claude Console" \
  --client-option 'theme={"background": "#000000"}' \
  --client-option disableLeaveAlert=true \
  "$here/console-launch.sh" &
ttyd_pid=$!

# Fail loud if either died on startup (port in use, syntax error, ...) instead of
# printing URLs that don't work.
sleep 1
if ! kill -0 "$app_pid" 2>/dev/null; then
  echo "FAILED: app.py exited immediately (port $MISSION_PORT in use? syntax error?)." >&2
  exit 1
fi
if ! kill -0 "$ttyd_pid" 2>/dev/null; then
  echo "FAILED: ttyd exited immediately (port $CONSOLE_TTYD_PORT in use?)." >&2
  exit 1
fi

echo
echo "[dev-run] Mission Dashboard dev stack is up:"
echo "  dashboard : http://$MISSION_HOST:$MISSION_PORT/"
echo "  console   : http://$MISSION_HOST:$CONSOLE_TTYD_PORT/  (basic auth: $credential)"
echo "  missions  : $MISSIONS_DIR"
echo
echo "Ctrl-C stops BOTH processes."

# Exit as soon as EITHER process dies — the EXIT trap then stops the survivor, so a
# crashed dashboard can't leave an orphaned ttyd (or vice versa).
wait -n "$app_pid" "$ttyd_pid"
