# Learning log

Newest first. One section per upskilling session and per weekly exchange.
Evidence is a path, a commit or a card; no adjectives. The weekly exchange owns
the scar table, the borrowed-practice pairs and coverage; a session owns its own
line.

## 2026-11-22 · verifier · V2 — a mutation score is a survivor list

- **Source:** mutmut 3.8.1 (Python 3.14 venv), documentation and README —
  <https://mutmut.readthedocs.io/en/latest/> and
  <https://github.com/boxed/mutmut>: mutates inside functions, “remembers work
  that has been done”, selects the tests that reach a mutant, and leaves
  `mutmut show <name>` as the diff to read.
- **Exercise:** mutation-tested `tools/handoff_check.py` (223 lines, the
  verifier’s first step per playbook principle 2a) against its 32-test suite in
  a scratch tree at `b000999`: 265 mutants, 195 killed, 70 survived, 0 no-tests,
  0 timeouts — 73.6%, 5.85 mutations/second. Read all 70 survivor diffs: 24
  equivalent or unreachable (`uri=True` removed, `--verify` dropped, `or True`,
  `"XXXX"` sentinels), 33 cosmetic (argparse help, message case, `[:7]`→`[:8]`),
  9 real — the exit-code ladder in `main()` (`--card` no longer required, a
  board error returns 3, a READY stamp with no head returns 2 instead of 1) and
  two error-path messages (`stamp or stamp["head"]` raises when `stamp is None`;
  `branch_head` crashes in a tree with no `.git`). Added `BaseSentinelTests`
  (two methods) for the stamp shapes `card_acceptance.py:575` writes today — a
  `base: none` and a 7-char base — and re-ran: 199 killed, 66 survived, 75.1%.
  `x_main__mutmut_109/111/112/116` flipped; each had passed the 32 tests while
  rejecting a legitimate READY with `base is 031f1e1, main is 031f1e1`.
- **Lesson:** 1) A mutation score is a claim about the suite, and the finding is
  the survivor list: triage every survivor as equivalent, cosmetic or a real gap
  before quoting the percentage — 57 of 70 here changed no asserted behaviour,
  so the 70 survivors were 9 real gaps, not 70. 2) Test the
  producer’s dialect at the consumer: every shape a tool writes (`base: none`
  when no `main` resolves, a short sha that `BASE_RE` accepts) is an input its
  reader must be tested against, or a rewritten sentinel check turns a good
  handoff into a change request — a false FAIL costs the builder a round.
- **Evidence:** `docs/ops/evidence/2026-11-22-verifier/README.md`,
  `mutation-summary-before.txt`, `mutation-summary-after.txt`,
  `survivors-before.txt`, `survivor-diffs-before.txt`, `survivors-triage.md`,
  `mutant-112.txt`, `mutant-116.txt`, `proposed-test.patch`,
  `pristine-suite.txt`; `docs/craft/verifier.md` — principle 6 rewritten,
  principle 11 added, Sources extended.
- **Next:** V3 — Conventional Comments: rewrite one review thread with labels
  and compare rework rounds before and after.

## 2026-11-21 · security · S2 — REPORT_DATA is the only field the caller chooses

- **Source:** AMD, *SEV Secure Nested Paging Firmware ABI Specification*, pub.
  56860 rev. 1.58 (May 2025), §7.3 and §4.3, Tables 9 and 22–24 —
  <https://www.amd.com/content/dam/amd/en/documents/developer/56860.pdf>: the
  64 bytes at 0x50 are guest-provided and the firmware “does not interpret this
  data”; MEASUREMENT is “the measurement calculated at launch”; REPORT_ID
  “persists with the guest instance throughout its lifetime”; the policy is the
  guest owner’s at launch and cannot change for the guest’s life; bit 17 of the
  policy is “Reserved. Must be one.”
- **Exercise:** decoded the live v5 report (`e99728ef…fbc`, policy `0x30000`,
  platform_info `0x5`, flags `0x0`, VMPL 0) and verified it end-to-end with
  `tools/snp_report.py` — VCEK fetched from AMD’s KDS, chain to the pinned ARK
  `69:d0:63:b4…`, CPU binding, signature, policy, TCB and header all PASS
  (`verify-live-report.txt`). Then `report_data_test.py`: two fetches with our
  own 64-byte nonces carry them back; the two reports differ only inside
  `0x50–0x90` and in the signature over it; `/attestation`, refreshed by
  `odile-attest-refresh.timer` (`OnCalendar=*:0/5`), carries `odile:<24 random
  bytes>` chosen by `attest.py`, and its `fetched_at` is outside the signed
  `report_b64`; one flipped bit makes the check say “a value this caller did
  not send.”
- **Lesson:** 1) A report binds a request only through REPORT_DATA: a report
  the machine labels on its own timer is fresh to the machine and replayable to
  the visitor, and an unsigned `fetched_at` is metadata, not proof. 2) A parsed
  field nobody asserts is not verified: `snp_report.py:135` stores REPORT_DATA
  and never prints or checks it, so the one field that decides freshness is
  invisible in the verifier’s own output.
- **Evidence:** `docs/ops/evidence/2026-11-21-security/README.md`,
  `report_data_test.py`, `report-data-test.txt`, `verify-live-report.txt`,
  `served-document.txt`, `make-check.txt`; `docs/craft/security.md` —
  principle 18, one replaced checklist row, one anti-pattern, one Source.
- **Next:** S3 — SLSA Build L2: compare Odile’s release provenance and name
  the missing artifact or signature step.

## 2026-11-20 · exchange — week of 2026-11-14

### Shipped and learned

- **ODL-479 — the handoff gate runs the 15 HQ-only modules** (`2198cfd`; the
  gap was filed as `t_a2452d17`). Taught that a gate is only as wide as the
  checkout it runs in: the 15 HQ-only modules never ran in a card worktree, so
  `main`'s merge gate was their first run — and it went red twice on
  `docs/runbooks/recycle.md` while every branch was green.
- **`t_ac14f68c` — both image builders' copy lists, derived** (`e63937a`).
  Taught that the required set is derived from `install-stack.sh` itself (36
  names across four gates), not kept as a second list; and that a green branch
  is not a green merge tree — 3 of 4 new tests failed against `main` after it
  moved 15 minutes past the branch's base.
- **ODL-242 — the key-cap re-cut** (`d2a0ce5`). Taught that a record's
  paraphrase of a decision is a new decision: the entry added "provider key id"
  to what HQ keeps, attributed it to G9, and would have made a public roadmap
  objective approved four hours earlier false when the card was built.

### Scars

| Date | Card | Seat | Finding | Root cause | Destination | Owner | Status |
|---|---|---|---|---|---|---|---|
| 2026-11-19 | t_ac14f68c | Verifier | Branch head green, merge tree red: 3 of 4 new tests fail against `main` + the branch after `main` moved past the branch's base | Repeat of the 2026-11-13 gate scar: the stamp certifies the branch head, and `card_acceptance` checked conflicts, not the merged tree's tests | lint/check — ODL-343 runs the card's changed test modules on the `git merge-tree` tree before stamping | Verifier | open — ODL-343 |
| 2026-11-20 | t_4624d851 | Product lead | A re-cut added "provider key id" to what HQ keeps, attributed to G9, contradicting G9's own words and a live public objective approved 4 h earlier | The entry was written from a summary of the decision, not diffed against the governing decision and the live public sentence; `board_sync check` validates shape, not meaning | checklist item — Product lead: a re-cut that names what HQ keeps is diffed against its cited decision and the live public sentence before handoff | Product lead | open |
| 2026-11-19 | t_6d956aee | Builder | Round 3 found the live tenant's vendor id and slug in the public tree; `recycle.md` §4 retyped two ids instead of copying the resting file | A runbook transcribed identifiers from the console and the test fixture used the live id as a stand-in; the leak gate matched bare ids, not quoted assignments or link text | lint/check — `d939e22` pins the positions the tenant id used | Builder | closed — `d939e22` |

