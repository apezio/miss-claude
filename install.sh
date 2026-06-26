#!/usr/bin/env bash
# Mission Dashboard — install firewall rule + systemd service.
# Run once as root:   sudo bash ~/mission-dashboard/install.sh
set -euo pipefail

PORT=4200
# EDIT THESE: the source IPs allowed to reach the dashboard (your admin/VPN addresses,
# the same ones your SSH allowlist trusts). The values below are RFC-5737 documentation
# placeholders — replace them before running, and do NOT open the port to 0.0.0.0/0.
ADMIN_IPS=(203.0.113.10 198.51.100.20 198.51.100.30)   # SSH admin allowlist
UNIT=mission-dashboard.service
# Resolve the unit file next to this script, so install.sh works wherever the repo lives.
SRC="$(dirname "$(readlink -f "$0")")/$UNIT"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root:  sudo bash $0" >&2
  exit 1
fi

echo "==> 1/3  Opening firewalld port $PORT/tcp to admin VPN IPs only"
for ip in "${ADMIN_IPS[@]}"; do
  firewall-cmd --permanent \
    --add-rich-rule="rule family=ipv4 source address=$ip port port=$PORT protocol=tcp accept"
done
firewall-cmd --reload
echo "    rich rules for $PORT:"
firewall-cmd --list-rich-rules | grep "port=\"$PORT\"" || true

echo "==> 2/3  Installing systemd unit"
install -m 0644 "$SRC" /etc/systemd/system/$UNIT
systemctl daemon-reload

echo "==> 3/3  Enabling + starting service"
# Free the port if a foreground/dev copy is holding it, so the unit can bind.
pkill -f 'mission-dashboard/app.py' 2>/dev/null || true
sleep 1
systemctl enable --now $UNIT

echo
echo "==> Done. Status:"
systemctl --no-pager --full status $UNIT | head -12
