# Reconciliation — week of 13–19 November 2026

**Seat:** finance (staff function) · **Unit:** F2, primary source
`docs/ops/finance/POLICY.md` + `tools/spend.py` · **Date:** 2026-11-20
**Scope:** one week of `SPEND.csv` against `LEDGER.csv`. Nothing was paid,
approved, capped or repriced by this session.

## Method

- Inference: the `SPEND.csv` rows for 13–19 Nov, summed, and the same window
  as printed in two earlier snapshots — `WEEKLY-2026-11-16.md` §3 (06:12 UTC)
  and `RECONCILIATION-2026-11-17.md` §1 (05:40 rows). A day is "closed" when it
  is before today; the question is whether a closed day's number is final.
- Causation: every profile `state.db`, sessions whose `started_at` day is 13 or
  14 Nov and whose `last_activity_at` day is later, with their whole cost.
- External: the week's completed invoices from the Servury API
  (`?action=invoices`, read-only) against the row or note that records each one;
  `?action=server&id=34bc6c43` for the renewal date the ledger should carry.
- Reproduction: `/usr/bin/python3 docs/ops/evidence/2026-11-20-finance/reconcile_week.py`
  → `reconcile-week.txt`; the raw API read is `servury-read-2026-11-20.json`.

## §1 — the week

| Day | Inference | Sessions |
|---|---:|---:|
| 13 Nov | $52.11 | 189 |
| 14 Nov | $56.06 | 209 |
| 15 Nov | $50.16 | 189 |
| 16 Nov | $12.61 | 59 |
| 17 Nov | $20.46 | 53 |
| 18 Nov | $34.64 | 152 |
| 19 Nov | $43.69 | 208 |
| **week** | **$269.74** | **1,059** |

**The week's inference figure is not stable, because a closed day restates.**
The same 9–15 Nov window reads $286.66 in the 16 Nov memo, $285.01 in the 17
Nov reconciliation and $276.34 today — **−$10.32 (−3.6%)** since the memo. Only
two days moved: 13 Nov −$6.89 and 14 Nov −$3.43; the other five moved less than
$0.005. The cause is in the generator: `finance_check.py` line 276 buckets each
session's **whole** cost on the date of `last_activity_at`, so when a session
resumes the whole cost travels to the new day. 23 sessions started on 13 or 14
Nov and resumed later, carrying $11.49 — the same order as the drift. The month
total is invariant; the daily shape and any week boundary are not.

## §2 — the unreconciled row: FIN-001

External cash that moved in the week was **$11.18**:

| Date | Invoice | Amount | Row | Recorded? |
|---|---|---:|---|---|
| 19 Nov | 58093838 `server_renew` "Auto-renewal for 7 days" | $9.03 | FIN-001 | **no row, no note** |
| 19 Nov | 07250b99 `server_renew` (dev, API, 5% partner) | $2.15 | FIN-006 | yes — in the row's note |

`grep -n "9.03\|58093838" docs/ops/finance/LEDGER.csv` returns nothing. The
account balance corroborates the charge independently: $31.55 on 15 Nov (after
FIN-007) less $9.03 = **$22.52**, the balance `servury-renew.json` reports
today. FIN-001's `renews_on` still reads **2026-11-19**, a day in the past,
while the API reports the box expires **2026-11-26T11:25:32Z** — the row is one
7-day term behind reality, and STATUS.md's fixed accrual ($39.99/month) is right
in amount but carries a renewal date that has already fired.

**Why it happened.** The host box has no panel session, so auto-renew is left on
as its safety net (POLICY.md, purchase route), and the panel charges list price:
$9.03 for seven days where the account key would charge $8.58 — $0.45 per term,
about **$23 a year** at 52 terms. `tools/servury_renew.py` skips `auto_renew:
true` boxes by design and names them; the charge is expected. What failed is the
ledger: a completed renewal moved the account balance and left no trace in
`LEDGER.csv`.

**Owner:** `savio` (FIN-001 owner; the term and the route are his decision;
finance cannot change a renewal). **Evidence that closes it:** this folder's
`servury-read-2026-11-20.json` — invoice 58093838 ($9.03, 2026-11-19) and
server 34bc6c43 (`expiration_date` 2026-11-26T11:25:32Z). On that evidence
FIN-001's `renews_on` becomes 2026-11-26 and its note carries the $9.03, the
new expiry and the $0.45 list-price premium. The next renewal is due by
2026-11-22 under the tool's 5-day window; until the date is corrected the row
cannot trigger its own 30-day alert.

## §3 — what otherwise reconciles

- Month to date: `SPEND.csv` sums to $447.8762, the same as the live check at
  06:57 UTC ($447.8762) and the 05:40 STATUS ($447.32 before today's later
  sessions). Each session is counted once.
- FIN-006's $2.15 renewal is recorded in the row's note with invoice, term and
  post-charge balance ($7.86) — the shape FIN-001 is missing.
- FIN-005 is `retired` and outside the fixed accrual ($60.40 = FIN-001 $39.99 +
  FIN-004 $0.43 + FIN-006 $9.99 + FIN-008 $9.99), so the destroyed tenant box
  does not inflate the forecast. Its `renews_on` (2026-12-10) is a stale cell on
  a retired row, not a commitment.
- FIN-002 (Cloudflare, $0.00, free plan) has no renewal by design.
- FIN-003 (NEAR AI) stays `metered / unpriced`: it has a meter (`SPEND.csv`,
  `tools/spend.py`) and no invoice. That is the known unpriced row, reported as
  unknown rather than zero for the third week; nothing new to close.

## Commands

- `/usr/bin/python3 docs/ops/evidence/2026-11-20-finance/reconcile_week.py`
- `/usr/bin/python3 tools/spend.py --json` → $6.3448 today, 802 sessions
- `/usr/bin/python3 ~/.hermes/profiles/finance/scripts/finance_check.py --json --no-commit`
  → MTD $447.8762, forecast $848.98
- `GET https://servury.com/api/reseller.php?action=invoices` and
  `?action=server&id=34bc6c43`, header `X-Reseller-Key` from
  `/root/.odile/servury_reseller_key` (0600), read-only.

_No payment, approval, cap, price or ledger row was changed by this session._
