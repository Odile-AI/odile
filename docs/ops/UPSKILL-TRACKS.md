# Upskilling tracks — one six-unit track per team member

**Status:** cycle 1 · starts 2026-11-06
**Owner:** each track owner; the Chief of Staff keeps the calendar.
**How to use:** the daily session takes the first row in its track’s table whose
`Landed` cell is `—`. Fill it as
`YYYY-MM-DD · <artifact or commit> · <one-line lesson>`. Do not reorder or skip
units; a unit that proves wrong is replaced at the track review, not quietly
dropped.

## Unit grammar

Every seat uses the same six-unit shape, with profession-specific content:

1. **Foundation** — the canonical text or standard, applied to a live artifact.
2. **Primary source** — official docs, a spec, source code or an audit; produce a
   test or a check.
3. **Teardown** — how a top project did one thing; copy the mechanism, not the
   aesthetic.
4. **Failure study** — a postmortem or audit; extract the missing check.
5. **Application** — apply the accumulated lessons to a current card; measure it.
6. **Teach-back** — teach the consuming seat; update a handoff or checklist.

A unit is done when the `Landed` cell has a date and an artifact, and the
playbook has either a lesson or a written no-change verdict. Unit 6 proposes the
first unit of the next cycle.

---

## Product lead

**Profession:** product management for a hand-sold, privacy-first alpha.
**Failure mode:** building the wrong thing well.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| P1 | Foundation — appetite and the betting table | Shape Up, “Appetite” and “The Betting Table” — https://basecamp.com/shapeup | For every current-milestone card, write its appetite and the one thing cut if it overruns; name one card to split or cut. | 2026-11-15 · docs/product/APPETITE-M1.md · A size band with no cut line is an estimate: 47 of 53 M1 cards were done before its window opened, and nothing could be cancelled when MH overran. |
| P2 | Primary — the PR/FAQ before the spec | Working Backwards PR/FAQ template — https://www.workingbackwards.com/resources/working-backwards-pr-faq | Write a one-page PR/FAQ for the next milestone; list the three FAQ answers the current spec cannot give. | — |
| P3 | Teardown — how Linear makes a release legible | Linear changelog — https://linear.app/changelog | Pick three entries; name the mechanism (customer sentence, before/after, one proof, no feature list). Rewrite one Odile release note in that shape. | — |
| P4 | Failure study — the four big risks | Cagan, “The Four Big Risks” — https://www.svpg.com/four-big-risks/ | Score the current milestone on value, usability, feasibility and viability; name the weakest and the test that would falsify it. | — |
| P5 | Application — opportunity solution tree | Torres, “Opportunity Solution Trees” — https://www.producttalk.org/2021/08/opportunity-solution-trees/ | Build a one-page tree for the first ten tenants: one opportunity, three solutions, one experiment, one kill criterion. | — |
| P6 | Teach-back — a user moment becomes acceptance criteria | Shape Up + Working Backwards | Teach Builder to turn one user moment into an acceptance criterion a command can check; add the example to the card template. | — |

---

## Architect

**Profession:** architecture for a small, trust-sensitive system.
**Failure mode:** a decision that is expensive to reverse, made invisibly.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| A1 | Foundation — the design doc | Google, “Design Docs at Google” — https://www.industrialempathy.com/posts/design-docs-at-google/ | Write a one-page design doc for the claim → ceremony control plane: problem, goals/non-goals, two options, recommendation, cost of being wrong. | 2026-11-07 · docs/architecture/DESIGN-claim-ceremony.md · A one-page mini doc earns its keep only with a rejected option; date the reversal at the first live row, not the merge. |
| A2 | Primary — twelve factors on one deploy path | The Twelve-Factor App — https://12factor.net/ | Audit one deploy path against factors III, IV, VI and IX; find one hidden state, config leak or dev/prod gap. | 2026-11-16 · docs/architecture/AUDIT-deploy-path.md · A `mode=ro` SQLite handle still writes `-shm`/`-wal`, so a read-only sandbox fails only when no other process holds the DB — twelve failed timer runs left a stale page that looked healthy. |
| A3 | Teardown — Stripe’s versioning promise | Stripe API versioning — https://docs.stripe.com/api/versioning | Propose a five-line versioning and compatibility rule for the control-plane interface; name one thing that would break it. | — |
| A4 | Failure study — GitLab’s database deletion | GitLab postmortem — https://about.gitlab.com/blog/postmortem-of-database-outage-of-january-31/ | Extract the missing backup and restore check; map it to Odile’s restore drill in M2. | — |
| A5 | Application — failure-mode matrix for the ceremony | Google SRE Book — https://sre.google/sre-book/table-of-contents/ | For verify, encrypt, unlock and install: partial failure, retry, idempotency, rollback and signal. Name the missing signal. | — |
| A6 | Teach-back — trust-boundary drift in an ADR | Nygard, ADR format — https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions | Teach Security to read an ADR for trust-boundary drift; add one question to the security checklist. | — |

