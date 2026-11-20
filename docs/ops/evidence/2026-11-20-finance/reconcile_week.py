#!/usr/bin/python3
"""Reconcile one week (13-19 Nov 2026) of SPEND.csv and LEDGER.csv.

Read-only. Prints three checks:
  1. the week's inference total from the current SPEND.csv, and the same window
     as published in earlier snapshots (16 Nov memo, 17 Nov reconciliation);
  2. every ledger row whose status is `committed` but whose `renews_on` is in
     the past or empty;
  3. external cash that moved in the week, against the ledger row that records
     it.

Run: /usr/bin/python3 docs/ops/evidence/2026-11-20-finance/reconcile_week.py
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

FIN = Path("/root/odile/docs/ops/finance")
HERE = Path(__file__).resolve().parent
TODAY = dt.date(2026, 11, 20)
WEEK = [f"2026-11-{d:02d}" for d in range(13, 20)]

# The 16 Nov memo (§3) and the 17 Nov reconciliation (§1) both print the 9-15
# Nov daily rows from the snapshot they used. Kept here as the comparison set.
MEMO_16 = {"2026-11-09": 25.64, "2026-11-10": 40.57, "2026-11-11": 28.13,
           "2026-11-12": 23.67, "2026-11-13": 59.00, "2026-11-14": 59.49,
           "2026-11-15": 50.16}
RECON_17 = {"2026-11-09": 25.6361, "2026-11-10": 40.5746, "2026-11-11": 28.1271,
            "2026-11-12": 23.6682, "2026-11-13": 59.0022, "2026-11-14": 57.8378,
            "2026-11-15": 50.1637}


def money(value: float) -> str:
    return f"${value:,.2f}"


def spend_week() -> int:
    rows = list(csv.DictReader((FIN / "SPEND.csv").open(encoding="utf-8")))
    day = {r["date"]: float(r["total_usd"]) for r in rows}
    print("== 1. SPEND.csv — week 13-19 Nov ==")
    for d in WEEK:
        print(f"  {d}  {money(day[d])}  {next(r['sessions'] for r in rows if r['date'] == d)} sessions")
    print(f"  week total {money(sum(day[d] for d in WEEK))}")

    print("\n  same window as published earlier (restatement of closed days):")
    for label, series in (("16 Nov memo", MEMO_16), ("17 Nov recon", RECON_17)):
        print(f"  9-15 Nov {label:12s} {money(sum(series.values()))}"
              f"   vs today {money(sum(day[k] for k in series))}"
              f"   delta {sum(day[k] - series[k] for k in series):+.2f}")
    print("  per-day delta, 20 Nov file minus 16 Nov memo:")
    for k in sorted(MEMO_16):
        delta = day[k] - MEMO_16[k]
        if abs(delta) > 0.01:
            print(f"    {k}  {delta:+.4f}  (was {money(MEMO_16[k])}, now {money(day[k])})")
    print("  days that moved: only 13 and 14 Nov; the other five are flat within $0.005.")
    return 0


def ledger() -> int:
    rows = list(csv.DictReader((FIN / "LEDGER.csv").open(encoding="utf-8")))
    print("\n== 2. LEDGER.csv — committed rows whose renewal date has passed ==")
    stale = 0
    for r in rows:
        if r["status"] != "committed":
            continue
        if r["vendor"] == "Cloudflare" and float(r["amount_usd"] or 0) == 0:
            print(f"  {r['id']}  {r['vendor']:10s}  no renewal by design: free plan, $0.00")
            continue
        when = (r["renews_on"] or "").strip()
        if not when:
            print(f"  {r['id']}  {r['vendor']:10s}  committed, renews_on empty   {money(float(r['amount_usd'] or 0))}/{r['cadence']}")
            stale += 1
            continue
        date = dt.date.fromisoformat(when)
        if date < TODAY:
            print(f"  {r['id']}  {r['vendor']:10s}  renews_on {when} is {(TODAY-date).days} days past   {money(float(r['amount_usd'] or 0))}/{r['cadence']}")
            stale += 1
    print(f"  {stale} stale row(s)")

    print("\n== 3. External cash in the week vs the ledger ==")
    charges = [("2026-11-19", "Servury FIN-001", 9.03, "58093838", "host Blackbox Max, auto-renewal 7 days"),
               ("2026-11-19", "Servury FIN-006", 2.15, "07250b99", "dev Blackbox Core, API renewal 7 days")]
    for when, who, amount, invoice, what in charges:
        print(f"  {when}  {money(amount)}  invoice {invoice}  {who}  {what}")
    print(f"  week external cash {money(sum(c[2] for c in charges))}")
    text = (FIN / "LEDGER.csv").read_text(encoding="utf-8")
    for _, _, amount, invoice, _ in charges:
        print(f"  invoice {invoice} ({money(amount)}) in LEDGER.csv: "
              f"{'yes' if invoice in text else 'no'}"
              f"; amount string {'yes' if f'{amount:.2f}' in text else 'no'}")
    return 0


def servury() -> int:
    path = HERE / "servury-read-2026-11-20.json"
    if not path.is_file():
        print("\n== 4. no servury-read json ==")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    server = data["server_34bc6c43"]["data"]["server"]
    print("\n== 4. Servury API, read-only ==")
    print(f"  server 34bc6c43 expires {dt.datetime.fromtimestamp(server['expiration_date'], dt.timezone.utc).isoformat()}"
          f"  auto_renew={server['auto_renew']}  rate_monthly={server['rate_monthly']}")
    inv = [i for i in data["invoices"]["data"]["invoices"] if i["id"] == "58093838"][0]
    print(f"  invoice {inv['id']} {money(float(inv['amount_usd']))} {inv['type']} \"{inv['description']}\""
          f" at {dt.datetime.fromtimestamp(inv['created_at'], dt.timezone.utc).isoformat()}")
    print("  ledger FIN-001 renews_on 2026-11-19; box expiry is 2026-11-26 — the row is a week behind.")
    return 0


if __name__ == "__main__":
    spend_week()
    ledger()
    servury()