### Borrowed practice

- **Growth → Finance.** Practice: demand and activation data — spend per channel,
  the cohort definition, and the cost of the first ten. Receiver: Finance manager
  (staff function), next session; no finance card is running or ready, so no
  comment was added. Due 2026-11-29, the finance track's next rotation slot.
  Verifier check: the finance memo defines the cohort, gives spend per channel and
  costs the first ten, and every number traces to a `SPEND.csv` row or an
  invoice id.
- **Finance → Designer.** Practice: the plan price and its gross-margin floor
  constrain the page promise — a feature that costs more than it earns is not a
  feature (the $49 figure is alpha.1's; the constraint is not). Receiver:
  Designer, next card `t_e23f3288` (ready), comment added. Due 2026-11-27, the
  designer's next rotation slot. Verifier check: the card's evidence prices every
  promise the page makes against the finance model and names the margin line, or
  records "no promise touched" with the diff that shows it.

### Coverage

7/9 tracks have a `Landed` date in the last 10 days — product-lead P1 11-15,
architect A2 11-16, designer D3 11-18, growth G2 11-19, finance F2 11-20,
security S1 11-12, verifier V1 11-13. Overdue: builder B2 (last landed 11-08,
12 days); chief-of-staff C1 never landed, and the 11-18 CoS session changed
`WORKFLOW-CHANGES.md` but filled no track row. Complete: none. The track that
needs the next session is **builder**: B2 is the only unit more than 10 days
stale, and its next rotation slot is 2026-11-26.

### Question

**Verifier** — which single change to your review routine makes the merge tree,
not the branch head, the thing you approve, and will you re-run the card's
changed tests against `git merge-tree` before the next approval? (Answer in the
verifier session on 2026-11-22.)

### Baseline

The 2026-11-13 exchange recorded the baseline: 1/5 first-pass approvals and 1.4
change-request events per card over the five most recently reviewed cards; no
event records an approval reason, so first-pass is inferred from zero change
requests. This week all three cards read reached approval in round 2 or later
(`t_ac14f68c` round 2, `t_4624d851` round 2, `t_6d956aee` round 4), but the
board still carries no approved-with-reason event, so a first-pass rate is **not
measurable from the board** without it.

## 2026-11-20 · finance · F2 — a closed day restates, and the balance caught a charge the ledger missed

- **Source:** `docs/ops/finance/POLICY.md` (sources of truth; a `committed`
  row's amount is the standing renewal) and `tools/spend.py`, read with the
  generator that writes the series,
  `~/.hermes/profiles/finance/scripts/finance_check.py` line 276 — the day
  bucket is `last_activity_at`, not `started_at`. The billing system of record is
  the Servury Blackbox API, <https://servury.com/docs/?api=blackbox>
  (`?action=invoices`, `?action=server`), read 2026-11-20.
- **Exercise:** reconciled 13–19 Nov. Inference $269.74 over 1,059 sessions; the
  same 9–15 Nov window reads $286.66 in the 16 Nov memo, $285.01 in the 17 Nov
  reconciliation and $276.34 today (−$10.32, −3.6%), and only two closed days
  moved — 13 Nov −$6.89 and 14 Nov −$3.43, the other five under $0.005 — because
  23 sessions started on 13/14 Nov and resumed later carry $11.49 to their
  last-activity day. External cash in the week was $11.18: $9.03 (invoice
  58093838, host Blackbox Max auto-renewal, 7 days) and $2.15 (07250b99, dev API
  renewal). The $2.15 is in FIN-006's note; the $9.03 is in no row and no note,
  and FIN-001's `renews_on` still reads 2026-11-19 while the API says the box
  expires 2026-11-26T11:25:32Z. The balance corroborates the charge
  independently: $31.55 − $9.03 = $22.52, the balance the renew job read this
  morning. Owner `savio`; the evidence that closes it is the invoice and expiry
  in `servury-read-2026-11-20.json`, after which the row's date becomes
  2026-11-26 and its note carries the $9.03 and the $0.45/term list-price
  premium (≈$23/yr). MTD reconciles: $447.8762 in `SPEND.csv` = $447.8762 from
  the live check at 06:57 UTC.
- **Lesson:** 1) A daily series is a view, not a ledger: a session's whole cost
  rides its last activity, so a closed day restates and a week figure is only
  true of the snapshot it came from — date it, derive it once, and never compare
  a Monday number with a Friday file. 2) The balance is the second witness: a
  charge the invoice list shows and `LEDGER.csv` does not is found by the
  balance delta in one line, and a renewal that fired must advance `renews_on` in
  the same week — a `committed` row whose date is past cannot trigger its own
  30-day alert.
- **Evidence:** `docs/ops/evidence/2026-11-20-finance/README.md`,
  `reconcile_week.py`, `reconcile-week.txt`, `servury-read-2026-11-20.json`,
  `make-check.txt` (`Ran 2328 tests in 580.330s`, `make check: OK`, exit 0);
  `docs/craft/finance.md` — principle 11, one checklist item, one Source.
- **Next:** F3 — Bessemer Cloud 100 benchmarks: copy one mechanism (cohort gross
  margin or NRR), reject one metric that does not apply pre-revenue.

## 2026-11-19 · growth · G2 — a recruit is an install, and the queue cannot send

- **Source:** Paul Graham, “Do Things That Don’t Scale” (July 2013) —
  <https://paulgraham.com/ds.html>, read 2026-11-19: *Recruit* (“You can't wait
  for users to come to you. You have to go out and get them.”; the Collison
  installation, “Right then, give me your laptop”), *Fragile* (“about 30 days of
  going out and engaging in person with users made the difference between success
  and failure”), and *Big* (“It's not enough just to do something extraordinary
  initially. You have to make an extraordinary effort initially.”).
- **Exercise:** wrote `docs/product/RECRUIT-FIRST-TEN.md` — who the ten are,
  where they come from, the ask, the install, the first week, the tracker and
  the kill lines — and staged three messages in
  `docs/product/GROWTH-QUEUE.md`: a warm email, a cold X DM and a forwardable
  intro. Nothing sent. The track row was cut on 6 November for ten paying
  tenants at $49; the product re-cut on 18 November to alpha.0 (private, no
  payments), so the script keeps the ten for alpha.1, notes alpha.0’s
  recommended five, and carries no price sentence.
- **Measured:** `curl -sS -D - -o /dev/null https://test.odile.fyi/register` →
  `HTTP/2 404`, `server: odile-bench`, 10 bytes on 2026-11-19, so the queue’s
  send condition is unmet and every row says so. `check_queue.py`: 3 rows, all
  `staged — not sent`; 3 message bodies; 15 placeholders; 129 / 95 / 88 words.
  `claims-trace.txt`: “Your own computer, with an agent inside it.”, “It keeps
  working when you close the tab.” and “One machine per person.” found verbatim
  in `docs/CLAIMS.md`; the install and no-price lines are the existing
  `templates/emails/invite.txt` / `BRAND.md` §Email copy and `MVP-V0.1.md` §0.
  `claims_lint.py` exit 0 (18 files, 13 forbidden phrases); `make check` exit 0
  — `Ran 2148 tests in 582.834s`, `make check: OK` (`make-check.txt`).
