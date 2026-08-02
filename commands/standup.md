---
name: standup
description: Rebuild the standup page from your Claude Code history and open it
---

Rebuild the standup page and open it in the browser.

Arguments passed to this command: `$ARGUMENTS`

1. Run the reporter, forwarding any arguments the user gave:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/standup.py" report $ARGUMENTS
   ```

   Useful flags: `--summaries` regenerates the per-week recap and the rewritten card
   titles (one `claude -p` call per stale week, so it takes a minute and is the only
   part that talks to a model). `--weeks N` changes how far back the page goes.

2. The command prints the path it wrote. Open it with `open` on macOS or `xdg-open`
   on Linux.

3. Report back in one line: how many weeks and sessions the page covers, and whether
   summaries were regenerated. Do not paste the page contents into the conversation.

If the ledger is empty, run `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/standup.py" backfill`
first to seed it from the transcripts already on disk, then report again.
