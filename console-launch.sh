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

# Self-heal a WEDGED tmux server before doing anything else.
#
# A tmux server that has lost its last session can hang mid-shutdown: it still owns the
# socket and still ACCEPTS connections, but drops each one immediately. Since this
# launcher is what ttyd runs per connection, every console open then dies instantly and
# ttyd reconnect-loops on it forever — dozens of dead spawns a second and not one usable
# console, until someone kills the server by hand. That was the outage of 2026-08-10.
#
# The three states are exactly distinguishable, so condemning one is safe:
#   healthy  -> `tmux ls` exits 0 and lists sessions
#   none yet -> "error connecting ... (No such file or directory)"  (normal cold start)
#   WEDGED   -> "server exited unexpectedly"                        <- only this one
# That message means the connection was accepted and then closed, which a merely slow or
# busy server cannot produce — so this can never strand a healthy server's sessions.
# A wedged server holds no reachable sessions by definition, so nothing is lost.
sessions_heal() {
  local sock="$TMUX_TMPDIR/tmux-$(id -u)/default"
  [ -S "$sock" ] || return 0
  case "$(timeout 5 tmux ls 2>&1)" in
    *"server exited unexpectedly"*) ;;
    *) return 0 ;;
  esac
  echo "[console] tmux server is wedged (accepting connections, then dropping them)."
  echo "[console] clearing it so this console can start — no reachable sessions are lost."
  fuser -k "$sock" >/dev/null 2>&1 || true   # SIGKILL: a wedged server ignores TERM
  sleep 1
  rm -f "$sock"                              # unlink so a fresh server binds cleanly
}
sessions_heal

# Attach, or back off — never exit instantly on failure.
#
# ttyd re-runs this launcher for every connection and the mission page's iframe reconnects
# on every disconnect. That loop is LOAD-BEARING: killing a session from the dashboard is
# exactly what makes the iframe recreate it, so it must stay fast in the normal case. The
# cost is that any instant failure here becomes a hot loop — on 2026-08-10 a wedged tmux
# server produced ~20 dead launcher spawns a second for over an hour.
#
# Every validation failure above already sleeps before exiting for this reason. This is the
# same guard for the last gap: the session is not there when we go to attach, because the
# create failed or it died in between (e.g. a session command that exits immediately). A
# successful create leaves the session present, so this never fires on the normal path and
# the reconnect stays instant.
attach_session() {
  local session="$1"
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    echo "[console] session '$session' could not be started (create failed, or it exited at once)."
    echo "[console] inspect with: TMUX_TMPDIR=$TMUX_TMPDIR tmux ls"
    echo "[console] pausing so this console does not spin — close this tab or fix the mission."
    sleep 5
    exit 1
  fi
  exec tmux attach-session -t "=$session"
}

MISSIONS_DIR="${MISSIONS_DIR:-$HOME/missions}"
WORKTREES_DIR="${WORKTREES_DIR:-$HOME/missclaude-worktrees}"
here="$(dirname "$(readlink -f "$0")")"
name="${1:-}"

