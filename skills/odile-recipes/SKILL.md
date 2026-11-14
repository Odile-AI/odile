---
name: odile-recipes
description: Morning brief, watchdog, research memo, CSV chart.
---

# Odile — four recipes

Load this when the user asks for a morning brief, a watchdog, a research memo or
a chart from a CSV. Each recipe is a complete job: do it in this turn, write the
file, and confirm the path with `ls -l`. A promise is not a result.

| Recipe | They say | Lands in |
|---|---|---|
| Morning brief | "brief", "run my brief", "set up my brief" | `~/briefs/YYYY-MM-DD.md` |
| Silent watchdog | "watch this page", "tell me when it changes" | `~/.hermes/scripts/watch-<slug>.py`, `~/.hermes/watch/<slug>.json`, `~/workspace/watchdog/<slug>.md` |
| Research memo | "research X", "look into X", "write me a memo" | `~/workspace/memos/YYYY-MM-DD-<slug>.md` |
| CSV chart | "analyse this CSV", "chart this file" | `~/workspace/analysis/<stem>-report.md` and `<stem>-chart.svg` |

Rules that hold for all four:

- Say "I", never "we". No emoji, no exclamation marks.
- One recipe per turn. If the user asks for two, finish one and say the other
  is next; do not interleave.
- Write the file even when a lookup fails. One honest line about the failure
  beats an empty promise.
- Never invent a number, a source, a row count or a schedule. If you did not
  read it, say so.
- End with the absolute path and the size, from `ls -l`. The file browser is
  the proof.

## 1. Morning brief

**When.** They ask for today's brief, or to set up, change or repair the daily
brief. If persistent memory has no `onboarded_at`, this is a first run: load
`odile-first-run` and follow that skill instead — it asks the five questions,
writes today's brief and creates the 07:00 job.

**Read first.** Memory holds `Name:`, `Time zone:` (plus `City:` when they
named one), `Wants:` (three things) and `Brief: HH:MM <IANA>`. If an answer is
missing, ask one question; store every answer you get in memory (target `user`)
in the same turn — `Name: …`, `Time zone: <IANA>` (and `City: …`),
`Wants: <a>; <b>; <c>` — and never re-ask an answer you hold. If the time zone
is missing, ask "Where are you? A city is enough." and store the IANA zone. If
the three things are missing, ask for three. One question at a time.

**Run it now.** Build today's brief:

1. Get the date in their zone, not UTC:
   `TZ=<zone> date +%F`.
2. One line of weather for the city:
   `curl -s 'https://wttr.in/<city>?format=3'`.
   If it fails, write "Weather unavailable." and continue.
3. One short item per thing in `Wants:`, looked up now with `web_search`.
   Include a link. If a lookup fails, one honest line for that thing.
4. Write `~/briefs/<date>.md` with the terminal, markdown, under 200 words:
   the date and zone; the weather line; the three items; one sentence on what
   you will watch tomorrow.
5. `mkdir -p ~/briefs` first, then `ls -l ~/briefs/<date>.md`.

**Schedule it.** Load `cronjob_manage` (`tool_search` for "cron schedule" if it
is not in the tool list). Read the existing jobs first: there is at most one job
named `Morning brief`. If it exists, edit it; never create a second one. Create
the job with these fields (the repo template is
`templates/cron/morning-brief.json`):

- name: `Morning brief`
- schedule: `<minute> <hour> * * *` from `Brief: HH:MM <IANA>`
- timezone: the IANA zone
- deliver: `local`
- enabled_toolsets: `["web", "terminal"]`
- prompt: the block below, with every `<placeholder>` replaced. It must be
  self-contained: the job runs in a fresh session and cannot see this chat.

```
Morning brief for <name>, <zone>. Write ~/briefs/<today YYYY-MM-DD>.md, where the date is today in <zone> (TZ=<zone> date +%F). Include the date; one line of weather for <city> from https://wttr.in/<city>?format=3; one short item with a link for each of: <thing 1>; <thing 2>; <thing 3>; and one sentence on what you will watch tomorrow. Use web_search. Under 200 words. If a lookup fails, say so in one line and continue. Write the file even if some lookups fail.
```

