#!/usr/bin/env python3
"""standup: record what each Claude Code session shipped, and report on it.

Verbs:
  install           Register the SessionEnd hook in ~/.claude/settings.json.
  record            SessionEnd hook. Reads the hook JSON on stdin, adds one line per session.
  backfill [--force] Read every transcript already on disk; --force re-reads known sessions.
  report [opts]     Render the history as a self-contained HTML page.
                    --weeks N, --out PATH, --summaries (regenerate week recaps and card text).

Data lives in ~/.claude/standup/. The file is append-only JSONL and readers keep the
last entry per session id, so re-recording a session (ended twice, or a backfill over
a live session) corrects rather than double-counts.

Recording touches neither the network nor a model: every fact is lifted from the
transcript, which already carries an `ai-title` (Claude Code's own session title) and
structured `pr-link` entries. Only `report --summaries` calls a model, via the local
`claude` CLI, and only when you ask for it.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
DATA = HOME / ".claude" / "standup"
HISTORY = DATA / "sessions.jsonl"
SUMMARIES = DATA / "summaries.json"
CONFIG = DATA / "config.json"
PAGE = DATA / "standup.html"
PROJECTS = HOME / ".claude" / "projects"
LEGACY = HOME / ".claude" / "sessions"

# Kept apart rather than summed at read time: cache reads dwarf the rest and are
# nearly free, so a single total would say far more about caching than about work.
TOKEN_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache_write",
    "cache_read_input_tokens": "cache_read",
}

# <KEY>-<number> shaped tokens that look like tickets but are not. Deliberately limited
# to universal false positives; add your own with "ignore_prefixes" in config.json.
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,5})-(\d{2,6})\b")
NOT_TICKETS = {"CVE", "ISO", "RFC", "UTF", "AES", "SHA", "PEP", "TF", "HTTP", "ASCII", "GPT", "PY"}

# Every `claude -p` call this tool makes leaves its own transcript on disk, which the
# next backfill would otherwise ingest as a real session. Those fakes carry every ticket
# named in the prompt, so they score high on impact and outrank genuine work. The prompt
# carries this marker and any transcript containing it is skipped, in both directions:
# never recorded, and filtered on read so histories polluted before the fix self-heal.
SENTINEL = "standup-internal-do-not-record"
LEGACY_SENTINEL = "Below is one week of my coding sessions"


def _is_self_generated(text: str) -> bool:
    return SENTINEL in text or LEGACY_SENTINEL in text


# A gap longer than this is the human being elsewhere, not working. Summing capped
# gaps gives time-at-keyboard; first-to-last span would count a lunch break or an
# overnight pause as effort (one real session spanned 13h that way).
IDLE_GAP_MINUTES = 10


def _text_parts(entry: dict) -> list[str]:
    """Human/assistant prose from a transcript entry, minus injected context.

    system-reminder blocks carry recalled memories and CLAUDE.md, which mention
    ticket ids from *other* work. Counting those would credit a session with
    tickets it never touched, so they are dropped before the ticket scan.
    """
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if isinstance(content, str):
        blocks = [content]
    elif isinstance(content, list):
        blocks = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    else:
        return []
    return [b for b in blocks if b and "<system-reminder>" not in b]


def _repo_of(cwd: str) -> str:
    """Repo name for a working directory, collapsing worktrees onto their parent repo.

    A worktree lives at <repo>/.claude/worktrees/<name>; crediting it to "<name>"
    would scatter one repo's work across many rows.
    """
    if not cwd:
        return ""
    marker = "/.claude/worktrees/"
    if marker in cwd:
        cwd = cwd.split(marker, 1)[0]
    return os.path.basename(cwd.rstrip("/"))


def _durations(stamps: list[str]) -> tuple[int, int, dict[str, int]]:
    """(active minutes, wall span minutes, active minutes per calendar day).

    Active time sums consecutive gaps, each capped at IDLE_GAP_MINUTES, so a pause
    contributes at most that cap instead of its full length.

    The split is per *day*, not per week, because where a week begins is a user setting
    (Sunday for some, Monday for others). Bucketing at record time would bake one answer
    into the file and silently misplace hours the moment that setting changed. A
    resumed session still needs splitting: 13% of these sessions span over 7 days, and
    charging all of it to the day it started would put a fortnight of work in one bar.
    """
    seen: list[datetime] = []
    for s in stamps:
        try:
            seen.append(datetime.fromisoformat(s.replace("Z", "+00:00")))
        except ValueError:
            continue
    if len(seen) < 2:
        return 0, 0, {}
    seen.sort()
    cap = timedelta(minutes=IDLE_GAP_MINUTES)
    active = 0.0
    per_day: dict[str, float] = defaultdict(float)
    for a, b in zip(seen, seen[1:]):
        secs = min(b - a, cap).total_seconds()
        active += secs
        per_day[a.date().isoformat()] += secs
    return (
        round(active / 60),
        round((seen[-1] - seen[0]).total_seconds() / 60),
        {k: round(v / 60) for k, v in sorted(per_day.items())},
    )


def _self_check() -> None:
    """Smallest checks that fail if the duration or ticket logic breaks."""
    base = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    iso = lambda m: (base + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")  # noqa: E731

    # Three minutes of steady work: active == span.
    assert _durations([iso(0), iso(1), iso(2), iso(3)])[:2] == (3, 3)
    # A 600-minute overnight gap counts as the cap, not its full length.
    active, span, _ = _durations([iso(0), iso(1), iso(601)])
    assert (active, span) == (1 + IDLE_GAP_MINUTES, 601), (active, span)
    # Unordered input must not produce negative time.
    assert _durations([iso(5), iso(0)])[:2] == (5, 5)
    assert _durations([iso(0)])[:2] == (0, 0) and _durations([])[:2] == (0, 0)
    # A session straddling days splits its time across the days it was worked.
    day_split = _durations([iso(0), iso(1), iso(7 * 1440), iso(7 * 1440 + 1)])[2]
    assert len(day_split) == 2 and sum(day_split.values()) > 0, day_split
    assert all(len(k) == 10 and k.count("-") == 2 for k in day_split), day_split

    grab = lambda t: [f"{k}-{n}" for k, n in TICKET_RE.findall(t) if k not in NOT_TICKETS]  # noqa: E731
    assert grab("fixed ABC-4512 and XYZ-4825") == ["ABC-4512", "XYZ-4825"]
    assert grab("bump for CVE-2026-25087") == []
    # Org-specific prefixes are no longer ignored by default; config opts them out.
    assert grab("logged INC-21") == ["INC-21"]

    assert _repo_of("/x/pr-agent-pro/.claude/worktrees/my-fix") == "pr-agent-pro"
    assert _repo_of("/x/my-service") == "my-service"
    assert _repo_of("") == ""

    reminder = {"message": {"content": [{"type": "text", "text": "see <system-reminder>ABC-1</system-reminder>"}]}}
    assert _text_parts(reminder) == []

    # Impact must rank shipped output above effort, and effort must saturate.
    assert _impact({}) == 0
    assert _impact({"minutes": 15, "turns": 6}) < 10
    assert _impact({"minutes": 90, "turns": 40, "prs": [{}], "tickets": ["A-1"]}) > 60
    # A marathon that shipped nothing loses to a short session that opened a PR.
    assert _impact({"minutes": 6000, "turns": 5000}) < _impact({"prs": [{}]})
    # Past the caps, more grinding buys nothing.
    assert _impact({"minutes": 180, "turns": 60}) == _impact({"minutes": 9999, "turns": 9999})
    # The tooltip is generated from these parts, so the badge must always be their sum.
    for sample in ({}, {"minutes": 47, "turns": 13}, {"minutes": 90, "turns": 40, "prs": [{}, {}],
                                                      "tickets": ["A-1", "B-2", "C-3"]}):
        assert sum(p for p, _ in _impact_parts(sample)) == _impact(sample), sample

    assert _tokens({}) == 0 and _tokens({"tokens": {"input": 5, "output": 7}}) == 12
    # Malformed usage must not crash a whole report.
    assert _tokens({"tokens": {"input": None, "output": 3}}) == 3
    assert (_tok(999), _tok(1500), _tok(2_400_000), _tok(5_108_458_495)) == ("999", "2k", "2.4M", "5.1B")

    # Guards the escaping bug that made install append a second, duplicate hook.
    installed = [{"hooks": [{"type": "command", "command": HOOK_CMD}]}]
    assert _hook_present(installed)
    assert _hook_present(json.loads(json.dumps(installed)))
    assert not _hook_present([])
    assert not _hook_present([{"hooks": [{"command": "echo hi"}]}, {"nope": 1}, "junk"])

    # Nothing may hardcode a forge or a tracker. With no config, tickets are plain text.
    # Point at a path that cannot exist, so the check does not depend on this machine.
    global CONFIG
    real_config, CONFIG = CONFIG, DATA / "no-such-config.json"
    _config.cache_clear()
    assert _ticket_url("ABC-1") == ""
    plain = _ticket_el("ABC-1", "chip", "ABC-1")
    # Not merely "no href": the icon carries a <use href>, so check for the link itself.
    assert plain.startswith("<span") and "<a " not in plain, plain
    assert "#i-gh" in _forge_icon("https://github.com/o/r/pull/1")
    assert "#i-pr" in _forge_icon("https://gitlab.com/o/r/-/merge_requests/1")
    assert "#i-pr" in _forge_icon("https://dev.azure.com/o/p/_git/r/pullrequest/1")
    assert "#i-pr" in _forge_icon("") and "#i-pr" in _forge_icon("not a url")

    # A configured tracker turns the same ticket into a link, and extends the ignore list.
    with tempfile.TemporaryDirectory() as tmp:
        CONFIG = Path(tmp) / "config.json"
        CONFIG.write_text('{"ticket_url": "https://tracker.test/{key}", "ignore_prefixes": ["INC"]}')
        _config.cache_clear()
        assert _ticket_url("ABC-1") == "https://tracker.test/ABC-1"
        assert _ticket_el("ABC-1", "chip", "ABC-1").startswith("<a ")
        assert "INC" in _ignored_prefixes()
        # A template missing the placeholder is ignored rather than producing a broken link.
        CONFIG.write_text('{"ticket_url": "https://tracker.test/browse"}')
        _config.cache_clear()
        assert _ticket_url("ABC-1") == ""
        CONFIG.write_text("not json at all")
        _config.cache_clear()
        assert _config() == {} and _ignored_prefixes() == NOT_TICKETS

    CONFIG = real_config
    _config.cache_clear()

    # Week grouping follows the configured start day, so a Sunday-to-Thursday week
    # groups differently from an ISO one. Wed 2026-07-29 sits in both, differently.
    wed = date(2026, 7, 29)
    with tempfile.TemporaryDirectory() as tmp:
        CONFIG = Path(tmp) / "config.json"
        for start, expected in (("monday", date(2026, 7, 27)), ("sunday", date(2026, 7, 26))):
            CONFIG.write_text(json.dumps({"week_start": start}))
            _config.cache_clear()
            assert _week_of(wed) == expected, (start, _week_of(wed))
        # An unknown value must fall back to ISO rather than crash the report.
        CONFIG.write_text('{"week_start": "funday"}')
        _config.cache_clear()
        assert _week_of(wed) == date(2026, 7, 27)
        # The start day is always its own week start.
        CONFIG.write_text('{"week_start": "sunday"}')
        _config.cache_clear()
        assert _week_of(date(2026, 7, 26)) == date(2026, 7, 26)
    CONFIG = real_config
    _config.cache_clear()

    # A pasted path becomes its filename, and real URLs survive untouched.
    assert _tidy_ask("verify this? /Users/me/.claude/jobs/9f/pasted-1.png") == "verify this? pasted-1.png"
    assert _tidy_ask("see https://linear.app/acme/issue/ABC-1") == "see https://linear.app/acme/issue/ABC-1"
    assert _tidy_ask("check github.com/o/r/pull/3") == "check github.com/o/r/pull/3"
    assert _tidy_ask("") == "" and _tidy_ask("no paths here") == "no paths here"
    # Shallow paths are left alone; collapsing "/tmp/x" to "x" would lose the meaning.
    assert _tidy_ask("in /tmp/x.log") == "in /tmp/x.log"
    assert _local_images("/no/such/file/nope.png") == []

    # The tool must never ingest its own summary runs, in either direction.
    assert _is_self_generated(_polish_prompt([{"session_id": "x", "started_at": "", "title": "t"}]))
    assert not _is_self_generated("fix the login bug")

    assert _pop("2 PRs", "") == "<b>2 PRs</b>"
    assert 'class="pop"' in _pop("2 PRs", "<a></a>")
    print("self-check ok")


def summarize(transcript: Path, session_id: str = "", reason: str = "") -> dict | None:
    """Reduce one transcript to one row. Returns None if it holds no real turns."""
    title = branch = cwd = kind = ""
    first_ts = last_ts = None
    stamps: list[str] = []
    prs: dict[tuple[str, int], str] = {}
    tickets: Counter[str] = Counter()
    tokens: Counter[str] = Counter()
    user_turns = 0
    first_prompt = ""
    ignored = _ignored_prefixes()

    with transcript.open(errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue

            etype = entry.get("type")
            ts = entry.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
                stamps.append(ts)
            cwd = entry.get("cwd") or cwd
            branch = entry.get("gitBranch") or branch
            kind = entry.get("sessionKind") or kind
            session_id = session_id or entry.get("sessionId") or ""

            if etype == "ai-title":
                title = entry.get("aiTitle") or title
            elif etype == "agent-name" and not title:
                title = entry.get("agentName") or title
            elif etype == "pr-link":
                num, repo = entry.get("prNumber"), entry.get("prRepository")
                if isinstance(num, int) and repo:
                    prs[(repo, num)] = entry.get("prUrl") or ""
            elif etype in ("user", "assistant"):
                if etype == "user":
                    user_turns += 1
                else:
                    usage = (entry.get("message") or {}).get("usage")
                    if isinstance(usage, dict):
                        for src, dst in TOKEN_FIELDS.items():
                            v = usage.get(src)
                            if isinstance(v, int):
                                tokens[dst] += v
                for text in _text_parts(entry):
                    # The opening ask, in the user's own words, is the closest thing to
                    # an explanation of the session that exists without asking a model.
                    # Pasted-file paths get prepended to the prompt, so drop leading ones.
                    if etype == "user" and not first_prompt:
                        prompt = " ".join(text.split())
                        prompt = re.sub(r"^(?:/\S+\s+)+", "", prompt)
                        first_prompt = prompt[:280]
                    for key, num in TICKET_RE.findall(text):
                        if key not in ignored:
                            tickets[f"{key}-{num}"] += 1

    if not first_ts or user_turns == 0 or _is_self_generated(first_prompt):
        return None

    minutes, span_minutes, day_minutes = _durations(stamps)

    return {
        "session_id": session_id or transcript.stem,
        "title": title or first_prompt[:120] or "(untitled)",
        "ask": first_prompt,
        "repo": _repo_of(cwd),
        "branch": branch,
        "kind": kind,
        "reason": reason,
        "started_at": first_ts,
        "ended_at": last_ts,
        "minutes": minutes,
        "span_minutes": span_minutes,
        "day_minutes": day_minutes,
        "turns": user_turns,
        "tokens": dict(tokens),
        # Most-mentioned first: the ticket a session actually worked stands out
        # from one mentioned in passing.
        "tickets": [t for t, _ in tickets.most_common(8)],
        "prs": [{"repo": r, "number": n, "url": u} for (r, n), u in sorted(prs.items())],
    }


@lru_cache(maxsize=1)
def _config() -> dict:
    """User settings, read once per run. Absent or malformed config must never stop a report.

    Recognised keys:
      ticket_url       URL template with a {key} placeholder, e.g.
                       "https://linear.app/acme/issue/{key}". Unset means tickets
                       render as plain text instead of links.
      ignore_prefixes  Extra <KEY>- prefixes to treat as not-a-ticket, e.g. ["INC"].
      model            Model for `report --summaries`. Defaults to haiku.
    """
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _ignored_prefixes() -> set[str]:
    """Universal false positives, plus whatever this user added in config."""
    extra = _config().get("ignore_prefixes")
    if not isinstance(extra, list):
        return NOT_TICKETS
    return NOT_TICKETS | {str(x).upper() for x in extra}


def _migrate() -> None:
    """Move data from the pre-rename location, once, and signpost the old page.

    Someone will have the old page bookmarked, so leave a stub behind rather than
    letting them stare at a file that silently stopped updating.
    """
    if HISTORY.exists():
        return
    # Same folder, previous filename.
    renamed = DATA / "ledger.jsonl"
    if renamed.exists():
        renamed.replace(HISTORY)
        print(f"renamed ledger.jsonl to {HISTORY.name}", file=sys.stderr)
        return
    if not LEGACY.is_dir():
        return
    old_ledger = LEGACY / "ledger.jsonl"
    if not old_ledger.exists():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    for src, dst in ((old_ledger, HISTORY), (LEGACY / "summaries.json", SUMMARIES)):
        if src.exists():
            src.replace(dst)
    stub = LEGACY / "velocity.html"
    if stub.exists():
        stub.write_text(
            "<!doctype html><meta charset=utf-8><title>Moved</title>"
            '<body style="font:15px system-ui;padding:3rem;max-width:34rem">'
            "<h1>This page moved</h1><p>standup now writes to "
            f'<code>{PAGE}</code>. Update your bookmark.</p>'
            f'<p><a href="{PAGE.as_uri()}">Open it</a></p>'
        )
    print(f"moved your history from {LEGACY} to {DATA}", file=sys.stderr)


def append(row: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def load() -> list[dict]:
    """Recorded rows, one per session, last write winning, oldest first."""
    if not HISTORY.exists():
        return []
    rows: dict[str, dict] = {}
    with HISTORY.open(errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not row.get("session_id"):
                continue
            if _is_self_generated(row.get("ask") or ""):
                continue
            rows[row["session_id"]] = row
    return sorted(rows.values(), key=lambda r: r.get("started_at") or "")


def cmd_record() -> int:
    """SessionEnd hook. Must never fail loudly: a broken record must not break exit."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = payload.get("transcript_path")
    if not path or not Path(path).exists():
        return 0
    row = summarize(Path(path), payload.get("session_id") or "", payload.get("reason") or "")
    if not row:
        return 0
    append(row)
    if os.environ.get("STANDUP_NOTIFY") == "1":
        _notify(row)
    return 0


