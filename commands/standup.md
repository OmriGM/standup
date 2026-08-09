---
name: standup
description: Rebuild the standup page, or print a paste-ready summary of recent work
---

Arguments: `$ARGUMENTS`

The script takes a verb first. Pick the right one rather than appending the arguments to
a verb the user did not ask for.

**If the arguments begin with a verb** (`say`, `report`, `backfill`, `install`), pass them
straight through, since the script dispatches on the first argument:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/standup.py" $ARGUMENTS
```

**Otherwise** treat them as flags for the page and prefix the verb yourself:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/standup.py" report $ARGUMENTS
```

## After it runs

**`say`** prints a summary meant for a human to paste into chat. Show it back verbatim in
a code block and say nothing else about it. Add `--copy` if the user wants it on the
clipboard, `--days N` to widen the window past the default of one day.

**`report`** prints the path it wrote. Open it with `open` on macOS or `xdg-open` on
Linux, then report in one line how many weeks and sessions the page covers and whether
summaries were regenerated. Never paste the page contents into the conversation.
`--summaries` regenerates the weekly recaps and card titles, which is the only part that
calls a model, so mention that it takes a minute.

If the history is empty, run `backfill` first to seed it from the transcripts already on
disk, then run the original verb again.
