# Working on standup

One Python file, `hooks/standup.py`, plus the manifests around it. No dependencies beyond
the standard library, and that is a feature worth protecting.

## Before every commit

```bash
python3 hooks/standup.py self-check          # asserts the scoring and parsing logic
python3 .github/check_manifests.py           # asserts the manifest invariants
HOME="$(mktemp -d)" python3 hooks/standup.py report   # must render from an empty history
```

CI runs all three, plus the version check below. Run them locally first.

## Bump the version. Every time.

**If you change anything under `hooks/`, `commands/` or `.claude-plugin/`, bump
`version` in `.claude-plugin/plugin.json` in the same commit.**

Installs are cached per version. A change pushed under an unchanged version reaches
nobody, and it looks like it worked. This has already happened: three commits shipped
under a stale `0.1.1` and no user ever saw them.

`.github/check_version_bumped.py` fails the build if you forget. Do not work around it.

Everything after the bump is automatic. Push to `main` and the release workflow tags the
commit and publishes a GitHub Release. Never create a release by hand.

## Traps this codebase has already fallen into

Each of these shipped once. They are cheap to reintroduce and hard to spot.

- **CSS animation `fill-mode: both` outranks `:hover`.** A filled animation keeps applying
  its final `transform` and `opacity` forever, beating any normal declaration. It silently
  killed the card hover lift and the dimming of hidden cards. Use `backwards` for staggered
  entrances, so the delay is covered and the property is released afterwards.
- **`@media (prefers-reduced-motion)` must stay at the very end of the stylesheet.** At
  equal specificity the later rule wins, so overrides placed above the rules they override
  do nothing at all.
- **CSS beats SVG presentation attributes.** `.ico { fill: currentColor }` overrides
  `fill="none"` on a symbol and renders outline icons as solid shapes. Style them with a
  class instead.
- **Python 3.9 is the floor and it is tested in CI.** No backslashes inside f-string
  expressions, which only became legal in 3.12. A local `ast.parse(feature_version=(3,9))`
  does *not* catch this; only a real 3.9 interpreter does.
- **Never declare `hooks/hooks.json` under `manifest.hooks`.** That path loads
  automatically, and naming it again makes Claude Code load it twice and refuse the plugin.
  `manifest.hooks` is only for hook files outside the standard location.
- **Round components, not the total.** The impact score is the sum of the parts its tooltip
  prints. Rounding the total instead makes badges that cannot be added up.
- **The tool must never record its own runs.** `report --summaries` shells out to `claude`,
  which leaves a transcript. It is skipped via `SENTINEL`, both on write and on read.

## Things that are deliberate

- **The generated page embeds all data.** It is one self-contained file on purpose. That is
  also why the README tells people not to share it.
- **`--summaries` is opt-in.** It is the only thing that sends anything off the machine.
  It must never run from the hook.
- **Time is stored per day, not per week.** Where a week starts is a user setting, so
  bucketing at record time would misplace hours the moment it changed.
- **Effort is capped in the impact score.** A long session that shipped nothing must never
  outrank one that opened a pull request. The self-check asserts this.

## Verify against real data

`self-check` covers the logic, but rendering bugs only show up with a real history. After
a change, run `python3 hooks/standup.py report` against your own data and open the page.
Several bugs here were invisible until someone looked at the actual output.

## Never commit

Your own history. `sessions.jsonl`, `summaries.json`, `config.json` and the generated
`standup.html` all live in `~/.claude/standup/`, and `.gitignore` catches stray copies.
The history contains the opening message of every session you have ever run.
