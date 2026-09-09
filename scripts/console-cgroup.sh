#!/usr/bin/env bash
# console-cgroup.sh — put a console pane in its own cgroup, so that everything it ever
# spawns can be ended in one write. SOURCED (never executed) by console-session.sh /
# console-session-wt.sh / console-session-int.sh, and by the ad-hoc console commands
# console-launch.sh builds.
#
# WHY A CGROUP AND NOT SIGNALS. When a console ends, tmux SIGHUPs the pane's process
# GROUP. Dev/preview servers are started `nohup npx next dev … &` (a repo's
# scripts/dev-preview.sh, typically) and nohup exists precisely to ignore SIGHUP, so they
# survived, reparented to PID 1, and stayed in claude-console.service's cgroup for
# weeks — 284 tasks of a 4096 cap on 2026-09-07. Playwright's `npm exec` trees detach the
# same way. Anything based on signals or on walking the process tree is unreliable by
# construction: a child only has to daemonize to escape it, and agents keep inventing new
# ways to daemonize. A process cannot leave its cgroup. nohup, setsid, disown and
# double-fork were all verified powerless against `cgroup.kill` (2026-09-07).
#
# The counterpart is app.py's _kill_tmux_session, which writes 1 to this cgroup's
# cgroup.kill and rmdirs it after the graceful Ctrl-D + kill-session.
#
# Requires Delegate=yes on claude-console.service (systemd then chowns the unit's cgroup
# to the service user). See that unit; setup.sh generates it.
#
# FAILS OPEN, ALWAYS. A console that cannot get a cgroup must still start — just
# unprotected, exactly as every console behaved before this existed. There is no
# imaginable cgroup problem worth refusing the operator a working console over.

# console_cgroup_join [session-name]
#
# Must be SOURCED from the pane's own top-level shell: it moves $$ — the caller's shell —
# and every later child inherits the cgroup. Running it as a subprocess would move only
# the subprocess, which then exits. (Sourcing does not fork, so $$ is right here; in a
# subshell or a pipeline it would not be.)
console_cgroup_join() {
  local session="${1:-}"

  # cgroup v2 only. On v1 (or no cgroupfs) there is no unified hierarchy to join and no
  # cgroup.kill to end it with; do nothing rather than half a job.
  [ -e /sys/fs/cgroup/cgroup.controllers ] || return 0

  # The tmux session name is the cgroup name: it is what app.py's kill path already has
  # in hand, so the two sides need no extra bookkeeping to agree. Ask tmux if the caller
  # did not say (works for missions and for ad-hoc local-/remote- consoles alike).
  if [ -z "$session" ] && [ -n "${TMUX:-}" ]; then
    session="$(tmux display-message -p '#{session_name}' 2>/dev/null)"
  fi
  [ -n "$session" ] || return 0
  # Same charset app.py validates before touching the path (see _session_cgroup). tmux
  # session names come from console-launch.sh and are already narrower than this, so a
  # rejection here means something is wrong, not merely unusual.
  case "$session" in
    *[!A-Za-z0-9._-]*|""|.|..) return 0 ;;
  esac

  # Our own cgroup, from the unified hierarchy line ("0::/system.slice/…").
  local rel
  rel="$(sed -n 's/^0:://p' /proc/self/cgroup 2>/dev/null)"
  [ -n "$rel" ] || return 0

  local cg="/sys/fs/cgroup${rel}/${session}"
  # An identically named cgroup can be left over from a previous session whose rmdir lost
  # a race with a slow-dying process. mkdir -p is happy with that, and it is empty by
  # then; nothing else could be in it, because the name is this session's.
  mkdir -p "$cg" 2>/dev/null || {
    echo "[console] note: no per-session cgroup (${cg} could not be created)." >&2
    echo "[console] the console works normally; processes it leaves behind are not" >&2
    echo "[console] auto-reaped. Is Delegate=yes set on claude-console.service?" >&2
    return 0
  }
  if ! echo $$ > "$cg/cgroup.procs" 2>/dev/null; then
    echo "[console] note: could not join ${cg}; running without process containment." >&2
    rmdir "$cg" 2>/dev/null
    return 0
  fi
  export MISS_CONSOLE_CGROUP="$cg"
  return 0
}