- **Lesson:** 1) The recruit is an install, not a link: when someone says yes,
  get on a call, share the screen and stay until the machine is running and the
  first file exists; the provisioning wait is the past-tense interview. A “yes,
  send me the link” is a maybe. 2) The first week is the product: a named human
  asks one question on day 1, 3 and 7 — did the file survive, what did you ask
  for today, what did you stop doing by hand — and the answers, not the signup,
  are the retention signal.
- **Evidence:** `docs/product/RECRUIT-FIRST-TEN.md`,
  `docs/product/GROWTH-QUEUE.md`; `docs/ops/evidence/2026-11-19-growth/` —
  `README.md`, `check_queue.py`, `check-queue.txt`, `placeholders.txt`,
  `register-state.txt`, `claims-trace.txt`, `claims-lint.txt`, `make-check.txt`;
  `docs/craft/growth.md` — principles 2 and 3, one checklist item, one
  anti-pattern.
- **Next:** G3 — Superhuman’s concierge onboarding: map the concierge moment to
  Odile’s first-run ceremony; name one step to hand-hold.

## 2026-11-18 · chief-of-staff · a cap is a claim until it is measured

- **Source:** the 17 November inference audit (§3.1, in
  `docs/ops/finance/AUDIT-2026-11-17-inference-spend.md`) and the board's own
  run log: `task_runs.error` for 217 failed runs, and `gateway.log` for the
  worker-cap boot lines.
- **Exercise:** count what each knob change actually did. Iteration budget
  150 → 250 (between 11-11 and 11-17): 37 runs had hit `150/150`; after the
  raise **8 runs hit `250/250`**, the ones inspected carrying an empty summary —
  the cap was not the cause. Worker cap 7 → 3 → 6 → 8 in 36 hours: at 3 the review-lane
  reservation left `ready_budget = 3 − 1`, so six seats idled with 33 cards
  queued; at 7 the binding limit had been a provider quota wall (29 exits in
  same-minute bursts). Reasoning effort was unmeasured: a 4-sample probe on the
  real route returned max 403 (260–562), high 196 (172–211) and low 172
  (151–212) reasoning tokens, and **`medium`/`minimal` answer HTTP 400** — the
  docs' full ladder is not this provider's ladder.
- **Lesson:** a workflow knob is a claim about the system until a baseline, a
  probe and a revert condition exist. Moving a cap to fix a card-size problem
  buys a longer wall; moving it to fix a queue buys a different queue. Every
  change needs its second-order victim named (which lane gets the slot, how
  many review rounds the effort level costs) and a watch date, or it is
  indistinguishable from a guess one week later.
- **Evidence:** `docs/ops/WORKFLOW-CHANGES.md` (register with all five rows and
  the probe numbers), `docs/ops/OPERATING.md` § *Changing a workflow knob*,
  `docs/backlog.yaml` dispatch comment, and the probe transcript in this
  session (4 samples per level, one prompt, `cloud-api.near.ai`).
- **Next:** re-measure the reasoning-effort change on 2026-11-20 (review
  rounds/card and $/card; revert above +20%); reconcile the `finance` profile's
  stale `max_in_progress: 3`; and put the next cap change through the same
  procedure — no exceptions for urgency.

## 2026-11-18 · designer · D3 — a status code is a screen, and the shipped 404 had no way back

- **Source:** Linear Method, *Principles & Practices*
  <https://linear.app/method/introduction> (“Aim for clarity”, “Simple first,
  then powerful”), plus the product pages that show the empty-state mechanism —
  Start Guide <https://linear.app/docs/start-guide> (a new workspace opens on a
  demo workspace with issues, projects and cycles already in it) and Issues
  <https://linear.app/docs/creating-issues> (`C` is the one way to start);
  all read 2026-11-18. The Method index names no empty-state section; the
  mechanism is in the product docs, and the teardown says so.
- **Exercise:** the unknown-path state on the live service, before and after.
  `curl -i https://odile.fyi/no-such-page` → `404`, `text/plain; charset=utf-8`,
  10 bytes, body `not found`; rendered at 390×844 and 1440×900 it has
  `document.title === ''`, 0 links, 0 blocks before an action and no action.
  `app.py:895` is the final `_dispatch` return. The rewrite
  (`docs/ops/evidence/2026-11-18-designer/404-after.html`) reuses the shipped
  `web/faq.html` stylesheet verbatim and adds the state line, heading, one
  sentence and one action: measured 3 blocks before the action, action at
  y=263 (390) / 282 (1440), 44 px high, in the first viewport, 2 links both
  `/`, no horizontal overflow, contrast 13.33 / 7.55 / 13.33:1, and a 2 px ink
  focus outline on both tab stops. The one-line route change is proposed in
  BEFORE-AFTER.md §5, not applied.
- **Lesson:** principle 12 and the error checklist item: every status-code
  branch that can reach a stranger ships in the brand with one way back, and the
  audit is `curl -i` plus a link count. “Empty, error and loading states
  designed” did not catch this because a `text/plain` branch is not a page and
  never enters a design review; the same 404 had already been photographed in
  the 2026-11-07 UX set (`ux/23-404-390.png`, byte-identical today) and stayed
  unchanged for eleven days — a screenshot in an evidence folder is not a check.
- **Evidence:** `docs/ops/evidence/2026-11-18-designer/BEFORE-AFTER.md`,
  `404-after.html`, `before-response.txt`, `measurements.json`,
  `before-390x844.png`, `before-1440x900.png`, `after-390x844.png`,
  `after-1440x900.png`, `after-390x844-focus.png`, `after-1440x900-focus.png`,
  `make-check.txt` (`Ran 1996 tests … OK`, `make check: OK`, exit 0);
  `docs/craft/designer.md` — principle 12, the error checklist item, one
  anti-pattern, two Sources. **Commit: none** — `docs/craft/` and `docs/ops/`
  are local-only in this tree (`.git/info/exclude`), so the playbook, the track
  row, the log and the evidence stay in the working tree.
- **Next:** D4 — WebAIM Million: take the top three failure classes, check
  Odile’s public page against each and fix one.

## 2026-11-16 · architect · A2 — a read-only handle writes, and the sandbox is part of the contract

- **Source:** *The Twelve-Factor App* — III *Config*
  <https://12factor.net/config>, IV *Backing services*
  <https://12factor.net/backing-services>, VI *Processes*
  <https://12factor.net/processes>, IX *Disposability*
  <https://12factor.net/disposability>, read 2026-11-16; SQLite, *Write-Ahead
  Logging* <https://www.sqlite.org/wal.html> — a WAL database's `-shm` and
  `-wal` are created by the first reader; systemd, *systemd.exec*
  <https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html> —
  `ProtectHome=read-only` and `ReadWritePaths`.
- **Exercise:** the HQ deploy path (`./deploy.sh` → `/opt/odile` →
  `odile.service` plus the four timers) audited one factor at a time with one
  live command each — the table is `docs/architecture/AUDIT-deploy-path.md` §1.
  Factor IV produced the defect: `odile-roadmap.service` failed every run from
  03:45Z (12 by 06:30Z, the 13th at 06:45Z) with `roadmap: board db
  /root/.hermes/kanban/boards/odile/kanban.db: unable to open database file`,
  while `/var/lib/odile/roadmap.html` kept mtime 03:30. Four cases on a copy of
  the board, each run inside the unit's own sandbox properties: read-only
  directory, `mode=ro`, no side files → `SQLITE_CANTOPEN`; `immutable=1` → ok,
  `journal_mode=delete`, 388 rows; `ReadWritePaths` on the database directory →
  ok and **two side files created**; side files already present → ok. The board
  is WAL, the read-only reader writes `-shm`/`-wal`, `ProtectHome=read-only`
  forbids it, and the timer passes only while another process holds the board
  open — which is what made it intermittent.
