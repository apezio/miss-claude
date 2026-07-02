#!/usr/bin/env bash
# setup.sh — one-command installer for Miss Claude (the Mission Dashboard).
#
# Renders the systemd units with YOUR user/paths, optionally installs the
# in-browser Claude console (ttyd + tmux), and enables both services. Run as root:
#
#   sudo bash setup.sh
#
# Run it with --dry-run first to see exactly what it will do, changing nothing.
# Anything not passed as a flag is prompted for when run interactively.
set -euo pipefail

# --- where the repo lives (this script's own directory) ----------------------
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# --- defaults ----------------------------------------------------------------
APP_USER="${SUDO_USER:-$(id -un)}"
PORT=4200
CONSOLE_PORT=4201
LABEL="$(hostname -s 2>/dev/null || hostname)"
TOKEN=""
ENABLE_CONSOLE=1
CONSOLE_USER=""
CONSOLE_PASS=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: sudo bash setup.sh [options]

  --user USER          account that runs the services (default: invoking user)
  --port N             dashboard port (default 4200)
  --label TEXT         short label shown in the UI header (default: hostname)
  --token TOKEN        enable app token auth (default: none)
  --no-console         do not install the ttyd Claude console
  --console-port N     console port (default 4201)
  --console-user USER  ttyd basic-auth username (default: --user)
  --console-pass PASS  ttyd basic-auth password (prompted if not given)
  --dry-run            print what would happen; change nothing
  -h, --help           this help

Examples:
  sudo bash setup.sh
  sudo bash setup.sh --dry-run --no-console
EOF
}

# --- parse flags -------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)         APP_USER="$2"; shift 2;;
    --port)         PORT="$2"; shift 2;;
    --label)        LABEL="$2"; shift 2;;
    --token)        TOKEN="$2"; shift 2;;
    --no-console)   ENABLE_CONSOLE=0; shift;;
    --console-port) CONSOLE_PORT="$2"; shift 2;;
    --console-user) CONSOLE_USER="$2"; shift 2;;
    --console-pass) CONSOLE_PASS="$2"; shift 2;;
    --dry-run)      DRY_RUN=1; shift;;
    -h|--help)      usage; exit 0;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1;;
  esac
done

die() { echo "Error: $*" >&2; exit 1; }
have_tty() { [[ -t 0 && -t 1 ]]; }

# Run a system-changing command, or just print it under --dry-run.
run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '  [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# --- preflight helpers: detect the package manager, check/install tools ------
PKG_MGR=""
detect_pkg_mgr() {
  if   command -v dnf     >/dev/null 2>&1; then PKG_MGR=dnf
  elif command -v apt-get >/dev/null 2>&1; then PKG_MGR=apt
  elif command -v yum     >/dev/null 2>&1; then PKG_MGR=yum
  elif command -v zypper  >/dev/null 2>&1; then PKG_MGR=zypper
  elif command -v pacman  >/dev/null 2>&1; then PKG_MGR=pacman
  elif command -v brew    >/dev/null 2>&1; then PKG_MGR=brew
  else PKG_MGR=""
  fi
}

# The exact command a human would run to install $1 (for loud failure messages).
pkg_install_cmd() {
  case "$PKG_MGR" in
    dnf)    echo "sudo dnf install -y $1";;
    yum)    echo "sudo yum install -y $1";;
    apt)    echo "sudo apt-get update && sudo apt-get install -y $1";;
    zypper) echo "sudo zypper install -y $1";;
    pacman) echo "sudo pacman -S --noconfirm $1";;
    brew)   echo "brew install $1";;
    *)      echo "(install '$1' with your OS package manager)";;
  esac
}

# Attempt to install package $1 via the detected manager (honors --dry-run).
pkg_install() {
  case "$PKG_MGR" in
    dnf)    run dnf install -y "$1";;
    yum)    run yum install -y "$1";;
    apt)    run apt-get update && run apt-get install -y "$1";;
    zypper) run zypper install -y "$1";;
    pacman) run pacman -S --noconfirm "$1";;
    brew)   run brew install "$1";;
    *)      return 1;;
  esac
}

