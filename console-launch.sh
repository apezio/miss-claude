#!/usr/bin/env bash
# console-launch.sh — launched by ttyd as: console-launch.sh <mission>
# (the mission name arrives from the iframe's ?arg=<mission> via ttyd's --url-arg).
#
# Attaches to — or creates — a per-mission tmux session running Claude in that
# mission's directory. tmux is the persistence layer: the session outlives ttyd
# and browser reloads, so reconnecting lands you back in the same live Claude.
#
# The session's command is console-session.sh (which prints the banner and runs
# Claude). Launching it as the session command — rather than typing it with
# send-keys — avoids a race where keystrokes are dropped before the shell is ready.
#
# Part of the Mission Dashboard (see app.py / README.md).
set -uo pipefail

# Shared tmux socket dir (matches claude-console.service + mission-dashboard.service)
# so the sandboxed dashboard can see/kill these sessions. Self-pins for manual runs.
export TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.tmux-console}"

MISSIONS_DIR="${MISSIONS_DIR:-$HOME/missions}"
WORKTREES_DIR="${WORKTREES_DIR:-$HOME/missclaude-worktrees}"
here="$(dirname "$(readlink -f "$0")")"
name="${1:-}"

# === REMOTE CONSOLES (optional side feature — delete this block to remove) =========
# ttyd calls us as: console-launch.sh remote <host> <dir> [name]  (from the dashboard's
# /remote page, ?arg=remote&arg=<host>&arg=<dir>[&arg=<name>]). Wrap an SSH login to
# <host> in a LOCAL tmux session — there is NO tmux on the remote side. Two modes:
#   - NO name: legacy shared console —
#       ssh -tt <host> 'cd <dir> && claude --continue --dangerously-skip-permissions || claude ...'
#     --continue RESUMES the most recent conversation for that dir on the remote (Claude keys
#     history off the cwd); it errors with no history, so we fall back to a fresh session.
#   - WITH a name: a DISTINCT, RESUMABLE console. We derive a deterministic session UUID
#     from host|dir|name (uuidgen v5) and run
#       ssh -tt <host> 'cd <dir> && claude --resume <uuid> ... || claude --session-id <uuid> ...'
#     so a given name always resumes ITS OWN conversation (--resume), creating it with that
#     exact id on first use (--session-id). A different name = a separate conversation.
# --dangerously-skip-permissions matches how the mission consoles launch Claude: this is
#  firewall- + auth-gated admin tooling, so tool calls run without interactive prompts.
# Guard requires a non-empty $2 so a mission literally named "remote" (single arg)
# still falls through to the normal mission path below. Validation mirrors app.py
# (REMOTE_HOST_RE / REMOTE_DIR_RE / REMOTE_NAME_RE) as defense in depth before the values
# hit the command. The name only ever feeds uuidgen's stdin-equivalent --name (never a
# shell command or the tmux session name directly), so its broader charset can't break out.
if [[ "${1:-}" == "remote" && -n "${2:-}" ]]; then
  rhost="$2"; rdir="${3:-}"; rname="${4:-}"
  rhost_re='^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$'
  # Blank dir is valid — `cd ''` below is a no-op, so a fresh SSH login shell just
  # stays at its own $HOME (mirrors the local console's blank-dir-means-home default).
  rdir_re='^(/[A-Za-z0-9 ._/@:+-]{0,255})?$'
  rname_re='^[A-Za-z0-9 ._/@:&()#+-]{1,64}$'
  if [[ ! "$rhost" =~ $rhost_re || ! "$rdir" =~ $rdir_re ]]; then
    echo "Invalid remote host or directory."
    sleep 5; exit 1
  fi
  if [[ -n "$rname" && ! "$rname" =~ $rname_re ]]; then
    echo "Invalid remote console name."
    sleep 5; exit 1
  fi
  C="~/.local/bin/claude"
  if [[ -n "$rname" ]]; then
    # Deterministic, RFC-valid session UUID from host|dir|name — the resume key. uuidgen
    # output is [0-9a-f-] only, so it is safe to interpolate into the remote command.
    sid="$(uuidgen --sha1 --namespace @url --name "$rhost|$rdir|$rname")"
    h="${sid//-/}"; session="remote-${h:0:12}"
    # --resume the name's own session; on first use it doesn't exist yet, so fall back to
    # --session-id to CREATE it with that exact id (next open then resumes it). The {…;}
    # group keeps the fallback inside the successful cd.
    ssh_cmd=$(printf 'ssh -tt %q %q' "$rhost" \
      "cd '$rdir' && { $C --resume $sid --dangerously-skip-permissions || $C --session-id $sid --dangerously-skip-permissions; }")
  else
    # Deterministic session name so reopening the same host+dir RE-ATTACHES the live
    # session instead of spawning a duplicate. If the session is gone, --continue still
    # resumes the prior conversation, so reopening always lands you back where you were.
    rid="$(printf '%s' "$rhost|$rdir" | md5sum | cut -c1-12)"
    session="remote-$rid"
    # cd into the dir, resume the last Claude there, else start fresh. printf %q shell-
    # escapes the whole invocation so tmux's `sh -c` runs it verbatim — no second round of
    # word-splitting. (No `exec` so the `||` fallback can run.)
    ssh_cmd=$(printf 'ssh -tt %q %q' "$rhost" \
      "cd '$rdir' && { $C --continue --dangerously-skip-permissions || $C --dangerously-skip-permissions; }")
  fi
  # Keep the tmux pane ALIVE when ssh exits or fails — mirrors the mission console's
  # `exec bash` tail (console-session.sh). Without this, a host that fails instantly
  # (e.g. an unresolvable alias, refused connection, or a self-connect that exits) makes
  # the session command die immediately, the tmux session vanish, and ttyd reconnect-loop
  # by re-running this launcher forever. Dropping to a LOCAL shell with a clear message
  # leaves a live session for ttyd to attach to, so there is nothing to spin on.
  rhost_q=$(printf '%q' "$rhost")
  remote_cmd="$ssh_cmd; ec=\$?; printf '\n[remote console] connection to %s ended (exit %s).\nYou are now in a LOCAL shell on the jumpbox — close this tab to finish.\n' $rhost_q \"\$ec\"; exec bash --login -i"
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    tmux new-session -d -s "$session" "$remote_cmd"
  fi
  exec tmux attach-session -t "=$session"