- **Lesson:** a handle that says read-only is still a writer when the store is
  WAL, so the sandbox property and the resource handle are one decision:
  principle 11 now says so and names `ReadWritePaths`, and the design checklist
  requires the unit's `ExecStart` rehearsed once inside the unit's own sandbox
  with a cold cache, because a root shell proves the code and not the unit. The
  one-line fix (`ReadWritePaths=/root/.hermes/kanban/boards/odile`) is proposed
  in the audit and not applied; it belongs to a card.
- **Evidence:** `docs/architecture/AUDIT-deploy-path.md` (the four-factor table,
  the four cases, the blast radius and three further observations — the deploy
  ships ten host-only paths into `/opt/odile`, three demo hostnames are
  constants in `app.py`, and `odile-claim.service` names no precondition for
  `/root/.odile/config.yaml`);
  `docs/ops/evidence/2026-11-16-architect/probe_wal_ro.sh`,
  `…/wal-readonly.txt`, `…/disposability.txt` (start 19 ms, SIGTERM exit 8 ms,
  no drain), `…/deploy-ships-ignored.txt`; `docs/craft/architect.md` — principle
  11, one checklist item, two Sources. **Commit: 2cbbe25, 2a00aa1** — the
  architecture doc and its 07:15Z note; the pre-commit gate ran `make check: OK`
  on the first and passed again on this tree before the second. `docs/craft/` and
  `docs/ops/` are local-only in this tree (`.git/info/exclude`), so the playbook,
  the track row and this entry stay in the working tree.
- **Next:** A3 — Stripe's versioning promise: propose a five-line compatibility
  rule for the control-plane interface and name the one change that would break
  it.

## 2026-11-15 · growth · G1 — a brand reply under the founder's post is an interruption

- **Source:** X's own help, read 2026-11-15 — *About conversations on X*
  (participants up to 50, original author unremovable),
  *Conversations Recommendations* (replies from followed accounts and replies the
  root author answered first, verified second, everyone else last),
  *How to Repost* (a quote starts its own conversation), *How to delete a Post*,
  *How to customize your profile* (a pin is the first post a visitor sees), and
  the API *Metrics* page (`url_link_clicks` is non-public, user context, 30 days).
- **Exercise:** the 14 November thread read as published, then 14 infra/dev-tool
  company accounts read for what the brand account does when its founder posts:
  9 of the 13 whose founder account is named repost him inside their last 20
  reposts (Cal.com 11, Render 9, Warp 8, Turso 3; Railway 2; Resend, Vercel,
  Supabase, Linear 1),
  4 never do (PostHog reposts its co-founder instead), and in the 10 accounts
  whose replies were read, all 24 go to customers, partners or the account
  itself, none to a founder. The
  quote exists and is small: Turso's quote of its founder's announcement took
  5.5% of its likes and 7.5% of its views.
- **Lesson:** amplification is a repost, a quote is a footnote with the company's
  name on it, and a reply to the founder is an interruption that carries 61
  characters of handles ahead of its copy. Three rules changed: principle 13 now
  says the brand answers once and never in the founder's thread;
  `X-SUBMISSION.md` §1 and §7 item 4 make the brand's one surface a quote; the
  live reply `2121645919175074120` is recommended for deletion, since the pinned
  quote carries its link and the login page carries its boundary sentence and
  credential (read 08:39Z).
- **Evidence:** `docs/growth/X-PLAYBOOK.md` (six rules, the worked example, every
  source with the date read); `docs/growth/X-SUBMISSION.md` §6k postscript;
  `docs/craft/growth.md` principle 13. **Commit: none** — `docs/growth/`,
  `docs/craft/` and `docs/ops/` are local-only in this tree, so `git status`
  stays clean and forcing them would publish the operating record.
- **Next:** the 48-hour read due 2026-11-16T16:48Z — a stranger reporting `PASS`
  on the verifier, plus X's link clicks on the two posts carrying the link.

## 2026-11-15 · product-lead · P1 — an appetite without a no-go is an estimate

- **Source:** Basecamp, *Shape Up*, ch. 3
  <https://basecamp.com/shapeup/1.2-chapter-03#setting-the-appetite> — “Estimates
  start with a design and end with a number. Appetites start with a number and
  end with a design”; ch. 8
  <https://basecamp.com/shapeup/2.2-chapter-08#the-circuit-breaker> and the
  glossary — the circuit breaker is “cancel projects that don’t ship in one cycle
  by default instead of extending them by default”.
- **Exercise:** every card in the live milestone, costed and given a cut line —
  `docs/product/APPETITE-M1.md`. M1 (window 2026-11-15/2026-11-28) holds 53 cards
  = **58.0 agent-days** on the playbook's size bands (26 S, 24 M, 3 L) against a
  ceiling of 7 × 14 = 98 agent-days at the live cap. The board's `completed_at`
  shows **47 of 53 done before 2026-11-15T00:00Z and none after**: the milestone
  opened 89% complete, so its window is a label on work done in M-1/M0/MH time.
  The real bet is 6.0 agent-days (ODL-207/516/517/518/519/524) plus 5.5
  agent-days of MH work still open past a window that shut on 13 November.
  Two tooling facts fell out: `docs/backlog.yaml` records
  `dispatch.max_concurrent: 4` while `kanban.max_in_progress` is 7 — the drift
  ODL-394 was landed to prevent — and `tools/product_pulse.py:194` reports MH as
  the current milestone because it returns the first milestone in file order with
  any unfinished card. The two human cards are the gate's critical path: ODL-207
  (the five-day soak) is blocked with no `docs/dogfood/week1.md`, and ODL-524
  blocks 515 → 517 → 518 → 519. Named one split: ODL-516 carries two problems
  (a gate workflow and a one-root public-tree re-cut) and is 517's declared
  dependency.
- **Lesson:** an appetite is a bound chosen before the design, and its companion
  is the no-go. Odile records `size` (an estimate) and no cut line, so a
  milestone can open with 47 of its 53 cards already landed and nothing can be
  cancelled when a cycle overruns — which is exactly what happened to MH. Two
  rules changed: principle 3 now requires the one-sentence no-go per card, and the
  card checklist requires the milestone's open appetite to be summed against the
  live dispatch cap, with every card serving one of that milestone's exit
  criteria.
- **Evidence:** `docs/product/APPETITE-M1.md` (tables A and B, the arithmetic
  block, `tools/product_pulse.py --json` at 07:07Z, the board's `completed_at`
  histogram); `docs/craft/product-lead.md` — principle 3, one checklist item,
  chapter URLs in Sources. **Commit: none** — `docs/ops/` and `docs/craft/` are
  local-only in this tree (`.git/info/exclude`, installed with the 2026-11-14
  re-root `24ee323`), so `git add` refuses all four paths and `git status` stays
  clean; forcing them would publish the operating record to `Odile-AI/odile`.
- **Next:** P2 — write a one-page PR/FAQ for M2 (the machine factory) and list
  the three FAQ answers `docs/MVP-V0.1.md` §9 cannot give.

## 2026-11-13 · exchange — week of 2026-11-06