# === REMOTE CONSOLES (optional side feature — delete this block to remove) =========
# ttyd calls us as: console-launch.sh remote <host> <dir> [name]  (from the dashboard's
# /remote page, ?arg=remote&arg=<host>&arg=<dir>[&arg=<name>]). Wrap an SSH login to
# <host> in a LOCAL tmux session — there is NO tmux on the remote side. Two modes:
#   - NO name: a FRESH console — random session name, plain
#       ssh -tt <host> 'cd <dir> && claude --dangerously-skip-permissions'
#     Every open is a brand-new conversation (no --continue, no re-attach); want to get
#     back to a conversation? use a named console.
#   - WITH a name: a DISTINCT, RESUMABLE console. We derive a deterministic session UUID
#     from host|dir|name (uuidgen v5) and run
#       ssh -tt <host> 'cd <dir> && <is there a transcript?> && claude --resume|--session-id <uuid> ...'
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
    # --resume the name's own session, or --session-id to CREATE it with that exact id on
    # first use — decided ON THE REMOTE, before claude starts, by looking for the
    # transcript ($CLAUDE_CONFIG_DIR or ~/.claude, projects/<cwd with every non-alnum
    # char turned into '-'>/<uuid>.jsonl). It used to be `--resume … || --session-id …`,
    # but the failing first link still showed the interactive "do you trust this folder?"
    # dialog and exited before that answer was saved, so every first open asked twice.
    # `printf %s` not echo: tr would turn a trailing newline into a stray '-'.
    # Portability of this and the other two remote one-liners: they need `tr` and POSIX
    # unmatched-glob semantics on the FAR host — fine for the bash/coreutils fleet, and a
    # non-POSIX login shell (zsh) fails closed: a message, no claude.
    ssh_cmd=$(printf 'ssh -tt %q %q' "$rhost" \
      "cd '$rdir' && export CLAUDE_CODE_DISABLE_MOUSE=1 && P=\"\${CLAUDE_CONFIG_DIR:-\$HOME/.claude}/projects/\$(printf %s \"\$(pwd -P)\" | tr -c 'A-Za-z0-9' '-')\" && if [ -f \"\$P/$sid.jsonl\" ]; then A=\"--resume $sid\"; else A=\"--session-id $sid\"; fi && $C \$A --dangerously-skip-permissions")
  else
    # NO name: FRESH by default — a random session name (new tmux session every open,
    # never re-attaching an old one) and a plain claude with NO --continue, so it can't
    # resume whatever conversation happens to be newest for that dir. Resumable consoles
    # are the NAMED path above; stray unnamed sessions are visible/killable on the index.
    rid="$(head -c16 /dev/urandom | md5sum | cut -c1-12)"
    session="remote-$rid"
    # printf %q shell-escapes the whole invocation so tmux's `sh -c` runs it verbatim —
    # no second round of word-splitting.
    ssh_cmd=$(printf 'ssh -tt %q %q' "$rhost" \
      "cd '$rdir' && export CLAUDE_CODE_DISABLE_MOUSE=1 && $C --dangerously-skip-permissions")
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
  attach_session "$session"
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
  # NAMED console: deterministic session name keyed off dir+name so reopening the same
  # target RE-ATTACHES the live session instead of spawning a duplicate (mirrors the
  # remote console). C is an absolute path (no spaces) — safe to single-quote.
  C="$HOME/.local/bin/claude"
  if [[ -n "$lname" ]]; then
    lid="$(printf '%s' "$ldir|$lname" | md5sum | cut -c1-12)"
    # A NAMED local console must resume ITS OWN conversation — the dir is shared, and
    # Claude keys --continue off the cwd, so --continue would resume whatever conversation
    # is the latest for that dir, not this name's. Deterministic session UUID from
    # dir|name (uuidgen v5, same recipe as the named remote console above); --resume it,
    # creating it with that exact id on first open (--session-id). uuidgen output is
    # [0-9a-f-] only -> shell-safe.
    # Which of the two it is, is decided HERE, from the transcript on disk
    # (scripts/claude-session-args.py), so the pane runs claude ONCE. Chaining them with
    # `||` meant the failing --resume still showed the interactive "do you trust this
    # folder?" dialog and exited before the answer was saved — i.e. the operator had to
    # answer it twice on every first open. The remote strings above do the same check,
    # but written in POSIX sh so it runs on the far side (this helper is local-only).
    sid="$(uuidgen --sha1 --namespace @url --name "$ldir|$lname")"
    sess_args="$(python3 "$here/scripts/claude-session-args.py" "$ldir" "$sid")"
    # NO-PYTHON3 FLOOR: no helper => no flags => a fresh, unpinned conversation every
    # open. Answer the same question in pure shell (the probe the remote strings use):
    # a transcript under <config>/projects/<slug of the dir>/ means resume, else create.
    if [[ -z "$sess_args" ]]; then
      lproj="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$(printf %s "$(cd "$ldir" && pwd -P)" | tr -c 'A-Za-z0-9' '-')"
      if [[ -f "$lproj/$sid.jsonl" ]]; then sess_args="--resume $sid"; else sess_args="--session-id $sid"; fi
    fi
    claude_cmd="'$C' $sess_args --dangerously-skip-permissions"
  else
    # NO name: FRESH by default — a random session name (new tmux session every open)
    # and a plain claude with NO --continue, so it can't resume whatever conversation
    # happens to be newest for that dir. Resumable consoles are the NAMED path above;
    # stray unnamed sessions are visible/killable on the index.
    lid="$(head -c16 /dev/urandom | md5sum | cut -c1-12)"
    claude_cmd="'$C' --dangerously-skip-permissions"
  fi
  session="local-$lid"
  local_cmd="export PATH=\"\$HOME/.local/bin:\$HOME/bin:\$PATH\"; $claude_cmd; exec bash --login -i"
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    tmux new-session -d -s "$session" -c "$ldir" -e CLAUDE_CODE_DISABLE_MOUSE=1 "$local_cmd"
  fi
  attach_session "$session"
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
# by the dashboard (Spawn wizard + /create); see app.py write_mission_meta / dev_meta.
# scripts/mission-env.py is the ONE reader of that file: it prints shell-quoted
# MISS_* assignments (repo root/id, worktree, feature + integration branch, integration
# worktree, preview port, role) which are eval'd here and exported into the pane, so
# the same recorded identity reaches the wrappers, the guard hook and Claude's own
# context — never re-derived from a cwd or the dashboard's default repo. Absent or
# malformed => the legacy inference (worktree-exists ? dev : ops in the mission dir),
# so every existing mission behaves exactly as before.
meta_file="$data_dir/mission.json"
MISS_MODE=""; MISS_TARGET_KIND=""; MISS_TARGET_PATH=""; MISS_TARGET_HOST=""
MISS_TARGET_REMOTE_DIR=""; MISS_ROLE=""; MISS_REPO_ROOT=""; MISS_REPO_ID=""
MISS_WORKTREE=""; MISS_FEATURE_BRANCH=""; MISS_INTEGRATION_BRANCH=""
MISS_INTEGRATION_WORKTREE=""; MISS_PREVIEW_PORT=""; MISS_SESSION_ID=""
if [[ -f "$meta_file" ]]; then
  eval "$(python3 "$here/scripts/mission-env.py" "$meta_file" 2>/dev/null)"