fi
# === end REMOTE CONSOLES ==========================================================

# === LOCAL CONSOLE (stateless Claude in a jumpbox dir; no mission folder) ==========
# ttyd calls us as: console-launch.sh local <dir> [name]  (from the Spawn wizard's
# Console + Local dir choice, ?arg=local&arg=<dir>[&arg=<name>]). Like the remote
# console above but with NO ssh — Claude runs LOCALLY in <dir> inside a local tmux
# session. Guard requires a non-empty $2 so a mission literally named "local" still
# falls through to the normal mission path. Validation mirrors app.py (REMOTE_DIR_RE /
# REMOTE_NAME_RE) as defense in depth before the values hit tmux.
if [[ "${1:-}" == "local" && -n "${2:-}" ]]; then
  ldir="$2"; lname="${3:-}"
  ldir_re='^/[A-Za-z0-9 ._/@:+-]{0,255}$'
  lname_re='^[A-Za-z0-9 ._/@:&()#+-]{1,64}$'
  if [[ ! "$ldir" =~ $ldir_re ]]; then
    echo "Invalid local directory."
    sleep 5; exit 1
  fi
  if [[ -n "$lname" && ! "$lname" =~ $lname_re ]]; then
    echo "Invalid local console name."
    sleep 5; exit 1
  fi
  if [[ ! -d "$ldir" ]]; then
    echo "No such directory: $ldir"
    sleep 5; exit 1
  fi
  # Deterministic session name keyed off dir (+name) so reopening the same target
  # RE-ATTACHES the live session instead of spawning a duplicate (mirrors the remote
  # console). C is an absolute path (no spaces) — safe to single-quote.
  C="$HOME/.local/bin/claude"
  if [[ -n "$lname" ]]; then
    lid="$(printf '%s' "$ldir|$lname" | md5sum | cut -c1-12)"
    # A NAMED local console must resume ITS OWN conversation — the dir is shared, and
    # Claude keys --continue off the cwd, so --continue would resume whatever conversation
    # is the latest for that dir, not this name's. Deterministic session UUID from
    # dir|name (uuidgen v5, same recipe as the named remote console above); --resume it,
    # creating it with that exact id on first open (--session-id). uuidgen output is
    # [0-9a-f-] only -> shell-safe.
    sid="$(uuidgen --sha1 --namespace @url --name "$ldir|$lname")"
    claude_cmd="{ '$C' --resume $sid --dangerously-skip-permissions || '$C' --session-id $sid --dangerously-skip-permissions; }"
  else
    lid="$(printf '%s' "$ldir" | md5sum | cut -c1-12)"
    # No name: shared console for the dir — resume the last conversation there, else fresh.
    claude_cmd="{ '$C' --continue --dangerously-skip-permissions || '$C' --dangerously-skip-permissions; }"
  fi
  session="local-$lid"
  local_cmd="export PATH=\"\$HOME/.local/bin:\$HOME/bin:\$PATH\"; $claude_cmd; exec bash --login -i"
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    tmux new-session -d -s "$session" -c "$ldir" -e CLAUDE_CODE_DISABLE_MOUSE=1 "$local_cmd"
  fi
  exec tmux attach-session -t "=$session"