### Shipped and learned

- **ODL-500 — the judge's five minutes at demo.odile.fyi** (merge `3f8a0c3`).
  Taught that a brand record must name the commit whose tree carries the surface
  it certifies: the record said `9a9217f`, the 175-line vendor-white chrome CSS
  with none of the palette the same record measured; the palette landed in
  `fd7bdc0`.
- **ODL-502 — the Servury 2026.11.2 re-pin** (merge `47b8ae7`). Taught that a
  pin is provenance, not a version string: the entry that works carries the
  source URL, `fetched_at` and the committing ancestor, the released bytes
  re-verify 4/4, and `--print-pin` in a tree without the release entry deletes
  it.
- **ODL-215 — backups and the restore drill** (merge `9f43829`). Taught that a
  two-machine drill has two T0s: the runbook reported 38m03s from B's destroy
  while restoring A's home, and the end-to-end RTO was 84m13s; anchor the clock
  to the machine whose data is restored.

### Scars

| Date | Card | Seat | Finding | Root cause | Destination | Owner | Status |
|---|---|---|---|---|---|---|---|
| 2026-11-13 | ODL-500, ODL-502, ODL-215 | Builder | Round 1 on all three cards read for this exchange was a change request for one reason: the handoff omitted `make acceptance CARD=<id>` output | The gate is prose in three documents and its only durable artifact is text a person pastes; a metadata paraphrase reads as compliance because nothing binds a READY run to the head under review | lint/check — ODL-336 stamps the gate on the card and `handoff_check.py` refuses a handoff with no stamp for HEAD | Builder | open — ODL-336 |
| 2026-11-12 | S1 session (no card); B1 | Security | The 405 and `/claim` 413 branches answer before reading the declared body — B1's connection-reuse class, four days later on two branches its test never touched | B1 fixed the branch, not the class; no test enumerates early-return branches | lint/check — a test that drives every early-return branch and sends the next request on the same connection | Builder | open |
| 2026-11-13 | ODL-215 | Builder | §5.5 reported a 38m03s RTO for a restore of A's home, timed from B's destroy; end-to-end was 84m13s | A two-machine drill has two T0s; the record took the last destroy instead of the restored object | checklist item — anchor T0 to the machine whose data is restored and state both clocks | Builder | closed — `56dc6eb` |

Card created: **ODL-336** — the handoff's acceptance gate as a stamped board fact.

### Borrowed practice

- **Verifier → Chief of staff.** Practice: evidence a stranger can rerun; a done
  card whose branch is not on `main` did not happen. Receiver: Chief of staff,
  next card (no CoS card is running or ready, so no comment was added); due
  2026-11-14, the CoS rotation session. Verifier check:
  `git merge-base --is-ancestor <sha> main` passes and every evidence command
  re-runs from a clean checkout with the recorded output.
- **Chief of staff → Growth.** Practice: a blocked launch decision is one
  question with options and an owner, not a new card or a status meeting.
  Receiver: Growth, next card (no growth card is running or ready, so no comment
  was added); due 2026-11-19, Growth's next rotation session. Verifier check:
  each blocked launch decision is exactly one human card carrying two options, a
  recommendation, an owner and the date asked, and no product card was created
  for it.

### Coverage

7/9 tracks have a `Landed` date in the last 10 days — architect 11-07, builder
11-08, designer 11-09 (D2; D1 on 11-06), growth 11-10, finance 11-11, security
11-12, verifier 11-13. Overdue: product-lead P1 (rotation slot 11-06, seven
days). Complete: none. Next session by rotation is chief-of-staff C1 on 11-14;
the track that needs a slot is **product-lead** — the only one with zero units.

### Question

**Builder** — which single change to your handoff routine makes the `make
acceptance` output impossible to omit, and will the next handoff carry the board
stamp rather than a paraphrase? (Answer in the builder session on 2026-11-17.)

### Baseline

The 2026-11-06 entry recorded 1 first-pass approval in 3 reviews. The five most
recent completed cards with a review (`changes_requested` events on the board)
show 1/5 first-pass (ODL-501) and 7 change-request events over five cards =
1.4 rework rounds per card; `review_requested` totals 13 (2.6 per card). The
event proves rework; no event records the approval reason, so first-pass is
inferred from zero change requests.

## 2026-11-13 · verifier · V1 — a nit is a deferred must, so name its destination

- **Source:** Google, *The Standard of Code Review* —
  <https://google.github.io/eng-practices/review/reviewer/standard.html>: approve
  a change once it “definitely improves the overall code health”, and prefix
  optional polish “Nit: ” so the author knows “it’s just a point of polish that
  they could choose to ignore”. *How to write code review comments* —
  <https://google.github.io/eng-practices/review/reviewer/comments.html>: a
  comment explains why in the code’s terms, and “it is the developer’s
  responsibility to fix a CL, not the reviewer’s.”
- **Exercise:** calibrated the ODL-502 round-1 review (`t_4a1a0337`, 05:18Z,
  5,378 bytes) — every finding labelled: 1 must, 3 nit, 0 question, 1
  disclosure; the must is 25% of the actionable set. Scored each against what the
  author did: all three nits were actioned, two in the docs-only commit
  `6173c06` (`verify.md` 9+/5−, `REVIEW-demo-public.md` 1+/1−), the third
  answered in the round-2 handoff; none of the three named a destination.
  Widened to the twelve verifier reviews over 3 KB posted on 13 November
  (`review-headings.txt`): nine headings for the non-blocking bucket, “nit” in
  2 of 12, musts counted when they exist and nits never. Rewrote F3 in full
  (handoff “Deploy note”, sentence 1) with the commands that settle it —
  `deploy.txt:4` names the tree `b1c8909`, `cmp` on `odile-verify` and
  `odile-verify.pin` exits 0, `cmp` on `app.py` exits 1 and
  `grep -c first-visit-preview app.py` is 0 against 2 at `app.py:775–780` — plus
  the replacement sentence and “no redeploy”.
- **Lesson:** a non-blocking finding is not optional polish at Odile: 3 of 3 were
  actioned, so a nit is a deferred must. Give it the same reproducible target as
  a must and one destination word — `must`, `nit-now`, `nit-later` (a card),
  `question` — because nine headings for one bucket made the author read intent
  off an adjective, and “is imprecise” made the author compose the correction.
  Playbook rule 7 widened from “request changes” to every finding, with the
  destination list; one checklist item added.
- **Evidence:** `docs/ops/evidence/2026-11-13-verifier/CALIBRATION-ODL-502.md`,
  `review-headings.txt`; `docs/craft/verifier.md` — rule 7, checklist item,
  Google’s standard and comments pages in Sources.
- **Next:** V2 — mutation testing with mutmut: run it on one module and report
  the score and the one test to add.

## 2026-11-12 · security · S1 — frame the connection, not just the request

- **Source:** OWASP ASVS 5.0, V4 API and Web Service, requirement 4.2.1 (L2) —
  <https://github.com/OWASP/ASVS/tree/master/5.0/en>: every component must
  determine the boundaries of an incoming HTTP message by the mechanism the HTTP
  version defines, to prevent request smuggling. RFC 9112 §9.3 —
  <https://www.rfc-editor.org/rfc/rfc9112.html#section-9.3>: “A server MUST read
  the entire request message body or close the connection after sending its
  response; otherwise, the remaining data on a persistent connection would be
  misinterpreted as the next request.” Caddy v2.6.2 `reverse_proxy`,
  `addForwardedHeaders` — <https://github.com/caddyserver/caddy/blob/v2.6.2/modules/caddyhttp/reverseproxy/reverseproxy.go>:
  `trusted_proxies` defaults to none, so an incoming `X-Forwarded-For` is
  ignored and the header is set to the immediate peer.
