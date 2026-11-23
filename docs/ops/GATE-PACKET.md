# Gate packet — Monday 23 November 2026

**Prepared:** 2026-11-23 07:20 UTC.
**Milestone:** **A2 — Automatic deployment** · window 2026-11-22 → 2026-12-06 (`docs/backlog.yaml`). **13 days left.**
**Objective:** A person registers with an email and a password and, with no operator in the loop, gets a working Odile instance — a machine is ordered or recycled, its inference key is issued, and they sign in and use it. Production flow minus the payment; recycling preferred over ordering.
**Demo:** Register a new account on the bench, wait, sign in, ask the agent to write a file.
**Gate question:** Does the first hour actually work in a person's hands — register → ready → sign in → chat → file survives — with no operator and no terminal?

## 1. Exit criteria

A criterion is met only on evidence that reproduces. **3 met / 3 not met** at 07:15Z.

| # | Exit criterion (`docs/backlog.yaml`, A2) | Verdict | Evidence (reproducible) | Card |
|---|---|---|---|---|
| 1 | A registration on the bench produces a tenant with a machine id, an address and its own inference key, with no operator command in the path. | **MET** | `docs/evidence/a2-registration.md` §4: registration 2026-11-22T11:17:47Z → `server_id` 11:21:27Z = **3 min 40 s**; journal `server 9984… at 69.80.225…; key odile-odl-a1758a sk-9b0****` (full values in evidence §4); the order came from `odile-bench-register.timer`, no operator command. | ODL-341 (`t_4d69e43d`, merged `cd6734f`) |
| 2 | The registered password opens the instance; a chat turn and a file write succeed. | **NOT MET** | `curl https://odl-a1758a.test.odile.fyi/` at 07:12Z → `curl: (7) Failed to connect … port 443` (DNS resolves to the bench IPv4; evidence §4); the machine is stock Debian, no Odile install; no ready email is queued. The installed tenant's `/verify` (`odl-340a1e`, release 2026.11.21-2) is proven in `docs/evidence/a2-registration.md` §7, but that is not this registration's machine. | ODL-925 (`t_59b53ca9`, running 8.0 h, review round 2) |
| 3 | A recycled machine carries nothing from the previous tenant: a fresh disk, a fresh boot key and a fresh inference key. | **NOT MET** | The primitive is proved: `docs/product/SPIKE-servury-reinstall.md` §9/§11 — new chip/new key, the seal re-provisioned, a fresh per-machine seed token, "supported, with the caveat that the disk image is shared". No run shows a **recycled machine serving a new tenant with a fresh inference key**, and **no A2 card owns that end-to-end check**. | none — ODL-568 (`t_6d956aee`) owns only the mechanism |
| 4 | A failed provision leaves no billed machine and returns the tenant to a retryable state. | **MET** | `docs/evidence/a2-registration.md` §3: deliberate bad plan id → `provision odl-a1758a: FAILED - create: HTTP 404: Plan not found.; machine None terminated=False`; row `failed` with the provider's sentence, no machine id; retry `POST /account/retry` → 303 and row back to `queued`. | ODL-341 (`t_4d69e43d`) |
| 5 | One tester completes the flow end to end; that tester's trial is 14 days, the account page states its end date, and the tester's lifetime inference is capped at ten dollars. | **NOT MET** | The two policy legs are met: the account page states the trial end (ODL-583, merged `db97100`) and the key carries a $10 `spendLimit` (ODL-242, merged `60e195e`). The tester leg is absent: the only bench registration is `a2-341@…` (a builder's address; evidence §1); ODL-948 is blocked on Savio. | ODL-948 (`t_52e26ceb`, blocked) + ODL-947 (`t_e8466425`, todo) |
| 6 | No raw inference key is stored on HQ; the seed carries it into the machine once, and HQ keeps only the key's label and its first few characters. | **MET** | `docs/evidence/a2-registration.md` §4: `tenant_keys` holds `label`, `key_prefix sk-9b0****` and `key_id`, never the key; the NEAR API readback returns the same prefix. §6: the seed body carries the raw key once and was not attached; `/var/tmp/odile-341/seed-body.json` is gone, and the bench DB contains only the prefix. | ODL-242 (`t_20799488`) + ODL-341 |

**What the table says:** the mechanism is real — a registration becomes a machine in 3m40s, a failure leaves nothing billed, and no raw key rests at HQ. The **product** is not yet real: 443 is refused on the machine that registration created, criterion 3 has no card, and no person has finished the flow.

## 2. Open human cards (assignee `savio`)

| Card | Age | Question | Recommendation |
|---|---:|---|---|
| `t_52e26ceb` — ODL-948 | 0.1 d | Name the first A2 tester and send the invitation, or re-cut criterion 5 to the cold walk? | **A — invite one named person** (one email, one 14-day machine ~$4.28, one $10 key, one 20-minute watched call). If no name exists by 1 December, take **B** before the gate rather than fail on a person nobody asked for. |
| `t_ce72ed5b` — A4 | 0.7 d | Confirm ADR-005's 15-row layer table (7 in place / 5 rebuild-and-restore / 3 not updatable), §4's signed-document model route and §5's self-update trigger? | **A — confirm all three as written.** Nothing in A4 can be built until the table is confirmed; the ADR is still *Proposed* for this reason. |
| `t_a548cb0a` — A3 R2 | 1.0 d | Create the per-tenant R2 buckets and mint two bucket-scoped tokens for each (read-write on the guest, read-only in the recovery file)? | **A — do it now.** `/root/.odile/r2/` does not exist; `odilectl provision` refuses by name. This is the one credential A3's code half (ODL-924) waits on, and A3 opens tomorrow. |
| `t_02b8df9d` — A3 B2 | 5.3 d | Open a Backblaze B2 account in Canada East and mint the keys? | **Close as answered, not a live ask.** Savio answered on 19 Nov: the B2 account was suspended at signup; `docs/architecture/ADR-backup.md` re-cut to Cloudflare R2 as the interim. Do not leave the superseded question in the queue. |
| `t_f835316b` — top-up | 10.1 d (scheduled) | Top up $8.98 on the dev account and $1.20 for tenant #1? | **Close and replace.** Superseded by the 23 Nov finance memo: **$13.60** for the host Servury account before 10 Dec. One current ask, not two stale amounts. |

**One ask has no card yet:** the 23 Nov finance memo's **$13.60 host top-up before 10 Dec** (above $10 → Savio). Raise one human card after the gate; do not let it live only in the memo.

## 3. The three cards that should run next, in order

1. **ODL-929** (`t_3adf2d03`, builder, todo; parents ODL-577 done, ODL-925 running) — re-cut the embedded verify page and installed help to B-once and one recovery code, and re-sign the release. It is the copy cut that ODL-932 and ODL-947 wait on.
2. **ODL-932** (`t_96eff57f`, builder, todo; parent ODL-929) — confirm the demo's customer row is the landed grant row after ODL-929's release cut, and run the live check. It keeps the public demo truthful while testers and judges see it.
3. **ODL-947** (`t_e8466425`, product-lead, todo; parents ODL-925, ODL-929, ODL-932, ODL-946) — the cold end-to-end walk: register, ready, sign in, chat, write a file that survives. It is the only card that proves the demo and produces criterion 2's and 5's evidence.

Order is the dependency chain: 929 → 932 → 947. **ODL-925** (the guest half) runs in parallel and must merge first; today it is in review round 2 at `459daf4`, 8.0 h in.

## 4. Capability

**16 / 54 units landed** across the nine tracks — Product lead 1, Architect 2, Builder 1, Designer 3, Growth 2, Finance manager 2, Security 2, Verifier 2, Chief of staff 1. Applied this week: verifier V2 (*a mutation score is a survivor list*), security S2 (*REPORT_DATA is the only field the caller chooses*), chief-of-staff C1 (*count state changes, not comments*). **Top recurring scar:** branch-green / merge-tree-red after `main` moves — repeated 13 → 19 November (destination ODL-343, done on the board, still recorded *open* in `LEARNING-LOG.md`). Builder B2 is 15 days stale; the only track overdue.

## 5. Finance

**Inference month to date $569.45.** Forecast **$893.75** = inference **$833.35** + fixed **$60.40**; method: trailing seven active days 15–22 Nov ($37.70/day) × 7 remaining days + MTD, fixed at monthly accrual. Envelope: external **$66.85** ($6.45 paid + $60.40 accrued) of $100.00; inference is **87.7% of the revised $950.00 ceiling** (`STATUS.md` prints 67% against the stale $1,250.00 in `BUDGET.yaml`). **No ALERT or BREACH — STATUS `OK`**; four renewal NOTEs, nearest FIN-001 on 26 Nov (FIN-005's is stale — retired 18 Nov). The memo asks **$13.60** before 10 Dec (no human card yet); the $2.15 bench machine is still unledgered (FIN-009).

## 6. What would make this gate fail

`odl-a1758a.test.odile.fyi` still refusing 443 on 6 December, or the cold walk needing a call or a second attempt; criterion 3 with no card and no run; and criterion 5 with ODL-948 unanswered and no tester.

## 7. The decision the Product Lead needs to make this week

**Decide A3's start tomorrow:** run its code-only cards (ODL-924, ODL-933, ODL-934) in parallel with A2's last mile, or hold A3 until ODL-947 passes — and cut the missing criterion-3 recycle card this week either way.

## 8. The date line — ODL-310

**Primitive GO; the stock 2026.11.2 initramfs is NO-GO; a custom measured image is required.** Live branch **A (custom measured UKI) → late February–March 2027**; the only slack on record — **~1 week against 18 January** — belongs to the Servury-mode branch that round 3 ruled out, and **no slack is recorded for the custom branch**. **B (signed app)** only if round 2 overturns the primitive. Evidence: `docs/product/SPIKE-sealed-unlock.md` (ODL-310) and `docs/product/DECISION-strict-no-cli.md` (`8a3617a`). The spike file's own §0 still carries the 10 Nov "~2 weeks, 18 Jan alive" re-estimate; the ruling record supersedes it.

## 9. Pipeline — 129 h since the last gate (17 Nov 22:10Z)

**Spawns 7.7 per shipped card** (1,548 / 200; target ≤ 1.5) · **wait : run 5.7 h vs 4.6 h = 1.24** (target ≤ 1.0) · **review rounds 1.9 per card** (375 / 200; target ≤ 1.2) · **5 parks** (2.5 per 100 cards; target < 10). Last 24 h: 4.6 spawns/ship, wait 0.6 h vs 4.2 h = 0.14, **review 2.3 rounds/ship**, 0 parks.

**The one that moved: review rounds — worse.** 1.9 → 2.3 per shipped card in the last day while queueing improved (1.24 → 0.14). The 3+ round bucket doubled week over week — **42 cards at $2.89 against $0.48 for a clean card (6.0×)** — from handoffs coming back for context reloads (BRAND copy cells, ADR line pointers, merge-tree/base-moved). Spawns fell from 7.7 to 4.6 but remain three times the target; parks are healthy.

---

_Prepared by the Chief of Staff seat for the weekly gate review. The Product Lead owns the product call; the gate review and Savio mark the milestone. Every probe above is from the timestamp shown; no criterion is met on prose._