fi
# === end LOCAL CONSOLE ============================================================

# Validate $1 as DATA only — it is never eval'd, only used as a tmux name and a
# path component. Same charset the dashboard enforces (NAME_RE in app.py).
if [[ ! "$name" =~ ^[A-Za-z0-9._-]+$ || "$name" == "." || "$name" == ".." ]]; then
  echo "Invalid or missing mission name. Open the Console tab from a mission page."
  sleep 5; exit 1
fi

data_dir="$MISSIONS_DIR/$name"
if [[ ! -d "$data_dir" ]]; then
  echo "No such mission directory: $data_dir"
  sleep 5; exit 1
fi

# Per-mission metadata (mission.json) decides WHERE/HOW the console runs. It is written
# by the dashboard (Spawn wizard + /create); see app.py write_mission_meta / mission_target.
# Absent or malformed => the legacy inference (worktree-exists ? dev : ops in the mission
# dir), so every existing mission behaves exactly as before. Read with python3 (stdlib;
# no new deps). A bad file yields empty fields -> falls through to the legacy branch.
meta_file="$data_dir/mission.json"
mode=""; tkind=""; tpath=""; thost=""; tremote=""; drepo=""; dbase=""; dwt=""
if [[ -f "$meta_file" ]]; then
  mapfile -t _meta < <(python3 - "$meta_file" <<'PY'
import json, sys
try:
    m = json.load(open(sys.argv[1]))
    if not isinstance(m, dict):
        m = {}
except Exception:
    m = {}
t = m.get("target") or {}
d = m.get("dev") or {}
# Unknown mode strings (hand-edited sidecars, e.g. "local") count as ops — the
# dashboard's mission_target() normalizes the same way, so the CWD badge and the
# actual console dir can't diverge. A MISSING/empty mode stays empty (legacy path),
# matching mission_target()'s requirement of a truthy mode.
mode = m.get("mode")
if isinstance(mode, str) and mode and mode not in ("ops", "dev", "console"):
    mode = "ops"
def s(x):
    return (x if isinstance(x, str) else "").replace("\n", " ").replace("\t", " ")
for v in (mode, t.get("kind"), t.get("path"), t.get("host"),
          t.get("remote_dir"), d.get("repo"), d.get("base_branch"), d.get("worktree")):
    print(s(v))
PY
)
  mode="${_meta[0]:-}";  tkind="${_meta[1]:-}";   tpath="${_meta[2]:-}"
  thost="${_meta[3]:-}"; tremote="${_meta[4]:-}"
  drepo="${_meta[5]:-}"; dbase="${_meta[6]:-}";   dwt="${_meta[7]:-}"
fi

session="mission-$name"