Verify, do not assume: read `$HOME/.hermes/cron/jobs.json` and confirm the job
is enabled, its expression matches the time asked for, its timezone is the IANA
name, and `next_run_at` is in the future. If the cron tool fails, retry once;
if it still fails, say the brief file is there and the schedule needs one more
try. Never claim a schedule you did not create.

**Done.** Say, in two sentences, where the file is and when the next one lands.
If this turn also scheduled the job, name the local time and zone.

## 2. Silent watchdog

**When.** They ask to watch a page, a price, a listing, a status page or a
document, and to hear only when it changes. This is not a recurring summary: a
tick with no change must produce no message.

**What you need.** A URL and what to watch on it. If the URL is missing, ask
one question: "Which page should I watch?" If the URL is there but the thing to
watch is vague ("watch this page"), watch the whole page text. If they say "the
price", use a match for the price on that page: fetch it once, find the price
string, and build a regex with one capture group around the number.

**The config.** Pick a slug from the host and the thing watched, lowercase
letters, digits and hyphens: `example-com-pricing`. Write
`~/.hermes/watch/<slug>.json`:

```json
{
  "version": 1,
  "url": "https://example.com/pricing",
  "label": "Example pricing — Pro plan",
  "match": "Pro plan[^$]*\$([0-9.]+)",
  "group": 1,
  "max_failures": 3,
  "log": "~/workspace/watchdog/<slug>.md"
}
```

`match` is a Python regular expression with one capture group, or `null` to
watch the whole page text. `group` defaults to 1 when the pattern has a group.
Keep the label human: it is the first line of every alert. A local
`http://127.0.0.1:<port>/...` URL is fine: the script runs on this machine. The
script writes the state file; never write or edit the state by hand.

**The script.** Write the block below byte for byte to
`~/.hermes/scripts/watch-<slug>.py` (use `write_file`, then `chmod 755`). The
script derives its slug from its own filename: `watch-<slug>.py` reads
`~/.hermes/watch/<slug>.json`. Do not edit the script per watch; the config
carries every difference. Do not write your own watchdog: a hand-written script
is not this recipe.

After writing it, check the copy:
`sha256sum ~/.hermes/scripts/watch-<slug>.py` must print
`ec137ee9abcdd30358451761c2f432fadb2646f55dafc914807b82f472ff07ee`. If it prints anything else, the file is not the tested
script; rewrite it from the block before you schedule anything.