def _notify(row: dict) -> None:
    """Best-effort macOS banner so a finished session reports itself.

    No-op elsewhere: osascript is macOS-only and shelling out to it on Linux would
    just emit a confusing error from inside a hook nobody is watching.
    """
    if sys.platform != "darwin":
        return
    bits = []
    if row["prs"]:
        bits.append(f"{len(row['prs'])} PR" + ("s" if len(row["prs"]) > 1 else ""))
    if row["tickets"]:
        bits.append(row["tickets"][0])
    if row["minutes"]:
        bits.append(f"{row['minutes']}m")
    subtitle = " · ".join(bits) or row.get("repo", "")
    title = row["title"][:60].replace('"', "'")
    subtitle = subtitle[:60].replace('"', "'")
    os.system(
        'osascript -e \'display notification "%s" with title "Session logged" subtitle "%s"\' '
        ">/dev/null 2>&1 &" % (subtitle, title)
    )


def cmd_backfill(argv: list[str]) -> int:
    """Read every transcript on disk. --force re-reads sessions already recorded.

    The file is append-only and load() keeps the last row per session, so a forced
    pass simply supersedes older rows rather than needing to rewrite the file.
    """
    force = "--force" in argv
    known = {r["session_id"] for r in load()}
    added = 0
    for transcript in sorted(PROJECTS.glob("*/*.jsonl")):
        if transcript.stem in known and not force:
            continue
        try:
            row = summarize(transcript)
        except Exception as exc:  # one unreadable transcript must not stop the sweep
            print(f"skip {transcript.name}: {exc}", file=sys.stderr)
            continue
        if row:
            append(row)
            added += 1
    verb = "refreshed" if force else "backfilled"
    print(f"{verb} {added} sessions ({len(known)} already present)")
    return 0