- **Exercise:** mapped every route `app.py` serves to ASVS 5.0 L2
  (`docs/security/ASVS-L2-MAP.md`), with the live headers measured per route
  (`surface.txt`). Found 4.2.1 unmet and reproduced it on the live site with
  three read-only commands: `POST /nope` with a body answered 405 (the handler
  returns before reading `Content-Length`, `app.py:853–856`), the next `GET /`
  answered **501** with `Unsupported method ('abcGET')`, the one after 200. The
  body stayed in the stream, `BaseHTTPRequestHandler` parsed it as the next
  request line, and Caddy handed the poisoned response to the next visitor on
  its pooled upstream connection. The same class is on the `/claim` 413 branch
  (`framing.txt`). The map also disproved a hypothesis: the app takes the first
  `X-Forwarded-For` element for its rate-limit key (`app.py:850`), but a
  two-hop Caddy 2.6.2 probe shows the client's value is discarded
  (`caddy-xff.txt`), so there is no live rate-limit bypass — only a contract
  pinned to this Caddy version.
- **Lesson:** 1) An error branch is not done at the status line, for the second
  time and on a different branch: any early return that answers before reading
  the declared body must drain it or close the connection, because the proxy
  reuses the connection and the next client inherits the framing. B1 fixed the
  `/waitlist` 413 branch; the 405 and `/claim` 413 branches were outside that
  test's reach, so test the class, not the branch. 2) Measure the proxy's header
  contract at two hops instead of reading its docs: the same `split(",")[0]`
  that is safe behind Caddy 2.6.2 (which overwrites `X-Forwarded-For`) is
  attacker-controlled behind a proxy that appends, so pin the behaviour with a
  probe and re-run it on the upgrade.
- **Evidence:** `docs/security/ASVS-L2-MAP.md`;
  `docs/ops/evidence/2026-11-12-security/` — `README.md`, `surface.txt`,
  `live-desync.txt`, `caddy-xff.txt`, `ratelimit-key-contract.txt`,
  `framing.txt`, `probe_framing.py`, `probe_via_caddy.py`, `echo_headers.py`,
  `Caddyfile.probe2`; `docs/craft/security.md` — principle 11, two checklist
  items, ASVS 5.0, RFC 9112 and the Caddy source added to Sources.
- **Next:** S2 — AMD SEV-SNP, the attestation report itself: read the report
  fields and policy bits, write a plain-language test for one field and run it.

## 2026-11-11 · finance · F1 — support is COGS, and a cap is not a cost

- **Source:** Stripe, *SaaS Metrics: A Complete Guide to Tracking Business Growth*
  — <https://stripe.com/resources/more/essential-saas-metrics>: gross margin is
  ((Revenue − COGS) / Revenue) × 100, and in a SaaS business “COGS typically
  includes the costs associated with hosting, software development, and customer
  support.” Odile side: `docs/MVP-V0.1.md` v5 §8 prices the machine, the $12.00
  inference allowance, backup, email, DNS and the Stripe fee, then puts support at
  “20 minutes per user per week in alpha labour, not in the per-tenant table.”
- **Exercise:** built `model.py` to read `BUDGET.yaml`, `LEDGER.csv`, `SPEND.csv`
  and §8 rather than transcribe them, and ran it. The committed machine line is
  FIN-005 at $18.99 against the $19.00 plan (−$0.01); the Stripe fee on $49 is
  $1.72 at the budget's 2.9% + $0.30; cash COGS is $33.66 and the margin $15.34
  (31.3%). Support is 1.444 h/month at 20 min/week, so break-even is **$10.62/h**:
  $10/h leaves $0.90, $15/h loses $6.33 (−12.9%), $25/h loses $20.77 (−42.4%).
  A model built only from `BUDGET.yaml` — whose per-tenant block is
  infrastructure-only — reports $21.67 and 55.8%, a 24.5-point overstatement;
  the $12.00 allowance exists only in §8. Ten tenants: $490.00 − $336.60 =
  $153.40 gross margin, $19.76 HQ fixed accrual leaves $133.64, and at $15/h
  support the cohort is −$83.03/month before acquisition cost. The only measured
  inference rate is $131.16 over 757 sessions ($0.1733/session), so the $12.00
  cap is ~69 sessions; FIN-003 stays `metered`/`unpriced`, so the cap bounds the
  cost but does not measure it. Largest unknown: the support hour's price and
  allocation — no cap, no rate, no ledger row, and the only line that flips the
  sign.
- **Lesson:** 1) Support is COGS: put the hours and a stated rate in the
  per-tenant table, or print the break-even rate beside the margin; a margin that
  omits the hour is a ceiling, and at $49 the whole margin is 1.44 h at $10.62/h.
  2) Model from the union of the plan files, not the one called “budget”: a line
  present in one file and absent in the other is a variance, not a zero —
  `BUDGET.yaml` alone reports 55.8% where the full §8 table reports 31.3%.
- **Evidence:** `docs/ops/evidence/2026-11-11-finance/` — `README.md`, `model.py`,
  `model-output.txt`; `docs/craft/finance.md` — principle 6, one checklist item,
  and the dead `stripe.com/guides/saas-metrics` source URL (404) replaced with
  the fetched one. No change to `BUDGET.yaml`; the model-file completion is a
  proposal in the README, not an approval.
- **Next:** F2 — `docs/ops/finance/POLICY.md` + `tools/spend.py`: reconcile one
  week of `SPEND.csv` and `LEDGER.csv`; find one unpriced or unreconciled row;
  name the owner and the evidence that would close it.

## 2026-11-10 · growth · G1 — score the sentence, then date it to the product

- **Source:** April Dunford, “A Quickstart Guide to Positioning” (15 March 2021,
  a summary of *Obviously Awesome*) —
  <https://www.aprildunford.com/post/a-quickstart-guide-to-positioning>: the five
  components in one order (competitive alternatives → differentiated features →
  value → target segmentation → market category), each defined by the one before
  it; trap 1, “phantom competitors” and “what would a customer do if your offering
  didn’t exist”; trap 2, a new category must be taught before it frames anything
  (she cites 90% of tech IPOs in the five years to 2021 as positioned in existing
  markets). Product side: `docs/MVP-V0.1.md` v5 (10 November) §4 R1–R6 — no CLI,
  no user-managed key, sealed boot.
- **Exercise:** wrote `docs/product/POSITIONING.md` against the live page, not the
  template. `curl -fsS https://odile.fyi/` returned a lede with no alternative in
  it (“Your own computer, with an agent inside it. No one at Odile can log in to a
  tenant machine. The only SSH key is yours.”). The tree moved while the session
  ran: `docs/MVP-V0.1.md` v5 appeared in the working tree (10 November, the
  owner’s requirement), so the canvas was rebuilt on R1–R6 — the buyer is now
  non-technical, and the proof moves from “your key” to a keyless sealed boot.
  Scored three approved candidates 0–2 per component: A (live) 4/10, B “A real
  Linux machine, not a chat window.” 4 (it names the alternative and the wrong
  reader), D “It keeps working when you close the tab.” 6. Picked D as the lead of
  the pair D → A → proof, with the reversal named: fewer than three of the first
  five calls describing the ceiling as lost work or re-explained context. Dropped
  C, “One machine, one key, one owner.”, because R2 retires the user’s key. A
  trace script read the 75 quoted sentences from `CLAIMS.md` §1: all candidates
  are approved substrings and the lint is clean; the live proof sentence carries
  the house noun **tenant**, and its second half, “The only SSH key is yours.”, is
  approved but retired by R2 — the page is one product generation behind and the
  deck re-cut is a card after ODL-310.
