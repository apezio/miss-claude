#!/usr/bin/env bash
# make-certs.sh — generate the local TLS mini-CA + server certificate that Miss
# Claude serves HTTPS with. openssl only: no internet, no ACME, no new packages.
#
# WHY A CA AND NOT A BARE SELF-SIGNED CERT
# The dashboard (4200) iframes the ttyd console (4201). Those are two separate
# ORIGINS, and a browser silently refuses to load an iframe whose certificate it
# doesn't trust — no warning, no click-through, just a dead console pane. With one
# CA installed in the browser, BOTH origins are trusted outright: the console works
# and neither port nags. That is the whole reason for the extra layer.
#
# Usage:
#   bash scripts/make-certs.sh                     # create (or refresh) the certs
#   bash scripts/make-certs.sh --san dash.example --san 10.0.0.5   # extra names/IPs
#   bash scripts/make-certs.sh --force-ca          # start over with a NEW CA
#                                                  # (must be re-installed in the browser)
#
# Output ($MISS_TLS_DIR, default ~/.miss-claude/tls):
#   ca.crt      install THIS in your browser/OS trust store. Public, safe to copy.
#   ca.key      600. Anyone holding it can mint certs your browser trusts — keep it here.
#   server.crt  what app.py (MISSION_TLS_CERT) and ttyd (--ssl-cert) serve.
#   server.key  600, the matching private key.
#
# Re-running reuses the existing CA and only re-issues the leaf, so certificates can
# be renewed or given new SANs WITHOUT re-installing anything in the browser.
#
# Part of the Mission Dashboard (see app.py / README.md).
set -euo pipefail

TLS_DIR="${MISS_TLS_DIR:-$HOME/.miss-claude/tls}"
CA_DAYS="${MISS_CA_DAYS:-3650}"     # 10y — installed once, don't churn it
LEAF_DAYS="${MISS_LEAF_DAYS:-825}"  # 825d is the longest browsers accept without complaint
FORCE_CA=0
EXTRA_SANS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --san)      EXTRA_SANS+=("$2"); shift 2;;
    --force-ca) FORCE_CA=1; shift;;
    --dir)      TLS_DIR="$2"; shift 2;;
    -h|--help)  sed -n '2,26p' "$0"; exit 0;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 1;;
  esac
done

command -v openssl >/dev/null 2>&1 || { echo "MISSING: openssl" >&2; exit 1; }

mkdir -p "$TLS_DIR"
chmod 700 "$TLS_DIR"
cd "$TLS_DIR"

# --- collect the names/IPs the cert must cover -------------------------------------
# A cert is only valid for the exact name typed in the URL bar, so cover every way
# this box gets reached: FQDN, short name, localhost, and each of its own IPv4s.
# Chrome/Firefox ignore CN entirely and match SANs only — hence the long list.
names=(localhost)
ips=(127.0.0.1)
fqdn="$(hostname -f 2>/dev/null || hostname)"
short="$(hostname -s 2>/dev/null || true)"
[[ -n "$fqdn"  ]] && names+=("$fqdn")
[[ -n "$short" && "$short" != "$fqdn" ]] && names+=("$short")
# hostname -I lists every configured IPv4/IPv6; keep the v4s (browsers reach this box by IP).
for ip in $(hostname -I 2>/dev/null || true); do
  [[ "$ip" == *:* ]] && continue
  ips+=("$ip")
done
for extra in ${EXTRA_SANS+"${EXTRA_SANS[@]}"}; do
  if [[ "$extra" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then ips+=("$extra"); else names+=("$extra"); fi
done

# de-dup, then render the SAN list openssl wants (DNS.1=..., IP.1=...)
san_lines=""; n=0
for v in $(printf '%s\n' "${names[@]}" | awk '!seen[$0]++'); do
  n=$((n+1)); san_lines+="DNS.$n = $v"$'\n'
done
n=0
for v in $(printf '%s\n' "${ips[@]}" | awk '!seen[$0]++'); do
  n=$((n+1)); san_lines+="IP.$n = $v"$'\n'
done

# --- the CA: created once, reused forever ------------------------------------------
if [[ $FORCE_CA -eq 1 ]] && [[ -e ca.key || -e ca.crt ]]; then
  echo "==> --force-ca: replacing the existing CA (re-install ca.crt in your browser afterwards)"
  rm -f ca.key ca.crt ca.srl
fi
if [[ -f ca.key && -f ca.crt ]]; then
  echo "==> Reusing existing CA ($TLS_DIR/ca.crt) — no browser re-install needed."
else
  echo "==> Creating the Miss Claude local CA (valid ${CA_DAYS}d)"
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days "$CA_DAYS" \
    -keyout ca.key -out ca.crt \
    -subj "/CN=Miss Claude local CA ($fqdn)/O=Miss Claude" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
fi

# --- the leaf: re-issued on every run (renewal / new SANs, same trusted CA) ---------
echo "==> Issuing the server certificate (valid ${LEAF_DAYS}d)"
ext_file="$(mktemp)"
trap 'rm -f "$ext_file" leaf.csr' EXIT
cat > "$ext_file" <<EOF
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
$san_lines
EOF

openssl req -newkey rsa:2048 -nodes -sha256 \
  -keyout server.key.new -out leaf.csr -subj "/CN=$fqdn" 2>/dev/null
openssl x509 -req -in leaf.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days "$LEAF_DAYS" -sha256 -extfile "$ext_file" -out server.crt.new 2>/dev/null

# Swap in only once BOTH halves exist, so a failed run can't leave a live service
# pointing at a key that doesn't match its certificate.
mv server.key.new server.key
mv server.crt.new server.crt
chmod 600 ca.key server.key
chmod 644 ca.crt server.crt

echo
echo "Certificates written to $TLS_DIR:"
printf '  %s\n' "ca.crt      <- install in your browser/OS trust store" \
                "server.crt  MISSION_TLS_CERT / ttyd --ssl-cert" \
                "server.key  MISSION_TLS_KEY  / ttyd --ssl-key"
echo
echo "Valid for:"
printf '%s' "$san_lines" | sed 's/^/  /'
echo
echo "Install the CA on the machine you browse FROM (copy ca.crt to it first):"
echo "  Firefox : Settings > Privacy & Security > Certificates > View Certificates"
echo "            > Authorities > Import > tick \"Trust this CA to identify websites\""
echo "  Chrome  : Settings > Privacy and security > Security > Manage certificates"
echo "            > Authorities > Import"
echo "  macOS   : open ca.crt in Keychain Access (System) > set to \"Always Trust\""
echo "  Linux   : sudo cp ca.crt /etc/pki/ca-trust/source/anchors/miss-claude-ca.crt"
echo "            && sudo update-ca-trust      # covers curl; browsers need the steps above"