---

## Builder

**Profession:** implementation for a system whose evidence must reproduce.
**Failure mode:** it works here, on this machine, this time.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| B1 | Foundation — TDD and test sizes | Beck, *TDD by Example*; Google Testing Blog, “Test Sizes” — https://testing.googleblog.com/2010/12/test-sizes.html | Convert one weak test to a behaviour test; show it fails before the fix and passes after. | 2026-11-08 · docs/ops/evidence/2026-11-08-builder/b1-test-and-fix.patch · An error test is not done at the status line: the next request on the same connection is the assertion. |
| B2 | Primary — what an enormous test surface looks like | SQLite, “How SQLite Is Tested” — https://www.sqlite.org/testing.html | Add one property-based or fuzz test to a pure function; report the before/after coverage or mutation score. | — |
| B3 | Teardown — Linux patch discipline | Linux, “Submitting Patches” — https://docs.kernel.org/process/submitting-patches.html | Split one oversized diff into two or three logical commits; measure the review diff size before and after. | — |
| B4 | Failure study — the dependency that vanished | npm, “kik, left-pad, and npm” — https://blog.npmjs.org/post/141577284765/kik-left-pad-and-npm | List Odile’s runtime dependencies and the one whose removal would break a fresh install; add a check or a note. | — |
| B5 | Application — evidence from a fresh clone | Builder playbook checklist | On the current card, run the evidence block from a fresh clone in a temp directory; time it; fix the one step a stranger cannot reproduce. | — |
| B6 | Teach-back — the change-detector test | Google Testing Blog + mutation testing | Teach Verifier to spot a test that passes on the broken version; add one item to the verifier checklist. | — |

---

## Designer

**Profession:** interface design for a trust product at 390 px.
**Failure mode:** a screen a stranger cannot use without being told.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| D1 | Foundation — hierarchy, spacing, one action | Wathan & Schoger, *Refactoring UI* — https://www.refactoringui.com/ | Audit one screen for hierarchy, spacing scale and one primary action; make the smallest fix and record before/after. | 2026-11-06 · docs/craft/designer.md · Measure the blocks before the action and the gaps between them; the acknowledgment belongs to the warning, the action to the first viewport. |
| D2 | Primary — WCAG 2.2, measured | W3C, WCAG 2.2 — https://www.w3.org/TR/WCAG22/ | Run a contrast and focus audit on a real page, including `::placeholder`, `::selection` and pseudo-elements; record computed values. | 2026-11-09 · docs/ops/evidence/2026-11-09-designer/ · Contrast is per glyph pixel and per viewport: 7.61:1 at 390×844, 2.30:1 at 320×568; sweep the height and diff selection pixels. |
| D3 | Teardown — the empty state as product | Linear method — https://linear.app/method | Rewrite one empty or error state in the product voice; show before/after and the one action it offers. | 2026-11-18 · docs/ops/evidence/2026-11-18-designer/BEFORE-AFTER.md · A status code is a screen: the shipped 404 was 10 bytes of text/plain with no link; the rewrite carries the brand, one sentence and one action. |
| D4 | Failure study — the million-page accessibility baseline | WebAIM Million — https://webaim.org/projects/million/ | Take the top three failure classes; check Odile’s public page against each; fix one. | — |
| D5 | Application — every state at both widths | Designer playbook checklist | Audit the prototype at 390 px and 1440 px with long content, keyboard and reduced motion; fix one state. | — |
| D6 | Teach-back — acceptance criteria for a trust moment | Odile trust moments, `docs/MVP-V0.1.md` §3–§6 | Teach Product Lead to write one acceptance criterion for “who holds the key” that a stranger can check; add it to the design checklist. | — |

---

## Growth

