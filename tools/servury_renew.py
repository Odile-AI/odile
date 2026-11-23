#!/usr/bin/env python3
"""Renew Servury Blackbox servers from the API before their term ends.

Why this exists: the panel's auto-renew charges **list price**. Invoice
`58093838` on 2026-11-19 was "Auto-renewal for 7 days" $9.03 on Blackbox Max —
exactly 39.99/31*7 — on an account whose key carries the 5% partner discount.
`?action=renew` with the same key charges the partner price (8.58 for that
term). So auto-renew is turned off on the boxes we can reach and this tool does
the renewing, while there is still slack to retry.

    ./tools/servury_renew.py                      # dry run, every configured key
    ./tools/servury_renew.py --apply              # renew what is inside the window
    ./tools/servury_renew.py --server 168020b4 --threshold 30 --days 7 --apply

Policy: 7-day terms inside a 5-day window. Price is linear on Blackbox
(`retail/31 * days`, no short-term fee, no long-term discount), so the term
length changes only how often this runs; 7 days keeps every charge under $10 and
leaves five daily runs to retry a failure before the 24-hour deletion window.

A server whose `auto_renew` is true is skipped and named: renewing it as well
would stack days and pull cash forward while the panel still charges list price.
That applies to any box whose account we have no panel session on.

The trial pays for the machine. Each tenant DB the service writes — the bench's
`/var/lib/odile-test/waitlist.db` and production's `/var/lib/odile/waitlist.db`
— carries the account row's `trial_ends_at` beside the provider's `server_id`,
so a server whose trial has ended is skipped, recorded as `trial_ended` in the
status document and the audit log, and its provider term is left to expire.

Exit: 0 nothing due or everything renewed; 3 an alert (balance, cap, failure);
2 usage. Stdlib only, so the cron job needs no virtualenv.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

API = "https://servury.com/api/reseller.php"
AUDIT = Path("/root/.odile/servury-renew.log")
STATUS = Path("/root/.odile/servury-renew.json")
LOCK = Path("/root/.odile/servury-renew.lock")
DEFAULT_KEYS = (
    ("host", Path("/root/.odile/servury_reseller_key")),
    ("dev", Path("/root/.odile/servury_reseller_key_lkbxu6i9")),
)
# The databases the service itself writes (ODL-341). No flag means both, so the
# cron job needs no edit to stop paying for an ended trial.
TRIAL_DBS = (
    Path("/var/lib/odile-test/waitlist.db"),
    Path("/var/lib/odile/waitlist.db"),
)
TRIAL_STAMP = "%Y-%m-%dT%H:%M:%SZ"
DAY = 86400


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def term_price(rate_monthly: float | str, days: int, discount_percent: float | int) -> Decimal:
    """What ?action=renew charges: retail term, then the partner discount.

    The API documents the order — `retail_price / 31 * days` rounded to the
    cent, minus the discount, rounded again — so the rounding happens twice and
    a renewal is not simply 5% of the list term.
    """
    retail = money(Decimal(str(rate_monthly)) / Decimal(31) * Decimal(days))
    return money(retail * (Decimal(100) - Decimal(str(discount_percent))) / Decimal(100))


def days_left(expiration: int | float, at: datetime.datetime) -> float:
    return (float(expiration) - at.timestamp()) / DAY


def call(action: str, key: str, *, params: dict | None = None, server_id: str | None = None,
         timeout: float = 40.0) -> dict:
    query = {"action": action}
    if server_id:
        query["id"] = server_id
    url = f"{API}?{urllib.parse.urlencode(query)}"
    body = json.dumps(params).encode() if params is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"X-Reseller-Key": key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"{action}: HTTP {error.code} {detail[:300]}") from None
    if not payload.get("success", True):
        raise RuntimeError(f"{action}: {payload.get('error')}")
    return payload.get("data") or {}


def audit(line: str) -> None:
    stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
    with AUDIT.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {line}\n")


def read_key(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def trial_ends(paths: tuple[Path, ...]) -> tuple[dict[str, tuple[str, datetime.datetime]], list[str]]:
    """`server_id` -> (the stamp as written, its instant), read from the tenant DBs.

    The account row is the only record that knows which machine a trial pays for
    (ODL-341): `accounts.server_id` names the provider's server and
    `trial_ends_at` is registration's instant plus 14 days. A database that is
    absent or has no accounts table (production before ODL-341 reached it) and a
    stamp that will not parse are notes, not alerts: those servers keep exactly
    the renewal behaviour they had before this check existed.
    """
    ends: dict[str, tuple[str, datetime.datetime]] = {}
    notes: list[str] = []
    for path in paths:
        if not path.is_file():
            notes.append(f"trial db {path}: absent — no trial check from it")
            continue
        try:
            with contextlib.closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro",
                                                    uri=True)) as db:
                rows = list(db.execute(
                    "SELECT server_id, trial_ends_at FROM accounts"
                    " WHERE server_id IS NOT NULL AND server_id != ''"))
        except sqlite3.Error as error:
            notes.append(f"trial db {path}: {error} — no trial check from it")
            continue
        for server_id, stamp in rows:
            try:
                when = datetime.datetime.strptime(stamp, TRIAL_STAMP).replace(tzinfo=datetime.UTC)
            except (TypeError, ValueError):
                notes.append(f"trial db {path}: account for {server_id} has "
                             f"trial_ends_at {stamp!r} — unreadable, treated as no trial")
                continue
            ends.setdefault(server_id, (stamp, when))
    return ends, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Renew Servury Blackbox servers at the partner price.")
    parser.add_argument("--apply", action="store_true", help="actually renew (default: dry run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and charge nothing (the default, named for a rehearsal)")
    parser.add_argument("--days", type=int, default=7, help="term to buy, 7 to 365 (default 7)")
    parser.add_argument("--threshold", type=float, default=5.0,
                        help="renew when this many days or fewer are left (default 5)")
    parser.add_argument("--server", default="", help="only this 8-character server id")
    parser.add_argument("--max-charge", type=Decimal, default=Decimal("10.00"),
                        help="refuse a single renewal above this amount (default 10.00, the card floor)")
    parser.add_argument("--key-file", action="append", default=[], help="label=path, repeatable")
    parser.add_argument("--trial-db", action="append", metavar="PATH",
                        help="tenant database whose accounts.trial_ends_at stops a machine; "
                             "repeatable (default: the bench and production databases)")
    parser.add_argument("--json", action="store_true", help="print the status document")
    args = parser.parse_args(argv)

    if not 7 <= args.days <= 365:
        print("servury-renew: --days must be 7 to 365", file=sys.stderr)
        return 2
    if args.apply and args.dry_run:
        print("servury-renew: --apply and --dry-run are opposites — pick one", file=sys.stderr)
        return 2

    keys: list[tuple[str, Path]] = []
    if args.key_file:
        for spec in args.key_file:
            label, _, path = spec.partition("=")
            keys.append((label or Path(path).stem, Path(path)))
    else:
        keys = [(label, path) for label, path in DEFAULT_KEYS]

    at = now()
    actions: list[dict] = []
    alerts: list[str] = []
    accounts: list[dict] = []
    notes: list[str] = []
    trials, trial_notes = trial_ends(tuple(Path(p) for p in (args.trial_db or TRIAL_DBS)))
    notes.extend(trial_notes)

    with LOCK.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("servury-renew: another run holds the lock — skipped", file=sys.stderr)
            return 0

        for label, path in keys:
            if not path.is_file():
                notes.append(f"{label}: no key at {path} — skipped")
                continue
            key = read_key(path)
            try:
                account = call("account", key)
                servers = call("servers", key).get("servers", [])
            except RuntimeError as error:
                alerts.append(f"{label}: {error}")
                continue
            balance = Decimal(str(account.get("balance", 0)))
            discount = account.get("discount_percent", 0)
            accounts.append({"label": label, "balance": float(balance), "discount": discount,
                             "servers": len(servers)})

            for brief in servers:
                server_id = brief["id"]
                if args.server and server_id != args.server:
                    continue
                left = days_left(brief.get("expiration_date", 0), at)
                if left > args.threshold:
                    continue
                trial = trials.get(server_id)
                if trial is not None and at >= trial[1]:
                    # The trial that pays for this machine is over: buy no more
                    # days, and leave the provider's last term to run out.
                    actions.append({
                        "key": label, "server": server_id, "hostname": brief.get("hostname"),
                        "days_left": round(left, 1), "term": args.days,
                        "trial_ends_at": trial[0], "auto_renew": bool(brief.get("auto_renew")),
                        "applied": False, "skipped": "trial_ended",
                    })
                    notes.append(f"{server_id}: trial ended {trial[0]} — not renewed; "
                                 f"the provider's term runs out")
                    audit(f"TRIAL_ENDED key={label} server={server_id} trial_ends_at={trial[0]}")
                    continue
                try:
                    detail = call("server", key, server_id=server_id).get("server", brief)
                except RuntimeError as error:
                    alerts.append(f"{server_id}: {error}")
                    continue
                rate = detail.get("rate_monthly", brief.get("rate_monthly", 0))
                price = term_price(rate, args.days, discount)
                row = {"key": label, "server": server_id, "hostname": detail.get("hostname"),
                       "days_left": round(left, 1), "term": args.days, "price": float(price),
                       "auto_renew": bool(detail.get("auto_renew")), "applied": False}

                if detail.get("auto_renew"):
                    row["skipped"] = "auto-renew on (panel charges list price)"
                    # One line the day before it fires: frequent enough to be a
                    # reminder, rare enough not to be a daily nag.
                    if left <= 1:
                        notes.append(
                            f"{server_id} auto-renews in {max(left, 0):.1f}d at list price — "
                            f"partner price would be ${price}")
                    actions.append(row)
                    continue
                if price > args.max_charge:
                    row["skipped"] = f"${price} above the ${args.max_charge} cap"
                    alerts.append(f"{server_id}: ${price} for {args.days}d exceeds --max-charge ${args.max_charge}")
                    actions.append(row)
                    continue
                if balance < price:
                    row["skipped"] = f"balance ${balance} below ${price}"
                    alerts.append(f"{label}: balance ${balance} < ${price} for {server_id} — top up")
                    actions.append(row)
                    continue

                if not args.apply:
                    row["skipped"] = "dry run"
                    actions.append(row)
                    continue
                try:
                    result = call("renew", key, params={"days": args.days}, server_id=server_id)
                except RuntimeError as error:
                    row["skipped"] = str(error)
                    alerts.append(f"{server_id}: renewal failed — {error}")
                    actions.append(row)
                    continue
                charged = Decimal(str(result.get("amount_charged", price)))
                balance -= charged
                row.update({"applied": True, "charged": float(charged),
                            "new_expiration": result.get("new_expiration"),
                            "balance_after": float(balance)})
                actions.append(row)
                audit(
                    f"RENEW key={label} server={server_id} days={args.days} "
                    f"retail_rate={rate} charged={charged} new_expiration={result.get('new_expiration')} "
                    f"balance_after={balance}")

        status = {
            "checked_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "applied": args.apply,
            "accounts": accounts,
            "actions": actions,
            "alerts": alerts,
            "notes": notes,
        }
        STATUS.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(status, indent=2))
    else:
        for row in actions:
            if row.get("applied"):
                print(f"renewed {row['server']} {row['term']}d ${row['charged']:.2f} "
                      f"balance ${row['balance_after']:.2f}")
            elif row.get("skipped") != "dry run":
                print(f"skipped {row['server']} — {row['skipped']}")
        for note in notes:
            print(note)
        for alert in alerts:
            print(f"ALERT {alert}")
    return 3 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
