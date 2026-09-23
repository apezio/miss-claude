# console-fallback-rc.sh — the rcfile for the interactive shell a mission console drops
# to when its agent exits (console-session.sh, console-session-wt.sh: `exec bash --rcfile`).
#
# Why this file exists. The pane's agent is launched with
# `claude --settings <console-hooks…json>`, and those hooks are the ONLY thing that keeps
# <mission dir>/.console-session pointing at the transcript the console is really writing.
# When Claude exits, the pane used to drop to a bare `bash --login -i`, where the obvious
# recovery — retyping `claude --resume …` — silently starts an UNHOOKED session. The marker
# then freezes, and after the next /clear the dashboard's context badge and chat tab both
# sit on an abandoned transcript, confidently reporting a stale number while the console
# looks alive. That happened on 2026-09-15 (mission swift-cedar: badge stuck at 611k/61%
# for ~9h while the live session was at 295k/29%).
#
# So: re-source the normal startup files, then shadow `claude` with a function that puts
# --settings back. `command claude` (or any absolute path) still bypasses it deliberately.

# A login shell would have sourced these; --rcfile means we do it ourselves.
[ -f /etc/bashrc ] && . /etc/bashrc
[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"

if [ -n "${MISSION_HOOKS:-}" ] && [ -f "${MISSION_HOOKS:-}" ]; then
  claude() { command claude --settings "$MISSION_HOOKS" "$@"; }
  printf '[console] `claude` here re-attaches this mission'"'"'s hooks (--settings %s).\n' \
    "$(basename "$MISSION_HOOKS")"
  printf '[console] Use `command claude` to start one WITHOUT them (the context badge will not follow it).\n'
fi