- **Lesson:** 1) Audit the sentence, not the argument: score it 0–2 on Dunford’s
  five components as they appear in it, and check every noun — a promise that
  names no alternative, or a proof that says `tenant`, is doing no work on the
  page. 2) Date the copy to the product: diff the live page against the
  requirement table and the claims deck’s last-reviewed date before rewriting a
  word. An approved sentence can describe a retired mechanism, and then the copy
  is blocked on a deck re-cut, not on a copywriter.
- **Evidence:** `docs/product/POSITIONING.md`;
  `docs/ops/evidence/2026-11-10-growth/` — `README.md`, `promises.txt`,
  `live-lede.txt`, `claims_trace.py`, `claims-trace.txt`,
  `live-lede-trace.txt`, `claims-lint.txt`; `docs/craft/growth.md` — principle
  12, one checklist item, the quickstart guide added to Sources.
- **Next:** G2 — Paul Graham, *Do Things That Don’t Scale*: write the hand-recruit
  script for the first ten and stage three messages in the growth queue; do not
  send.

## 2026-11-09 · designer · D2 — contrast is per glyph pixel and per viewport

- **Source:** W3C, *WCAG 2.2* (Recommendation, 12 December 2024), 1.4.3
  Contrast (Minimum) — 4.5:1 for normal text, 3:1 for large; 1.4.11 Non-text
  Contrast — 3:1; 2.4.7 Focus Visible; 2.4.13 Focus Appearance (AAA) —
  <https://www.w3.org/TR/WCAG22/>. CSS Pseudo-Elements Level 4 §3.1–3.2 —
  <https://www.w3.org/TR/css-pseudo-4/#selectordef-selection>: `::selection` is
  a highlight pseudo-element, styleable only through colour, background and
  other non-layout properties.
- **Exercise:** audited the live `https://odile.fyi/` in Chrome for Testing 145
  through Playwright at 390×844, 1440×900, 375×667, 390×700, 390×600 and
  320×568. Each text run was shot twice in the same box — text visible, text
  hidden — and only pixels the glyphs cover were measured against the declared
  colour. Footnote: 7.61:1 at 390×844, 7.31:1 at 1440×900, 5.03:1 at 375×667,
  **3.55:1 at 390×600 (7.0 % of glyph pixels under 4.5)** and **2.30:1 at
  320×568 (31.2 % under 4.5)**; the height sweep at 390 px runs 2.47 (560) →
  7.61 (844) with the same token. `::placeholder` `rgb(107,95,80)` passes at
  every size, thinnest 4.73:1 at 320×568. `::selection` changes 89.7 % of the
  lede (paper on ink, 15.56:1) but **0 of 15,224 pixels** on the waitlist button
  and 0 of 15,576 on the `/judge` button, where ink selection paints on an ink
  fill. All tab stops show a ring (2.4.7); ring contrast falls to 2.87–2.95 at
  320×568 and 390×600, below the AAA 2.4.13 3:1 line. A candidate mobile veil
  floor (`.90 84%, .88 100%`) lifts the footnote to 7.06:1 at 320×568 and the
  rings to 3.27–3.32, priced but not applied to `web/`.
- **Lesson:** 1) Contrast is a property of the glyph pixel and the viewport, not
  the token: a fixed, viewport-sized veil moved the same footnote from 7.61:1 to
  2.30:1 by height alone, so audit 320×568, 390×600 and 844×390 with the lowest
  text in view and report the worst pixel and the share below 4.5. 2) Diff
  selection states instead of trusting declared colours: a global ink
  `::selection` is invisible on every ink-filled control (0 pixels changed), and
  inverted controls need their own selection rule.
- **Evidence:** `docs/ops/evidence/2026-11-09-designer/` — `README.md`, six
  `contrast-focus-*.json`, `height-sweep.csv`, `fix-probe.txt`,
  `selection-probe.json`, `foot-320x568-before.png` / `-after-fix.png`,
  `selection-button-390-before.png` / `-selected.png`, scripts `audit.py`,
  `height-sweep.py`, `fix-probe.py`, `shots.py`; `docs/craft/designer.md` — two
  checklist items, one anti-pattern, CSS Pseudo-Elements 4 added to Sources.
- **Next:** D3 — Linear method, the empty state as product: rewrite one empty or
  error state in the product voice; show before/after and the one action it
  offers.

## 2026-11-08 · builder · B1 — an error test is not done at the status line

- **Source:** Simon Stewart, *Test Sizes*, Google Testing Blog (2010-12-13) —
  <https://testing.googleblog.com/2010/12/test-sizes.html>: a small test has no
  network, database, filesystem, thread or sleep (60 s); a medium test may use
  localhost, a database and a filesystem (300 s). RFC 9112, *HTTP/1.1*, §9.3
  Persistence — <https://www.rfc-editor.org/rfc/rfc9112.html>: “A server MUST
  read the entire request message body or close the connection after sending its
  response; otherwise, the remaining data on a persistent connection would be
  misinterpreted as the next request.”
- **Exercise:** `tests/test_flow.py::test_oversized_body_is_refused` passed
  while proving only the first response. Converted it to
  `test_oversized_body_leaves_the_connection_usable`: one 9,000-byte POST, read
  the 413, then `GET /health` on the same `http.client.HTTPConnection`. Before
  the fix the second response was 501, the 9,000 `x` bytes parsed as the request
  method (`docs/ops/evidence/2026-11-08-builder/red-before-fix.txt`). The fix —
  `self.close_connection = True` on the 413 branch and `Connection: close` in
  `_send` — made the second response 200 with body `ok` (`green-after-fix.txt`).
  `tests.test_flow` ran 44 tests OK and `make check` passed with the fix
  (`make-check-with-fix.txt`). Patch saved as `b1-test-and-fix.patch`; `app.py`
  and `tests/test_flow.py` were reverted to HEAD because this session commits
  docs only.
- **Lesson:** 1) An error-path test is not done at the status line: send one
  valid request after the error on the same client. A handler that refuses a
  body must drain it or close the connection, or the unread bytes become the next
  request line. 2) Size the test by resources, not importance: the flow suite
  opens a localhost socket, a temp database and a subprocess, so by Google’s
  table it is a medium test — and that is the size that can see connection reuse.
- **Evidence:** `docs/ops/evidence/2026-11-08-builder/` — `red-before-fix.txt`,
  `green-after-fix.txt`, `make-check-with-fix.txt`, `b1-test-and-fix.patch`;
  `docs/craft/builder.md` — two checklist items, one anti-pattern, the Google
  row sharpened, RFC 9112 added to Sources.
- **Next:** B2 — SQLite, *How SQLite Is Tested*: add one property-based or fuzz
  test to a pure function; report the before/after coverage or mutation score.

## 2026-11-07 · architect · A1 — one page, one rejected option, and the date the door closes

- **Source:** Google, *Design Docs at Google* (Malte Ubl, 2020-07-06) —
  <https://www.industrialempathy.com/posts/design-docs-at-google/>: context and
  scope, goals and non-goals (a non-goal is a could-be goal, not a negation),
  alternatives considered, cross-cutting concerns, the 1–3-page mini doc, and
  when not to write one. SQLite, *ALTER TABLE* —
  <https://www.sqlite.org/lang_altertable.html> — for the migration check.
