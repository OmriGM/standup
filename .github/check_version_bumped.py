#!/usr/bin/env python3
"""Fail when shipped files change without a version bump.

Installs are cached per version. A change pushed under an unchanged version reaches
nobody and looks like it worked, which is the worst kind of failure: silent. This has
already happened once, when three commits shipped under a stale 0.1.1.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ".claude-plugin/plugin.json"
# Everything a user actually receives. Docs and CI are not shipped to installs.
SHIPPED = ("hooks/", "commands/", ".claude-plugin/")


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT)
    return done.stdout.strip() if done.returncode == 0 else ""


def main() -> int:
    # Refs are overridable so the guard itself can be tested against real history.
    base = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"
    head = sys.argv[2] if len(sys.argv) > 2 else "HEAD"

    previous = git("show", f"{base}:{MANIFEST}")
    if not previous:
        print("no previous manifest to compare against, skipping")
        return 0

    changed = [f for f in git("diff", "--name-only", base, head).splitlines() if f]
    shipped = [f for f in changed if f.startswith(SHIPPED) and f != MANIFEST]
    if not shipped:
        print("no shipped files changed, no bump needed")
        return 0

    was = json.loads(previous)["version"]
    now = json.loads(git("show", f"{head}:{MANIFEST}") or (ROOT / MANIFEST).read_text())["version"]
    if was != now:
        print(f"version bumped {was} -> {now}")
        return 0

    print(f"\nThese files ship to users but the version is still {now}:\n")
    for f in shipped:
        print(f"  {f}")
    print(
        f'\nBump "version" in {MANIFEST} and push again.\n'
        "Installs are cached per version, so without a bump this change reaches nobody.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
