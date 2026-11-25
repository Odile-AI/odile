#!/usr/bin/python3
"""The off-host liveness watcher for HQ (ODL-354).

Every five minutes this script asks one question from a box that is not HQ:
does ``https://odile.fyi/health`` answer HTTP 200 with the body ``ok``?  Three
consecutive failures send Savio exactly one Matrix message; the first success
after that sends one recovery message.  It runs on ``hqrecover``, a Servury Nano
that serves no production traffic, so an HQ outage cannot silence the alarm.

Why a separate box: measured 2026-11-25, HQ's last journal entry before the
loss was 2026-11-24T11:03:06Z and the next boot began 2026-11-25T02:48:57Z —
15 h 45 m with no host.  Every board watcher (the five-minute watchdog, the
recovery sweep, the pulse) lives on HQ, so nothing paged.  The scheduler's own
log line records the missed pulse: ``Job 'Odile CoS pulse' missed its scheduled
time (2026-11-24T12:00:00+00:00, grace=5400s)``, run 2026-11-25T03:11:52Z.

The contract is deliberately small:

  * **probe** — ``curl --fail`` the liveness URL.  Success is HTTP 200 *and*
    the exact body ``ok``; a proxy or captive-portal 200 is not liveness.
  * **threshold** — 3 consecutive failures, so one blip sends nothing.
  * **one message per outage** — the Matrix transaction id is derived from
    the first failure, and the state file's ``alert_open`` gate stops a fourth
    failure from sending a second one.  A crash between the send and the state
    write retries the same transaction id, which the homeserver deduplicates.
  * **one recovery** — the same transaction-id rule, derived from the same
    outage, and only when an alert was actually delivered.
  * **state** — every probe updates ``/var/lib/odile-hq-watch/state.json``
    (last probe, last success, last alert, last recovery, consecutive
    failures); the cron log carries one line per probe.

The only secret is the Matrix bot token in
``/etc/odile-hq-watch/matrix-token`` (mode 0600, root).  The script holds no
HQ key, no tenant key, and opens no listening port.

    watch.py                 # one probe (cron runs this every 5 minutes)
    watch.py --dry-run       # probe, print the decision; write/send nothing
    watch.py --status        # print the state file as JSON
    watch.py --config PATH --state PATH
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

DEFAULT_CONFIG = Path("/etc/odile-hq-watch/config.json")
DEFAULT_STATE = Path("/var/lib/odile-hq-watch/state.json")
DEFAULT_TOKEN = Path("/etc/odile-hq-watch/matrix-token")

DEFAULTS: dict = {
    "probe_url": "https://odile.fyi/health",
    "expected_body": "ok",
    "failure_threshold": 3,
    "connect_timeout_s": 5,
    "max_time_s": 10,
    "curl": "/usr/bin/curl",
    "matrix_homeserver": "https://matrix.org",
    "matrix_room_id": None,
    "matrix_token_file": str(DEFAULT_TOKEN),
    "state_file": str(DEFAULT_STATE),
    "probe_name": socket.gethostname() or "watcher",
}

STATE_DEFAULTS: dict = {
    "schema": 1,
    "last_probe_at": None,
    "last_probe_ok": None,
    "last_success_at": None,
    "last_failure_at": None,
    "first_failure_at": None,
    "first_failure_epoch": None,
    "consecutive_failures": 0,
    "outage_peak_failures": 0,
    "last_error": None,
    "alert_open": False,
    "alert_delivered": False,
    "last_alert_at": None,
    "last_alert_event_id": None,
    "last_alert_error": None,
    "last_send_error": None,
    "last_recovery_at": None,
    "last_recovery_event_id": None,
    "last_outage_started_at": None,
    "probes_total": 0,
    "alerts_total": 0,
    "recoveries_total": 0,
}


def iso(epoch: float) -> str:
    """UTC, second precision — the format every message and state value uses."""
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(path: Path) -> dict:
    config = dict(DEFAULTS)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return config
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    config.update(value)
    if not config.get("matrix_room_id"):
        raise ValueError(f"{path} has no matrix_room_id")
    config["failure_threshold"] = int(config["failure_threshold"])
    config["connect_timeout_s"] = int(config["connect_timeout_s"])
    config["max_time_s"] = int(config["max_time_s"])
    return config


def load_state(path: Path) -> dict:
    state = dict(STATE_DEFAULTS)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return state
    try:
        value = json.loads(text)
    except ValueError:
        # A torn or hand-edited state file must not stop the alarm.  Keep the
        # bytes for the next reader and start from defaults; the worst case is
        # one duplicate message after a crash that already tore the file.
        broken = Path(str(path) + f".corrupt-{int(time.time())}")
        Path(path).replace(broken)
        print(f"state {path} was not JSON; moved to {broken}", file=sys.stderr)
        return state
    if not isinstance(value, dict):
        return state
    state.update(value)
    return state


def save_state(path: Path, state: dict) -> None:
    """Atomic replace: a crash leaves the old bytes, never half a JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    # The rename is what publishes the new bytes.  fsync the directory too, so
    # the phase-1 alert record survives a host crash before the Matrix PUT.
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    http_code: int
    body: str
    exit_code: int
    error: str
    elapsed_s: float


