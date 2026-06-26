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
#  auth-gated admin tooling, so tool calls run without interactive prompts.
# Guard requires a non-empty $2 so a mission literally named "remote" (single arg)
# still falls through to the normal mission path below. Validation mirrors app.py
# (REMOTE_HOST_RE / REMOTE_DIR_RE / REMOTE_NAME_RE) as defense in depth before the values
# hit the command. The name only ever feeds uuidgen's stdin-equivalent --name (never a
# shell command or the tmux session name directly), so its broader charset can't break out.
if [[ "${1:-}" == "remote" && -n "${2:-}" ]]; then
  rhost="$2"; rdir="${3:-}"; rname="${4:-}"
  rhost_re='^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$'
  rdir_re='^/[A-Za-z0-9 ._/@:+-]{0,255}$'
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
    remote_cmd=$(printf 'ssh -tt %q %q' "$rhost" \
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
    remote_cmd=$(printf 'ssh -tt %q %q' "$rhost" \
      "cd '$rdir' && { $C --continue --dangerously-skip-permissions || $C --dangerously-skip-permissions; }")
  fi
  if ! tmux has-session -t "=$session" 2>/dev/null; then
    tmux new-session -d -s "$session" "$remote_cmd"
  fi
  exec tmux attach-session -t "=$session"
fi
# === end REMOTE CONSOLES ==========================================================

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

# If this mission has a dev worktree (same name), run the console THERE as a feature
# worker; otherwise run in the mission data dir as an ops console. Either way the tmux
# session keeps the name mission-<name>, so app.py's live/kill/console logic is unchanged.
wt_dir="$WORKTREES_DIR/$name"
if [[ -d "$wt_dir" ]]; then
  dir="$wt_dir"
  sess_cmd="$here/console-session-wt.sh"
else
  dir="$data_dir"
  sess_cmd="$here/console-session.sh"
fi

session="mission-$name"

# "=" forces an exact session-name match (no prefix matching). On first creation the
# pane runs the chosen session script in $dir; reconnects just re-attach.
if ! tmux has-session -t "=$session" 2>/dev/null; then
  tmux new-session -d -s "$session" -c "$dir" "$sess_cmd"
fi

exec tmux attach-session -t "=$session"