# --- Ops mission whose console runs on a REMOTE host over SSH ----------------------
# The mission docs stay LOCAL in $data_dir (edit them via the dashboard); only the live
# Claude runs on the remote, the same SSH shape as the remote-console feature. The tmux
# session is still mission-$name, so the dashboard's live/kill logic is unchanged.
# Validation mirrors app.py (REMOTE_HOST_RE / REMOTE_DIR_RE) as defense in depth.
if [[ "$mode" == "ops" && "$tkind" == "remote" ]]; then
  rhost_re='^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$'
  # Blank dir is valid — see the REMOTE CONSOLES block above for why `cd ''` is safe.
  rdir_re='^(/[A-Za-z0-9 ._/@:+-]{0,255})?$'
  if [[ ! "$thost" =~ $rhost_re || ! "$tremote" =~ $rdir_re ]]; then
    echo "Mission $name has an invalid remote host/dir in mission.json."
    sleep 5; exit 1
  fi
  C="~/.local/bin/claude"
  # The remote dir is often SHARED by several missions (and ad-hoc remote consoles), and
  # Claude keys --continue off the cwd — so --continue would resume whatever conversation
  # happens to be the latest for that dir, NOT this mission's. Derive a deterministic
  # session UUID from the mission NAME (uuidgen v5, same recipe as the local-dir ops path
  # below) and --resume the mission's own conversation, creating it with that exact id on
  # first open (--session-id). uuidgen output is [0-9a-f-] only -> shell-safe.
  sid="$(uuidgen --sha1 --namespace @url --name "$name")"
  ssh_cmd=$(printf 'ssh -tt %q %q' "$thost" \
    "cd '$tremote' && { $C --resume $sid --dangerously-skip-permissions || $C --session-id $sid --dangerously-skip-permissions; }")
  name_q=$(printf '%q' "$name"); thost_q=$(printf '%q' "$thost")
  remote_cmd="$ssh_cmd; ec=\$?; printf '\n[mission %s] connection to %s ended (exit %s).\nYou are now in a LOCAL shell on the jumpbox — close this tab to finish.\n' $name_q $thost_q \"\$ec\"; exec bash --login -i"
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    tmux new-session -d -s "$session" "$remote_cmd"
  fi
  exec tmux attach-session -t "=$session"
fi

# --- Dev mission whose worktree + console run on a REMOTE host over SSH -------------
# The git worktree (branch claude/<name>) lives on the remote — created by app.py
# create_remote_worktree; mission docs stay LOCAL in $data_dir. Claude runs in the
# remote worktree as a FEATURE WORKER, guarded by prevent-misswork.py shipped to
# ~/.miss-claude on the remote. FAIL-CLOSED: re-ship + verify the guard here and refuse
# to launch if it can't be confirmed (we are about to run --dangerously-skip-permissions
# on the remote, so it must NOT run without its guardrail). This branch must precede the
# local `mode == dev` path below. Validation mirrors app.py as defense in depth.
if [[ "$mode" == "dev" && "$tkind" == "remote-repo" ]]; then
  rhost_re='^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$'
  rdir_re='^/[A-Za-z0-9 ._/@:+-]{0,255}$'
  base_re='^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$'
  : "${dbase:=working}"
  if [[ ! "$thost" =~ $rhost_re || ! "$dwt" =~ $rdir_re \
        || ! "$drepo" =~ $rdir_re || ! "$dbase" =~ $base_re ]]; then
    echo "Mission $name has an invalid remote host/worktree/repo/base in mission.json."
    sleep 5; exit 1
  fi
  if ! bash "$here/scripts/ship-rails.sh" "$thost"; then
    echo "Refusing to start the remote dev console: the guard rails could not be verified on $thost."
    echo "(prevent-misswork.py must be present + runnable in ~/.miss-claude on the remote;"
    echo " a remote dev console must never run --dangerously-skip-permissions with no guard.)"
    sleep 8; exit 1
  fi
  C="~/.local/bin/claude"
  # Remote command: cd into the worktree, export the feature-worker role + which repo/base
  # this develops + the guard hook path ($MISSWORK_HOOK, read by miss-rails.settings.json),
  # then launch the GUARDED Claude. Single-quoted values are allow-list validated (no
  # quotes/$); \$HOME and ~ expand on the remote. printf %q wraps it as one ssh arg.
  remote_inner="cd '$dwt' && export CLAUDE_MISS_ROLE=feature PRIMARY_REPO='$drepo' WORKTREES_DIR=\"\$HOME/missclaude-worktrees\" BASE_BRANCH='$dbase' MISSWORK_HOOK=\"\$HOME/.miss-claude/prevent-misswork.py\" MISS_ROLE_CONTEXT=\"\$HOME/.miss-claude/miss-role-context.py\" CLAUDE_CODE_DISABLE_MOUSE=1 && S=\"\$HOME/.miss-claude/miss-rails.settings.json\" && { $C --settings \"\$S\" --continue --dangerously-skip-permissions || $C --settings \"\$S\" --dangerously-skip-permissions; }"
  ssh_cmd=$(printf 'ssh -tt %q %q' "$thost" "$remote_inner")
  name_q=$(printf '%q' "$name"); thost_q=$(printf '%q' "$thost")
  remote_cmd="$ssh_cmd; ec=\$?; printf '\n[mission %s · dev] connection to %s ended (exit %s).\nYou are now in a LOCAL shell on the jumpbox — close this tab to finish.\n' $name_q $thost_q \"\$ec\"; exec bash --login -i"
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    tmux new-session -d -s "$session" "$remote_cmd"
  fi
  exec tmux attach-session -t "=$session"
