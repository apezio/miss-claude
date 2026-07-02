#!/usr/bin/env bash
# ship-rails.sh <host> — copy the Miss Claude dev GUARD to a remote host and VERIFY it.
#
# A REMOTE dev mission runs Claude in a worktree ON the remote with
# --dangerously-skip-permissions, so its PreToolUse guardrail (prevent-misswork.py) must
# live on the remote too. This copies that hook + the settings that wires it
# (miss-rails.settings.json) to ~/.miss-claude/ on <host>, then verifies the hook is
# present and parses under the remote python3.
#
# FAIL-CLOSED: exits 0 ONLY when the guard is confirmed installed + runnable. Callers
# (app.py ensure_remote_rails at spawn; console-launch.sh at every remote-dev launch)
# MUST refuse to launch the dangerous remote Claude on any non-zero exit. Idempotent —
# safe to re-run on every launch (re-ships if a host was wiped/re-provisioned).
#
# Part of the Mission Dashboard (see app.py / CLAUDE.md). Modern hosts only: it runs
# ssh/scp directly (no OPENSSL_CONF legacy-SHA1 shim), matching the remote console.
set -uo pipefail

host="${1:-}"
if [[ -z "$host" ]]; then
  echo "ship-rails: missing host argument" >&2
  exit 2
fi
# Same allow-list as app.py REMOTE_HOST_RE / console-launch.sh (ssh alias, host, user@host).
if [[ ! "$host" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$ ]]; then
  echo "ship-rails: invalid host '$host'" >&2
  exit 2
fi

here="$(dirname "$(readlink -f "$0")")"   # scripts/
appdir="$(dirname "$here")"               # repo root (primary checkout in production)
hook="$appdir/.claude/hooks/prevent-misswork.py"
settings="$appdir/miss-rails.settings.json"
rolectx="$here/miss-role-context.py"
for f in "$hook" "$settings" "$rolectx"; do
  if [[ ! -f "$f" ]]; then
    echo "ship-rails: missing local bundle file: $f" >&2
    exit 2
  fi
done

# 1) ensure the (private) bundle dir, 2) copy the bundle into it. miss-role-context.py
# is the SessionStart role-rules injector (behavioural rails for repos whose CLAUDE.md
# doesn't carry the workflow); the settings file wires it via $MISS_ROLE_CONTEXT.
if ! ssh "$host" 'mkdir -p ~/.miss-claude && chmod 700 ~/.miss-claude'; then
  echo "ship-rails: could not create ~/.miss-claude on $host (ssh failed?)" >&2
  exit 3
fi
if ! scp -q "$hook" "$settings" "$rolectx" "$host:.miss-claude/"; then
  echo "ship-rails: scp of the guard bundle to $host failed" >&2
  exit 3
fi

# 3) VERIFY on the remote: the hook is present and parses under the remote python3, and
# the settings file is valid JSON. The script is fed on stdin to remote `python3 -`, so
# nothing needs quoting through the shell. Prints OK only when both checks pass.
verify_out="$(ssh "$host" python3 - <<'PY'
import ast, json, os
base = os.path.expanduser("~/.miss-claude")
ast.parse(open(os.path.join(base, "prevent-misswork.py")).read())
ast.parse(open(os.path.join(base, "miss-role-context.py")).read())
json.load(open(os.path.join(base, "miss-rails.settings.json")))
print("OK")
PY
)"
if [[ "$verify_out" != *OK* ]]; then
  echo "ship-rails: guard verification failed on $host (python3 present? hook intact?)" >&2
  exit 4
fi
exit 0