**Profession:** positioning and early demand for a ten-person alpha.
**Failure mode:** being unheard, or measuring motion instead of activation.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| G1 | Foundation — the positioning canvas | Dunford, *Obviously Awesome* — https://www.aprildunford.com/obviously-awesome | Write the canvas for Odile: market, alternative, differentiator, proof. Write three candidate one-sentence promises; score each against the canvas and `docs/CLAIMS.md`; pick one and name the evidence that would change it. | 2026-11-10 · docs/product/POSITIONING.md · Score the sentence on the five components, then date it to the product; an approved sentence can describe a retired mechanism. |
| G2 | Primary — do things that don’t scale | Paul Graham, “Do Things That Don’t Scale” — https://paulgraham.com/ds.html | Write the hand-recruit script for the first ten and stage three messages in the growth queue; do not send. | 2026-11-19 · docs/product/RECRUIT-FIRST-TEN.md + docs/product/GROWTH-QUEUE.md · A recruit is an install: the call ends with the first file, and the day 1/3/7 questions are the retention signal. |
| G3 | Teardown — Superhuman’s concierge onboarding | First Round, Superhuman onboarding — https://review.firstround.com/superhuman-onboarding-playbook/ | Map the concierge moment to Odile’s first-run ceremony; name one step to hand-hold. | — |
| G4 | Failure study — the Qwikster mispositioning | Wired, “Qwikster Deleted From the Queue” — https://www.wired.com/2011/10/qwikster-deletes-netflix/ | Name the positioning error (two products, one price, one promise); find the same risk in Odile’s alpha. | — |
| G5 | Application — one experiment with a kill criterion | Growth playbook + activation definition | Define one experiment: the behaviour it changes, the metric, the result that kills it, and the seven-day read. | — |
| G6 | Teach-back — claims discipline shapes copy | `docs/CLAIMS.md` + Dunford | Teach Designer how a claim becomes a visual proof; add one growth check to the design review. | — |

---

## Finance manager (staff function)

**Profession:** finance for a pre-revenue, high-margin infrastructure product.
**Failure mode:** a number without a source, or a spend approved to unblock a
card. This is the ninth track; the profile is a staff function under the Chief of
Staff, not a ninth authority.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| F1 | Foundation — unit economics at ten tenants | Stripe, *SaaS metrics* — https://stripe.com/resources/more/essential-saas-metrics; `docs/MVP-V0.1.md` §8 | Build a one-page per-tenant model at $49: machine, inference allowance, payment fees, support. Use actual variable cost from `BUDGET.yaml` and `LEDGER.csv`; name the largest unknown. | 2026-11-11 · docs/ops/evidence/2026-11-11-finance/ · Support is COGS — 20 min/user/week breaks even at $10.62/h and consumes the whole $15.34 margin. |
| F2 | Primary — the ledger and the cap | `docs/ops/finance/POLICY.md` + `tools/spend.py` | Reconcile one week of `SPEND.csv` and `LEDGER.csv`; find one unpriced or unreconciled row; name the owner and the evidence that would close it. | 2026-11-20 · docs/ops/evidence/2026-11-20-finance/ · A closed day restates when a session resumes; the balance delta caught the $9.03 charge no row held. |
| F3 | Teardown — how a top SaaS reports unit economics | Bessemer, *Cloud 100 Benchmarks* — https://www.bvp.com/atlas/the-cloud-100-benchmarks-report | Copy one mechanism (cohort gross margin or net revenue retention); reject one metric that does not apply pre-revenue. One page, with the reason. | — |
| F4 | Failure study — MoviePass at $9.95 | Forbes, “CEO Mitch Lowe Pulls Back the Curtain on MoviePass” — https://www.forbes.com/sites/robcain/2017/09/18/ceo-mitch-lowe-pulls-back-the-curtain-on-moviepass-and-explains-its-economics | Name the unit-economics error and the one number that would have stopped it; find the same risk in Odile’s $49 plan. | — |
| F5 | Application — the monthly close | `docs/ops/finance/POLICY.md` + `BUDGET.yaml` | Run a mock close on the current month: every variance over 10% or $20 with a cause, and next month’s envelope with a measured basis. A proposal is not an approval. | — |
| F6 | Teach-back — unit economics constrains the page | The F1 model + `docs/CLAIMS.md` | Teach Designer how the $49 price and the gross-margin floor constrain one page promise; add the line to the design checklist. | — |

---

## Security

