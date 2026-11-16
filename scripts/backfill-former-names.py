#!/usr/bin/env python3
"""backfill-former-names — record the names missions had BEFORE app.py started
keeping them (mission.json `former_names`), so old /m/<old>/... URLs redirect.

  scripts/backfill-former-names.py            dry run: print what would be written
  scripts/backfill-former-names.py --apply    write it
  ... --days N          how far back to read the dashboard journal (default 30)
  ... --journal-only    skip the worktree source
  ... --journal FILE    read journal lines from FILE instead of journalctl

Two sources, both confirmed against the folders that exist now:
  journal   "auto-named mission '<old>' -> '<new>'" lines; a chain is followed to
            the name that is a mission today (a later hand rename is not logged,
            so a chain that ends nowhere is skipped).
  worktree  a feature mission whose recorded worktree folder is not its name was
            spawned under that folder's name. No date: covers older renames too.
An old name that is a live mission is never touched, and one claimed by two
missions (generated names repeat) is skipped unless the journal settles it.
A hand rename of an ops mission was never logged and cannot be recovered.
Pure stdlib. Picked up by the running dashboard within a minute — no restart."""
import argparse
import json
import os
import re
import subprocess
import sys

MISSIONS_DIR = os.path.realpath(
    os.environ.get("MISSIONS_DIR", os.path.expanduser("~/missions")))
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
RENAMED_RE = re.compile(r"auto-named mission '([^']+)' -> '([^']+)'")


def safe(name):
    return bool(NAME_RE.match(name or "")) and name not in (".", "..")


def read_meta(name):
    try:
        with open(os.path.join(MISSIONS_DIR, name, "mission.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def journal_lines(days, path):
    if path:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    out = subprocess.run(
        ["journalctl", "-u", "mission-dashboard", "--no-pager", "--since", "%d days ago" % days],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
    return out.stdout.splitlines()


def plan(live, metas, lines, use_worktrees=True):
    """({old: (mission, source)}, [skip notes]) — what to record, nothing written."""
    found, skipped = {}, []
    if use_worktrees:
        claims = {}
        for name, meta in metas.items():
            dev = meta.get("dev") if isinstance(meta.get("dev"), dict) else {}
            wt = dev.get("worktree")
            if meta.get("mode") != "dev" or dev.get("role") == "integrator" \
                    or not isinstance(wt, str):
                continue
            old = os.path.basename(wt.rstrip("/"))
            if safe(old) and old != name and dev.get("branch") in (None, "claude/" + old):
                claims.setdefault(old, []).append(name)
        for old, names in claims.items():
            if len(names) == 1:
                found[old] = (names[0], "worktree")
            else:
                skipped.append("%s: claimed by %s" % (old, ", ".join(sorted(names))))
    renames = {}
    for line in lines:
        m = RENAMED_RE.search(line)
        if m:
            renames[m.group(1)] = m.group(2)      # chronological: the latest wins
    for old in renames:
        cur, seen = renames[old], {old}
        while cur not in live and cur in renames and cur not in seen:
            seen.add(cur)
            cur = renames[cur]
        if cur in live:
            found[old] = (cur, "journal")
        elif old not in found:
            skipped.append("%s: renamed to %s, which is not a mission now" % (old, cur))
    for old in sorted(found):
        if old in live:
            skipped.append("%s: is a live mission" % old)
            del found[old]
        elif not safe(old):
            del found[old]
    return found, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--journal-only", action="store_true")
    ap.add_argument("--journal")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every name")
    args = ap.parse_args()

    live = {n for n in os.listdir(MISSIONS_DIR)
            if safe(n) and os.path.isdir(os.path.join(MISSIONS_DIR, n))}
    metas = {n: m for n, m in ((n, read_meta(n)) for n in live) if m}
    found, skipped = plan(live, metas, journal_lines(args.days, args.journal),
                          use_worktrees=not args.journal_only)

    todo, already, counts = {}, 0, {"journal": 0, "worktree": 0}
    for old, (mission, source) in sorted(found.items()):
        meta = metas.get(mission)
        if meta is None:
            skipped.append("%s: %s has no readable mission.json" % (old, mission))
            continue
        names = meta.get("former_names")
        if isinstance(names, list) and old in names:
            already += 1
            continue
        todo.setdefault(mission, []).append(old)
        counts[source] += 1
        if args.verbose:
            print("%-9s %s -> %s" % (source, old, mission))
    if args.verbose:
        for note in skipped:
            print("skip      " + note)

    if args.apply:
        for mission, olds in todo.items():
            meta = metas[mission]
            names = meta.get("former_names")
            meta["former_names"] = [n for n in (names if isinstance(names, list) else [])
                                    if n not in olds] + olds
            p = os.path.join(MISSIONS_DIR, mission, "mission.json")
            with open(p + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(json.dumps(meta, indent=2) + "\n")
            os.replace(p + ".tmp", p)
    print("%s: %d old names on %d missions (%d from the journal, %d from worktrees); "
          "%d already recorded, %d skipped%s" % (
              "WRITTEN" if args.apply else "DRY RUN — nothing written",
              sum(counts.values()), len(todo), counts["journal"], counts["worktree"],
              already, len(skipped), "" if args.verbose else " (-v lists them)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