<!-- BEGIN watch.py -->
```python
#!/usr/bin/python3
"""Odile watchdog — print nothing unless the watched value changed.

This is the script half of the silent-watchdog recipe. The odile-recipes skill
copies it to ``~/.hermes/scripts/watch-<slug>.py`` and writes one config per
watch at ``~/.hermes/watch/<slug>.json``:

    {
      "version": 1,
      "url": "https://example.com/pricing",
      "label": "Example pricing — Pro plan",
      "match": "Pro plan[^$]*\\$([0-9.]+)",   optional; group 1 by default
      "group": 1,                              optional; 0 means the whole match
      "max_failures": 3,                       consecutive failures before one alert
      "log": "~/workspace/watchdog/example-pricing.md"   optional
    }

Contract
--------
* First successful read writes the baseline and prints nothing.
* A read identical to the baseline prints nothing.
* A changed read prints one alert and stores the new value; the next identical
  read is silent again, so one change produces one alert.
* A fetch failure is not a change. Failures are counted; the script prints one
  alert when ``max_failures`` consecutive tries fail, then stays silent until
  the source recovers. Recovery resets the counter.
* Alerts are also appended to the watch log (one line per alert), so the file
  browser carries the history even when chat delivery is off.

Exit codes: 0 a normal tick (silent or an alert); 2 a broken config; 1 anything
the scheduler should surface as a failed script.

    python3 watch-example.py --debug        # diagnostics on stderr, never stdout
    python3 watch-example.py --test-change  # show the alert shape, change nothing
    python3 watch-example.py --print-value  # print the extracted value, then exit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1
TIMEOUT_SECONDS = 20
MAX_BYTES = 512 * 1024
USER_AGENT = "odile-watch/1 (+https://odile.fyi)"
PREVIEW_CHARS = 120
STATE_CHARS = 500


class ConfigError(Exception):
    """The per-watch config is missing or unusable."""


def hermes_home() -> Path:
    """The tenant's Hermes home, or ~/.hermes when the env is absent."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".hermes"


def watch_slug(script: Path) -> str:
    """The slug is the script's own filename: watch-<slug>.py."""
    stem = script.stem
    return stem[len("watch-"):] if stem.startswith("watch-") else stem


def load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except ValueError as exc:
        raise ConfigError(f"config is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config must be a JSON object: {path}")
    url = str(data.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"config needs an http(s) url: {path}")
    label = str(data.get("label") or "").strip()
    if not label:
        raise ConfigError(f"config needs a label: {path}")
    match = data.get("match")
    if match is not None and not isinstance(match, str):
        raise ConfigError(f"config match must be a string: {path}")
    group = data.get("group")
    if group is not None and (not isinstance(group, int) or group < 0):
        raise ConfigError(f"config group must be a non-negative integer: {path}")
    max_failures = data.get("max_failures", 3)
    if not isinstance(max_failures, int) or max_failures < 1:
        raise ConfigError(f"config max_failures must be a positive integer: {path}")
    return data


def fetch(url: str, timeout: float = TIMEOUT_SECONDS) -> str:
    """Bounded GET; raises OSError/ValueError on any failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 — http(s) checked
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"response larger than {MAX_BYTES} bytes")
    return body.decode("utf-8", errors="replace")


def collapse(text: str) -> str:
    return " ".join(text.split())


def extract_value(body: str, match: str | None, group: int | None) -> str:
    """The watched value: a regex match, or the whole body with whitespace collapsed."""
    if not match:
        return collapse(body)
    found = re.search(match, body, re.DOTALL)
    if found is None:
        raise ValueError(f"pattern not found: {match!r}")
    index = group if group is not None else (1 if found.groups() else 0)
    try:
        return found.group(index).strip()
    except IndexError as exc:
        raise ValueError(f"match group {index} does not exist") from exc


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def preview(value: str) -> str:
    text = collapse(value)
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


def load_state(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return None
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_log(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except OSError as exc:
        print(f"watch: could not append {path}: {exc}", file=sys.stderr)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def change_alert(label: str, url: str, old: str, new: str) -> str:
    return f"Watchdog: {label}\nChanged: {preview(old)} → {preview(new)}\n{url}"


def failure_alert(label: str, url: str, failures: int, error: str) -> str:
    return f"Watchdog: {label}\nCould not read {url} on {failures} consecutive tries: {collapse(error)}"


def run_once(config: dict, state: dict | None, body: str | None, error: str | None = None):
    """Decide one tick. Returns ``(state, alert_or_None, summary)``.

    Pure except for the caller's save: the tests drive it with a fake body or a
    fake error, so the state machine is checked without a network.
    """
    label = str(config["label"])
    url = str(config["url"])
    max_failures = int(config.get("max_failures", 3))
    state = dict(state or {})
    failures = int(state.get("failures", 0))
    alerted_failure = bool(state.get("alerted_failure", False))

    if body is None:
        failures += 1
        alert = None
        if failures >= max_failures and not alerted_failure:
            alert = failure_alert(label, url, failures, error or "unknown error")
            alerted_failure = True
        state.update({"version": STATE_VERSION, "failures": failures,
                      "alerted_failure": alerted_failure, "checked_at": utc_now()})
        return state, alert, f"failure {failures}/{max_failures}"

    value = extract_value(body, config.get("match"), config.get("group"))
    new_hash = digest(value)
    if state.get("hash") is None:
        state.update({"version": STATE_VERSION, "hash": new_hash,
                      "preview": preview(value), "failures": 0,
                      "alerted_failure": False, "checked_at": utc_now()})
        return state, None, "baseline"

    if new_hash == state.get("hash"):
        if failures or alerted_failure:
            state.update({"failures": 0, "alerted_failure": False, "checked_at": utc_now()})
        return state, None, "unchanged"

    old = str(state.get("preview", ""))
    state.update({"version": STATE_VERSION, "hash": new_hash, "preview": preview(value),
                  "failures": 0, "alerted_failure": False, "checked_at": utc_now()})
    return state, change_alert(label, url, old, value), "changed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One Odile watchdog tick.")
    parser.add_argument("--config", help="per-watch config (default: $HERMES_HOME/watch/<slug>.json)")
    parser.add_argument("--state", help="state file (default: $HERMES_HOME/watch/<slug>.state.json)")
    parser.add_argument("--debug", action="store_true", help="diagnostics on stderr")
    parser.add_argument("--test-change", action="store_true",
                        help="print the alert shape for the current value, write nothing")
    parser.add_argument("--print-value", action="store_true",
                        help="print the extracted value and exit (debugging)")
    args = parser.parse_args(argv)

    script = Path(__file__).resolve()
    slug = watch_slug(script)
    home = hermes_home()
    config_path = Path(args.config).expanduser() if args.config else home / "watch" / f"{slug}.json"
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 2

    state_path = Path(args.state).expanduser() if args.state else Path(
        config.get("state") or home / "watch" / f"{slug}.state.json").expanduser()
    log_path = Path(
        config.get("log") or Path.home() / "workspace" / "watchdog" / f"{slug}.md").expanduser()
    label = str(config["label"])
    url = str(config["url"])

    try:
        body = fetch(url)
        error = None
    except (OSError, ValueError, urllib.error.URLError) as exc:
        body, error = None, str(exc)

    if args.debug:
        print(f"watch: slug={slug} config={config_path} state={state_path}", file=sys.stderr)
        print(f"watch: url={url} fetch={'failed' if body is None else f'{len(body)} bytes'}"
              + (f" ({error})" if error else ""), file=sys.stderr)

    if body is not None and (args.print_value or args.test_change):
        try:
            value = extract_value(body, config.get("match"), config.get("group"))
        except ValueError as exc:
            print(f"watch: {exc}", file=sys.stderr)
            return 2
        if args.print_value:
            print(value)
            return 0
        current = load_state(state_path) or {}
        print(f"Watchdog: {label}\nChanged: (test) → {preview(value)}\n{url}")
        if args.debug:
            print(f"watch: test-change; stored preview={current.get('preview')!r}", file=sys.stderr)
        return 0

    state, alert, summary = run_once(config, load_state(state_path), body, error)
    try:
        save_state(state_path, state)
    except OSError as exc:
        print(f"watch: could not save {state_path}: {exc}", file=sys.stderr)
        return 1
    if alert:
        append_log(log_path, f"- {utc_now()} — {alert.splitlines()[1]}")
        print(alert)
    if args.debug:
        print(f"watch: {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```
