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
TLS=1
REDIRECT_PORT=4202
LABEL="$(hostname -s 2>/dev/null || hostname)"
TOKEN=""
ENABLE_CONSOLE=1
CONSOLE_AUTH=1
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
  --no-console-auth    install the console with NO basic auth. Use this if you
                       open the dashboard from a PHONE: ttyd enforces basic auth
                       on the WebSocket upgrade too, and WebKit browsers (every
                       iOS browser, and Safari on macOS) never attach cached
                       basic credentials to a WebSocket handshake — the console
                       page loads, the socket is refused, and you get an endless
                       "Press ⏎ to Reconnect". Only do this where the port is
                       already restricted (firewall/VPN); it leaves the console
                       as open as the dashboard itself.
  --no-tls             serve plain http (default: https, generating a local CA
                       + certificate with scripts/make-certs.sh)
  --redirect-port N    port that 301s http -> https (default 4202; 0 disables)
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
    --no-console-auth) CONSOLE_AUTH=0; shift;;
    --no-tls)       TLS=0; shift;;
    --redirect-port) REDIRECT_PORT="$2"; shift 2;;
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
TLS_DIR="$HOME_DIR/.miss-claude/tls"
CONSOLE_INDEX="$HOME_DIR/.miss-claude/ttyd-index.html"
WORKTREES_DIR="$HOME_DIR/missclaude-worktrees"
TMUX_DIR="$HOME_DIR/.tmux-console"
[[ -f "$REPO_DIR/app.py" ]] || die "app.py not found in $REPO_DIR (run this script from the repo)"

# --- interactive fill-ins ----------------------------------------------------
[[ -n "$CONSOLE_USER" ]] || CONSOLE_USER="$APP_USER"
if [[ "$CONSOLE_AUTH" -eq 0 ]]; then
  # --no-console-auth wins over a password given on the same command line, rather than
  # quietly installing the credential the flag says not to install.
  [[ -n "$CONSOLE_PASS" ]] && echo "note: --no-console-auth given — ignoring --console-pass" >&2
  CONSOLE_PASS=""
fi
if [[ "$ENABLE_CONSOLE" -eq 1 && "$CONSOLE_AUTH" -eq 1 && -z "$CONSOLE_PASS" && "$DRY_RUN" -eq 0 ]]; then
  if have_tty; then
    read -r -s -p "ttyd console basic-auth password for '$CONSOLE_USER': " CONSOLE_PASS; echo
    [[ -n "$CONSOLE_PASS" ]] || die "console password cannot be empty (or pass --no-console)"
  else
    die "console enabled but no --console-pass given (and no TTY to prompt)"
  fi
fi
[[ "$DRY_RUN" -eq 1 && "$CONSOLE_AUTH" -eq 1 && -z "$CONSOLE_PASS" ]] && CONSOLE_PASS="<prompted-at-install>"

# --- summary -----------------------------------------------------------------
echo
echo "Miss Claude setup"
echo "  repo dir:      $REPO_DIR"
echo "  run as user:   $APP_USER  (home: $HOME_DIR)"
echo "  dashboard:     port $PORT   label '${LABEL:-<none>}'   token: $([[ -n "$TOKEN" ]] && echo set || echo none)"
echo "  missions dir:  $MISSIONS_DIR"
if [[ "$TLS" -eq 1 ]]; then
  echo "  tls:           https, certs in $TLS_DIR$([[ "$REDIRECT_PORT" != 0 ]] && echo "   http->https on port $REDIRECT_PORT")"
else
  echo "  tls:           disabled (plain http)"
fi
if [[ "$ENABLE_CONSOLE" -eq 1 && "$CONSOLE_AUTH" -eq 1 ]]; then
  echo "  console:       port $CONSOLE_PORT   ttyd user '$CONSOLE_USER'"
  echo "                 (basic auth ON — the console will NOT work from an iPhone/iPad or"
  echo "                  Safari; pass --no-console-auth for a phone-usable console)"
elif [[ "$ENABLE_CONSOLE" -eq 1 ]]; then
  echo "  console:       port $CONSOLE_PORT   no basic auth (phone-usable; port must be firewalled)"
else
  echo "  console:       disabled"
fi
[[ "$DRY_RUN" -eq 1 ]] && echo "  MODE:          DRY RUN — nothing will be changed"
echo

