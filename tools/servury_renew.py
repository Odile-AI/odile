#!/usr/bin/env python3
"""Renew Servury Blackbox servers from the API before their term ends.

Why this exists: the panel's auto-renew charges **list price**. Invoice
`58093838` on 2026-11-19 was "Auto-renewal for 7 days" $9.03 on Blackbox Max —
exactly 39.99/31*7 — on an account whose key carries the 5% partner discount.
`?action=renew` with the same key charges the partner price (8.58 for that
term then). So auto-renew is turned off on the boxes we can reach and this tool
does the renewing, while there is still slack to retry.

    ./tools/servury_renew.py                      # dry run, every configured key
    ./tools/servury_renew.py --apply              # renew what is inside the window
    ./tools/servury_renew.py --server 168020b4 --threshold 30 --days 7 --apply

Policy: 7-day terms inside a 5-day window. Servury prices a term by its length
(measured 2026-11-24, not inferred): the retail term is `rate/31 * days` rounded
to the cent; a term under 31 days adds a 15% short-term fee; terms of 90, 180
and 365 days take 5%, 10% and 15% off, the highest reached applying; and the
account's partner discount comes last. Every step rounds to the cent. Seven days
therefore keeps every charge under $10 — a $9.99 box is $2.47, not the $2.15 the
old linear model quoted — and leaves five daily runs to retry a failure before
the 24-hour deletion window. A longer term costs less per day and needs a card
that names the amount: 365 days on that box is $94.98.

A server whose `auto_renew` is true is skipped and named: renewing it as well
would stack days and pull cash forward while the panel still charges list price.
That applies to any box whose account we have no panel session on.

A server that is being retired by policy buys no more days. The list is
`deploy/servury/run-off.txt` (one 8-character id per line, `#` starts a
comment, a missing file means none; `--run-off-file` names another), and every
Blackbox Core is on it by plan: the three Cores on the dev account are pinned
there, and a Core a bench test orders later is skipped without an edit. Such a
server is recorded as `run_off` in the status document and the audit log — no
price, no balance check, no `--max-charge` alert, no renewal — and its provider
term is left to run out. `run_off` is not `trial_ended`: one says the fleet is
being retired, the other that the trial that paid for the machine has finished.

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
# The retirement list the daily job reads. A committed file, not a constant in
# this module: the policy changes without the tool changing. A missing file is
# an empty list.
RUN_OFF_FILE = Path(__file__).resolve().parents[1] / "deploy" / "servury" / "run-off.txt"
# Blackbox Core, measured 2026-11-24 from `?action=servers`: the three boxes on
# the dev account carry `plan_id` 108 and `plan` "Blackbox Core". Every Core is
# internal R&D until it expires (Savio, 2026-11-24), so the plan is the rule and
# the ids in the file are the pinned cases — a Core a bench test orders later is
# retired without an edit. The Nano is plan 138 and is kept.
RUN_OFF_PLAN_IDS = frozenset({108})
STAMP = "%Y-%m-%dT%H:%M:%SZ"
DAY = 86400


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Servury's term schedule, from `?action=account`'s `limits` block on 2026-11-24.
SHORT_TERM_UNDER_DAYS = 31
SHORT_TERM_FEE_PERCENT = 15
TERM_DISCOUNTS = ((365, 15), (180, 10), (90, 5))


def term_price(rate_monthly: float | str, days: int, discount_percent: float | int) -> Decimal:
    """What ?action=renew charges for a term, in the vendor's order of operations.

    Measured 2026-11-24: the retail term is `rate/31 * days` rounded to the
    cent; a term under 31 days adds the 15% short-term fee; a term of 90, 180 or
    365 days takes the highest term discount reached (5%, 10%, 15%) — one
    discount, not a stack; then the account's partner discount applies. Every
    step rounds to the cent, so the price is not one multiplication.
    """
    retail = money(Decimal(str(rate_monthly)) / Decimal(31) * Decimal(days))
    if days < SHORT_TERM_UNDER_DAYS:
        retail = money(retail * (Decimal(100) + Decimal(SHORT_TERM_FEE_PERCENT)) / Decimal(100))
    else:
        for threshold, percent in TERM_DISCOUNTS:
            if days >= threshold:
                retail = money(retail * (Decimal(100) - Decimal(percent)) / Decimal(100))
                break
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


def stamp_of(epoch: int | float | None) -> str:
    """A provider epoch (seconds) as the UTC stamp the audit log writes."""
    return datetime.datetime.fromtimestamp(float(epoch or 0), datetime.UTC).strftime(STAMP)


def looks_like_server_id(token: str) -> bool:
    """The provider's ids are eight lowercase hex characters."""
    return len(token) == 8 and all(ch in "0123456789abcdef" for ch in token)


def read_run_off(path: Path) -> tuple[set[str], list[str]]:
    """The ids retired by policy, and a note for anything the file got wrong.

    The file is data, not code: one 8-character id per line, `#` starts a
    comment to the end of the line, and blank lines are ignored. A missing file
    is an empty list — the plan rule still retires every Blackbox Core — and a
    line that is not an id is named in the notes rather than obeyed, so a typo
    cannot silently retire the wrong box or silently fail to retire the right
    one.
    """
    ids: set[str] = set()
    if not path.is_file():
        return ids, [f"run-off list {path}: absent — no pinned ids"]
    notes: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return ids, [f"run-off list {path}: {error} — no pinned ids"]
    for number, line in enumerate(lines, 1):
        token = line.split("#", 1)[0].strip().lower()
        if not token:
            continue
        if not looks_like_server_id(token):
            notes.append(f"run-off list {path}:{number}: ignoring {token!r} — "
                         f"not an 8-character server id")
            continue
        ids.add(token)
    return ids, notes


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
                when = datetime.datetime.strptime(stamp, STAMP).replace(tzinfo=datetime.UTC)
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
    parser.add_argument("--run-off-file", metavar="PATH", default="",
                        help="the retirement list: one 8-character server id per line, "
                             f"comments allowed (default: {RUN_OFF_FILE.name} under deploy/servury/)")
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
    run_off_ids, run_off_notes = read_run_off(
        Path(args.run_off_file) if args.run_off_file else RUN_OFF_FILE)
    notes.extend(run_off_notes)

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
                plan_id = brief.get("plan_id")
                listed = server_id.lower() in run_off_ids
                if listed or plan_id in RUN_OFF_PLAN_IDS:
                    # Retired by policy: buy no days, leave the last term to run
                    # out, and raise no balance or cap alert — nothing is due.
                    source = "listed" if listed else f"plan {plan_id}"
                    expires = stamp_of(brief.get("expiration_date"))
                    actions.append({
                        "key": label, "server": server_id, "hostname": brief.get("hostname"),
                        "days_left": round(left, 1), "term": args.days,
                        "plan_id": plan_id, "plan": brief.get("plan"),
                        "expires": expires, "auto_renew": bool(brief.get("auto_renew")),
                        "applied": False, "skipped": "run_off", "run_off_source": source,
                    })
                    notes.append(f"{server_id}: run-off ({source}) — not renewed; expires {expires}")
                    audit(f"RUN_OFF key={label} server={server_id} expires={expires}")
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