<!-- END watch.py -->

**Test it before you schedule it.** Four commands, four expected results:

| Command | Expected |
|---|---|
| `python3 ~/.hermes/scripts/watch-<slug>.py --print-value` | the value you will compare, nothing else |
| `python3 ~/.hermes/scripts/watch-<slug>.py --debug` | no stdout; stderr says `baseline` |
| `python3 ~/.hermes/scripts/watch-<slug>.py --debug` | no stdout; stderr says `unchanged` |
| `python3 ~/.hermes/scripts/watch-<slug>.py --test-change` | the alert shape, with `(test)`; state unchanged |

If `--print-value` fails, fix the match until it returns the number or line
you want. A broken match must not be scheduled.

**Schedule it.** Load `cronjob_manage`. Create one job with these fields (the
repo template `templates/cron/watchdog.json` records the same shape):

- name: `Watch: <label>`
- schedule: `every 6h` unless they asked for a frequency; use what they asked
- no_agent: `true`
- script: `watch-<slug>.py` (a bare filename under `~/.hermes/scripts/`)
- deliver: `local` — the alert is in the watch log and the job's run output;
  the Webapp/API session is stateless, so a chat delivery can mark a good alert
  as failed
- prompt: none — in no-agent mode the script is the job

Verify: the job is listed, enabled, its schedule is what they asked for, its
name is exactly `Watch: <label>`, its script is exactly `watch-<slug>.py`, its
mode is no-agent, and its deliver is `local`. If any field differs, fix the job
before you speak. The first tick writes the baseline and prints nothing; that
is correct, not a failure.