# --- unit renderers (emit to stdout) ----------------------------------------
# ttyd's TLS flags, or empty for --no-tls. Kept as one pre-built string (with a
# leading space) so the ExecStart line renders identically either way — building it
# inline in the heredoc mangles the backslash continuations.
TTYD_SSL_ARGS=""
[[ "$TLS" -eq 1 ]] && TTYD_SSL_ARGS=" --ssl --ssl-cert $TLS_DIR/server.crt --ssl-key $TLS_DIR/server.key"

# ttyd's basic-auth flag, or empty for --no-console-auth. Same one-string trick as the SSL
# args above, and for a second reason here: it must not become a blank continuation LINE
# when disabled — an empty line inside ExecStart's backslash continuation truncates the
# command. See --no-console-auth in usage() for why a phone needs this off.
TTYD_CRED_ARG=""
[[ "$CONSOLE_AUTH" -eq 1 ]] && TTYD_CRED_ARG=" --credential $CONSOLE_USER:$CONSOLE_PASS"

# ttyd's custom-page flag: ttyd's own index.html plus the injected wheel fix (see
# scripts/make-console-index.sh). Same one-string trick again. Declared here with the
# other ttyd args but FILLED IN BY STEP 1, once that page has actually been built: ttyd
# refuses to start when --index names a missing file, and a console with the scroll bug
# beats no console at all.
TTYD_INDEX_ARG=""

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
$([[ "$TLS" -eq 1 ]] && cat <<TLSENV
Environment=MISSION_TLS_CERT=$TLS_DIR/server.crt
Environment=MISSION_TLS_KEY=$TLS_DIR/server.key
Environment=MISSION_REDIRECT_PORT=$REDIRECT_PORT
TLSENV
)
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
$(if [[ "$TLS" -eq 1 ]]; then cat <<TLSENV
Environment=MISSION_SELF_URL=https://127.0.0.1:$PORT
Environment=MISSION_TLS_CA=$TLS_DIR/ca.crt
TLSENV
else cat <<PLAINENV
# Stated explicitly, not left to be inferred: certificates in $TLS_DIR can outlive a
# --no-tls reinstall, and a mission-doc hook that guessed from their presence would
# hand the model an https + --cacert curl that this http dashboard refuses.
Environment=MISSION_SELF_URL=http://127.0.0.1:$PORT
PLAINENV
fi)
$(if [[ "$CONSOLE_AUTH" -eq 1 ]]; then cat <<'AUTHNOTE'
# --credential is BASIC AUTH, and ttyd enforces it on the WebSocket upgrade as well as on
# the page. WebKit browsers (every iOS browser; Safari on macOS) never attach cached basic
# credentials to a WebSocket handshake, so on those the console page loads and then loops
# on "Press ⏎ to Reconnect" forever. Reinstall with --no-console-auth (or just delete the
# --credential argument below and restart) to make the console usable from a phone; the
# port is then as open as the dashboard's own — keep it firewalled.
AUTHNOTE
else cat <<'NOAUTHNOTE'
# Installed with --no-console-auth: NO --credential, deliberately. ttyd enforces basic auth
# on the WebSocket upgrade too, and WebKit browsers (every iOS browser; Safari on macOS)
# never send cached basic credentials on a WebSocket handshake — with a credential set, a
# phone loads the console page and then loops on "Press ⏎ to Reconnect" forever. This port
# is therefore protected only by the firewall, exactly like the dashboard's own port.
NOAUTHNOTE
fi)
$(if [[ -n "$TTYD_INDEX_ARG" ]]; then cat <<'INDEXNOTE'
# --index below is ttyd's own page + scripts/console-wheel-fix.js, which keeps a two-finger
# trackpad scroll over the terminal from being translated into Up/Down keypresses at Claude.
# ExecStartPre rebuilds it when ttyd is upgraded (a no-op otherwise, and '-' = never block
# the start). NOTE ttyd refuses to start if the --index file is missing: drop both lines to
# go back to ttyd's built-in page.
INDEXNOTE
echo "ExecStartPre=-/usr/bin/bash $REPO_DIR/scripts/make-console-index.sh --out $CONSOLE_INDEX"
fi)
ExecStart=/usr/bin/ttyd --port $CONSOLE_PORT --interface 0.0.0.0 --writable --url-arg$TTYD_SSL_ARGS$TTYD_CRED_ARG$TTYD_INDEX_ARG \\
  --client-option fontSize=14 --client-option "titleFixed=Claude Console" \\
  --client-option 'theme={"background": "#000000"}' \\
  --client-option disableLeaveAlert=true \\
  $REPO_DIR/console-launch.sh
