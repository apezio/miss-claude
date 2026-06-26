#!/usr/bin/env bash
# setup.sh — one-command installer for Miss Claude (the Mission Dashboard).
#
# Renders the systemd units with YOUR user/paths, opens the firewall to your
# admin IPs, optionally installs the in-browser Claude console (ttyd + tmux),
# and enables both services. Run as root:
#
#   sudo bash setup.sh --ip 203.0.113.10 --ip 198.51.100.20
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
ENABLE_FIREWALL=1
CONSOLE_USER=""
CONSOLE_PASS=""
DRY_RUN=0
declare -a ADMIN_IPS=()

usage() {
  cat <<'EOF'
Usage: sudo bash setup.sh [options]

  --user USER          account that runs the services (default: invoking user)
  --ip IP              admin source IP allowed through the firewall (repeatable)
  --port N             dashboard port (default 4200)
  --label TEXT         short label shown in the UI header (default: hostname)
  --token TOKEN        enable app token auth (default: none — firewall only)
  --no-console         do not install the ttyd Claude console
  --console-port N     console port (default 4201)
  --console-user USER  ttyd basic-auth username (default: --user)
  --console-pass PASS  ttyd basic-auth password (prompted if not given)
  --no-firewall        skip the firewalld rules (e.g. you manage the firewall yourself)
  --dry-run            print what would happen; change nothing
  -h, --help           this help

Examples:
  sudo bash setup.sh --ip 203.0.113.10 --ip 198.51.100.20
  sudo bash setup.sh --dry-run --ip 203.0.113.10 --no-console
EOF
}

# --- parse flags -------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)         APP_USER="$2"; shift 2;;
    --ip)           ADMIN_IPS+=("$2"); shift 2;;
    --port)         PORT="$2"; shift 2;;
    --label)        LABEL="$2"; shift 2;;
    --token)        TOKEN="$2"; shift 2;;
    --no-console)   ENABLE_CONSOLE=0; shift;;
    --console-port) CONSOLE_PORT="$2"; shift 2;;
    --console-user) CONSOLE_USER="$2"; shift 2;;
    --console-pass) CONSOLE_PASS="$2"; shift 2;;
    --no-firewall)  ENABLE_FIREWALL=0; shift;;
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
if [[ "$ENABLE_FIREWALL" -eq 1 && ${#ADMIN_IPS[@]} -eq 0 ]]; then
  if have_tty; then
    echo "Enter the admin source IP(s) allowed to reach the dashboard (space-separated)."
    echo "These are the addresses you already trust for SSH. Example: 203.0.113.10 198.51.100.20"
    read -r -p "Admin IPs: " -a ADMIN_IPS
  fi
  [[ ${#ADMIN_IPS[@]} -gt 0 ]] || die "no admin IPs given (use --ip, or --no-firewall to skip)"
fi

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
if [[ "$ENABLE_FIREWALL" -eq 1 ]]; then
  echo "  firewall IPs:  ${ADMIN_IPS[*]}"
else
  echo "  firewall:      skipped (managed elsewhere)"
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

# --- firewall ----------------------------------------------------------------
open_port() {
  local p="$1" ip
  for ip in "${ADMIN_IPS[@]}"; do
    run firewall-cmd --permanent \
      --add-rich-rule="rule family=ipv4 source address=$ip port port=$p protocol=tcp accept"
  done
}

# ============================================================================
echo "==> 1. systemd units"
install_unit "mission-dashboard.service" "$(render_dashboard_unit)"
[[ "$ENABLE_CONSOLE" -eq 1 ]] && install_unit "claude-console.service" "$(render_console_unit)"

if [[ "$ENABLE_CONSOLE" -eq 1 ]]; then
  echo "==> 2. console prerequisites (ttyd, tmux)"
  if command -v ttyd >/dev/null && command -v tmux >/dev/null; then
    echo "  ttyd + tmux already present"
  elif command -v dnf >/dev/null; then
    run dnf install -y ttyd tmux
  elif command -v apt-get >/dev/null; then
    run apt-get update && run apt-get install -y ttyd tmux
  else
    echo "  WARNING: install 'ttyd' and 'tmux' yourself (no dnf/apt found)"
  fi
  run chmod 0755 "$REPO_DIR/console-launch.sh"
fi

if [[ "$ENABLE_FIREWALL" -eq 1 ]]; then
  echo "==> 3. firewall (open ports to admin IPs only)"
  if command -v firewall-cmd >/dev/null; then
    open_port "$PORT"
    [[ "$ENABLE_CONSOLE" -eq 1 ]] && open_port "$CONSOLE_PORT"
    run firewall-cmd --reload
  else
    echo "  WARNING: firewall-cmd not found — open port $PORT (and $CONSOLE_PORT) to your admin IPs yourself."
  fi
fi

echo "==> 4. enable + start services"
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