**Done.** Say what is watched, how often it is checked, and where the alert
lands: `~/workspace/watchdog/<slug>.md`, the history in the file browser, and
the job's run output. They will hear from it only when the value changes or the
page cannot be read three times. Email delivery is ODL-235. "Pause my watchdog"
means `cronjob_manage` edit with `enabled: false`; "stop watching" means remove
the job and leave the files.

## 3. Research memo

**When.** They ask to research a topic or answer a question with sources, and to
keep it. One memo, in a file, with links.

**Do.**

1. Turn the request into one question sentence. If the topic is one ambiguous
   word, ask one clarifying question; otherwise act.
2. `web_search` two or three queries: the plain question, a primary-source
   query (`site:` an official domain when one exists), and a recent-news query.
3. `web_extract` the two or three best sources. Prefer primary sources — the
   organisation's own page, the filing, the paper, the documentation — over a
   summary of them.
4. Write `~/workspace/memos/<YYYY-MM-DD>-<slug>.md` with the terminal, under
   600 words:

```
# <title>
Date: <date> · For: <name if known>

## Question
<one sentence>

## What I found
- <finding> — [<source>](<url>)
- ...

## What I could not verify
- <the gap, or "Nothing material.">

## Sources
1. <title> — <url>
```

Every factual bullet carries a link. A number appears only if a source states
it. If two sources disagree, say both and name the disagreement. If fewer than
two sources survive extraction, write that in "What I could not verify".

5. `ls -l` the file, then answer in chat with three sentences and the path.

**Done.** The memo is in the file browser under `workspace/memos/`. The chat
answer is a summary, not the memo.

## 4. CSV chart

**When.** They upload or point at a CSV and ask what is in it, or ask for a
chart. The work happens on this machine with Python; no service is called.

**Find the file.** Use the path in their message. If there is none, take the
newest `.csv` under `~/workspace` (then `~/Downloads`, then `$HOME`) and say
which one you picked. If there is still none, ask: "Which CSV should I look at?"

**Read it.** Run Python with the standard library (`csv`, `collections`,
`statistics`, `html`). Do not use pandas or matplotlib; they are not installed.
Report the real numbers: rows, columns, each column's type (number, date,
text), missing values, and the top values of the text columns. Do not load a
file larger than 16 MiB into memory at once; count in a stream and aggregate.

**Write the report.** `~/workspace/analysis/<stem>-report.md`, under 400 words:

```
# <stem>.csv
Date: <date>
Rows: <n> · Columns: <m>

## Columns
| column | type | missing | example |

## Findings
- <three to five sentences with the real numbers>

## Chart
`<stem>-chart.svg` — <what it shows, in one sentence>
```

**Draw one chart.** `~/workspace/analysis/<stem>-chart.svg`, 960×540, brand
paper and ink: background `#F6F0E4`, text `#1C1813` and `#4A4136`, bars
`#1C1813`, grid `rgba(28,24,19,.18)`. Choose the shape the data supports:

- a text column and a number column: the number summed (or averaged, if the
  column is a rate) by the top eight categories, sorted descending;
- a text column alone: the top eight value counts;
- numbers alone: a histogram of the first number column, eight bins.

Horizontal bars, category labels on the left, value at the end of each bar,
the title naming both columns, and a footer `odile · <date> · <rows> rows`.
Truncate a label at 28 characters. Escape `&`, `<` and `>` in text. If there
are more than eight categories, say "top 8 of <n>" in the title. Do not draw a
second chart; one clear chart beats four crowded ones.

**Done.** `ls -l` both files. In chat: the row count, the one finding that
matters, and the two paths. Say what the chart does not show if the data is
thin or has many missing values.
