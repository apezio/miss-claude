#!/usr/bin/env bash
# make-console-index.sh — build the console's index.html: ttyd's OWN page plus two
# small injected scripts, served via ttyd --index:
#   scripts/console-wheel-fix.js    trackpad scroll must not type Up/Down at Claude
#   scripts/console-drop-relay.js   a file dropped on the terminal goes to the mission
#                                   page's upload, instead of the browser opening it
#
# WHY THIS EXISTS
# Claude runs on the terminal's alternate screen. xterm.js translates a wheel gesture
# over a buffer with no scrollback into ESC [ A / ESC [ B, so a two-finger trackpad
# scroll over the console types Up/Down at Claude and cycles its prompt history. The fix
# is one call to xterm's own attachCustomWheelEventHandler() (see console-wheel-fix.js)
# — but the console is a CROSS-ORIGIN iframe (port 4201), so the dashboard page cannot
# reach into it. ttyd's --index is the supported way to get the line in there.
#
# HOW
# ttyd's page is embedded in the binary, so we ask a throwaway ttyd for it: one is
# started on a UNIX SOCKET in a temp dir (no port to collide with the live console, and
# nothing listening on the network), its "/" is fetched, the script is inserted before
# </body>, and the result is written atomically. The live claude-console.service is never
# touched. Client options (--client-option ...) are delivered over the websocket, NOT
# baked into the page, so this copy stays correct when those change.
#
# It IS coupled to the ttyd version, so the generated file carries a stamp (ttyd version
# + a hash of the injected scripts) and a re-run is a no-op unless either changed.
# claude-console.service re-runs this as ExecStartPre, which picks up a ttyd upgrade;
# ttyd reads the --index file per request, so a plain re-run after editing a script is
# live on the next console load, no restart. --force rebuilds regardless.
#
# Usage:
#   bash scripts/make-console-index.sh              # create/refresh if needed
#   bash scripts/make-console-index.sh --force      # rebuild unconditionally
#   bash scripts/make-console-index.sh --out FILE   # write somewhere else
#
# Output ($MISS_STATE_DIR, default ~/.miss-claude):
#   ttyd-index.html   pass it to ttyd as --index. Deleting it is safe: without --index
#                     ttyd serves its built-in page (and both bugs come back).
#
# Part of the Mission Dashboard (see app.py / README.md).
set -euo pipefail

here="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# ${HOME:-...} because this also runs as a systemd ExecStartPre, where the environment is
# whatever the unit gives it (and --out is passed there anyway, so STATE_DIR goes unused).
STATE_DIR="${MISS_STATE_DIR:-${HOME:-$PWD}/.miss-claude}"
OUT="$STATE_DIR/ttyd-index.html"
SNIPPETS=("$here/scripts/console-wheel-fix.js" "$here/scripts/console-drop-relay.js")
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift;;
    --out)   OUT="$2"; shift 2;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

command -v ttyd >/dev/null 2>&1 || { echo "make-console-index: ttyd not found on PATH" >&2; exit 1; }
for f in "${SNIPPETS[@]}"; do
  [[ -f "$f" ]] || { echo "make-console-index: missing $f" >&2; exit 1; }
done

# Stamp: "<!-- miss-claude console page (ttyd 1.7.7, scripts 0123abcd4567) -->". Rebuild
# when the installed ttyd or the injected scripts no longer match the current file.
ttyd_ver="$(ttyd --version 2>/dev/null | awk '{print $NF}')"
[[ -n "$ttyd_ver" ]] || ttyd_ver="unknown"
js_hash="$(cat "${SNIPPETS[@]}" | sha256sum | cut -c1-12)"
STAMP="<!-- miss-claude console page (ttyd $ttyd_ver, scripts $js_hash) -->"

if [[ "$FORCE" -eq 0 && -f "$OUT" ]] && grep -qF "$STAMP" "$OUT"; then
  echo "make-console-index: $OUT is current (ttyd $ttyd_ver)"
  exit 0
fi

tmp="$(mktemp -d)"
ttyd_pid=""
cleanup() {
  [[ -n "$ttyd_pid" ]] && kill "$ttyd_pid" 2>/dev/null
  rm -rf "$tmp"
}
trap cleanup EXIT

sock="$tmp/ttyd.sock"
# `true` is the throwaway ttyd's command; nothing ever connects a websocket to it, we
# only GET the page. Binding a UNIX socket keeps it off the network entirely.
ttyd --interface "$sock" true >"$tmp/ttyd.log" 2>&1 &
ttyd_pid=$!

for _ in $(seq 1 50); do
  [[ -S "$sock" ]] && break
  kill -0 "$ttyd_pid" 2>/dev/null || break
  sleep 0.1
done
if [[ ! -S "$sock" ]]; then
  echo "make-console-index: the throwaway ttyd never created $sock" >&2
  sed 's/^/  ttyd: /' "$tmp/ttyd.log" >&2
  exit 1
fi

# Fetch + inject in one stdlib python3 (no curl dependency, and http.client handles
# chunked/Content-Length framing for us).
python3 - "$sock" "$tmp/index.html" "$STAMP" "${SNIPPETS[@]}" <<'PY'
import http.client, socket, sys

sock_path, out_path, stamp = sys.argv[1:4]
snippet_paths = sys.argv[4:]


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a UNIX socket — same request/response handling, other transport."""

    def __init__(self, path):
        super().__init__("localhost")
        self._unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout if isinstance(self.timeout, (int, float)) else 10)
        s.connect(self._unix_path)
        self.sock = s


conn = UnixHTTPConnection(sock_path)
conn.timeout = 10
conn.request("GET", "/", headers={"Host": "localhost", "Accept-Encoding": "identity"})
resp = conn.getresponse()
if resp.status != 200:
    sys.exit("make-console-index: ttyd answered %s %s" % (resp.status, resp.reason))
html = resp.read().decode("utf-8")
conn.close()

# Sanity-check the page we got is the one the fix targets before shipping it: it must be
# ttyd's terminal page, and it must publish the xterm Terminal as window.term (what
# console-wheel-fix.js attaches to). A silent mismatch would leave a console that looks
# fine and still eats scroll gestures.
for needle in ("</body>", "window.term", "attachCustomWheelEventHandler"):
    if needle not in html:
        sys.exit("make-console-index: ttyd's index.html has no %r — refusing to ship a "
                 "page the wheel fix cannot attach to (ttyd too old or changed?)" % needle)

injected = stamp + "\n"
for path in snippet_paths:
    with open(path, "r", encoding="utf-8") as fh:
        snippet = fh.read()
    if "</script" in snippet.lower():
        sys.exit("make-console-index: %s contains </script — it cannot be inlined" % path)
    injected += "<script>\n%s</script>\n" % snippet

cut = html.rindex("</body>")
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write(html[:cut] + injected + html[cut:])
PY

mkdir -p "$(dirname "$OUT")"
# Atomic: a half-written index.html would stop ttyd from starting at all.
mv -f "$tmp/index.html" "$OUT"
chmod 0644 "$OUT"
echo "make-console-index: wrote $OUT (ttyd $ttyd_ver + wheel fix + drop relay)"
