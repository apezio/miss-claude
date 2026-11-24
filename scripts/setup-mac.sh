#!/bin/bash
# setup-mac.sh — install Miss Claude on macOS as one per-user LaunchAgent.
#
# setup.sh is the Linux/systemd installer. On a Mac, one agent running dev-run.sh
# (dashboard on 127.0.0.1:4200 + ttyd console on :4201, plain http) is the install:
#
#   brew install bash tmux ttyd python
#   scripts/setup-mac.sh --dry-run     # print the plist, change nothing
#   scripts/setup-mac.sh               # write it and (re)load the agent
#   scripts/setup-mac.sh --no-load     # write it only
#
# Writes ~/Library/LaunchAgents/local.miss-claude.plist (mode 600: it holds the
# console credential) and logs to ~/Library/Logs/miss-claude/ (mode 600). A re-run keeps
# the existing DEV_CREDENTIAL (or uses $DEV_CREDENTIAL if set). Deploys after that are
# `launchctl kickstart -k gui/$(id -u)/local.miss-claude` (what miss-ship.py runs).
# With ROOM_RELAY_HOST set in local.env it also writes and loads local.miss-claude.relay,
# which runs scripts/room-relay.py (docs/internal/ROOM.md, "Link two rooms").
# Runs under macOS /bin/bash 3.2 on purpose: it is what checks for a newer bash.
set -euo pipefail

LABEL="local.miss-claude"
DRY_RUN=0
LOAD=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-load) LOAD=0 ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 1 ;;
  esac
done

die() { echo "setup-mac: $*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "this is the macOS installer; on Linux use: sudo bash setup.sh"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
[[ -f "$REPO_DIR/dev-run.sh" ]] || die "dev-run.sh not found in $REPO_DIR"

# --- prerequisites -------------------------------------------------------------
command -v brew >/dev/null 2>&1 || die "Homebrew not found: https://brew.sh"
BREW_PREFIX="$(brew --prefix)"
BASH_BIN="$BREW_PREFIX/bin/bash"
[[ -x "$BASH_BIN" ]] || die "Homebrew bash not found at $BASH_BIN: brew install bash"
"$BASH_BIN" -c '(( BASH_VERSINFO[0] >= 4 ))' || die "$BASH_BIN is older than bash 4: brew upgrade bash"
for tool in tmux ttyd python3; do
  PATH="$BREW_PREFIX/bin:$PATH" command -v "$tool" >/dev/null 2>&1 \
    || die "'$tool' not found: brew install $tool"
done

# --- values --------------------------------------------------------------------
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/miss-claude"
USER_NAME="$(id -un)"
AGENT_PATH="$HOME/.local/bin:$BREW_PREFIX/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

CREDENTIAL="${DEV_CREDENTIAL:-}"
if [[ -z "$CREDENTIAL" && -f "$PLIST" ]]; then
  CREDENTIAL="$(plutil -extract EnvironmentVariables.DEV_CREDENTIAL raw -o - "$PLIST" 2>/dev/null || true)"
fi
if [[ -z "$CREDENTIAL" ]]; then
  CREDENTIAL="$USER_NAME:$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')"
fi

RELAY_LABEL="$LABEL.relay"
RELAY_PLIST="$AGENT_DIR/$RELAY_LABEL.plist"
RELAY_HOST="$(sed -n 's/^ROOM_RELAY_HOST=//p' "$REPO_DIR/local.env" 2>/dev/null | tail -1 | tr -d "\"'" || true)"

xml() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }

render_plist() {  # $1 = the credential to print
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$(xml "$BASH_BIN")</string><string>$(xml "$REPO_DIR/dev-run.sh")</string></array>
  <key>WorkingDirectory</key><string>$(xml "$REPO_DIR")</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(xml "$AGENT_PATH")</string>
    <key>HOME</key><string>$(xml "$HOME")</string>
    <key>USER</key><string>$(xml "$USER_NAME")</string>
    <key>DEV_CREDENTIAL</key><string>$(xml "$1")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>$(xml "$LOG_DIR/miss-claude.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG_DIR/miss-claude.err")</string>
</dict></plist>
EOF
}

render_relay_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$RELAY_LABEL</string>
  <key>ProgramArguments</key><array><string>$(xml "$BREW_PREFIX/bin/python3")</string><string>$(xml "$REPO_DIR/scripts/room-relay.py")</string></array>
  <key>WorkingDirectory</key><string>$(xml "$REPO_DIR")</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(xml "$AGENT_PATH")</string>
    <key>HOME</key><string>$(xml "$HOME")</string>
    <key>USER</key><string>$(xml "$USER_NAME")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$(xml "$LOG_DIR/room-relay.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG_DIR/room-relay.log")</string>
</dict></plist>
EOF
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "# dry run: nothing written. Would write $PLIST (mode 600):"
  render_plist "${CREDENTIAL%%:*}:<hidden>"
  if [[ -n "$RELAY_HOST" ]]; then
    echo "# ROOM_RELAY_HOST is set: would also write $RELAY_PLIST:"
    render_relay_plist
  fi
  echo "# logs: $LOG_DIR (mode 600)"
  [[ "$LOAD" -eq 1 ]] && echo "# then: launchctl bootstrap gui/$(id -u) $PLIST"
  exit 0
fi

# --- write ---------------------------------------------------------------------
umask 077
mkdir -p "$AGENT_DIR" "$LOG_DIR"
chmod 700 "$LOG_DIR"
for f in miss-claude.log miss-claude.err room-relay.log; do
  touch "$LOG_DIR/$f"
  chmod 600 "$LOG_DIR/$f"
done
render_plist "$CREDENTIAL" > "$PLIST.tmp"
plutil -lint -s "$PLIST.tmp" || { rm -f "$PLIST.tmp"; die "generated plist failed plutil -lint"; }
chmod 600 "$PLIST.tmp"
mv "$PLIST.tmp" "$PLIST"
echo "wrote $PLIST"
if [[ -n "$RELAY_HOST" ]]; then
  render_relay_plist > "$RELAY_PLIST.tmp"
  plutil -lint -s "$RELAY_PLIST.tmp" || { rm -f "$RELAY_PLIST.tmp"; die "generated relay plist failed plutil -lint"; }
  mv "$RELAY_PLIST.tmp" "$RELAY_PLIST"
  echo "wrote $RELAY_PLIST"
fi

if [[ "$LOAD" -eq 0 ]]; then
  echo "not loaded (--no-load). Load it with: launchctl bootstrap gui/$(id -u) $PLIST"
  exit 0
fi

# --- load (a re-run replaces the running agent; tmux consoles keep running) --------
DOMAIN="gui/$(id -u)"
reload() {  # $1 = label, $2 = plist
  if launchctl print "$DOMAIN/$1" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$1" || true
    sleep 1
  fi
  launchctl bootstrap "$DOMAIN" "$2"
}
reload "$LABEL" "$PLIST"
if [[ -n "$RELAY_HOST" ]]; then
  reload "$RELAY_LABEL" "$RELAY_PLIST"
  echo "loaded $RELAY_LABEL (room relay to $RELAY_HOST; log $LOG_DIR/room-relay.log)"
fi
echo "loaded $LABEL. Dashboard: http://127.0.0.1:4200/  console login: ${CREDENTIAL%%:*} (password in $PLIST)"
echo "logs: $LOG_DIR"