# Is $1 on PATH for the account that will actually run the services? (claude is
# typically a per-user install, so a root check would give a false negative.)
svc_has_cmd() {
  if [[ "$DRY_RUN" -eq 0 && "$APP_USER" != "$(id -un)" ]]; then
    su - "$APP_USER" -c "command -v $1" >/dev/null 2>&1
  else
    command -v "$1" >/dev/null 2>&1
  fi
}

# Ensure required command $1 (from package $2) exists; try to install it via the
# package manager, and if it still isn't there, fail LOUDLY with the exact
# command to run. Pass "$3" = "check" to never auto-install (report + fail only).
require_tool() {
  local cmd="$1" pkg="${2:-$1}" mode="${3:-auto}"
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "  ok: $cmd  ($(command -v "$cmd"))"
    return 0
  fi
  if [[ "$mode" == auto && -n "$PKG_MGR" ]]; then
    echo "  missing: $cmd — installing via $PKG_MGR ..."
    pkg_install "$pkg" || true
    if [[ "$DRY_RUN" -eq 1 ]] || command -v "$cmd" >/dev/null 2>&1; then
      echo "  ok: $cmd installed"
      return 0
    fi
  fi
  die "required tool '$cmd' not found. Install it and re-run setup:
    $(pkg_install_cmd "$pkg")"
}

# --- must be root (except dry-run) ------------------------------------------
if [[ "$DRY_RUN" -eq 0 && $EUID -ne 0 ]]; then
  die "run as root:  sudo bash $0 ...   (or add --dry-run to preview)"
fi

# --- resolve user + home -----------------------------------------------------
HOME_DIR="$(getent passwd "$APP_USER" | cut -d: -f6)"
[[ -n "$HOME_DIR" ]] || die "user '$APP_USER' not found"
MISSIONS_DIR="$HOME_DIR/missions"
WORKTREES_DIR="$HOME_DIR/missclaude-worktrees"
TMUX_DIR="$HOME_DIR/.tmux-console"
[[ -f "$REPO_DIR/app.py" ]] || die "app.py not found in $REPO_DIR (run this script from the repo)"

# --- interactive fill-ins ----------------------------------------------------
[[ -n "$CONSOLE_USER" ]] || CONSOLE_USER="$APP_USER"
if [[ "$ENABLE_CONSOLE" -eq 1 && -z "$CONSOLE_PASS" && "$DRY_RUN" -eq 0 ]]; then
  if have_tty; then
    read -r -s -p "ttyd console basic-auth password for '$CONSOLE_USER': " CONSOLE_PASS; echo
    [[ -n "$CONSOLE_PASS" ]] || die "console password cannot be empty (or pass --no-console)"
  else
    die "console enabled but no --console-pass given (and no TTY to prompt)"
  fi
fi
[[ "$DRY_RUN" -eq 1 && -z "$CONSOLE_PASS" ]] && CONSOLE_PASS="<prompted-at-install>"

# --- summary -----------------------------------------------------------------
echo
echo "Miss Claude setup"
echo "  repo dir:      $REPO_DIR"
echo "  run as user:   $APP_USER  (home: $HOME_DIR)"
echo "  dashboard:     port $PORT   label '${LABEL:-<none>}'   token: $([[ -n "$TOKEN" ]] && echo set || echo none)"
echo "  missions dir:  $MISSIONS_DIR"
if [[ "$ENABLE_CONSOLE" -eq 1 ]]; then
  echo "  console:       port $CONSOLE_PORT   ttyd user '$CONSOLE_USER'"
else
  echo "  console:       disabled"
fi
[[ "$DRY_RUN" -eq 1 ]] && echo "  MODE:          DRY RUN — nothing will be changed"
echo

