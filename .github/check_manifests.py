#!/usr/bin/env python3
"""Manifest checks that run in CI.

Written in plain Python rather than shelling out to `claude plugin validate` so it
needs nothing installed. It also asserts the specific mistakes that have already
bitten real installs, which a generic schema check would not catch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        FAILURES.append(message)


def main() -> int:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())

    print("plugin.json")
    for key in ("name", "version", "description", "license"):
        check(key in plugin, f"has {key}")
    # Shipped once and broke every install: the standard hooks file is loaded
    # automatically, and naming it again makes Claude Code load it twice and refuse.
    check(
        "hooks" not in plugin,
        "does not re-declare hooks/hooks.json (that path loads automatically)",
    )

    print("marketplace.json")
    names = [p.get("name") for p in market.get("plugins", [])]
    check(plugin["name"] in names, f"lists the plugin {plugin['name']!r}")
    for entry in market.get("plugins", []):
        src = ROOT / entry.get("source", ".")
        check(src.is_dir(), f"source {entry.get('source')!r} exists")

    print("hooks.json")
    check(bool(hooks.get("hooks")), "declares at least one hook event")
    for event, entries in hooks.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                check(
                    "${CLAUDE_PLUGIN_ROOT}" in cmd,
                    f"{event} command uses CLAUDE_PLUGIN_ROOT rather than a fixed path",
                )
                for frag in cmd.split('"'):
                    if frag.endswith(".py"):
                        rel = frag.replace("${CLAUDE_PLUGIN_ROOT}/", "")
                        check((ROOT / rel).is_file(), f"{rel} exists")

    print("files")
    for rel in ("README.md", "LICENSE", "hooks/standup.py", "commands/standup.md"):
        check((ROOT / rel).is_file(), f"{rel} present")
    # Nobody's own history should ever be committed alongside the tool.
    for leaked in ROOT.rglob("sessions.jsonl"):
        check(False, f"session history committed: {leaked.relative_to(ROOT)}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
