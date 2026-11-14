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