fi
mode="$MISS_MODE"; tkind="$MISS_TARGET_KIND"; tpath="$MISS_TARGET_PATH"
thost="$MISS_TARGET_HOST"; tremote="$MISS_TARGET_REMOTE_DIR"
drepo="$MISS_REPO_ROOT"; dbase="$MISS_INTEGRATION_BRANCH"; dwt="$MISS_WORKTREE"
msid="$MISS_SESSION_ID"

# A mission RENAMED by the dashboard carries its ORIGINAL resume UUID in mission.json
# (session_id, pinned by app.py rename_mission) — the uuid-keyed branches below prefer
# it over re-deriving one from the (new) name, so the console keeps resuming the same
# conversation. Strict format check because the value is interpolated into commands.
sid_re='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
[[ "$msid" =~ $sid_re ]] || msid=""

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
  # first open (--session-id). A pinned session_id from mission.json (a renamed mission's
  # original uuid) wins. uuidgen output is [0-9a-f-] only -> shell-safe.
  # Which of the two is decided on the remote BEFORE claude starts — does the transcript
  # (<config>/projects/<slug of the cwd>/<uuid>.jsonl) exist? — so claude runs once. The
  # old `--resume … || --session-id …` chain made the operator answer the folder-trust
  # dialog for every failing link, because each one exited before the answer was saved.
  sid="${msid:-$(uuidgen --sha1 --namespace @url --name "$name")}"
  ssh_cmd=$(printf 'ssh -tt %q %q' "$thost" \
    "cd '$tremote' && export CLAUDE_CODE_DISABLE_MOUSE=1 && P=\"\${CLAUDE_CONFIG_DIR:-\$HOME/.claude}/projects/\$(printf %s \"\$(pwd -P)\" | tr -c 'A-Za-z0-9' '-')\" && if [ -f \"\$P/$sid.jsonl\" ]; then A=\"--resume $sid\"; else A=\"--session-id $sid\"; fi && $C \$A --dangerously-skip-permissions")
  name_q=$(printf '%q' "$name"); thost_q=$(printf '%q' "$thost")
  remote_cmd="$ssh_cmd; ec=\$?; printf '\n[mission %s] connection to %s ended (exit %s).\nYou are now in a LOCAL shell on the jumpbox — close this tab to finish.\n' $name_q $thost_q \"\$ec\"; exec bash --login -i"
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    tmux new-session -d -s "$session" "$remote_cmd"
  fi
  attach_session "$session"
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
  # then launch the GUARDED Claude. The YES SHIP path is a plain script shipped next to
  # the guard (~/.miss-claude/miss-ship.py, verified by ship-rails), so nothing extra is
  # attached at launch. Single-quoted values are allow-list validated (no
  # quotes/$); \$HOME and ~ expand on the remote. printf %q wraps it as one ssh arg.
  # $R is the session flag, resolved on the remote first: --continue only when the remote
  # worktree's project dir (<config>/projects/<slug of the cwd>) already holds a
  # transcript, else nothing = a fresh session. `--continue || plain` used to show the
  # folder-trust dialog twice on a worktree with no history, since the failing --continue
  # exited before the answer was saved. The probe stays INSIDE the `&&` chain (`done &&`,
  # and `done` exits 0 because the unconditional `break` is the loop's last command): a
  # failed cd must NOT reach claude, or the console would run
  # --dangerously-skip-permissions with an empty --settings, i.e. no guard hook.
  # `pwd -P` because Claude Code slugs the physical cwd.
  id_re='^[A-Za-z0-9._-]{0,120}$'; port_re='^[0-9]{0,5}$'
  [[ "$MISS_REPO_ID" =~ $id_re && "$MISS_FEATURE_BRANCH" =~ ^(claude/[A-Za-z0-9._-]+)?$ \
     && "$MISS_PREVIEW_PORT" =~ $port_re ]] || { echo "Mission $name has invalid identity fields in mission.json."; sleep 5; exit 1; }
  remote_inner="cd '$dwt' && export CLAUDE_MISS_ROLE=feature MISS_MODE=dev MISS_TARGET_KIND=remote-repo PRIMARY_REPO='$drepo' WORKTREES_DIR=\"\$HOME/missclaude-worktrees\" BASE_BRANCH='$dbase' MISS_REPO_ROOT='$drepo' MISS_REPO_ID='$MISS_REPO_ID' MISS_WORKTREE='$dwt' MISS_FEATURE_BRANCH='$MISS_FEATURE_BRANCH' MISS_INTEGRATION_BRANCH='$dbase' MISS_PREVIEW_PORT='$MISS_PREVIEW_PORT' MISSWORK_HOOK=\"\$HOME/.miss-claude/prevent-misswork.py\" MISS_ROLE_CONTEXT=\"\$HOME/.miss-claude/miss-role-context.py\" CLAUDE_CODE_DISABLE_MOUSE=1 && S=\"\$HOME/.miss-claude/miss-rails.settings.json\" && P=\"\${CLAUDE_CONFIG_DIR:-\$HOME/.claude}/projects/\$(printf %s \"\$(pwd -P)\" | tr -c 'A-Za-z0-9' '-')\" && R= && for f in \"\$P\"/*.jsonl; do [ -f \"\$f\" ] && R=--continue; break; done && $C --settings \"\$S\" \$R --dangerously-skip-permissions"
  ssh_cmd=$(printf 'ssh -tt %q %q' "$thost" "$remote_inner")
  name_q=$(printf '%q' "$name"); thost_q=$(printf '%q' "$thost")
  remote_cmd="$ssh_cmd; ec=\$?; printf '\n[mission %s · dev] connection to %s ended (exit %s).\nYou are now in a LOCAL shell on the jumpbox — close this tab to finish.\n' $name_q $thost_q \"\$ec\"; exec bash --login -i"
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    tmux new-session -d -s "$session" "$remote_cmd"
  fi
  attach_session "$session"