Restart=on-failure
RestartSec=2
# tmux is the persistence layer and is spawned into THIS unit's cgroup, so the default
# KillMode=control-group would SIGTERM the tmux server — and every Claude session under
# it — on any stop/restart. Kill only ttyd (the MainPID) and leave tmux running, so a
# reconnect re-attaches the same sessions. Without this line, a systemctl restart of
# claude-console silently destroys every running mission.
KillMode=process

# NOTE: deliberately NOT sandboxed like mission-dashboard.service. This is an interactive
# admin shell that runs ssh/sudo/claude and writes ~/.claude, so ProtectSystem=strict /
# NoNewPrivileges would break it. The firewall pin$([[ "$CONSOLE_AUTH" -eq 1 ]] && echo " + basic-auth") is the control.

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

  # The console's page: ttyd's own index.html + scripts/console-wheel-fix.js, so a
  # two-finger trackpad scroll over the terminal scrolls the dashboard page instead of
  # typing Up/Down at Claude. Built as the SERVICE account (it lands next to the certs in
  # its ~/.miss-claude). Best-effort: a console without the fix beats no console, and the
  # unit only passes --index when the file actually exists.
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] would build $CONSOLE_INDEX from ttyd's own page"
    TTYD_INDEX_ARG=" --index $CONSOLE_INDEX"
  elif runuser -u "$APP_USER" -- env MISS_STATE_DIR="$HOME_DIR/.miss-claude" \
         bash "$REPO_DIR/scripts/make-console-index.sh" >/dev/null 2>&1; then
    echo "  ok: console page ($CONSOLE_INDEX)"
    TTYD_INDEX_ARG=" --index $CONSOLE_INDEX"
  else
    echo "  WARNING: could not build $CONSOLE_INDEX — the console falls back to ttyd's"
    echo "           stock page, where a trackpad scroll over the terminal types Up/Down"
    echo "           at Claude. See why with:"
    echo "           runuser -u $APP_USER -- bash $REPO_DIR/scripts/make-console-index.sh"
  fi
fi

echo "==> 2. TLS certificate"
if [[ "$TLS" -eq 1 ]]; then
  require_tool openssl
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] would run scripts/make-certs.sh as '$APP_USER' -> $TLS_DIR"
  elif [[ -f "$TLS_DIR/server.crt" && -f "$TLS_DIR/server.key" ]]; then
    echo "  ok: certificate already present ($TLS_DIR/server.crt)"
    echo "      refresh or add names with: sudo -u $APP_USER bash $REPO_DIR/scripts/make-certs.sh"
  else
    # As the SERVICE account, not root: the services read these files as $APP_USER,
    # and the private keys are 600.
    runuser -u "$APP_USER" -- env MISS_TLS_DIR="$TLS_DIR" \
      bash "$REPO_DIR/scripts/make-certs.sh" >/dev/null \
      || die "certificate generation failed (run scripts/make-certs.sh by hand to see why)"
    echo "  generated $TLS_DIR/server.crt (CA: $TLS_DIR/ca.crt)"
  fi
else
  echo "  skipped (--no-tls): the dashboard and console will serve plain http"
fi

echo "==> 3. systemd units"
install_unit "mission-dashboard.service" "$(render_dashboard_unit)"
[[ "$ENABLE_CONSOLE" -eq 1 ]] && install_unit "claude-console.service" "$(render_console_unit)"
[[ "$ENABLE_CONSOLE" -eq 1 ]] && run chmod 0755 "$REPO_DIR/console-launch.sh"

echo "==> 4. enable + start services"
run systemctl daemon-reload
run systemctl enable --now mission-dashboard.service
[[ "$ENABLE_CONSOLE" -eq 1 ]] && run systemctl enable --now claude-console.service

echo
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete — nothing was changed. Re-run without --dry-run (as root) to apply."
else
  if [[ "$TLS" -eq 1 ]]; then
    echo "Done. Dashboard: https://<this-host>:$PORT/"
    echo
    echo "  LAST STEP — install the CA on the machine you browse from, or the browser will"
    echo "  warn on the dashboard and silently blank the console pane:"
    echo "      scp <this-host>:$TLS_DIR/ca.crt ."
    echo "  then import ca.crt as a trusted authority (Firefox/Chrome: Certificates >"
    echo "  Authorities > Import; macOS: Keychain Access > Always Trust)."
    [[ "$REDIRECT_PORT" != 0 ]] && echo "  http://<this-host>:$REDIRECT_PORT/ redirects to https."
  else
    echo "Done. Dashboard: http://<this-host>:$PORT/"
  fi
  echo "  systemctl status mission-dashboard$([[ "$ENABLE_CONSOLE" -eq 1 ]] && echo ' claude-console')"
fi