fi

# --- Local console: choose the working dir + session script -----------------------
# `new_env` becomes extra `tmux new-session -e KEY=VAL` args, baking the per-mission
# repo/base (dev) or mission identity (local-dir ops) into the new pane's environment.
new_env=()
if [[ "$mode" == "dev" ]]; then
  # Dev mission: run the console in its git worktree as a FEATURE WORKER, and tell
  # claude-miss (via console-session-wt.sh) which local repo/base this mission develops.
  dir="${dwt:-$WORKTREES_DIR/$name}"
  # A vanished worktree (pruned after integration, or a bad mission.json path) would
  # make `tmux new-session -c` fail instantly and ttyd reconnect-loop on this launcher.
  # Fail with a clear message instead.
  if [[ ! -d "$dir" ]]; then
    echo "Mission $name is a dev mission but its worktree is missing: $dir"
    echo "Recreate it (git -C <repo> worktree add \"$dir\" claude/$name) or fix"
    echo "the mission's mission.json, then reopen this console."
    sleep 8; exit 1
  fi
  sess_cmd="$here/console-session-wt.sh"
  new_env+=( -e "PRIMARY_REPO=${drepo:-$here}" -e "BASE_BRANCH=${dbase:-working}" \
             -e "WORKTREES_DIR=$WORKTREES_DIR" -e "MISSIONS_DIR=$MISSIONS_DIR" )
elif [[ "$mode" == "ops" && ( "$tkind" == "local-dir" || "$tkind" == "local-repo" ) && -n "$tpath" ]]; then
  # Ops mission whose console works in a chosen local dir (not the mission folder).
  # The docs still live in $data_dir, so pass the mission identity to console-session.sh.
  # The cwd here is a SHARED dir (e.g. the user's home from a blank Path), whose Claude history
  # is NOT unique to this mission — so a plain --continue would resume some unrelated
  # conversation that happens to be the latest for that dir. Derive a deterministic session
  # UUID from the mission NAME (uuidgen v5, same recipe as the remote/local consoles above)
  # and hand it to console-session.sh, which uses --resume <uuid> || --session-id <uuid> so
  # this mission always re-attaches ITS OWN conversation; a different mission in the same dir
  # gets a separate one. uuidgen output is [0-9a-f-] only -> shell-safe.
  dir="$tpath"
  sess_cmd="$here/console-session.sh"
  mid="$(uuidgen --sha1 --namespace @url --name "$name")"
  new_env+=( -e "MISSION_NAME=$name" -e "MISSION_DATA_DIR=$data_dir" -e "MISSIONS_DIR=$MISSIONS_DIR" \
             -e "MISSION_SESSION_ID=$mid" )
else
  # No (or unrecognized) meta: the original inference — a same-named worktree => dev,
  # else an ops console in the mission folder. Keeps every existing mission identical.
  wt_dir="$WORKTREES_DIR/$name"
  if [[ -d "$wt_dir" ]]; then
    dir="$wt_dir"
    sess_cmd="$here/console-session-wt.sh"
  else
    dir="$data_dir"
    sess_cmd="$here/console-session.sh"
  fi
fi

# "=" forces an exact session-name match (no prefix matching). On first creation the
# pane runs the chosen session script in $dir; reconnects just re-attach. An empty
# new_env array expands to nothing (bash 4.4+), so the legacy path is byte-for-byte same.
if ! tmux has-session -t "=$session" 2>/dev/null; then
  tmux new-session -d -s "$session" -c "$dir" "${new_env[@]}" "$sess_cmd"
fi

exec tmux attach-session -t "=$session"