fi

# --- Local console: choose the working dir + session script -----------------------
# `new_env` becomes extra `tmux new-session -e KEY=VAL` args, baking the per-mission
# repo/base (dev) or mission identity (local-dir ops) into the new pane's environment.
new_env=()
if [[ "$mode" == "dev" && "$MISS_ROLE" == "integrator" ]]; then
  # INTEGRATOR mission: the console runs claude-miss-integrator in the checkout that
  # holds this repo's integration branch — recorded at spawn (app.py
  # ensure_integration_worktree), so which repo gets integrated never depends on the
  # pane's cwd or on the dashboard's default repo. Both the repo and the checkout
  # must still be there; refuse clearly instead of letting tmux fail and ttyd spin.
  dir="${MISS_INTEGRATION_WORKTREE:-$drepo}"
  if [[ -z "$drepo" || ! -d "$drepo" || ! -d "$dir" ]]; then
    echo "Mission $name is an integrator mission but its repo/integration checkout is missing:"
    echo "  repo:     ${drepo:-?}"
    echo "  checkout: ${dir:-?}"
    echo "Fix the mission's mission.json (dev.repo / dev.integration_worktree), then reopen."
    sleep 8; exit 1
  fi
  sess_cmd="$here/console-session-int.sh"
  new_env+=( -e "PRIMARY_REPO=$drepo" -e "BASE_BRANCH=${dbase:-working}" \
             -e "INTEGRATION_WORKTREE=$dir" -e "WORKTREES_DIR=$WORKTREES_DIR" \
             -e "MISSIONS_DIR=$MISSIONS_DIR" -e "MISSION_NAME=$name" \
             -e "MISSION_DATA_DIR=$data_dir" -e "MISS_REPO_ROOT=$drepo" \
             -e "MISS_REPO_ID=$MISS_REPO_ID" -e "MISS_INTEGRATION_BRANCH=${dbase:-working}" \
             -e "MISS_INTEGRATION_WORKTREE=$dir" )