**Profession:** the access claim and the evidence behind it.
**Failure mode:** a claim stronger than the proof.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| S1 | Foundation — ASVS, mapped | OWASP ASVS — https://owasp.org/www-project-application-security-verification-standard/ | Map the current surface to ASVS L2; find one gap with a reproduction command. | 2026-11-12 · docs/security/ASVS-L2-MAP.md · A 405 branch that answers before reading the body poisons Caddy's pooled upstream connection; the next visitor got 501 `Unsupported method ('abcGET')`. |
| S2 | Primary — the attestation report itself | AMD SEV-SNP — https://www.amd.com/en/developer/sev.html | Read the report fields and policy bits; write a plain-language test for one field and run it. | 2026-11-21 · docs/ops/evidence/2026-11-21-security/report-data-test.txt · REPORT_DATA is the only requester-chosen field: the served report carries HQ's five-minute label, so it proves a boot, not a session. |
| S3 | Teardown — what build provenance looks like | SLSA — https://slsa.dev/ | Compare Odile’s release provenance to SLSA Build L2; name the missing artifact or signature step. | — |
| S4 | Failure study — SEVered | “SEVered: Subverting AMD’s Virtual Machine Encryption” — https://arxiv.org/abs/1805.09604 | Extract the verification step Odile lacks; check that the threat model names the host. | — |
| S5 | Application — the handoff audit | Security playbook checklist | Re-run the handoff audit on the current test tenant, or on the M1 runbook if no tenant exists; write one residual risk with an owner and a date. | — |
| S6 | Teach-back — threat-model a new provider | ASVS + the architecture checklist | Teach Architect how to threat-model a new provider; add one question to the architecture checklist. | — |

---

## Verifier

**Profession:** independent review of evidence.
**Failure mode:** rubber-stamping a confident handoff.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| V1 | Foundation — what a review is for | Google, *Code Review* guide — https://google.github.io/eng-practices/review/ | Calibrate one review: label every comment must/nit/question; measure the ratio; rewrite one vague finding. | 2026-11-13 · docs/ops/evidence/2026-11-13-verifier/CALIBRATION-ODL-502.md · A nit is a deferred must: name the sentence it replaces and where it goes — this commit, a named card, or drop. |
| V2 | Primary — does the suite notice a defect? | mutation testing with mutmut — https://github.com/boxed/mutmut | Run mutation testing on one module; report the score and the one test to add. | — |
| V3 | Teardown — labels that make severity explicit | Conventional Comments — https://conventionalcomments.org/ | Rewrite one review thread with labels; compare rework rounds before and after. | — |
| V4 | Failure study — Therac-25 | Leveson & Turner, Therac-25 — https://escholarship.org/content/qt5dr206s3/qt5dr206s3.pdf | Identify the review lens that would have caught the race; add it to the rotation. | — |
| V5 | Application — a second lens on an old approval | Verifier playbook checklist | Re-review a previously approved card with the contract lens; find one defect or confirm it with evidence. | — |
| V6 | Teach-back — evidence a stranger can rerun | Odile handoff format, `docs/AGENT-ORG.md` §4 | Teach Builder how to write evidence a stranger can rerun; add one line to the handoff template. | — |

---

## Chief of staff

**Profession:** operating cadence and decision flow for a small agent company.
**Failure mode:** becoming a forwarding layer.
**Cycle 1:** 2026-11-06 → complete when all six Landed.

| # | Unit | Primary source | Exercise on Odile | Landed |
|---|---|---|---|---|
| C1 | Foundation — managerial leverage | Grove, *High Output Management*, ch. 1–3 | List every intervention this week, its output, and the ones that changed nothing; remove one recurring activity from `OPERATING.md`. | — |
| C2 | Primary — the gate as a decision table | Shape Up, “The Betting Table” — https://basecamp.com/shapeup | Redesign the gate packet as a decision table: criterion, met/not, evidence, owner. Remove one status line. | — |
| C3 | Teardown — one channel, one decision log | FEMA, Incident Command System — https://www.fema.gov/emergency-managers/nims/components | Apply “one channel, one decision log” to the decision queue; cut one duplicate channel. | — |
| C4 | Failure study — the question nobody asked | Feynman, Appendix F — https://www.nasa.gov/history/rogersrep/v2appf.htm | Extract the management question that was never asked (“what is the evidence against launch?”); add it to the gate packet. | — |
| C5 | Application — the decision-queue audit | Chief of Staff playbook checklist | Every row has two options, a recommendation, an owner and an age; close or escalate stale rows; measure decision age. | — |
| C6 | Teach-back — one blocked launch decision, one row | `docs/AGENT-ORG.md` §3 + the growth queue | Teach Growth how one blocked launch decision becomes one row with options and an owner, not a new card; add the line to the growth launch checklist. | — |

---

## Track completion

When all six `Landed` cells in a seat’s table are filled:

1. The seat’s next session reads the scar log and the next milestone’s failure
   modes, and writes a three-line proposal for cycle 2 in the seat’s section:
   the new first unit, the source, and the card it will change.
2. The weekly exchange checks that every unit changed something; a unit that
   changed nothing is deleted or replaced before cycle 2 starts.
3. The CoS records `cycle 1 complete — <date>` in the seat’s section and in the
   gate line.