def probe_hq(config: dict) -> ProbeResult:
    """One curl.  ``--fail`` makes a 4xx/5xx a nonzero exit, so a status page
    that says 503 can never read as healthy."""
    curl = config.get("curl") or shutil.which("curl") or "/usr/bin/curl"
    maximum = int(config["max_time_s"])
    command = [
        curl,
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        str(maximum),
        "--connect-timeout",
        str(int(config["connect_timeout_s"])),
        "--output",
        "-",
        "--write-out",
        "\n%{http_code} %{time_total}",
        str(config["probe_url"]),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=maximum + 5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return ProbeResult(False, 0, "", 127, f"curl did not run: {error}"[:300], 0.0)

    body, separator, tail = completed.stdout.rpartition("\n")
    http_code, elapsed = 0, 0.0
    if separator:
        parts = tail.split()
        if parts and parts[0].isdigit():
            http_code = int(parts[0])
        if len(parts) > 1:
            try:
                elapsed = float(parts[1])
            except ValueError:
                pass
    body = body.strip()
    expected = str(config["expected_body"]).strip()
    error = completed.stderr.strip()
    if completed.returncode == 0 and http_code == 200 and body == expected:
        return ProbeResult(True, http_code, body, 0, "", elapsed)
    if not error:
        if http_code != 200:
            error = f"HTTP {http_code or 'no response'}"
        else:
            error = f"body {body[:80]!r} != {expected!r}"
    return ProbeResult(False, http_code, body, completed.returncode, error[:300], elapsed)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    event_id: str | None
    error: str


def send_matrix(config: dict, text: str, transaction: str) -> SendResult:
    """One Matrix message via the client-server API, idempotent on ``transaction``."""
    token_file = Path(config["matrix_token_file"])
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        mode = token_file.stat().st_mode & 0o777
        if mode & 0o077:
            print(
                f"warning: {token_file} mode {mode:04o}, expected 0600",
                file=sys.stderr,
            )
    except OSError as error:
        return SendResult(False, None, f"cannot read {token_file}: {error}"[:300])
    if not token:
        return SendResult(False, None, f"{token_file} is empty")

    room = urllib.parse.quote(str(config["matrix_room_id"]), safe="")
    txn = urllib.parse.quote(transaction, safe="")
    url = (
        f"{str(config['matrix_homeserver']).rstrip('/')}/_matrix/client/v3/rooms/"
        f"{room}/send/m.room.message/{txn}"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps({"msgtype": "m.text", "body": text}).encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(64 * 1024) or b"{}")
    except urllib.error.HTTPError as error:
        detail = error.read(300).decode("utf-8", "replace")
        return SendResult(False, None, f"HTTP {error.code}: {detail}"[:300])
    except (urllib.error.URLError, OSError, ValueError) as error:
        return SendResult(False, None, str(error)[:300])
    event_id = payload.get("event_id") if isinstance(payload, dict) else None
    if not event_id:
        return SendResult(False, None, f"no event_id in {payload!r}"[:300])
    return SendResult(True, str(event_id), "")


def apply_probe(state: dict, result: ProbeResult, now: float, threshold: int) -> tuple[dict, str | None]:
    """Fold one probe into the state and decide whether a message is due.

    Pure: no clock, no network, no disk.  ``alert`` is due on the threshold-th
    consecutive failure while no confirmed alert is open — including a retry of
    an alert attempt whose delivery was never confirmed.  ``recovery`` is due on
    the first success after a confirmed alert.  The caller persists the alert
    attempt before the PUT, so either the alert or its retry carries the same
    transaction id and the homeserver deduplicates it.
    """
    new = dict(state)
    new["probes_total"] = int(new.get("probes_total") or 0) + 1
    new["last_probe_at"] = iso(now)
    new["last_probe_ok"] = result.ok
    action: str | None = None

    if result.ok:
        old_failures = int(new.get("consecutive_failures") or 0)
        new["last_success_at"] = iso(now)
        new["consecutive_failures"] = 0
        if new.get("alert_open"):
            if not new.get("alert_delivered"):
                # The alert was attempted but its delivery was never confirmed
                # (a crash between the PUT and the state write).  Finish that
                # alert with the same transaction id first; the homeserver
                # deduplicates it, and the recovery follows on the next tick.
                action = "alert"
            else:
                new["recovered_after_failures"] = old_failures or int(
                    new.get("outage_peak_failures") or 0
                )
                action = "recovery"
        else:
            new["last_error"] = None
            new["first_failure_at"] = None
            new["first_failure_epoch"] = None
            new["outage_peak_failures"] = 0
    else:
        failures = int(new.get("consecutive_failures") or 0) + 1
        new["consecutive_failures"] = failures
        new["outage_peak_failures"] = failures
        new["last_failure_at"] = iso(now)
        new["last_error"] = result.error
        if not new.get("first_failure_at") or not new.get("first_failure_epoch"):
            new["first_failure_at"] = iso(now)
            new["first_failure_epoch"] = int(now)
        if failures >= threshold and (
            not new.get("alert_open") or not new.get("alert_delivered")
        ):
            action = "alert"
    return new, action


def format_alert(state: dict, config: dict, result: ProbeResult) -> str:
    # The alert can be resent after HQ has answered (a lost confirmation); the
    # current streak is then 0, so report the outage's peak instead.
    failures = max(
        int(state.get("consecutive_failures") or 0),
        int(state.get("outage_peak_failures") or 0),
    )
    last_error = state.get("last_error") or result.error or "unknown"
    return (
        f"ODILE HQ UNREACHABLE — probe {config['probe_name']}\n"
        f"{failures} consecutive failures: first {state.get('first_failure_at')}, "
        f"last {state.get('last_failure_at')} (UTC).\n"
        f"Last error: {last_error}\n"
        f"Probe: GET {config['probe_url']} — expected HTTP 200 body "
        f"{str(config['expected_body'])!r}; got HTTP {result.http_code or 'none'}, "
        f"{result.elapsed_s:.2f}s.\n"
        f"One recovery message follows when HQ answers again."
    )


def format_recovery(state: dict, config: dict, now: float) -> str:
    started = state.get("first_failure_at") or "unknown"
    epoch = state.get("first_failure_epoch")
    duration = ""
    if isinstance(epoch, (int, float)):
        seconds = max(0, int(now) - int(epoch))
        duration = f"{seconds // 60}m {seconds % 60:02d}s"
    failures = state.get("recovered_after_failures") or state.get("outage_peak_failures") or 0
    return (
        f"ODILE HQ RECOVERED — probe {config['probe_name']}\n"
        f"HQ answered at {iso(now)} (UTC); outage began {started}; "
        f"alert sent {state.get('last_alert_at') or 'not delivered'}.\n"
        f"Duration {duration or 'unknown'}; {failures} consecutive failures at the peak.\n"
        f"Probe: GET {config['probe_url']} — HTTP 200, body "
        f"{str(config['expected_body'])!r}."
    )


def run_once(
    config: dict,
    state_path: Path,
    *,
    probe_fn: Callable[[dict], ProbeResult] = probe_hq,
    send_fn: Callable[[dict, str, str], SendResult] = send_matrix,
    now: float | None = None,
    dry_run: bool = False,
    stream=sys.stdout,
) -> dict:
    state = load_state(state_path)
    result = probe_fn(config)
    moment = time.time() if now is None else now
    new_state, action = apply_probe(state, result, moment, int(config["failure_threshold"]))
    sent: SendResult | None = None
    text: str | None = None

    if action == "alert":
        text = format_alert(new_state, config, result)
        transaction = f"odile-hq-watch-alert-{new_state.get('first_failure_epoch') or int(moment)}"
    elif action == "recovery":
        text = format_recovery(new_state, config, moment)
        transaction = f"odile-hq-watch-recovery-{new_state.get('first_failure_epoch') or int(moment)}"
    else:
        transaction = ""

    if action and not dry_run:
        if action == "alert":
            # Phase 1: publish the attempt before the network call.  A crash
            # after the PUT but before the confirmation leaves
            # alert_delivered=false, so the next tick resends the same
            # transaction id (the homeserver deduplicates it) instead of
            # forgetting that an alert is owed.
            new_state["alert_open"] = True
            new_state["alert_delivered"] = False
            new_state["last_alert_error"] = None
            save_state(state_path, new_state)
        sent = send_fn(config, text or "", transaction)
        if sent.ok:
            if action == "alert":
                new_state["alert_open"] = True
                new_state["alert_delivered"] = True
                new_state["last_alert_at"] = iso(moment)
                new_state["last_alert_event_id"] = sent.event_id
                new_state["last_alert_error"] = None
                new_state["last_send_error"] = None
                new_state["alerts_total"] = int(new_state.get("alerts_total") or 0) + 1
            else:
                new_state["alert_open"] = False
                new_state["alert_delivered"] = False
                new_state["last_recovery_at"] = iso(moment)
                new_state["last_recovery_event_id"] = sent.event_id
                new_state["last_send_error"] = None
                new_state["last_error"] = None
                new_state["last_outage_started_at"] = new_state.get("first_failure_at")
                new_state["recoveries_total"] = int(new_state.get("recoveries_total") or 0) + 1
                new_state["first_failure_at"] = None
                new_state["first_failure_epoch"] = None
                new_state["outage_peak_failures"] = 0
        else:
            new_state["last_send_error"] = sent.error
            if action == "alert":
                new_state["alert_open"] = True
                new_state["alert_delivered"] = False
                new_state["last_alert_error"] = sent.error

    if not dry_run:
        save_state(state_path, new_state)

    failures = int(new_state.get("consecutive_failures") or 0)
    parts = [
        iso(moment),
        f"probe={'ok' if result.ok else 'fail'}",
        f"http={result.http_code}",
        f"exit={result.exit_code}",
        f"failures={failures}/{config['failure_threshold']}",
        f"alert={'open' if new_state.get('alert_open') else 'no'}",
    ]
    if action:
        parts.append(f"action={action}")
        if dry_run:
            parts.append("sent=dry-run")
        elif sent is not None and sent.ok:
            parts.append(f"sent=yes event={sent.event_id}")
        elif sent is not None:
            parts.append(f"sent=no error={json.dumps(sent.error)}")
    if not result.ok:
        parts.append(f"error={json.dumps(result.error)}")
    print(" ".join(parts), file=stream)
    if dry_run and text:
        print(f"DRY-RUN would send ({transaction}):", file=stream)
        print(text, file=stream)

    return {
        "state": new_state,
        "probe": result,
        "action": action,
        "sent": sent,
        "text": text,
        "transaction": transaction,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Off-host liveness watcher for HQ (ODL-354)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--state", default=None, help="override the state file")
    parser.add_argument("--dry-run", action="store_true", help="probe and print; write/send nothing")
    parser.add_argument("--status", action="store_true", help="print the state file and exit")
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except (OSError, ValueError) as error:
        print(f"config error: {error}", file=sys.stderr)
        return 2
    state_path = Path(args.state or config.get("state_file") or DEFAULT_STATE)

    if args.status:
        print(json.dumps(load_state(state_path), indent=1, sort_keys=True))
        return 0

    try:
        run_once(config, state_path, dry_run=args.dry_run)
    except OSError as error:
        print(f"state error: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
