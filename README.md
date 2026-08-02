# standup

**What did you actually ship last week?**

If you run a few Claude Code sessions a day, you genuinely cannot remember. `standup`
reads the transcripts you already have on disk and turns them into one page: every
session, ranked by what it delivered, grouped by week, with the PRs and tickets it
produced.

No API keys. No account. No network calls. It reads files you already have.

<!-- Add a screenshot at docs/standup.png and uncomment:
![standup](docs/standup.png)
-->

## Install

As a plugin:

```
/plugin marketplace add OmriGM/standup
/plugin install standup
```

Then seed it from the history you already have:

```
/standup
```

<details>
<summary>Or as a single file, with no plugin</summary>

It is one Python file with no dependencies beyond the standard library.

```bash
mkdir -p ~/.claude/hooks
curl -o ~/.claude/hooks/standup.py \
  https://raw.githubusercontent.com/OmriGM/standup/main/hooks/standup.py
python3 ~/.claude/hooks/standup.py install
python3 ~/.claude/hooks/standup.py backfill
python3 ~/.claude/hooks/standup.py report
```

`install` registers the SessionEnd hook in `~/.claude/settings.json`. Do not do this
*and* install the plugin, or every session will be recorded twice.

</details>

## How it works

A `SessionEnd` hook appends one line per session to an append-only JSONL ledger, then
rebuilds a self-contained HTML page. Everything is derived from the transcript, which
already carries Claude Code's own session title and structured PR links, so recording
costs nothing but a file read.

- **Impact ranking.** Each session is scored by what it shipped: 40 points a PR, 12 a
  ticket, then at most 20 for focused time and 8 for depth of back-and-forth. The caps
  are the point. A four hour session with nothing to show for it can never outrank one
  that opened a PR.
- **Real working time.** Gaps over 10 minutes are not counted, so an overnight pause is
  not billed as effort. A session resumed across weeks is split across the weeks it was
  actually worked.
- **PRs opened per week**, counting each PR once, in the week it first appears.
- **Sort** by recency, impact, duration or token count. **Hide** sessions you never want
  to see again, and unhide them later.
- **Token counts** per session and per week, split between generated output, fresh
  input, cache writes and cache reads.

## Configuration

Optional. Create `~/.claude/standup/config.json`:

```json
{
  "ticket_url": "https://linear.app/your-org/issue/{key}",
  "ignore_prefixes": ["INC", "SEV"],
  "model": "haiku",
  "embed_images": false
}
```

| Key | Meaning |
| --- | --- |
| `ticket_url` | Template with a `{key}` placeholder. Works with Linear, Jira, GitHub Issues, Shortcut, anything with a predictable URL. Unset means tickets render as plain text instead of guessing at a link. |
| `ignore_prefixes` | Extra `ABC-123` shaped prefixes that are not tickets in your world. Universal false positives like `CVE` and `RFC` are already excluded. |
| `model` | Model used by `--summaries`. Defaults to `haiku`. |
| `embed_images` | Show screenshots you pasted into a prompt, as thumbnails on the card. Off by default, see below. |

Absolute file paths in a prompt are always collapsed to their filename, so a card reads
`Can you help me verify this? pasted-1.png` rather than a wall of path.

`embed_images` goes further and inlines the picture itself. It is off by default for one
reason: only a base64 data URI renders reliably (Firefox refuses `file://` subresources
outside the document's own directory), and that bakes your screenshots into a page the
privacy section tells you not to share. It also grows the page by about a third more than
each image's size on disk.

Pull request icons are chosen from the PR's own host, so GitHub gets its mark and every
other forge gets a neutral pull-request glyph.

## Week summaries

`report --summaries` writes a one sentence recap for each week and rewrites every card's
title and description into something readable. This is the only feature that calls a
model. It shells out to your local `claude` CLI, so it uses the auth you already have and
needs no API key.

```bash
python3 ~/.claude/hooks/standup.py report --summaries
```

Results are cached per week and only regenerate when a week gains new sessions.

## Privacy

Worth reading before you use this, and especially before you share anything it produces.

- **Everything is local by default.** Recording never touches the network.
- **The ledger stores the verbatim opening prompt of every session**, along with repo
  names and branch names, at `~/.claude/standup/ledger.jsonl`.
- **The generated HTML embeds all of that data.** It is not a template that reads a data
  file, it is a single file with everything baked in. Sending someone the page means
  sending them your entire history. Share a screenshot instead.
- **`--summaries` is the one thing that sends data off your machine.** It passes session
  titles and the first 240 characters of each opening prompt to a model. It is opt-in
  behind a flag and never runs automatically from the hook.

## Requirements

Python 3.9 or newer, and Claude Code. No third party packages. Desktop notifications are
macOS only and are skipped silently elsewhere.

## Credits

Neutral forge and tracker icons are [Octicons](https://primer.style/octicons) (MIT).

## License

MIT