- **Exercise:** wrote `docs/architecture/DESIGN-claim-ceremony.md` for the
  claim → ceremony control plane, grounded in the live tree. `app.py` schema v2
  is live but has no claim route; its
  `CHECK (claimed_at IS NULL OR ssh_public_key IS NOT NULL)` rejects a keyless
  claim, and the state enum still carries `waiting_for_owner`, two encodings of
  the passphrase ceremony that Savio's 7 November answer (ODL-309: basic auth,
  no user SSH key) retires. Probe in
  `docs/ops/evidence/2026-11-07-architect/schema-probe.txt`: the keyless insert
  fails with `CHECK constraint failed`; on SQLite 3.46.1 the three
  `ALTER TABLE` forms that would drop it all fail with syntax errors. The doc
  recommends option B (zero-touch claim, sealed unlock) gated on ODL-310, and —
  independent of that — schema v3 before ODL-201 creates the first tenant,
  because removing the CHECK is the documented nine-step `writable_schema` edit
  or the twelve-step rebuild, both of which warn about a corrupt file on a
  schema typo.
- **Lesson:** a one-page mini doc is the right size for one interface or schema
  decision, and it earns its keep only if a real alternative is rejected for a
  trade-off — otherwise it is an implementation manual. Date the reversal: name
  the event after which the choice stops being cheap (the first live row), not
  just "one-way" or "two-way".
- **Evidence:** `docs/architecture/DESIGN-claim-ceremony.md`; probe transcript
  above; `docs/craft/architect.md` — checklist item 4 sharpened with the
  event-dated reversal, a mini-doc/implementation-manual item added, SQLite
  ALTER TABLE added to Sources.
- **Next:** A2 — twelve factors on one deploy path: audit `deploy.sh` against
  factors III, IV, VI and IX; find one hidden state, config leak or dev/prod gap.

## 2026-11-06 · designer · D1 — measure the blocks before the action, and the gaps

- **Source:** Adam Wathan & Steve Schoger, *Refactoring UI*, ch. “Avoid ambiguous
  spacing” (https://www.refactoringui.com/); Nielsen Norman Group, “Scrolling and
  Attention” (https://www.nngroup.com/articles/scrolling-and-attention/) — 57 % of
  page-viewing time is spent above the fold; keep the major CTA there.
- **Exercise:** audited the claim screen (`prototype/userflow.html`,
  `data-screen="claim"`) in Chromium 145 through Playwright at 390×844 and
  1440×900. Nine blocks, one action. Sibling gaps at 390:
  `[18, 18, 18, 44, 18, 32, 18, 18]` — warning → acknowledgement 32 px against
  acknowledgement → button 18 px; the button's top is 912.81 px, 68.8 px below the
  fold, in a 1,654 px document. Injected the two-declaration fix
  (`.claim-head .checkline{margin-top:0}` and `.claim-head .row{margin-top:8px}`):
  gaps `[18, 18, 18, 44, 18, 18, 26, 18]`, button top 906.81 px, document
  1,648 px. At 1440 the button sits at 801.61 px and is visible. The landing
  page, same 390×844 viewport, puts its only button at 394.08 px after three
  blocks. The rotation put this seat on 11-06, one day before the track header's
  nominal start.
- **Lesson:** count the blocks before the only action and measure the gaps between
  them; proximity is the grouping signal, so the control that acknowledges a block
  must be nearer that block than the action it gates, and a fourth pre-action block
  at 390 px is a decision, not a detail.
- **Evidence:** `docs/craft/designer.md` — two checklist items added, the
  duplicate no-JS item deleted, NN/g added to Sources; screenshots
  `docs/ops/evidence/2026-11-06-designer/claim-before-390.png` and
  `.../claim-after-390.png`; measurements above reproducible with
  `getBoundingClientRect()` at the two viewports.
- **Next:** D2 — WCAG 2.2, measured: contrast and focus on a real page, including
  `::placeholder`, `::selection` and pseudo-elements.

## 2026-11-06 · exchange — baseline, week of 2026-11-02

- **Shipped and learned:**
  - M-1 was accepted after a cold walkthrough by the architect seat (`1817c51`,
    ODL-096): a stranger could explain who holds the key after one pass, and no
    must-fix remained. The lesson is that the cold-reader test found nothing the
    two review rounds had missed — the lens was already in the loop.
  - ODL-302 moved `BRAND.md` from Savio to the Designer (`95c85ad`). The
    verifier caught that the live Designer `SOUL.md` still sent brand changes to
    Savio after the repo rule changed: authority rules live in the repo and in
    the seat briefs, and `touches` only names the repo.
  - Designer D1 (`266a0d7`) turned the M-1 390 px claim-screen finding into two
    checklist items and a measured fix: the only action sat 69 px below the fold.
  - The Finance manager staff function is live (`docs/ops/finance/`, daily 05:40
    check); D-005 asks Savio for the November envelope.
- **Scars:**

  | Date | Card | Seat | Finding | Root cause | Destination | Owner | Status |
  |---|---|---|---|---|---|---|---|
  | 2026-11-06 | ODL-302 | Designer | The live `SOUL.md` still gave `BRAND.md` to Savio after the repo rule changed | Authority rules live in two places; `touches` covered only the repo | Verifier checklist: grep `~/.hermes/profiles/*/SOUL.md` when a card changes an authority rule | Verifier | open — SOUL fixed, checklist item pending |
  | 2026-11-06 | ODL-302 | CoS | `merge_card.py` exited 3 twice because `main` carried an uncommitted `docs/ops/OPERATING.md` | A shared tree and a worker that had not committed | Ops rule: merge hygiene in `docs/ops/OPERATING.md` | CoS | closed — rule landed |
  | 2026-11-06 | ODL-095/096 | Designer | The claim screen’s only action sat 69 px below the 390 px fold; earlier rounds passed it | Reviews measured overflow and contrast, not distance to the action | Designer checklist: count blocks and measure gaps; D1 (`266a0d7`) | Designer | closed |

- **Borrowed practice:**
  - Product lead → Builder: acceptance criteria map one-to-one to commands or
    artifacts. Next builder card; due by the builder’s session on 2026-11-08.
    Verifier check: every `dod` bullet names a command or an artifact.
  - Builder → Verifier: review lenses follow implementation reality. Next
    verifier card; due by the verifier’s session on 2026-11-13. Verifier check:
    one lens names the failure path and the test shape.
- **Coverage:** designer 1/9 landed (D1), no one overdue; the 9-track cycle
  started 2026-11-06. Next: architect 11-07, builder 11-08, designer D2 11-09,
  growth 11-10, finance 11-11, security 11-12, verifier 11-13, chief-of-staff
  11-14, product-lead 11-15.
- **Baseline:** of the three most recent reviews, one approved on first pass
  (ODL-096); two required changes (ODL-095 took a second round, ODL-302 one).
  The sample is too small to be a trend; the next exchange re-measures.
- **Question:** Verifier — which other authority rule would a repo-only diff
  miss, and what check would catch it before the card is done?

## 2026-11-06 · growth · old craft loop — recruit half outside your network

- **Source:** the growth playbook’s “Do Things That Don’t Scale” unit.
- **Lesson:** recruit half the first ten outside the founder’s network; interview
  the past to find the switching moment.
- **Evidence:** `89a27f6` (the old craft loop’s commit).
- **Next:** G1 — positioning canvas, under the new track.