# --- unit renderers (emit to stdout) ----------------------------------------
render_dashboard_unit() {
  cat <<EOF
[Unit]
Description=Mission Dashboard (local ops mission UI)
Documentation=file://$REPO_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/app.py
Environment=MISSION_PORT=$PORT
Environment=MISSION_HOST=0.0.0.0
Environment=MISSIONS_DIR=$MISSIONS_DIR
Environment=TMUX_TMPDIR=$TMUX_DIR
Environment=MISSION_LABEL=$LABEL
${TOKEN:+Environment=MISSION_TOKEN=$TOKEN}
Restart=on-failure
RestartSec=2

NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$MISSIONS_DIR $REPO_DIR $WORKTREES_DIR $TMUX_DIR
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
}

render_console_unit() {
  cat <<EOF
[Unit]
Description=Claude Console (ttyd -> tmux -> claude, per-mission, for the Mission Dashboard)
Documentation=file://$REPO_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$HOME_DIR
Environment=MISSIONS_DIR=$MISSIONS_DIR
Environment=TMUX_TMPDIR=$TMUX_DIR
ExecStart=/usr/bin/ttyd --port $CONSOLE_PORT --interface 0.0.0.0 --writable --url-arg \\
  --credential $CONSOLE_USER:$CONSOLE_PASS \\
  --client-option fontSize=14 --client-option "titleFixed=Claude Console" \\
  --client-option 'theme={"background": "#000000"}' \\
  --client-option disableLeaveAlert=true \\
  $REPO_DIR/console-launch.sh
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
}

# --- write a unit file (or print under dry-run) ------------------------------
install_unit() {
  local name="$1" content="$2" dest="/etc/systemd/system/$1"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] would write $dest:"
    printf '%s\n' "$content" | sed 's/^/      | /'
  else
    printf '%s\n' "$content" > "$dest"
    chmod 0644 "$dest"
    echo "  wrote $dest"
  fi
}

# ============================================================================
echo "==> 1. preflight — required tools"
detect_pkg_mgr
echo "  package manager: ${PKG_MGR:-<none detected>}"

# The app itself is Python-3 stdlib only, run by /usr/bin/python3.
require_tool python3

if [[ "$ENABLE_CONSOLE" -eq 1 ]]; then
  # ttyd lives in EPEL on RHEL/Alma/Rocky — enable it before trying to install.
  if [[ ( "$PKG_MGR" == dnf || "$PKG_MGR" == yum ) ]] && ! command -v ttyd >/dev/null 2>&1; then
    run "$PKG_MGR" install -y epel-release || true   # harmless if absent/already-on
  fi
  require_tool ttyd
  require_tool tmux
  # 'claude' (Claude Code CLI) is NOT a distro package and is usually installed
  # per-user, so check it on the SERVICE account's PATH and fail loudly if absent
  # — this is exactly the "console refused to connect" trap when it's missing.
  if ! svc_has_cmd claude; then
    die "the 'claude' CLI (Claude Code) is not on PATH for user '$APP_USER'.
    The console runs 'claude' per mission, so install it AS THAT USER (not root):
        curl -fsSL https://claude.ai/install.sh | bash
    or via npm:  npm install -g @anthropic-ai/claude-code
    then re-run this setup."
  fi
  echo "  ok: claude  (on PATH for '$APP_USER')"
fi

echo "==> 2. systemd units"
install_unit "mission-dashboard.service" "$(render_dashboard_unit)"
[[ "$ENABLE_CONSOLE" -eq 1 ]] && install_unit "claude-console.service" "$(render_console_unit)"
[[ "$ENABLE_CONSOLE" -eq 1 ]] && run chmod 0755 "$REPO_DIR/console-launch.sh"

echo "==> 3. enable + start services"
run systemctl daemon-reload
run systemctl enable --now mission-dashboard.service
[[ "$ENABLE_CONSOLE" -eq 1 ]] && run systemctl enable --now claude-console.service

echo
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete — nothing was changed. Re-run without --dry-run (as root) to apply."
else
  echo "Done. Dashboard: http://<this-host>:$PORT/"
  echo "  systemctl status mission-dashboard$([[ "$ENABLE_CONSOLE" -eq 1 ]] && echo ' claude-console')"
fi