HOOK_CMD = 'STANDUP_NOTIFY=1 python3 "$HOME/.claude/hooks/standup.py" record; python3 "$HOME/.claude/hooks/standup.py" report'  # noqa: E501


def _hook_present(ends: list) -> bool:
    """Walk the hook entries. Matching against json.dumps() would fail, because dumping
    escapes the quotes inside the command and the raw string stops being a substring."""
    for entry in ends:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks") or []:
            if isinstance(h, dict) and "standup.py" in str(h.get("command", "")):
                return True
    return False


def cmd_install() -> int:
    """Register the SessionEnd hook in ~/.claude/settings.json, leaving the rest intact."""
    path = HOME / ".claude" / "settings.json"
    try:
        settings = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as exc:
        print(f"{path} is not valid JSON, refusing to touch it: {exc}", file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        print(f"{path} is not a JSON object, refusing to touch it", file=sys.stderr)
        return 1

    hooks = settings.setdefault("hooks", {})
    ends = hooks.setdefault("SessionEnd", [])
    if not isinstance(ends, list):
        print(f"hooks.SessionEnd in {path} is not a list, refusing to touch it", file=sys.stderr)
        return 1
    if _hook_present(ends):
        print("already installed")
        return 0
    ends.append({"hooks": [{"type": "command", "command": HOOK_CMD, "async": True, "timeout": 20}]})

    # Write via a temp file so an interrupted run cannot truncate a live settings.json.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(path)
    print(f"installed SessionEnd hook in {path}\nnow run: python3 {Path(__file__).resolve()} backfill")
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

CSS = """
/* Near-black surfaces, hairline borders, one violet accent.
   Dark only and flat by intent -- no gradients anywhere on this page. */
:root{
 --bg:#08080a; --fg:#f2f2f5; --muted:#9b9baa; --faint:#6e6e7c;
 --card:#131316; --card-hi:#1b1b21; --chip:#1a1a20; --line:#26262c;
 --accent:#a793ff; --brand:#7c5cff; --hair:#1d1d23;
 /* A 1px top highlight is what reads as a real surface catching light. */
 --edge:inset 0 1px 0 rgba(255,255,255,.045);
 --ease:cubic-bezier(.32,.72,0,1);
 --spring:cubic-bezier(.34,1.4,.64,1);
}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"SF Pro Text",ui-sans-serif,"Segoe UI",sans-serif;
 -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
 font-variant-numeric:tabular-nums;letter-spacing:-.005em}
.wrap{max-width:1180px;margin:0 auto;padding:60px 26px 96px}

/* Staggered enter: header, chart, then each week group. */
.rise{animation:rise .5s cubic-bezier(.2,0,0,1) backwards;animation-delay:calc(var(--i,0)*100ms)}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}


/* Keyboard focus must stay visible: every chip is a link. */
a:focus-visible{outline:2px solid var(--brand);outline-offset:3px;border-radius:8px}

h1{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display",ui-sans-serif,sans-serif;
 font-size:40px;font-weight:700;line-height:1.08;letter-spacing:-.035em;margin:0 0 8px;
 text-wrap:balance}
h1 span{color:var(--accent)}
/* The one gradient on the page, deliberately, because it is the logotype. */
h1 .wt{display:inline-block;font-style:normal;font-size:1.32em;font-weight:800;
 letter-spacing:-.045em;margin-right:.05em;vertical-align:-.02em;
 background-image:linear-gradient(135deg,#c4b5fd 0%,#8b6dff 45%,#5b3df5 100%);
 background-size:220% 100%;background-position:0% 50%;
 -webkit-background-clip:text;background-clip:text;color:transparent;
 -webkit-text-fill-color:transparent;
 transform:rotate(-7deg);transform-origin:55% 65%;cursor:default;
 animation:tilt .65s var(--spring) both;
 transition:background-position .55s var(--ease)}
@keyframes tilt{from{opacity:0;transform:rotate(4deg) scale(.86)}
 to{opacity:1;transform:rotate(-7deg) scale(1)}}
/* Hover does a double-take: it flinches upright, overshoots, and the gradient
   sweeps across. Only the element itself moves, so the text clip stays aligned. */
h1 .wt:hover{background-position:100% 50%;animation:wtf .6s var(--spring)}
@keyframes wtf{
 0%{transform:rotate(-7deg) scale(1)}
 18%{transform:rotate(8deg) scale(1.16)}
 42%{transform:rotate(-13deg) scale(1.16)}
 66%{transform:rotate(5deg) scale(1.08)}
 84%{transform:rotate(-9deg) scale(1.02)}
 100%{transform:rotate(-7deg) scale(1)}}
/* The why, set apart as its own surface so it reads before the numbers do. */
.pitch{max-width:66ch;margin:18px 0 16px;padding:15px 19px;background:var(--card);
 border:1px solid var(--line);border-left:3px solid var(--brand);border-radius:14px;
 box-shadow:var(--edge);color:var(--muted);font-size:15px;line-height:1.62;text-wrap:pretty}
.pitch b{color:var(--fg);font-weight:660}
.sub{color:var(--muted);font-size:14.5px;margin:0 0 40px;letter-spacing:-.01em}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:48px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 20px;
 box-shadow:var(--edge);transition:border-color .3s var(--ease),transform .3s var(--spring)}
.tile:hover{border-color:#33333c;transform:translateY(-2px)}
.tile b{display:block;font-size:30px;font-weight:680;letter-spacing:-.04em;line-height:1.05;
 color:var(--accent);font-variant-numeric:tabular-nums}
.tile span{display:block;margin-top:5px;color:var(--faint);font-size:11px;
 text-transform:uppercase;letter-spacing:.08em;font-weight:600}

h2{font-size:11.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--faint);
 margin:0 0 14px;font-weight:700}

.chart{background:var(--card);border:1px solid var(--line);border-radius:20px;
 padding:24px 20px 16px;margin-bottom:50px;box-shadow:var(--edge)}
.bars{display:flex;align-items:flex-end;gap:10px;overflow-x:auto}
.bar{flex:1 0 40px;display:flex;flex-direction:column;align-items:center;gap:7px}
/* The plot area needs its own explicit height. A percentage against the whole column
   would include the value and the date label, so any bar over about two thirds would
   clamp to the same pixel height and the chart would silently understate its peaks. */
.track{height:120px;width:100%;display:flex;align-items:flex-end}
.bar i{display:block;width:100%;min-height:3px;border-radius:6px;background:var(--brand);
 transition:background-color .25s var(--ease);
 /* Grow out of the axis on load, one after another. */
 transform-origin:bottom;animation:grow .8s var(--ease) backwards;
 animation-delay:calc(var(--i,0)*70ms + 150ms)}
@keyframes grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}
.bar b,.bar em{animation:fade .5s var(--ease) both;animation-delay:calc(var(--i,0)*70ms + 350ms)}
@keyframes fade{from{opacity:0}to{opacity:1}}
.bar:hover i{background:var(--accent)}
.bar i.empty{background:var(--line)}
.bar:hover i.empty{background:var(--line)}
/* Outlined, not filled: reads as a boundary marker rather than a week to beat. */
.bar i.seed{background:transparent;box-shadow:inset 0 0 0 1.5px var(--line)}
.bar:hover i.seed{background:transparent;box-shadow:inset 0 0 0 1.5px var(--brand)}
.bar[title]{cursor:help}
.bar[title] b{color:var(--faint)}
.bar b{font-size:12.5px;font-weight:650;letter-spacing:-.01em}
.bar em{font-style:normal;font-size:11px;color:var(--faint);white-space:nowrap}

/* Sort acts inside each week, so weekly totals stay true whatever the order. */
/* Sticky so sorting stays reachable, with the blur Apple uses to keep chrome legible
   over content instead of hiding it behind a solid slab. */
.sortbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;
 justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px;padding:14px 0;
 background:color-mix(in srgb,var(--bg) 76%,transparent);
 -webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px)}
.sortbar h2{margin:0}
.tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sorts{position:relative;display:flex;gap:2px;background:var(--card);border:1px solid var(--line);
 border-radius:12px;padding:4px;box-shadow:var(--edge)}
/* One pill that slides between options, rather than a background flicking on and off. */
.thumb{position:absolute;top:4px;bottom:4px;left:0;width:0;border-radius:8px;background:var(--brand);
 opacity:0;transition:transform .42s var(--spring),width .42s var(--spring),opacity .2s}
.thumb.ready{opacity:1}
.ghost{appearance:none;background:var(--card);border:1px solid var(--line);color:var(--muted);
 border-radius:12px;padding:14px 15px;font:600 12px/1 inherit;cursor:pointer;box-shadow:var(--edge);
 transition:background-color .25s var(--ease),color .25s var(--ease),border-color .25s var(--ease),
 transform .3s var(--spring)}
.ghost:hover{color:var(--fg);border-color:var(--brand);transform:translateY(-1px)}
.ghost:active{transform:scale(.96)}
.ghost.on{background:var(--brand);border-color:var(--brand);color:#fff}
.ghost:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.sortbtn{position:relative;z-index:1;appearance:none;border:0;background:transparent;
 color:var(--muted);cursor:pointer;font:600 12px/1 inherit;letter-spacing:.01em;
 display:inline-flex;align-items:center;gap:6px;padding:8px 13px;border-radius:8px;
 transition:color .25s var(--ease)}
.sortbtn::after{content:"";position:absolute;inset:-6px 0}
.sortbtn:hover{color:var(--fg)}
.sortbtn.on{color:#fff}
.sortbtn:active{transform:scale(.96)}
.sortbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.week{margin-bottom:36px}
.weekhead{display:flex;align-items:center;gap:12px;padding:0 2px 12px;flex-wrap:wrap;
 cursor:pointer;list-style:none}
.weekhead::-webkit-details-marker{display:none}
.weekhead:focus-visible{outline:2px solid var(--accent);outline-offset:4px;border-radius:8px}
/* Chevron points right when closed, down when open. */
.weekhead::before{content:"";flex:none;width:7px;height:7px;margin-bottom:2px;
 border-right:1.7px solid var(--faint);border-bottom:1.7px solid var(--faint);
 transform:rotate(-45deg);transition-property:transform,border-color;transition-duration:.2s;
 transition-timing-function:cubic-bezier(.2,0,0,1)}
details[open]>.weekhead::before{transform:rotate(45deg)}
.weekhead:hover::before{border-color:var(--accent)}
.weekhead h3{margin:0 auto 0 0;font-size:15.5px;letter-spacing:-.015em;font-weight:650;
 text-wrap:balance}
.weekhead .meta{color:var(--muted);font-size:12.5px;white-space:nowrap}
.weekhead .meta b{color:var(--accent);font-weight:650}
/* Model-written, so it is set apart from the counted facts above it. */
.recap{margin:0 0 16px;padding:0 0 0 12px;border-left:2px solid var(--brand);color:var(--muted);
 font-size:13.5px;line-height:1.6;max-width:80ch;text-wrap:pretty}

/* Hover/focus reveal: the counts in the header open the items behind them. */
.pop-host{position:relative;display:inline-block;cursor:default;
 border-bottom:1px dashed var(--line)}
.pop-host:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}
.pop{position:absolute;top:calc(100% + 10px);right:0;z-index:30;width:300px;max-height:322px;
 overflow-y:auto;overscroll-behavior:contain;display:flex;flex-direction:column;gap:2px;
 background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px;
 text-align:left;white-space:normal;box-shadow:0 16px 40px -12px #000;
 opacity:0;visibility:hidden;transform:translateY(-6px) scale(.97);transform-origin:top right;
 transition-property:opacity,transform,visibility;transition-duration:.18s;
 transition-timing-function:cubic-bezier(.2,0,0,1)}
.pop-host:hover .pop,.pop-host:focus-within .pop{opacity:1;visibility:visible;transform:none}
/* Bridges the 10px gap so the pointer can travel into the panel without it closing. */
.pop::before{content:"";position:absolute;top:-10px;left:0;right:0;height:10px}
.pop-row{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:6px;
 font-size:12px;font-weight:600;color:var(--fg);opacity:0;transform:translateY(-4px);
 transition-property:opacity,transform,background-color;transition-duration:.2s;
 transition-timing-function:cubic-bezier(.2,0,0,1)}
.pop-host:hover .pop-row,.pop-host:focus-within .pop-row{opacity:1;transform:none;
 transition-delay:calc(var(--i,0)*22ms)}
.pop-row .ico{width:13px;height:13px;flex:none;fill:var(--accent)}
.pop-row span{flex:none}
.pop-row em{font-style:normal;font-weight:500;font-size:11.5px;color:var(--faint);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pop-row:hover{background:var(--card-hi)}

/* The score needs explaining where people meet it, not only in the footer. */
.tipinfo{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;
 border:1px solid var(--line);border-radius:50%;background:var(--card);box-shadow:var(--edge);
 cursor:help;flex:none;
 transition:color .25s var(--ease),border-color .25s var(--ease)}
.tipinfo>b{font-size:12px;font-weight:700;color:var(--faint);line-height:1}
.tipinfo::after{content:"";position:absolute;inset:-7px;border-radius:50%}
.tipinfo:hover{border-color:var(--brand)}
.tipinfo:hover>b{color:var(--accent)}
.pop.tip{width:290px;padding:14px 16px;display:block;font-size:12.5px;line-height:1.62;
 color:var(--muted);font-weight:400;white-space:normal}
.pop.tip>b{display:block;margin-bottom:7px;color:var(--fg);font-size:12.5px;font-weight:660}
.pop.tip em{font-style:normal;font-weight:700;color:var(--accent)}
.pop.tip i{display:block;margin-top:9px;padding-top:9px;border-top:1px solid var(--hair);
 font-style:normal;color:var(--faint);font-size:11.5px;line-height:1.55}

/* A details element has no native open transition, so reuse the page's enter animation. */
details[open]>.recap,details[open]>.cards{animation:rise .3s cubic-bezier(.2,0,0,1) both}

/* Three across on desktop, stepping down rather than squeezing. */
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
@media (max-width:980px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:640px){.cards{grid-template-columns:1fr}}

.card{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);
 border-radius:16px;padding:18px 20px;min-height:132px;cursor:pointer;box-shadow:var(--edge);
 transition:background-color .3s var(--ease),border-color .3s var(--ease),
 transform .4s var(--spring),box-shadow .3s var(--ease);
 animation:rise .5s var(--ease) backwards;animation-delay:calc(var(--n,0)*35ms)}
.card:hover{background:var(--card-hi);border-color:#3a3a45;transform:translateY(-3px);
 box-shadow:var(--edge),0 12px 28px -12px rgba(0,0,0,.7)}
.card:active{transform:translateY(-1px) scale(.995)}
.card.open{border-color:var(--brand);cursor:default}

/* 0fr to 1fr animates a height the browser cannot otherwise interpolate. */
.more{display:grid;grid-template-rows:0fr;transition:grid-template-rows .4s var(--ease)}
.card.open .more{grid-template-rows:1fr}
.more-in{overflow:hidden;min-height:0}
.more dl{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;margin:14px 0 0;
 padding-top:14px;border-top:1px solid var(--hair);font-size:12px}
.more dt{color:var(--faint);font-weight:600;white-space:nowrap}
.more dd{margin:0;color:var(--muted);overflow-wrap:anywhere}
.more .full{margin:12px 0 0;padding:11px 13px;background:var(--bg);border-radius:10px;
 color:var(--muted);font-size:12px;line-height:1.6;max-height:180px;overflow-y:auto;
 white-space:pre-wrap;overflow-wrap:anywhere}
.more .bd{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.more .bd span{background:var(--chip);border:1px solid var(--line);border-radius:6px;
 padding:3px 8px;font-size:11px;color:var(--muted);font-weight:600}
.more .bd b{color:var(--accent);font-weight:700}
/* flex-start, not baseline: the hide button holds only an SVG, so its baseline is the
   bottom margin edge and it floats above the pill it sits beside. */
.card .top{display:flex;align-items:flex-start;gap:12px}
.card .name{flex:1;min-width:0;font-weight:650;font-size:14.5px;letter-spacing:-.01em;
 line-height:1.4;text-wrap:balance}
/* One cluster so the controls share an edge and the pair aligns as a unit. The nudge
   sits them on the title's first line rather than the top of its line box. */
.card .act{flex:none;display:flex;align-items:center;gap:6px;margin-top:-1px}
/* A pill weighted by magnitude: faint when nothing shipped, filled and haloed when
   plenty did. min-width keeps one and three digit scores on the same optical edge. */
.card .score{flex:none;display:inline-flex;align-items:center;justify-content:center;gap:4px;
 min-width:44px;font-size:11.5px;font-weight:700;
 line-height:1;padding:6px 9px;border-radius:999px;font-variant-numeric:tabular-nums;
 border:1px solid transparent;
 transition:background-color .3s var(--ease),color .3s var(--ease),box-shadow .3s var(--ease)}
.card .score.lo{color:var(--faint);background:transparent;border-color:var(--line)}
.card .score.mid{color:var(--accent);background:var(--chip);border-color:#352f63}
.card .score.hi{color:#fff;background:var(--brand);border-color:var(--brand);
 box-shadow:0 0 0 3px rgba(124,92,255,.15)}
.card:hover .score.hi{box-shadow:0 0 0 5px rgba(124,92,255,.2)}

/* Hiding stays out of the way until you want it, but never leaves the tab order.
   The 40px box is mostly transparent and pulled back in with negative margin, so the
   hit area meets the minimum while the visible control stays small. */
.hide{appearance:none;border:0;background:transparent;color:var(--faint);cursor:pointer;flex:none;
 display:inline-grid;place-items:center;width:40px;height:40px;margin:-7px;border-radius:50%;
 opacity:0;transition-property:opacity,color,background-color;transition-duration:.2s;
 transition-timing-function:var(--ease)}
.hide .ico{grid-area:1/1;width:14px;height:14px;fill:currentColor;
 transition-property:opacity,scale,filter;transition-duration:.3s;
 transition-timing-function:cubic-bezier(.2,0,0,1)}
/* Cross-fade the two states instead of swapping the glyph outright. */
.hide .gu{opacity:0;scale:.25;filter:blur(4px)}
.card.hid .hide .gx{opacity:0;scale:.25;filter:blur(4px)}
.card.hid .hide .gu{opacity:1;scale:1;filter:blur(0)}
.card:hover .hide,.hide:focus-visible{opacity:1}
.hide:hover{color:var(--fg);background:var(--chip)}
.hide:active{transform:scale(.96)}
.card.hid{display:none}
.card.hid .hide{opacity:1}
/* Revealed only while the Hidden toggle is on, and clearly marked as set aside.
   Dimming uses filter, not opacity: every card runs the rise animation with a both
   fill, and an animation's final value beats a plain opacity declaration. */
body.reveal .card.hid{display:flex;border-style:dashed;
 filter:grayscale(1) opacity(.45);transition:filter .3s var(--ease)}
body.reveal .card.hid:hover{filter:none}
/* Clamped so a long opening request can't stretch its whole row. */
.card .ask{margin:7px 0 0;color:var(--muted);font-size:13px;line-height:1.55;text-wrap:pretty;
 display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
/* Pasted screenshots: contained, never dictating the card's height. */
.shot{display:block;margin-top:10px;border-radius:8px;overflow:hidden}
.shot img{display:block;width:100%;max-height:150px;object-fit:cover;object-position:top;outline:1px solid rgba(255,255,255,.1);outline-offset:-1px;border-radius:8px}
.shot:hover img{outline-color:var(--brand)}

/* margin-top:auto keeps chips on the baseline of every card in the row. */
.card .foot{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:auto;padding-top:14px}
.card .foot:empty{display:none;}

a{color:var(--accent);text-decoration:none}
.chip{position:relative;display:inline-flex;align-items:center;gap:6px;border-radius:7px;
 padding:6px 10px;font-size:12px;line-height:1.35;white-space:nowrap;background:var(--chip);
 color:var(--accent);font-weight:600;border:1px solid var(--line);
 transition-property:background-color,border-color,transform;transition-duration:.15s}
.chip .ico{width:13px;height:13px;flex:none;fill:currentColor}
/* Outline icons: supplied here, not as attributes, so .ico's fill cannot win. */
.ico.ln{fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.sortbtn .ico{width:13px;height:13px;flex:none;fill:currentColor}
/* The bolt reads high and right in its box, so it is nudged back optically. */
.card .score .ico{width:10px;height:10px;flex:none;fill:currentColor;margin-top:.5px}
/* Visible chip is ~31px tall; bleed 5px per side to clear 40px without colliding. */
a.chip::after{content:"";position:absolute;inset:-5px}
a.chip:hover{background:var(--card-hi);border-color:var(--brand)}
a.chip:active{transform:scale(.96)}
/* Optical nudge: a borderless label reads as indented next to ringed chips. */
.chip.time{background:transparent;color:var(--faint);font-weight:500;border-color:transparent;
 padding-left:2px}

.empty-note{color:var(--muted);font-size:14px;padding:22px 0}
footer{display:flex;flex-direction:column;gap:16px;margin-top:56px;
 border-top:1px solid var(--hair);color:var(--faint);font-size:12px;
 line-height:1.65;padding-top:18px;text-wrap:pretty}
footer p{margin:0}
/* Byline sits above the small print, aligned right, reusing the GitHub mark
   already in the sprite rather than shipping a second copy of it. */
.by{align-self:flex-end;display:inline-flex;align-items:center;gap:8px;padding:8px 13px;
 background:var(--card);border:1px solid var(--line);border-radius:11px;box-shadow:var(--edge);
 color:var(--muted);font-size:12.5px;font-weight:600;white-space:nowrap;
 transition:color .25s var(--ease),border-color .25s var(--ease),transform .3s var(--spring)}
.by:hover{color:var(--fg);border-color:var(--brand);transform:translateY(-2px)}
.by .ico{width:15px;height:15px;fill:currentColor;flex:none}

/* Last on purpose: these override rules declared further up, and at equal
   specificity the later rule wins. Higher in the sheet they would do nothing. */
@media (prefers-reduced-motion:reduce){
 .rise{animation:none}
 .card:hover{transform:none}
 a.chip:active{transform:none}
 /* The reveal still happens, it just stops moving. */
 .pop{transform:none;transition-duration:.01ms}
 .pop-row{opacity:1;transform:none;transition-delay:0s!important;transition-duration:.01ms}
 details[open]>.recap,details[open]>.cards{animation:none}
 .weekhead::before{transition:none}
 .card,.bar i,.bar b,.bar em{animation:none}
 h1 .wt,h1 .wt:hover{animation:none;opacity:1}
 .thumb{transition:opacity .2s}
 .more{transition:none}
 .tile:hover,.card:hover,.ghost:hover{transform:none}
}
"""

# Official marks, defined once as <symbol>s and referenced per chip, so the page
# stays self-contained without repeating the path data hundreds of times.
GITHUB_SVG = '<svg class="ico" aria-hidden="true"><use href="#i-gh"/></svg>'
LINEAR_SVG = '<svg class="ico" aria-hidden="true"><use href="#i-ln"/></svg>'
GIT_SVG = '<svg class="ico" aria-hidden="true"><use href="#i-pr"/></svg>'
TAG_SVG = '<svg class="ico" aria-hidden="true"><use href="#i-tag"/></svg>'


def _forge_icon(url: str) -> str:
    """GitHub gets its own mark; every other forge gets a neutral pull-request glyph.

    Shipping only marks we can attribute cleanly avoids guessing at, and misusing,
    the trademarks of forges we cannot test against.
    """
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        return GIT_SVG
    return GITHUB_SVG if "github" in host else GIT_SVG


def _ticket_icon() -> str:
    return LINEAR_SVG if "linear.app" in str(_config().get("ticket_url", "")) else TAG_SVG

SPRITE = (
    '<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
    '<symbol id="i-gh" viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 '
    "3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09"
    "-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07"
    "-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 "
    "0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 "
    "1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 "
    '1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></symbol>'
    '<symbol id="i-ln" viewBox="0 0 100 100"><path d="M1.22541 61.5228c-.2225-.9485'
    ".90748-1.5459 1.59638-.857L38.3326 96.1783c.6889.6889.0915 1.8189-.857 1.5964C19.2892 93.4753 "
    "4.52469 78.7108 1.22541 61.5228ZM.00189135 46.8891c-.01764375.2833.08887215.5599.28957165.7606L52"
    ".3503 99.7085c.2007.2007.4773.3072.7606.2896 2.3692-.1476 4.6938-.46 6.9624-.9259.7645-.157 1"
    ".0301-1.0963.4782-1.6481L2.57595 39.4485c-.55186-.5519-1.49117-.2863-1.648174.4782-.465915 2"
    ".2686-.77832 4.5932-.92588465 6.9624ZM4.21093 29.7054c-.16649.3738-.08169.8106.20765 1.1l64"
    ".77602 64.776c.2894.2894.7262.3742 1.1.2077 1.7861-.7956 3.5171-1.6927 5.1855-2.684.5521-.3281"
    ".6373-1.0938.1836-1.5475L8.44305 24.3859c-.45375-.4537-1.21951-.3685-1.54755.1836-.99133 1.6684"
    "-1.88837 3.3994-2.68457 5.1859ZM12.6587 18.074c-.3701-.3701-.3931-.9637-.0443-1.3541C21.7795 6"
    ".45931 35.1114 0 49.9519 0 77.5927 0 100 22.4073 100 50.0481c0 14.8405-6.4593 28.1724-16.7199 "
    '37.3375-.3903.3488-.984.3258-1.3541-.0443L12.6587 18.074Z"/></symbol>'
    # Octicons (MIT), used for forges and trackers we do not ship a brand mark for.
    '<symbol id="i-pr" viewBox="0 0 16 16"><path d="M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 '
    "2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 "
    "2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 "
    "3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 "
    '0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z"/></symbol>'
    '<symbol id="i-tag" viewBox="0 0 16 16"><path d="M1 7.775V2.75C1 1.784 1.784 1 2.75 1h5.025c.464 0 '
    ".91.184 1.238.513l6.25 6.25a1.75 1.75 0 0 1 0 2.474l-5.026 5.026a1.75 1.75 0 0 1-2.474 0l-6.25-6.25A1.75 "
    "1.75 0 0 1 1 7.775Zm1.5 0c0 .066.026.13.073.177l6.25 6.25a.25.25 0 0 0 .354 0l5.025-5.025a.25.25 0 0 0 "
    '0-.354l-6.25-6.25a.25.25 0 0 0-.177-.073H2.75a.25.25 0 0 0-.25.25ZM6 5a1 1 0 1 1 0 2 1 1 0 0 1 0-2Z"/>'
    "</symbol>"
    # Real glyphs rather than the "x" and "undo" characters, which shift between fonts.
    '<symbol id="i-x" viewBox="0 0 16 16"><path d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.749.749 '
    "0 0 1 1.275.326.749.749 0 0 1-.215.734L9.06 8l3.22 3.22a.749.749 0 0 1-.326 1.275.749.749 0 0 "
    '1-.734-.215L8 9.06l-3.22 3.22a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042L6.94 8 3.72 '
    '4.78a.75.75 0 0 1 0-1.06Z"/></symbol>'
    '<symbol id="i-undo" viewBox="0 0 16 16"><path d="M1.705 8.005a.75.75 0 0 1 .834.656 5.5 5.5 0 0 0 '
    "9.592 2.97l-1.204-1.204a.25.25 0 0 1 .177-.427h3.646a.25.25 0 0 1 .25.25v3.646a.25.25 0 0 "
    "1-.427.177l-1.38-1.38A7.002 7.002 0 0 1 1.05 8.84a.75.75 0 0 1 .656-.834ZM8 2.5a5.487 5.487 0 0 "
    "0-4.131 1.869l1.204 1.204A.25.25 0 0 1 4.896 6H1.25A.25.25 0 0 1 1 5.75V2.104a.25.25 0 0 "
    '1 .427-.177l1.38 1.38A7.002 7.002 0 0 1 14.95 7.16a.75.75 0 0 1-1.49.178A5.5 5.5 0 0 0 8 2.5Z"/>'
    "</symbol>"
    # Outline icons carry no fill/stroke attributes: the .ln class supplies them, because
    # CSS beats presentation attributes and .ico{fill:currentColor} would fill them solid.
    '<symbol id="i-bolt" viewBox="0 0 16 16"><path d="M9.6 1 3 9.3h3.7L6.1 15l6.6-8.3H9L9.6 1Z"/></symbol>'
    '<symbol id="i-clock" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.2"/>'
    '<path d="M8 4.3V8l2.7 1.7"/></symbol>'
    '<symbol id="i-hour" viewBox="0 0 16 16"><path d="M4.3 1.9h7.4M4.3 14.1h7.4'
    "M5.4 1.9v2.3c0 1.9 2.6 2.6 2.6 3.8s-2.6 1.9-2.6 3.8v2.3"
    'M10.6 1.9v2.3c0 1.9-2.6 2.6-2.6 3.8s2.6 1.9 2.6 3.8v2.3"/></symbol>'
    '<symbol id="i-stack" viewBox="0 0 16 16"><ellipse cx="8" cy="4.2" rx="5.2" ry="2.3"/>'
    '<path d="M2.8 4.2v7.6c0 1.27 2.33 2.3 5.2 2.3s5.2-1.03 5.2-2.3V4.2"/>'
    '<path d="M2.8 8c0 1.27 2.33 2.3 5.2 2.3S13.2 9.27 13.2 8"/></symbol>'
    "</svg>"
)


# Deep absolute paths only, and never the tail of a URL: the lookbehind keeps
# "https://host/a/b" from matching at its second slash.
PATH_RE = re.compile(r"(?<![:\w/])(?:file://)?/(?:[^\s/'\"<>]+/){2,}[^\s'\"<>]+")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_EMBED_BYTES = 1_000_000


def _tidy_ask(text: str) -> str:
    """Collapse absolute paths to their basename so a card reads as a sentence.

    A pasted screenshot turns the opening prompt into a wall of path, which tells the
    reader nothing the filename does not.
    """
    return PATH_RE.sub(lambda m: os.path.basename(m.group(0).rstrip("/")), text or "")


def _local_images(text: str) -> list[Path]:
    """Image files named in a prompt that are still on disk."""
    found = []
    for raw in PATH_RE.findall(text or ""):
        p = Path(raw.replace("file://", "", 1))
        if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file() and p not in found:
            found.append(p)
    return found


def _image_html(text: str) -> str:
    """Inline pasted screenshots, but only when the user opted in.

    Off by default on purpose. Only a data URI renders reliably (Firefox refuses
    file:// subresources outside the document's own directory), and that bakes the
    picture into a page whose whole privacy story is "do not send this to anyone".
    """
    if not _config().get("embed_images"):
        return ""
    import base64
    import mimetypes

    out = []
    for p in _local_images(text):
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        if len(blob) > MAX_EMBED_BYTES:
            continue
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        b64 = base64.b64encode(blob).decode("ascii")
        # The thumbnail is embedded so it always renders; the link points at the original
        # file so full size costs nothing. Embedding both would double the page for it.
        out.append(
            f'<a class="shot" href="{html.escape(p.as_uri())}" target="_blank" rel="noreferrer">'
            f'<img loading="lazy" alt="{html.escape(p.name)}" src="data:{mime};base64,{b64}"></a>'
        )
    return "".join(out)


def _details(r: dict, score: int) -> str:
    """The panel behind a card. Everything here is already in the row, unshown until now."""
    rows: list[tuple[str, str]] = []
    where = " · ".join(x for x in (r.get("repo"), r.get("branch")) if x)
    if where:
        rows.append(("Where", html.escape(where)))

    try:
        began = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
        ended = datetime.fromisoformat((r.get("ended_at") or r["started_at"]).replace("Z", "+00:00"))
        rows.append(("When", html.escape(f"{began:%a %-d %b, %H:%M} to {ended:%H:%M}")))
    except (KeyError, ValueError):
        pass

    mins, span = r.get("minutes", 0), r.get("span_minutes", 0)
    if span:
        # Active versus elapsed is the interesting pair: it shows how much of a long
        # session was actually at the keyboard.
        rows.append(("Time", f"{mins}m at the keyboard, {span}m elapsed"))
    if r.get("turns"):
        rows.append(("Turns", str(r["turns"])))

    tok = r.get("tokens") or {}
    if tok:
        rows.append((
            "Tokens",
            " · ".join(
                f"{_tok(tok.get(k, 0))} {lbl}"
                for k, lbl in (("output", "out"), ("input", "in"),
                               ("cache_write", "cache write"), ("cache_read", "cache read"))
            ),
        ))

    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    full = _tidy_ask(r.get("ask") or "")
    return (
        '<div class="more"><div class="more-in">'
        + (f"<dl>{dl}</dl>" if dl else "")
        + (f'<p class="full">{html.escape(full)}</p>' if full else "")
        + "</div></div>"
    )


def _tokens(r: dict) -> int:
    """Every token the session moved, cache reads included."""
    t = r.get("tokens") or {}
    return sum(v for v in t.values() if isinstance(v, int))


def _tok(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _tok_title(r: dict) -> str:
    t = r.get("tokens") or {}
    parts = [f"{lbl} {_tok(t.get(k, 0))}" for k, lbl in
             (("output", "output"), ("input", "input"), ("cache_write", "cache write"), ("cache_read", "cache read"))]
    return " · ".join(parts)


def _score_tier(score: int) -> str:
    """Weight the badge by magnitude so a wall of cards ranks itself at a glance.

    40 is one pull request, so "mid" means the session shipped something at all and
    "hi" means it shipped more than once.
    """
    if score >= 100:
        return "hi"
    return "mid" if score >= 40 else "lo"


def _impact_parts(r: dict) -> list[tuple[int, str]]:
    """The score broken into the pieces shown in its tooltip.

    Each piece is rounded here rather than the total being rounded afterwards, so the
    number on the badge is exactly the sum of the reasons given for it. Round-then-sum
    and sum-then-round disagree, and a score that cannot be added up is not explainable.
    """
    parts: list[tuple[int, str]] = []
    prs, tix = len(r.get("prs", [])), len(r.get("tickets", []))
    if prs:
        parts.append((40 * prs, f"{prs} PR" + ("s" if prs != 1 else "")))
    if tix:
        parts.append((12 * tix, f"{tix} ticket" + ("s" if tix != 1 else "")))
    parts.append((round(min(r.get("minutes", 0), 180) / 180 * 20), "time spent"))
    parts.append((round(min(r.get("turns", 0), 60) / 60 * 8), "back and forth"))
    return parts


def _impact(r: dict) -> int:
    """Score a session by what it shipped, using effort only as a tiebreaker.

    A PR is the strongest evidence of delivered work and a ticket is tracked
    intent, so both dominate. Time and turns are deliberately capped low: a long
    grind with nothing to show for it must never outrank a session that shipped.
    """
    return sum(pts for pts, _ in _impact_parts(r))


def _polish_prompt(rows: list[dict]) -> str:
    """Ask for the week recap and every card's rewrite in one round trip."""
    lines = []
    for r in rows:
        prs = ", ".join(f"{p['repo']}#{p['number']}" for p in r.get("prs", []))
        lines.append(
            f'id={r["session_id"]} | repo={r.get("repo") or "none"} | {r.get("minutes", 0)}m'
            + (f" | PRs {prs}" if prs else "")
            + (f' | tickets {" ".join(r.get("tickets", []))}' if r.get("tickets") else "")
            + f' | transcript title: {r.get("title") or "(none)"}'
            + f' | opening request: {r.get("ask", "")[:240]}'
        )
    return (
        f"[{SENTINEL}]\n"
        "Below is one week of my coding sessions, one per line, each with an id.\n\n"
        + "\n".join(lines)
        + "\n\nReturn ONLY a JSON object, no prose and no code fences, shaped:\n"
        '{"recap": "...", "cards": {"<id>": {"title": "...", "line": "..."}}}\n\n'
        "recap: ONE sentence, max 30 words, on what I actually worked on this week. Name the recurring "
        "themes and repos, do not list every session.\n"
        "cards: one entry per id above. title is at most 6 words naming the concrete thing worked on, in "
        "title-less sentence case, no trailing period. line is one sentence, max 20 words, on what the "
        "session did. Both must describe the work itself, never the conversation, so never write "
        '"the user asked" or "investigated a question". If a session is too vague to describe, reuse its '
        "transcript title and say plainly what little is known.\n"
        "Plain past tense throughout. No em-dashes anywhere."
    )


def _polish(rows: list[dict]) -> dict:
    """Recap plus per-card rewrites for one week. Empty dict on any failure."""
    try:
        res = subprocess.run(
            ["claude", "-p", _polish_prompt(rows), "--model", "haiku"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        return {}
    # Strip a stray ```json fence before parsing; the model is asked not to add one.
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", res.stdout.strip())
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("cards"), dict):
        return {}
    cards = {
        sid: {"title": str(v.get("title", ""))[:120], "line": str(v.get("line", ""))[:300]}
        for sid, v in data["cards"].items()
        if isinstance(v, dict)
    }
    return {"recap": " ".join(str(data.get("recap", "")).split())[:400], "cards": cards}


def _week_polish(by_week: dict[date, list[dict]], refresh: bool) -> dict[date, dict]:
    """Cached per-week recaps and card rewrites, refreshed only when a week grows.

    Without --summaries nothing is generated and the page renders whatever is
    already cached, which keeps the SessionEnd hook free of model calls.
    """
    try:
        cache = json.loads(SUMMARIES.read_text())
    except (OSError, ValueError):
        cache = {}

    out: dict[date, dict] = {}
    wrote = False
    for key, rows in sorted(by_week.items(), reverse=True):
        if not rows:
            continue
        rows = sorted(rows, key=lambda r: r["started_at"])
        name = key.isoformat()
        stamp = f"{len(rows)}:{max(r['started_at'] for r in rows)}"
        hit = cache.get(name) if isinstance(cache.get(name), dict) else None
        if hit and hit.get("stamp") == stamp:
            out[key] = hit
            continue
        if not refresh:
            # A stale entry still describes most of the week, so show it rather than nothing.
            if hit:
                out[key] = hit
            continue
        fresh = _polish(rows)
        if fresh:
            entry = {"stamp": stamp, **fresh}
            out[key] = entry
            cache[name] = entry
            wrote = True
            print(f"summarized {name} ({len(rows)} sessions)", file=sys.stderr)
        elif hit:
            out[key] = hit

    if wrote:
        SUMMARIES.parent.mkdir(parents=True, exist_ok=True)
        SUMMARIES.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return out


# Where the working week begins. Sunday for much of the Middle East, Monday for ISO.
WEEK_STARTS = {"sunday": 6, "monday": 0}


def _week_start_weekday() -> int:
    """Python weekday index the week begins on. Monday (ISO) unless configured."""
    return WEEK_STARTS.get(str(_config().get("week_start", "monday")).lower(), 0)


def _week_of(day: date) -> date:
    """The date the containing week begins on, honouring the configured start day."""
    return day - timedelta(days=(day.weekday() - _week_start_weekday()) % 7)


def _week_key(iso: str) -> date:
    return _week_of(datetime.fromisoformat(iso.replace("Z", "+00:00")).date())


def _week_label(start: date) -> str:
    return f"{start:%b %-d}"


def _ticket_url(t: str) -> str:
    """Ticket link from the configured template. Empty when no tracker is configured."""
    tpl = str(_config().get("ticket_url", "") or "")
    return tpl.replace("{key}", t) if "{key}" in tpl else ""


def _ticket_el(t: str, cls: str, inner: str, extra: str = "") -> str:
    """A ticket links out when a tracker is configured, and is plain text otherwise."""
    url = _ticket_url(t)
    body = f"{_ticket_icon()}{inner}"
    if not url:
        return f'<span class="{cls}"{extra}>{body}</span>'
    return f'<a class="{cls}"{extra} href="{html.escape(url)}">{body}</a>'


def _pop(label: str, rows: str) -> str:
    """A count in the week header that reveals the items behind it on hover/focus."""
    if not rows:
        return f"<b>{label}</b>"
    return f'<span class="pop-host" tabindex="0"><b>{label}</b><span class="pop">{rows}</span></span>'


def cmd_report(argv: list[str]) -> int:
    weeks = 8
    out = PAGE
    summaries = "--summaries" in argv
    for i, a in enumerate(argv):
        if a == "--weeks" and i + 1 < len(argv):
            weeks = int(argv[i + 1])
        elif a == "--out" and i + 1 < len(argv):
            out = Path(argv[i + 1]).expanduser()

    all_rows = [r for r in load() if r.get("started_at")]
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    rows = [r for r in all_rows if datetime.fromisoformat(r["started_at"].replace("Z", "+00:00")) >= cutoff]

    by_week: dict[date, list[dict]] = defaultdict(list)
    for r in rows:
        by_week[_week_key(r["started_at"])].append(r)

    # PRs and tickets are deduped globally: one PR worked across three sessions is
    # one delivery, not three.
    all_prs = {(p["repo"], p["number"]) for r in rows for p in r.get("prs", [])}
    all_tickets = {t for r in rows for t in r.get("tickets", [])}
    total_min = sum(r.get("minutes", 0) for r in rows)

    # Hours per week are folded up from each session's per-day split, so a session
    # resumed across weeks lands in the weeks it was actually worked, and so changing
    # week_start regroups existing history instead of needing a re-record.
    hours: dict[date, float] = defaultdict(float)
    for r in rows:
        for day, mins in (r.get("day_minutes") or {}).items():
            try:
                hours[_week_of(date.fromisoformat(day))] += mins / 60
            except ValueError:
                continue
        # Rows written before the per-day split still have a week bucket; place their
        # time at the start of that ISO week rather than dropping it.
        for wk, mins in (r.get("week_minutes") or {}).items():
            try:
                y, w = wk.split("-W")
                hours[_week_of(date.fromisocalendar(int(y), int(w), 1))] += mins / 60
            except ValueError:
                continue

    # Credit a PR to the first week it shows up in, which is the week it was opened.
    # Scanned over all history, not the visible window: otherwise every PR merely
    # revisited inside the window would pile onto its oldest week and inflate that bar.
    pr_week: dict[tuple[str, int], date] = {}
    for r in sorted(all_rows, key=lambda x: x["started_at"]):
        wk = _week_key(r["started_at"])
        for p in r.get("prs", []):
            pr_week.setdefault((p["repo"], p["number"]), wk)
    opened: Counter[date] = Counter(pr_week.values())

    ordered = sorted(set(by_week) | set(hours))
    # Everything already open when tracking began lands in the first recorded week, so
    # that bar is a backlog, not a week's output. Mark it instead of letting it read as a record.
    seed = _week_key(all_rows[0]["started_at"]) if all_rows else None
    peak = max((n for k, n in opened.items() if k != seed), default=1) or 1

    bars = []
    for bi, key in enumerate(ordered):
        n = opened.get(key, 0)
        is_seed = key == seed
        cls = "seed" if is_seed else ("" if n else "empty")
        tip = " title=\"Tracking starts here, so this bar includes PRs opened earlier\"" if is_seed else ""
        bars.append(
            f'<div class="bar" style="--i:{bi}"{tip}><b>{n}</b>'
            f'<span class="track"><i class="{cls}" style="height:{min(100, max(3, round(n / peak * 100)))}%"></i></span>'
            f"<em>{html.escape(_week_label(key))}</em></div>"
        )

    polish = _week_polish(by_week, summaries)

    sections = []
    for n, key in enumerate(reversed(ordered)):
        wrows = sorted(by_week[key], key=lambda r: r["started_at"], reverse=True)
        wcards = polish.get(key, {}).get("cards") or {}
        wmin = round(hours.get(key, 0.0) * 60)

        # First writer wins and wrows is newest-first, so an item that spans several
        # sessions is credited to the most recent one that touched it.
        wprs: dict[tuple[str, int], tuple[str, str]] = {}
        wtix: dict[str, str] = {}
        cards = []
        for r in wrows:
            fixed = wcards.get(r.get("session_id", ""), {})
            raw_ask = r.get("ask") or ""
            name = _tidy_ask(fixed.get("title") or r.get("title") or "")
            ask = fixed.get("line") or _tidy_ask(raw_ask)
            shots = _image_html(raw_ask)
            for p in r.get("prs", []):
                wprs.setdefault((p["repo"], p["number"]), (p.get("url") or "#", name))
            for t in r.get("tickets", []):
                wtix.setdefault(t, name)

            when = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
            chips = [
                f'<a class="chip" href="{html.escape(p["url"] or "#")}">{_forge_icon(p.get("url") or "")}'
                f'{html.escape(p["repo"].split("/")[-1])} #{p["number"]}</a>'
                for p in r.get("prs", [])
            ]
            chips += [_ticket_el(t, "chip", html.escape(t)) for t in r.get("tickets", [])]
            mins = r.get("minutes", 0)
            tok = _tokens(r)
            chips.append(
                f'<span class="chip time"{f" title=\"{_tok_title(r)}\"" if tok else ""}>'
                f'{when:%a %-d %b}{f" &middot; {mins}m" if mins else ""}'
                f'{f" &middot; {_tok(tok)}" if tok else ""}</span>'
            )
            # The badge explains its own arithmetic, so the number is never a bare assertion.
            parts = _impact_parts(r)
            score = sum(p for p, _ in parts)
            why = html.escape(f"Impact {score} = " + " + ".join(f"{p} for {lbl}" for p, lbl in parts))
            cards.append(
                f'<article class="card" style="--n:{min(len(cards), 9)}" '
                f'data-ts="{html.escape(r["started_at"])}" '
                f'data-impact="{score}" data-min="{mins}" data-tok="{tok}" '
                f'data-sid="{html.escape(r.get("session_id") or "")}">'
                f'<div class="top"><span class="name">{html.escape(name)}</span>'
                f'<span class="act"><span class="score {_score_tier(score)}" title="{why}"><svg class="ico" aria-hidden="true"><use href="#i-bolt"/></svg>{score}</span>'
                '<button type="button" class="hide" title="Hide this session">'
                '<svg class="ico gx" aria-hidden="true"><use href="#i-x"/></svg>'
                '<svg class="ico gu" aria-hidden="true"><use href="#i-undo"/></svg>'
                "</button></span></div>"
                + (f'<p class="ask">{html.escape(ask)}</p>' if ask else "")
                + shots
                + f'<div class="foot">{"".join(chips)}</div>'
                + _details(r, score)
                + "</article>"
            )

        pr_rows = "".join(
            f'<a class="pop-row" style="--i:{i}" href="{html.escape(url)}">{_forge_icon(url)}'
            f'<span>{html.escape(repo.split("/")[-1])} #{num}</span><em>{html.escape(title)}</em></a>'
            for i, ((repo, num), (url, title)) in enumerate(sorted(wprs.items()))
        )
        tix_rows = "".join(
            _ticket_el(
                t,
                "pop-row",
                f"<span>{html.escape(t)}</span><em>{html.escape(title)}</em>",
                f' style="--i:{i}"',
            )
            for i, (t, title) in enumerate(sorted(wtix.items()))
        )
        wtok = sum(_tokens(r) for r in wrows)
        meta = (
            f'<b>{wmin // 60}h{wmin % 60:02d}</b> &middot; {len(wrows)} session{"s" if len(wrows) != 1 else ""}'
            f' &middot; {_pop(f"{len(wprs)} PR" + ("s" if len(wprs) != 1 else ""), pr_rows)}'
            f' &middot; {_pop(f"{len(wtix)} ticket" + ("s" if len(wtix) != 1 else ""), tix_rows)}'
            + (f" &middot; <b>{_tok(wtok)}</b> tokens" if wtok else "")
        )
        recap = html.escape(polish.get(key, {}).get("recap", ""))
        sections.append(
            f'<details class="week rise" style="--i:{min(n + 3, 6)}"{" open" if n == 0 else ""}>'
            f'<summary class="weekhead"><h3>Week of {html.escape(_week_label(key))}</h3>'
            f'<span class="meta">{meta}</span></summary>'
            + (f'<p class="recap">{recap}</p>' if recap else "")
            + f'<div class="cards">{"".join(cards)}</div></details>'
        )

    span_days = 0
    if rows:
        lo = datetime.fromisoformat(rows[0]["started_at"].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(rows[-1]["started_at"].replace("Z", "+00:00"))
        span_days = (hi - lo).days + 1

    body = f"""{SPRITE}<div class="wrap">
<header class="rise" style="--i:0">
<h1><em class="wt">WT*</em> did I just <span>ship</span>?</h1>
<p class="pitch"><b>AI burnout is real.</b> You shipped all week and cannot name one thing you did.
Not last week, not yesterday. This reads your own sessions back to you: what rolled out,
which tickets you touched, what you actually asked the agent. Walk into standup ready.</p>
<p class="sub">Last {weeks} weeks &middot; {span_days} days of history &middot; updated {datetime.now():%a %-d %b, %H:%M}</p>
</header>
<div class="tiles rise" style="--i:1">
  <div class="tile"><b>{len(all_prs)}</b><span>Pull requests</span></div>
  <div class="tile"><b>{len(all_tickets)}</b><span>Tickets</span></div>
  <div class="tile"><b>{round(len(all_prs) / max(1, len(ordered)), 1)}</b><span>PRs per week</span></div>
  <div class="tile"><b>{len(rows)}</b><span>Sessions</span></div>
  <div class="tile" title="{_tok(sum(r.get("tokens", {}).get("output", 0) for r in rows))} of it generated output">
    <b>{_tok(sum(_tokens(r) for r in rows))}</b><span>Tokens</span></div>
  <div class="tile"><b>{total_min // 60}h</b><span>Hours</span></div>
</div>
<section class="rise" style="--i:2">
<h2>Pull requests opened per week</h2>
<div class="chart"><div class="bars">
{''.join(bars) or '<span class="empty-note">No sessions recorded yet.</span>'}
</div></div>
</section>
<div class="sortbar rise" style="--i:2">
  <h2>Sessions by week</h2>
  <div class="tools">
    <button type="button" class="ghost hiddenbtn" aria-pressed="false" hidden>Hidden 0</button>
    <div class="sorts" role="group" aria-label="Sort sessions within each week">
      <span class="thumb" aria-hidden="true"></span>
      <button type="button" class="sortbtn on" data-k="ts"><svg class="ico ln" aria-hidden="true"><use href="#i-clock"/></svg>Recent</button>
      <button type="button" class="sortbtn" data-k="impact"><svg class="ico" aria-hidden="true"><use href="#i-bolt"/></svg>Impact</button>
      <button type="button" class="sortbtn" data-k="min"><svg class="ico ln" aria-hidden="true"><use href="#i-hour"/></svg>Longest</button>
      <button type="button" class="sortbtn" data-k="tok"><svg class="ico ln" aria-hidden="true"><use href="#i-stack"/></svg>Tokens</button>
    </div>
    <span class="pop-host tipinfo" tabindex="0" role="note" aria-label="How impact is scored">
      <b aria-hidden="true">?</b>
      <span class="pop tip">
        <b>How impact is scored</b>
        <em>40</em> per pull request opened, <em>12</em> per ticket touched. Then up to
        <em>20</em> for time at the keyboard, maxing out at three hours, and up to
        <em>8</em> for depth of back and forth, maxing out at sixty turns.
        <i>Effort is capped on purpose. A long session that shipped nothing can never
        outrank one that opened a pull request. Hover any score to see its own sum.</i>
      </span>
    </span>
  </div>
</div>
{''.join(sections) or '<p class="empty-note">Nothing recorded yet. Run <code>standup.py backfill</code>.</p>'}
<footer>
<a class="by" href="https://github.com/OmriGM" target="_blank" rel="noreferrer">{GITHUB_SVG}<span>Built by Omri Grossman</span></a>
<p>Built from Claude Code transcripts on this machine. Hours are time at keyboard: gaps over
{IDLE_GAP_MINUTES} minutes are not counted, and a session resumed across weeks is split into the weeks it was
actually worked. A session is a unit of <em>work</em>, not of delivery &mdash; the PR and ticket chips are the
shipped output. The chart counts each PR once, in the week it first appears, so revisiting an old PR does not
inflate a later week; a week header instead counts every PR it <em>touched</em>, which is why the two can differ.
The first bar is drawn as an outline because tracking starts there: it absorbs every PR that was already open,
so it is a backlog rather than a week's output, and it is left out of the chart's scale.
Only the most recent week is expanded by default.
<br><br>Hiding a card with its &times; keeps it out of the view without touching your history: the choice is stored
in this browser under <code>standup.hidden</code> and survives the page being rebuilt. Press <em>Hidden N</em> to
show what you set aside, then &#8634; on any card to bring it back. The weekly counts, the tiles and the chart
still include hidden sessions, because they are a record of what happened rather than of what you want to look at.
<br><br>Token counts come from each assistant message's reported usage and are summed per session. The headline
figure is every token moved, which is dominated by cache reads: they are re-sent context rather than new work, so
hover any token figure for the split between generated output, fresh input, cache writes and cache reads. Sessions
recorded before Claude Code reported usage show no token figure at all rather than a misleading zero.
<br><br>Impact scores shipped output, not effort: 40 per PR, 12 per ticket, then at most 20 for focused time
(saturating at 3h) and 8 for depth of back-and-forth (saturating at 60 turns). The caps are the point &mdash; a
long session with nothing to show for it cannot outrank one that opened a PR. Sorting reorders cards inside each
week, so the weekly totals above stay true. Week recaps are written by a local <code>claude -p</code> call and
cached; regenerate them with <code>standup.py report --summaries</code>.</p>
</footer>
</div>
<script>
(() => {{
  const grids = [...document.querySelectorAll('.cards')];
  const btns = [...document.querySelectorAll('.sortbtn')];
  // Descending on every key: newest, highest impact, longest.
  const cmp = k => k === 'ts'
    ? (a, b) => b.dataset.ts.localeCompare(a.dataset.ts)
    : (a, b) => b.dataset[k] - a.dataset[k];

  // The active pill is one element that slides, so switching sorts reads as movement
  // rather than as one box vanishing and another appearing.
  const thumb = document.querySelector('.thumb');
  const slide = b => {{
    thumb.style.width = b.offsetWidth + 'px';
    thumb.style.transform = 'translateX(' + b.offsetLeft + 'px)';
    thumb.classList.add('ready');
  }};

  for (const b of btns) b.addEventListener('click', () => {{
    for (const x of btns) x.classList.toggle('on', x === b);
    slide(b);
    for (const g of grids) {{
      const sorted = [...g.children].sort(cmp(b.dataset.k));
      sorted.forEach((el, i) => {{
        el.style.setProperty('--n', Math.min(i, 9));
        g.appendChild(el);
      }});
    }}
  }});
  const active = document.querySelector('.sortbtn.on');
  if (active) requestAnimationFrame(() => slide(active));
  addEventListener('resize', () => {{
    const on = document.querySelector('.sortbtn.on');
    if (on) slide(on);
  }});
  // The popovers live inside the week summary, so their clicks would otherwise toggle it shut.
  for (const h of document.querySelectorAll('.pop-host'))
    for (const ev of ['click', 'keydown']) h.addEventListener(ev, e => e.stopPropagation());

  // Hidden cards persist by session id. localStorage is unavailable in some file://
  // contexts, so every access is guarded and hiding simply lasts one visit if it fails.
  const KEY = 'standup.hidden';
  const cards = [...document.querySelectorAll('.card')];
  const toggle = document.querySelector('.hiddenbtn');
  let hidden;
  try {{ hidden = new Set(JSON.parse(localStorage.getItem(KEY)) || []); }} catch {{ hidden = new Set(); }}
  const save = () => {{ try {{ localStorage.setItem(KEY, JSON.stringify([...hidden])); }} catch {{}} }};

  function apply() {{
    let n = 0;
    for (const c of cards) {{
      const off = hidden.has(c.dataset.sid);
      c.classList.toggle('hid', off);
      c.querySelector('.hide').title = off ? 'Show this session again' : 'Hide this session';
      if (off) n++;
    }}
    toggle.textContent = 'Hidden ' + n;
    toggle.hidden = !n;
    if (!n) {{
      document.body.classList.remove('reveal');
      toggle.classList.remove('on');
      toggle.setAttribute('aria-pressed', 'false');
    }}
  }}

  for (const c of cards) c.querySelector('.hide').addEventListener('click', () => {{
    const id = c.dataset.sid;
    hidden.has(id) ? hidden.delete(id) : hidden.add(id);
    save();
    apply();
  }});

  toggle.addEventListener('click', () => {{
    const on = document.body.classList.toggle('reveal');
    toggle.classList.toggle('on', on);
    toggle.setAttribute('aria-pressed', String(on));
  }});

  apply();

  // Click a card to see what is behind it. Links, chips and the hide button keep
  // their own behaviour, so only clicks on the card itself toggle.
  for (const c of cards) c.addEventListener('click', e => {{
    if (e.target.closest('a, button')) return;
    const opening = !c.classList.contains('open');
    c.classList.toggle('open', opening);
    if (opening) for (const o of cards) if (o !== c) o.classList.remove('open');
  }});

  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Tiles count up to their value. Purely for the small hit of watching it land.
  if (!still) for (const el of document.querySelectorAll('.tile b')) {{
    const m = el.textContent.trim().match(/^([\\d.]+)(\\D*)$/);
    if (!m) continue;
    const target = parseFloat(m[1]), suffix = m[2];
    const dp = (m[1].split('.')[1] || '').length;
    let t0 = null;
    const tick = t => {{
      t0 ??= t;
      const p = Math.min((t - t0) / 1000, 1);
      el.textContent = (target * (1 - Math.pow(1 - p, 4))).toFixed(dp) + suffix;
      if (p < 1) requestAnimationFrame(tick);
    }};
    el.textContent = (0).toFixed(dp) + suffix;
    requestAnimationFrame(tick);
  }}
}})();
</script>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"<!doctype html><html><head><meta charset=utf-8>"
        f'<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>standup</title><style>{CSS}</style></head><body>{body}</body></html>"
    )
    print(out)
    return 0


def main() -> int:
    verb = sys.argv[1] if len(sys.argv) > 1 else "record"
    if verb != "self-check":
        _migrate()
    if verb == "record":
        return cmd_record()
    if verb == "backfill":
        return cmd_backfill(sys.argv[2:])
    if verb == "install":
        return cmd_install()
    if verb == "report":
        return cmd_report(sys.argv[2:])
    if verb == "self-check":
        _self_check()
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # a hook must never break the session it observes
        print(f"standup: {exc}", file=sys.stderr)
        sys.exit(0)
