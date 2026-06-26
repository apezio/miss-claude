#!/usr/bin/env bash
# Mission Dashboard — install the systemd service.
# (Edit mission-dashboard.service first: set User=/Group= and the paths.)
# Run once as root:   sudo bash install.sh
#
# Prefer setup.sh, which renders the unit with your user/paths automatically.
set -euo pipefail

UNIT=mission-dashboard.service
# Resolve the unit file next to this script, so install.sh works wherever the repo lives.
SRC="$(dirname "$(readlink -f "$0")")/$UNIT"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root:  sudo bash $0" >&2
  exit 1
fi

echo "==> 1/2  Installing systemd unit"
install -m 0644 "$SRC" /etc/systemd/system/$UNIT
systemctl daemon-reload

echo "==> 2/2  Enabling + starting service"
# Free the port if a foreground/dev copy is holding it, so the unit can bind.
pkill -f 'mission-dashboard/app.py' 2>/dev/null || true
sleep 1
systemctl enable --now $UNIT

echo
echo "==> Done. Status:"
systemctl --no-pager --full status $UNIT | head -12
