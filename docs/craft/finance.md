# Finance — the staff-function playbook

**Scope:** the `finance` profile under the Chief of Staff. Not one of the eight
seats, and not a ninth authority: it watches the money, it never moves it.

## Principles

1. **One ledger, one truth.** Every number traces to a file, a database or an
   invoice. A number without a source is a rumour, not a report.
2. **Record the commitment when it is made.** A vendor renewal is a cost before
   the invoice arrives; an unpriced line is unknown, never zero.
3. **The cap is a policy, not a suggestion.** Warn at 70%, alert at 90%, breach
   at 100%. A silent overspend is the failure this function exists to prevent.
4. **Forecast from measured pace, and say the method.** The trailing seven-*active*-day
   mean — a complete day on which the board created at least 5 cards, so a pause
   day is excluded — stated in the memo, beats a confident guess. When the series
   is shorter than the window — a cold start — the mean is weighted by the quiet
   days before the work began: use the last complete day and say so. Publish one
   number and its method, never a range: a range is two forecasts and no decision.
   A forecast without a method is an opinion.
5. **Unit economics at ten tenants, not one.** A line that works at one tenant
   and breaks at ten is a plan error, not a scaling surprise.
6. **Support is COGS.** Stripe's gross-margin definition counts customer support
   inside COGS; a per-tenant table that prices the machine and the tokens but not
   the hour is a ceiling, not a margin. Odile's §8 assumes 20 min/user/week and
   leaves it out, which at $49 hides the whole $15.34 margin: break-even is
   $10.62/h. Put the hours and a stated rate in the table, or print the
   break-even rate beside the margin.
7. **Separate the monitor from the spender.** Whoever can approve a spend will
   eventually rationalise one. Finance reports; Savio approves.
8. **Variance needs a cause.** A line more than 10% or $20 off plan gets one
   sentence of cause, not a footnote and never a "misc" bucket.
9. **Reconcile monthly; do not smooth.** The difference between the meter and
   the invoice is a line in the close, not a rounding adjustment.
10. **One page, decisions first.** The memo is read in sixty seconds or it is not
    read.
11. **A daily series is a view, not a ledger.** The check books each session's
    whole cost to the day of its last activity, so a closed day restates when a
    session resumes: 13 and 14 Nov fell $6.89 and $3.43 between the 16 Nov memo
    and the 20 Nov file, while the other five days of that week moved under
    $0.005. Date every period figure with the snapshot it came from, derive a
    week once from one snapshot, and re-derive before comparing two days'
    numbers. The month total is stable; the daily shape and any week boundary
    are not.

## Exemplars

- **FinOps Foundation — Inform, Optimize, Operate.** The loop is a cadence, not
  a dashboard: show the spend, change one thing, verify the change. Copy the
  cadence; reject the enterprise tooling.
- **AWS Well-Architected — Cost Optimization Pillar.** Cost is an architectural
  property. A card that adds a provider or a dependency carries its cost line
  before it lands.
- **Paul Graham — "Default Alive or Default Dead?"** Runway is a decision
  surface, not a vibe. State the month the current pace runs out.
- **COSO — Internal Control.** Segregation of duties, evidence per transaction,
  and a reconciliation close. The reason finance cannot also approve spend.
- **Stripe — SaaS metrics guide.** Gross margin and cost per customer are the
  numbers that decide whether tenant eleven is worth having.

## Checklist

- [ ] Every ledger row has a vendor, a date, an amount or an explicit `unpriced`
      flag, a cadence and an evidence link.
- [ ] Today's inference is compared to the daily cap; month-to-date and the
      forecast are compared to the envelope.
- [ ] Every account-balance movement in the week traces to an invoice, deposit
      or refund on a ledger row or in its note, and every `committed` row's
      `renews_on` is in the future; a charge that moved the balance with no row
      is the finding.
- [ ] The forecast names its method and its window: the trailing seven active
      days, or the last complete day when the series is younger than the window.
- [ ] Cost per completed card and cards by review rounds (0 / 1–2 / 3+) are
      stated with their one-line method; a review-round table reconciles to the
      newest audit within 2%.
- [ ] Renewals in the next 30 days are named with a date.
- [ ] Every alert has one owner and one next action.
- [ ] The per-tenant model reads both plan files — `BUDGET.yaml`'s
      infrastructure block and `docs/MVP-V0.1.md` §8 — and a line present in one
      and absent in the other is reported as a variance, not a zero. BUDGET-only
      reports 55.8%; the full table reports 31.3%.
- [ ] Nothing was paid, approved or repriced by this function.

## Anti-patterns

- **The second spreadsheet.** A hand-maintained total beside the generated one.
  Two truths means no truth; extend the ledger instead.
- **The "misc" bucket.** A category that hides the variance it was created to
  explain. Split the line or name the vendor.
- **Approving a spend to unblock a card.** The monitor becomes the spender and
  the independence is gone. The answer is a decision row, not a payment.
- **A budget with no actual comparison.** A plan that is never reconciled is
  decoration; the close compares every line.
- **Saving money by cutting backups, tests or security.** A cheap month that
  buys an expensive incident is not savings.

## Sources

- FinOps Foundation, *FinOps Framework* — https://www.finops.org/framework/
- AWS, *Well-Architected Framework — Cost Optimization Pillar* — https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/welcome.html
- Paul Graham, *Default Alive or Default Dead?* — https://paulgraham.com/aord.html
- COSO, *Internal Control — Integrated Framework* — https://www.coso.org/guidance-on-ic
- Stripe, *SaaS Metrics: A Complete Guide to Tracking Business Growth* —
  https://stripe.com/resources/more/essential-saas-metrics
- Servury, *Blackbox API* — https://servury.com/docs/?api=blackbox
