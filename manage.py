#!/usr/bin/python3
"""Waitlist admin, from the shell. No web admin panel, no login to attack.

    ./manage.py migrate             # create or upgrade the schema (idempotent)
    ./manage.py funnel              # signups → invited → … → active in 7 days
    ./manage.py count
    ./manage.py export [--out list.csv]
    ./manage.py rm someone@example.com
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from app import SCHEMA_TABLES, migrate

DB = os.environ.get("ODILE_DB", "/var/lib/odile/waitlist.db")


def con():
    c = sqlite3.connect(DB, timeout=5.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def cmd_count(_args):
    c = con()
    total = c.execute("SELECT count(*) FROM waitlist").fetchone()[0]
    since = c.execute("SELECT substr(created_at, 1, 10) d, count(*) n FROM waitlist GROUP BY d ORDER BY d DESC LIMIT 1").fetchone()
    c.close()
    print("on the list: %d" % total)
    if since:
        print("latest day:  %s (%d)" % (since["d"], since["n"]))
    return 0


def cmd_migrate(_args):
    c = con()
    try:
        status = migrate(c)
    finally:
        c.close()
    print("migrate: %s (schema v%d)" % (DB, status["version"]))
    for name in SCHEMA_TABLES:
        table = status["tables"][name]
        print("  %-8s %-15s %6d row(s)" % (
            name,
            "created" if table["created"] else "already present",
            table["rows"],
        ))
    if status["added"]:
        print("  added %s" % ", ".join(status["added"]))
    if status["backfilled"]:
        print("  backfilled %d signup event(s) from waitlist" % status["backfilled"])
    print("migrate: ok (idempotent; run again to confirm)")
    return 0


def cmd_funnel(_args):
    """The M0 exit criterion: one line per stage, oldest stage first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Each stage reads the record that is durable for that fact: the list for
    # signups, the tenant row for invited/claimed/installed, the append-only
    # event for activated (a tenant's state moves on after activation), and the
    # latest heartbeat for activity.
    stages = [
        ("signups", "SELECT count(*) FROM waitlist", ()),
        ("invited", "SELECT count(*) FROM tenants", ()),
        ("claimed", "SELECT count(*) FROM tenants WHERE claimed_at IS NOT NULL", ()),
        ("installed", "SELECT count(*) FROM tenants WHERE installed_at IS NOT NULL", ()),
        ("activated", "SELECT count(DISTINCT tenant_id) FROM events"
                      " WHERE kind = 'activated' AND tenant_id IS NOT NULL", ()),
        ("active in last 7 days",
         "SELECT count(*) FROM tenants WHERE last_heartbeat >= ?", (cutoff,)),
    ]
    c = con()
    try:
        counts = [(label, c.execute(sql, params).fetchone()[0])
                  for label, sql, params in stages]
    except sqlite3.OperationalError as exc:
        print("funnel: %s (run ./manage.py migrate first)" % exc, file=sys.stderr)
        return 1
    finally:
        c.close()
    for label, n in counts:
        print("%-22s %d" % (label, n))
    return 0


def cmd_export(args):
    c = con()
    rows = c.execute("SELECT email, created_at, token FROM waitlist ORDER BY id").fetchall()
    c.close()
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["email", "created_at", "token"])
            for r in rows:
                w.writerow([r["email"], r["created_at"], r["token"]])
        print("%d rows -> %s" % (len(rows), args.out))
    else:
        w = csv.writer(sys.stdout)
        w.writerow(["email", "created_at", "token"])
        for r in rows:
            w.writerow([r["email"], r["created_at"], r["token"]])
    return 0


def cmd_rm(args):
    c = con()
    cur = c.execute("DELETE FROM waitlist WHERE email = ?", (args.email.strip().lower(),))
    c.commit()
    c.close()
    print("deleted %d row(s)" % cur.rowcount)
    return 0


def main():
    p = argparse.ArgumentParser(description="odile waitlist admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create or upgrade the schema; idempotent").set_defaults(fn=cmd_migrate)
    sub.add_parser("funnel", help="signups -> invited -> claimed -> installed -> activated -> active").set_defaults(fn=cmd_funnel)
    sub.add_parser("count").set_defaults(fn=cmd_count)
    e = sub.add_parser("export")
    e.add_argument("--out", help="write CSV to this path instead of stdout")
    e.set_defaults(fn=cmd_export)
    r = sub.add_parser("rm")
    r.add_argument("email")
    r.set_defaults(fn=cmd_rm)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