elif [[ "$mode" == "dev" ]]; then
  # Dev mission: run the console in its git worktree as a FEATURE WORKER, and tell
  # claude-miss (via console-session-wt.sh) which local repo/base this mission develops.
  dir="${dwt:-$WORKTREES_DIR/$name}"
  # A vanished worktree (pruned after integration, or a bad mission.json path) would
  # make `tmux new-session -c` fail instantly and ttyd reconnect-loop on this launcher.
  # Fail with a clear message instead.
  if [[ ! -d "$dir" ]]; then
    echo "Mission $name is a dev mission but its worktree is missing: $dir"
    echo "Recreate it (git -C <repo> worktree add \"$dir\" claude/$(basename "$dir")) or fix"
    echo "the mission's mission.json, then reopen this console."
    sleep 8; exit 1
  fi
  sess_cmd="$here/console-session-wt.sh"
  # Pass the mission identity explicitly: a RENAMED dev mission keeps its original
  # worktree, so console-session-wt.sh can no longer derive the mission name / data
  # dir from the worktree's basename (it still falls back to that when unset).
  # A sidecar that names no repo (hand-written) gets the worktree's OWN repo — never
  # this launcher's checkout, which is Miss Claude's repo and may be the wrong one.
  if [[ -z "$drepo" ]]; then
    drepo="$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
    drepo="${drepo%/.git}"
  fi
  new_env+=( -e "PRIMARY_REPO=${drepo:-$here}" -e "BASE_BRANCH=${dbase:-working}" \
             -e "WORKTREES_DIR=$WORKTREES_DIR" -e "MISSIONS_DIR=$MISSIONS_DIR" \
             -e "MISSION_NAME=$name" -e "MISSION_DATA_DIR=$data_dir" \
             -e "MISS_REPO_ROOT=${drepo:-$here}" -e "MISS_REPO_ID=$MISS_REPO_ID" \
             -e "MISS_WORKTREE=$dir" -e "MISS_FEATURE_BRANCH=${MISS_FEATURE_BRANCH:-claude/$(basename "$dir")}" \
             -e "MISS_INTEGRATION_BRANCH=${dbase:-working}" \
             -e "MISS_INTEGRATION_WORKTREE=$MISS_INTEGRATION_WORKTREE" \
             -e "MISS_PREVIEW_PORT=$MISS_PREVIEW_PORT" )
elif [[ "$mode" == "ops" && ( "$tkind" == "local-dir" || "$tkind" == "local-repo" ) && -n "$tpath" ]]; then
  # Ops mission whose console works in a chosen local dir (not the mission folder).
  # The docs still live in $data_dir, so pass the mission identity to console-session.sh.
  # The cwd here is a SHARED dir (e.g. the user's home from a blank Path), whose Claude history
  # is NOT unique to this mission — so a plain --continue would resume some unrelated
  # conversation that happens to be the latest for that dir. Derive a deterministic session
  # UUID from the mission NAME (uuidgen v5, same recipe as the remote/local consoles above)
  # and hand it to console-session.sh, which looks the transcript up on disk and then runs
  # claude ONCE with --resume <uuid> or --session-id <uuid>, so
  # this mission always re-attaches ITS OWN conversation; a different mission in the same dir
  # gets a separate one. A pinned session_id from mission.json (a renamed mission's
  # original uuid) wins. uuidgen output is [0-9a-f-] only -> shell-safe.
  dir="$tpath"
  sess_cmd="$here/console-session.sh"
  mid="${msid:-$(uuidgen --sha1 --namespace @url --name "$name")}"
  new_env+=( -e "MISSION_NAME=$name" -e "MISSION_DATA_DIR=$data_dir" -e "MISSIONS_DIR=$MISSIONS_DIR" \
             -e "MISSION_SESSION_ID=$mid" )
else
  # No (or unrecognized) meta: the original inference — a same-named worktree => dev,
  # else an ops console in the mission folder. Keeps every existing mission identical.
  wt_dir="$WORKTREES_DIR/$name"
  if [[ -d "$wt_dir" ]]; then
    dir="$wt_dir"
    sess_cmd="$here/console-session-wt.sh"
    # Identity from the worktree itself (its repo may not be Miss Claude's).
    lrepo="$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
    lrepo="${lrepo%/.git}"
    if [[ -n "$lrepo" ]]; then
      new_env+=( -e "PRIMARY_REPO=$lrepo" -e "MISS_REPO_ROOT=$lrepo" -e "MISS_WORKTREE=$dir" \
                 -e "MISS_FEATURE_BRANCH=claude/$name" -e "MISSION_NAME=$name" \
                 -e "MISSION_DATA_DIR=$data_dir" )
    fi
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

attach_session "$session"
