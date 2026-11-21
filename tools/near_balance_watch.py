#!/usr/bin/python3
"""Watch Odile's NEAR AI prepaid balance — no LLM, every 15 minutes.

Odile's team runs on one NEAR AI org with a prepaid credit limit. On
2026-11-20 that limit was reached at 06:00:02Z and the first signal was a
`HTTP 402: Credit limit exceeded` on every worker, followed by a dispatcher
crash loop. Nothing warned first, because `cloud-api.near.ai` publishes no usage
endpoint (`/v1/usage`, `/v1/credits`, `/v1/balance`, `/v1/billing` all 404) and
the limit exists only inside the 402 body.

v1 therefore measures the wall by asking for it: one 1-token completion. 200 is
healthy, 402 is the wall and carries the numbers, 429/5xx is a WARN only after
two runs in a row so a transient provider wall never pages. The second half is
the wall that kills a worker between probes: any `HTTP 402` in today's
`logs/gateway.log` or a profile's `logs/agent.log` since the previous probe is
the same CRITICAL line, so the outage is named within 15 minutes even when the
probe itself succeeds.

Silent while healthy (empty stdout = the cron delivers nothing), one line
otherwise, and always exit 0: a cron runner reads a nonzero exit as a failed
job. One line per reminder window (6 h) while a wall stands — 96 identical
lines a day is noise, not an alarm.

v2, where a real balance read goes (not built here): once credits exist, the
ledger is one call — `GET https://cloud-platform.near.ai/v1/organizations/
<org_id>/usage/balance` returns `remaining`, `spend_limit` and `total_spent`
(`docs/product/SPIKE-near-key-api.md` §3.3, §5.1). That would warn *before* the
wall and spend no token, which is strictly better. v1 does not use it because
the call needs `security: [session_token]` — the account's rotating,
user-agent-bound `rt_…` refresh token, held at HQ (`SPIKE` §5.3, §5.4) — and
that platform access token does not exist yet. The inference key cannot read
the balance (`GET …/usage/balance` answers 401, §3.5).

    ./tools/near_balance_watch.py                 # the cron's run
    ./tools/near_balance_watch.py --base-url http://127.0.0.1:9/v1 \\
        --key-file /tmp/key.txt --state /tmp/s.json --hermes-root /tmp/h
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://cloud-api.near.ai/v1"
# The model the agents run on, so the probe measures the same path they use.
MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated"
KEY_ENV = "HERMES_CUSTOM_CLOUD_API_NEAR_AI_API_KEY"
# The Odile HQ state and profile roots; both are service accounts, and the
# cron runs as root. Overridable for a test or a different host.
STATE_PATH = Path("/root/.odile/near-balance-watch.json")
HERMES_ROOT = Path("/root/.hermes")
PREFIX_LEN = 6
TIMEOUT = 20.0
REMIND_SECONDS = 6 * 3600
# A record written while the previous scan was reading would land on the wrong
# side of its `since`; a minute of overlap is cheaper than a missed wall.
OVERLAP_SECONDS = 90
SOFT_THRESHOLD = 2
STATE_VERSION = 1

SPENT_LIMIT_RE = re.compile(r"Spent:\s*(\$\d+(?:\.\d+)?),\s*Limit:\s*(\$\d+(?:\.\d+)?)")
MESSAGE_RE = re.compile(r'"message"\s*:\s*"([^"\\]{1,200})')
RECORD_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s")
LOG_RELATIVE = (("logs", "gateway.log"), ("logs", "agent.log"))


def classify(status: int) -> str:
    """One of ``healthy``, ``critical`` or ``soft`` for an HTTP status.

    ``0`` is a transport error (the probe did not reach the provider); it is
    soft, because an unreachable host is not a measured balance. A 401/403/404
    is soft for the same reason a single 429 is: the card pages on the wall the
    provider names, and one blip must not page. Every non-200 that is neither a
    402 nor a 429/5xx therefore has to persist for two runs to say anything.
    """
    if status == 200:
        return "healthy"
    if status == 402:
        return "critical"
    return "soft"


def spent_limit(text: str | None) -> str | None:
    """The provider's ``Spent: $x, Limit: $y`` sentence, or None.

    It is the only place the numbers exist (there is no balance endpoint), so
    the alert quotes it verbatim rather than re-deriving a percentage.
    """
    match = SPENT_LIMIT_RE.search(text or "")
    if not match:
        return None
    return f"Spent: {match.group(1)}, Limit: {match.group(2)}"


def provider_sentence(status: int, body: str | None) -> str:
    """One line describing a non-200 answer: the wall numbers, else its message."""
    sentence = spent_limit(body)
    if sentence:
        return f"HTTP {status}: {sentence}"
    match = MESSAGE_RE.search(body or "")
    if match:
        return f"HTTP {status}: {match.group(1).strip()}"
    return f"HTTP {status}"


def key_prefix(key: str) -> str:
    """The first ``PREFIX_LEN`` characters — enough to name a key, never a key."""
    return key[:PREFIX_LEN]


def load_key(key_file: Path | None = None, environ: dict | None = None) -> str:
    """The inference key: ``--key-file``, then the environment, then a ``.env``.

    The cron's profile home is ``$HERMES_HOME``; a wrapper started outside it
    still finds the default profile. A file that holds ``NAME=value`` lines is
    read as an env file, and a file that is one bare token is read as the key —
    both shapes are in use at HQ and neither is printed.
    """
    env = os.environ if environ is None else environ
    if key_file is None:
        for name in (KEY_ENV, "ODILE_NEAR_KEY"):
            value = (env.get(name) or "").strip()
            if value:
                return value
    paths: list[Path] = []
    if key_file is not None:
        paths.append(Path(key_file))
    else:
        home = (env.get("HERMES_HOME") or "").strip()
        if home:
            paths.append(Path(home) / ".env")
        paths.append(HERMES_ROOT / ".env")
        paths.append(Path.home() / ".hermes" / ".env")
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == KEY_ENV:
                return value.strip().strip('"').strip("'")
        bare = text.strip()
        if key_file is not None and bare and "\n" not in bare and "=" not in bare:
            return bare
    return ""


def probe(base_url: str, key: str, model: str = MODEL, timeout: float = TIMEOUT) -> tuple[int, str]:
    """Ask for one token; return ``(status, body)`` and never raise.

    A 1-token completion is the cheapest request that reaches the billing check,
    and the refusal carries the numbers. ``status`` 0 means the probe did not
    reach the provider at all.
    """
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "."}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=payload,
        method="POST",
        headers={
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
            "user-agent": "odile-near-balance-watch/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read(65536).decode("utf-8", "replace") if error.fp else ""
        finally:
            error.close()
        return error.code, body
    except (urllib.error.URLError, OSError, ValueError) as error:
        return 0, str(error)


def log_paths(root: Path) -> list[Path]:
    """Today's gateway log and every profile agent log under ``root``."""
    root = Path(root)
    found = [root.joinpath(*parts) for parts in LOG_RELATIVE]
    found += sorted(root.glob("profiles/*/logs/agent.log"))
    return [path for path in found if path.is_file()]


