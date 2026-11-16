# ADR-004 — A destroyed tenant comes back on its own slug: `destroyed -> claimed`

**Status:** Accepted · 15 November 2026 · Architect · card `t_ce52bb8d` (ODL-218
follow-up). Implemented on this branch: `odilectl/state.py`,
`odilectl/provision.py`, `tests/test_state.py`, `tests/test_odilectl.py`,
`docs/runbooks/backup.md` §9, `docs/runbooks/offboard.md` §5 and
`docs/runbooks/provision.md` §9.
**Answers:** ODL-218 follow-up — "which way does a tenant come back after a
destroy, and what happens to its URL?"
**Short answer:** an edge back. `destroyed -> claimed` is in `TRANSITIONS`, and
the named move `StateStore.reopen(reason)` takes it: it keeps every earlier
event, appends one `claimed` event whose detail carries the reason, and clears
the dead machine's columns. The slug survives, and with it
`https://<slug>.odile.fyi`, the recovery file's name and the backup repository
path. A new slug per machine is rejected: it would change all three on every
rebuild and make an approved public sentence false. The way back is only real
when the order is recorded in **both** schemas — the state row to `first_boot`
and the web row pointed at the new machine — because the first boot's measured
line is written against the web row.
**Related:** `docs/CLAIMS.md` §1 ("Your slug is yours for the life of the
tenant, so the URL and the recovery file's name stay the same."; the
machine-record and append-only-log rows), §4.9, §5; `docs/MVP-V0.1.md` §6.2
(provisioning), §6.7 (backups), §12 D11 (random subdomains), D14 (the access
model, untouched); `docs/runbooks/backup.md` §5.2 and §9;
`docs/runbooks/offboard.md` §5 ("The mechanics — the same slug comes back
(ADR-004)", "The signed image is the panel's, not the API's", "The live
tenant"); `docs/runbooks/provision.md` §9 (rollback).
**Decision owner:** Architect. This ADR adds no provider, network path,
standing credential or trust boundary, and it strengthens no claim: it makes
the code match a sentence `docs/CLAIMS.md` already publishes. Savio is not
escalated.

---

## 1. The invariant at risk

**The published sentence.** `docs/CLAIMS.md` §1: *"Your slug is yours for the
life of the tenant, so the URL and the recovery file's name stay the same."*
The slug is a random, semi-private public identifier (D11); it is the URL, the
recovery file's name (`odile-recovery-<slug>.txt`), the DNS record, the ready
email and the backup repository path (`tenants/<slug>`). A rebuild that changes
it changes every one of those.

**The state machine's promise.** `StateStore.transition()` writes the row and
one append-only event in the same transaction, "so a state that changed without
a record is not representable". `destroyed` was a sink: the machine was gone,
and the row could never move again. That is true of the **machine**. It is not
true of the **tenant**, who may come back after a provider wipe, a failed
upgrade or a cancellation.

**The order/record promise.** A `provision` run must not place a paid order
that the row cannot record. Before this ADR it did: `provider.create` was
called first, the seed token was bound and the machine was polled to active,
and only then did `_record` raise. That is the failure the builder measured and
the reason this card exists.

**The row that names the machine.** `bootrecord.record_boot` writes a first
boot against the *web* row: it refuses a measured line whose `bb.iid` differs
from `tenants.server_id`, and refuses a spine at `claimed`/`provisioning`. An
order is therefore not recorded until both rows name the machine and the spine
has reached `first_boot`. Before the review's probe, `odilectl provision`
moved only `tenant_state`: after a rebuild the web row still named the dead
machine (`is ordered for instance 'srv-old', the measured line boots
'srv-new'`), and even with the web row fixed by hand the spine sat at
`provisioning` (`a boot record needs the order to have reached first_boot`).

## 2. What broke, measured

On 2026-11-13, against the ODL-218 drill slug:

```
StateError: odl-218a1b is destroyed; its release cannot be recorded
```

The path is not only offboard:

- `odilectl reconcile --apply` (ODL-212) marks a *missing* machine
  `destroyed`; the crash victim's rebuild then hits the same wall.
- `provision --apply` on a destroyed slug orders and bills the machine before
  it discovers the row cannot record it — a machine no row names.
- The offboard drill rebuilt under a **new slug**, so the URL changed, the
  recovery file's name changed and `backup-repo.txt` named a new repository.
  The drill's own runbook records that compromise.

The state machine had one answer, and it was the expensive one.

## 3. Options

| | A — an edge back (chosen) | B — a new slug per machine (rejected) |
|---|---|---|
| `destroyed` | Not terminal; one outgoing edge to `claimed` | Stays terminal |
| Slug / URL | Survive | Change on every rebuild |
| Recovery file name | Survives | Changes |
| Backup repository | `tenants/<same-slug>` survives; the old password is sealed under the new token | New repository; old data must be restored by hand or the old repo URL carried as a special case |
| DNS | Same name; the A record is deleted at destroy and re-created for the same slug | New record, new name |
| Public sentence | True | False unless CLAIMS §1 is weakened |
| Cost | One edge (18 → 19), one named move, one `claimed`-clears-machine rule, the both-schema order write, tests, three runbooks | A precheck, a slug-allocation rule for rebuilds, new DNS/recovery/repo plumbing, and a claim cut |
| Failure mode | A rebuild is a new machine under an old name — the old machine must really be gone | Every rebuild is a new identity; the person's bookmark, certificate fingerprint and repository move |

A third shape — a new state or a `reprovision` event kind — was rejected as
schema cost for a word: the event-kind `CHECK` would need a v4 table rebuild,
while `claimed` with `detail.reprovision` and `detail.reason` already carries
the fact.

## 4. Decision

1. **`TRANSITIONS["destroyed"] = ("claimed",)`.** The flow is now 19 edges.
   `destroyed` is the end of a *machine*, not of the tenant; its only outgoing
   edge is the reopen.
2. **`StateStore.reopen(slug, *, reason)`** is the named move. It:
   - refuses any state except `destroyed` (and is a no-op on an already
     `claimed` row, so a retried rebuild writes one event);
   - appends one `claimed` event with `from_state='destroyed'` and
     `detail={"reason": …, "reprovision": true}`;
   - keeps every earlier event — the log still proves the machine died.
3. **Entering `claimed` clears the machine columns** (`server_id`, `hostname`,
   `ipv4`, `plan_id`, `release`, `expires_at`, `initramfs_host_key`,
   `last_heartbeat`). A claim is *before* the order; a row at `claimed` names
   no machine. This is what makes the edge a rebuild rather than a relabel, and
   it keeps a dead `server_id` out of the unique column and out of reconcile's
   matches.
4. **`provision` reopens before the first provider call.** `_reopen_destroyed`
   moves the row to `claimed` before `provider.list`/`provider.create`, so the
   order lands on a row that can record it; a failed create leaves a retryable
   claim, not a paid orphan. The dry run lists `state.reopen` as its first
   action (read through SQLite's read-only URI mode) and says the URL and the
   recovery file keep their names. The report carries `reopened: true`. The
   `--backup-password-file` flag and the `tenants/<slug>` repository are
   unchanged.
5. **The order is recorded in both schemas.** After an order, `provision`
   writes the machine into the state row (`provisioning`) *and* points the web
   `tenants` row at it: `server_id`, `ipv4`, `expires_at`, the release it is
   ordered under, the key it is ordered with, `state='provisioned'` and
   `installed_at=NULL` (the new machine has not installed). `setup_token_hash`
   is not touched — `seed.bind` wrote the new token's hash before this write. A
   row that already names the machine is not rewritten, so a second run cannot
   move an `active` row backwards; and the write happens only once the state
   row names the machine, so the web row never runs ahead of the spine (a live
   `ready`/`suspended` row with no `provisioning` edge keeps the web row it
   had). The spine then takes `provisioning ->
   first_boot` (`await_boot`, the edge the claim path already takes after an
   order), which is the one state `record_boot` accepts a first boot from.
6. **`reconcile --apply`** still marks a missing machine `destroyed`; its
   finding now names the same-slug rebuild (`provision <slug>: same slug,
   destroyed -> claimed (reopen)`), so the operator sees the way back in the
   same output that applied the destroy.
7. **The URL consequence.** The name does not change. The Cloudflare A record
   *is* deleted when the machine is destroyed (offboard §3), so a rebuild
   re-creates the record for the same slug — the name and the recovery file are
   stable, the address is not. `docs/runbooks/offboard.md` §5 carries the step.
8. **The runbooks agree.** `docs/runbooks/backup.md` §9 no longer says the
   live rebuild takes a new slug; `docs/runbooks/offboard.md` §5 no longer
   documents the "new slug until the decision" compromise; `docs/runbooks/
   provision.md` §9 no longer says `destroy` does not move the row or that a
   re-provision is a new slug.

## 5. Failure modes and the order of operations

- **Order placed, record failed.** Fixed by reopening *before* the order. If
  `provider.create` fails, the row is `claimed` and the next run retries; if
  `_record` fails after an order, the existing `provider.list` reuse path finds
  the machine by hostname and records it rather than ordering a second one.
- **Two machines, one name.** `provision` refuses two provider rows with the
  same hostname; a reused active/pending row is recorded, not re-ordered.
- **Double charge on a retry.** `provider.list` first; `create` only when the
  hostname is absent; a queued preorder is reused.
- **Old seed token / old rebuild password.** `seed.bind` ends the previous
  token for the slug; `_write_rebuild_entry` overwrites or clears the slug's
  entry. Only ciphertext is stored.
- **Late boot record from the dead machine.** After the reopen the web row
  still names the last machine until the new order replaces it, so the window
  is covered on both sides: a record while the row is `destroyed` is refused
  ("a boot record cannot revive it"), one while the reopened row is `claimed`
  is refused ("needs the order to have reached first_boot"), and once
  `provision` has recorded the order the dead machine's `bb.iid` no longer
  matches `tenants.server_id`. The new machine's record is the one the
  instance check accepts. The probe that found the gap ("is ordered for
  instance 'srv-old', the measured line boots 'srv-new'") is the regression
  test `test_a_rebuilt_tenant_records_its_first_boot_on_the_new_machine`.
- **The web row and the state row disagree.** The order writes the spine
  (`claimed -> provisioning`), then the web row, then the spine's `first_boot`.
  A crash after the paid `create` at any of those points leaves the spine at
  least `claimed` and the machine named for the slug, so the next run takes the
  hostname-reuse path (`provider.list` finds it, no second order), records the
  order, finds the web row already naming the machine ("unchanged") or points
  it, and takes `await_boot` again. No state depends on the two rows being
  written in one transaction.
- **Old machine still running.** `reconcile` reports it while the row is
  `destroyed` (`zombie_vm`); after the reopen the old `server_id` is cleared,
  so it is reported as an unknown machine for a human to terminate. The
  provision path reuses it if it is still named for the slug.
- **DNS.** The record is deleted at destroy and re-created for the same slug
  after the new address exists (offboard §5 step 2).
- **Heartbeats.** A beat from the old machine can still refresh the slug's
  `last_heartbeat` while the row is `claimed`; it cannot move the state. The
  next order overwrites the machine facts. This is pre-existing behaviour and
  not changed here.

## 6. Reversibility

**Two-way in code.** Removing the tuple and the named move restores the old
terminality in one commit; the data added by a reopen is an ordinary
`destroyed -> claimed` event pair that the existing schema already accepts (no
migration, `STATE_SCHEMA_VERSION` stays 3).

**One-way in product identity.** "The slug is yours for the life of the
tenant" is already published in `docs/CLAIMS.md` §1. This ADR makes the code
honour it; choosing the other way would have required weakening that sentence.
The point after which the decision stops being cheap is the **first live
same-slug rebuild** (`t_8d50106b`'s destroy-and-rebuild of `odl-7971a8`):
after that, the tenant's history contains the edge and a second rebuild needs
it back.

## 7. Evidence

Hermetic, no order and no money (branch head of this card):

```
/usr/bin/python3 -m unittest tests.test_state tests.test_bootrecord tests.test_offboard
  Ran 101 tests … OK
/usr/bin/python3 -m unittest tests.test_odilectl
  Ran 92 tests … OK
/usr/bin/python3 -m unittest tests.test_odilectl.FixtureFlowTest
  Ran 14 tests … OK
/usr/bin/python3 -m unittest tests.test_claim
  Ran 39 tests … OK
/usr/bin/python3 -m unittest tests.test_seed
  Ran 45 tests … OK
make check
  Ran 1645 tests … OK (skipped=7); self-test: PASS; claims lint: OK;
  brand lint: OK; make check: OK (EXIT=0)
```

**The way back, end-to-end.** `test_a_rebuilt_tenant_records_its_first_boot_on_the_new_machine`
builds a both-schema database (`app.migrate` + `state.SCHEMA`) whose destroyed
tenant's web row still names `srv-old-0001`, runs `odilectl provision <slug>
--apply` against the recorded provider, asserts the web row now names the new
machine (`server_id`, address, expiry, ordered release, `state='provisioned'`,
`installed_at` NULL, and the token hash `seed.bind` wrote left intact) and the
spine is `first_boot`, then writes the new machine's measured line through
`record_boot`: `first_boot -> first_visit -> ready`, web row `active`. A record
for `srv-old-0001` is still refused by the instance check. Neutralising the
web-row write reproduces the old code exactly: the order succeeds, the row
still names `srv-old-0001`, and `record_boot` refuses (`is ordered for instance
'srv-old-0001', the measured line boots 'abc12345'`).

The CLI test `test_provision_on_a_destroyed_slug_reopens_it_under_the_same_slug`
asserts the same slug, the new `server_id`, and the event sequence `destroyed →
claimed → provisioning → first_boot`; the dry-run test asserts that
`state.reopen` is listed first and that the row is still `destroyed`
afterwards. The 18 → 19 edge count, the reopen's field clearing, its
idempotency, its refusal of a live tenant and the reconcile finding are in
`ReprovisionTest`.

**Mutation.** With `destroyed` changed back to `()` (and the edge count left at
19), `tests.test_state` fails
`test_every_state_is_reachable_from_invited_and_destroyed_reopens` and the edge
loop's count, the four `ReprovisionTest` cases error, and the two CLI tests
(the reopen and the both-schema rebuild) fail on exit 4 — 4 failures, 4 errors.
The new edge is not decorative: remove it and both schemas' tests go red.