def scan_logs(root: Path, since: float) -> list[dict]:
    """Every ``HTTP 402`` record written since ``since`` (epoch seconds).

    A traceback is one record spread over many lines, and only the record head
    carries a timestamp: a continuation line inherits the head's, which is what
    makes the wall visible in `gateway.log`, where the 402 arrives as the last
    line of a ``RuntimeError`` traceback. A hit with no enclosing timestamp (a
    rotated file whose head was cut) is kept rather than dropped.
    """
    zone = dt.datetime.now().astimezone().tzinfo
    hits: list[dict] = []
    for path in log_paths(root):
        current: dt.datetime | None = None
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                match = RECORD_TIME_RE.match(line)
                if match:
                    current = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
                    current = current.replace(tzinfo=zone)
                if "HTTP 402" not in line:
                    continue
                if current is not None and current.timestamp() < since:
                    continue
                hits.append(
                    {
                        "path": path,
                        "at": current.timestamp() if current is not None else None,
                        "spent_limit": spent_limit(line),
                    }
                )
    return hits


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def load_state(path: Path) -> dict:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        # A different shape: the first run of this version is a fresh baseline,
        # not a burst of false transitions.
        return {}
    return state


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def emit(kind: str, line: str, state: dict, now: dt.datetime) -> str:
    """The line, unless this kind already spoke inside the reminder window."""
    previous = state.get("alert_kind")
    previous_at = parse_iso(state.get("alerted_at"))
    if previous == kind and previous_at is not None:
        if (now - previous_at).total_seconds() < REMIND_SECONDS:
            return ""
    state["alert_kind"] = kind
    state["alerted_at"] = iso(now)
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NEAR AI balance watch (v1: probe the wall).")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--hermes-root", default=str(HERMES_ROOT))
    parser.add_argument("--timeout", type=float, default=TIMEOUT)
    parser.add_argument("--no-logs", action="store_true", help="probe only, skip the 402 log scan")
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    stamp = iso(now)
    state = load_state(Path(args.state))
    state["version"] = STATE_VERSION
    key = load_key(Path(args.key_file) if args.key_file else None)
    prefix = key_prefix(key) if key else "(no key)"
    soft_streak = int(state.get("soft_streak") or 0)

    since_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    previous_scan = parse_iso(state.get("last_scan_at"))
    window_start = previous_scan if previous_scan and previous_scan > since_midnight else since_midnight
    since = window_start.timestamp() - OVERLAP_SECONDS

    hits = [] if args.no_logs else scan_logs(Path(args.hermes_root), since)
    status, body = 0, ""
    verdict = "soft"
    if key:
        status, body = probe(args.base_url, key, args.model, args.timeout)
        verdict = classify(status)

    line = ""
    if not key:
        line = emit(
            "critical",
            f"Odile NEAR AI balance CRITICAL at {stamp} — no inference key in the "
            f"profile .env ({KEY_ENV}); the 15-minute probe cannot run.",
            state,
            now,
        )
    elif verdict == "critical":
        soft_streak = 0
        line = emit(
            "critical",
            f"Odile NEAR AI balance CRITICAL at {stamp} — {provider_sentence(status, body)} "
            f"(key {prefix}). Every worker fails with HTTP 402 until credits are added.",
            state,
            now,
        )
    elif hits:
        latest = next((hit["spent_limit"] for hit in reversed(hits) if hit["spent_limit"]), None)
        detail = latest or "no Spent/Limit line"
        line = emit(
            "critical",
            f"Odile NEAR AI balance CRITICAL at {stamp} — {len(hits)} HTTP 402 line(s) "
            f"between probes since {iso(window_start)}, latest {detail} "
            f"(key {prefix}); workers failed between probes.",
            state,
            now,
        )
    elif verdict == "soft":
        soft_streak += 1
        if soft_streak >= SOFT_THRESHOLD:
            line = emit(
                "soft",
                f"Odile NEAR AI balance WARN at {stamp} — probe answered "
                f"{provider_sentence(status, body)} {soft_streak} runs in a row "
                f"(key {prefix}); balance unknown, no page yet.",
                state,
                now,
            )
    else:
        soft_streak = 0
        state["alert_kind"] = None
        state["alerted_at"] = None

    state["soft_streak"] = soft_streak
    state["last_probe_at"] = stamp
    if not args.no_logs:
        state["last_scan_at"] = stamp
    save_state(Path(args.state), state)

    if line:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
