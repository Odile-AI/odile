# DESIGN — the claim → ceremony control plane

**Status:** one-page draft · 7 November 2026 · Architect · for Product lead,
Builder, Security, Verifier
**Related:** `docs/MVP-V0.1.md` §6.2–§6.5; `docs/product/DECISION-agent-access.md`
(amended 2026-11-07); ODL-201/202/203/231/310

## 1. Context and scope

Ten hand-sold tenants. `odile.fyi` creates, monitors and stops machines, holds no
credential that opens one (Invariant 2), and is never in the prompt path
(Invariant 4). Schema v2 is live in `app.py`; there is no claim route yet.

Two facts from the tree shape this design. A claimed tenant must carry an SSH
public key — `CHECK (claimed_at IS NULL OR ssh_public_key IS NOT NULL)` rejects a
keyless claim (`docs/ops/evidence/2026-11-07-architect/schema-probe.txt`). And
the state enum still contains `waiting_for_owner`, which exists only because the
disk asks for a passphrase at boot.

Savio's 7 November answer (ODL-309) makes agent access the V0.1 bar — invite →
workspace, no user SSH key — gated by Hermes Webapp basic auth, with sealed
auto-unlock proven or killed by ODL-310. The claim path must stop assuming a key
before the boot mechanism is settled.

## 2. Goals and non-goals

**Goals.** One invite becomes one reachable workspace, with no credential held at
HQ that opens it. One state machine and one append-only log, so the funnel is a
count over rows. The claim contract survives a change in how the disk unlocks. A
resend or a double POST lands on the same row, not a second tenant.

**Non-goals** — each could reasonably be a goal and is refused in writing: an HQ
account or password (escrow, kills Invariant 2); a highly available control plane
(one box, one SQLite file, one process); self-service payment (manual until M4);
a provider abstraction (Servury only — a second provider is a new trust boundary
and goes to Savio); being in the data path.

## 3. The design

State is what the control plane can prove; cause is what it was told.

```
invited ──claim redeemed──▶ claimed ──server_id──▶ provisioned ──seed/heartbeat──▶ installed ──heartbeat <7 d──▶ active
   └──token expires──▶ invited (claim_expired)                                         └──no heartbeat──▶ churned
```

`claimed` means the token is redeemed and the URL reserved; no machine yet.
`provisioned` means Servury returned `server_id`, `ipv4`, `plan`, `expires_at`.
`waiting_for_owner` is a cause, not a state: it explains a provisioned machine
with no heartbeat while the passphrase ceremony is the fallback. Under sealed
unlock a broken seal produces the same observable; the reason belongs in
`events.detail`.

**Interface.** `GET /claim?t=` validates and renders without consuming (mail
scanners prefetch links); `POST /claim` runs `UPDATE claims SET used_at=? WHERE
token_hash=? AND used_at IS NULL`, zero rows → 410, success → 303 to the ready
page. No key field, no password field. Token: 32 random bytes, SHA-256 at rest,
seven-day claim expiry; the setup token stays 24 h (§6.4). The
`claims_single_use` trigger is the concurrency guard, not a substitute for the
predicate.

**Data.** `ssh_public_key` becomes nullable, the CHECK goes, `claims` is
unchanged (the token is orthogonal to the boot credential), and `events.kind`
gains `claim_expired` — today an expired token leaves the row at `invited` with
no event, so "never opened" and "opened too late" are indistinguishable.

## 4. Alternatives considered

**A — claim-first key ceremony (the current cards).** Key generated before the
machine, pasted at claim, into the measured command line; first boot is verify,
encrypt, unlock, install. It proves "the only key is yours" with no new primitive
and the schema already encodes it. It costs six terminal sessions and fourteen
commands, passphrase support, and `waiting_for_owner`; reversal stops being cheap
once ten claim rows carry keys and the CHECK is load-bearing.

**B — zero-touch claim, sealed unlock (recommended).** Claim reserves the URL;
provisioning uses one signed release plus a random `seal_id`; the guest derives
and seals the LUKS key from the SNP-derived key; the Webapp opens behind basic
auth. One click, no terminal, no passphrase, no `waiting_for_owner`. It depends
on ODL-310; a broken seal means rebuild-and-restore; the proof moves from
`authorized_keys` to the seal policy plus attestation — new claims rows, Savio's
call.

## 5. Recommendation

**B, gated on ODL-310 at the 28 November M1 gate.** Independent of that answer,
ship **schema v3 before ODL-201 creates the first tenant**: SQLite 3.46.1 has no
`ALTER TABLE` form that drops a CHECK (three fail in the probe), and the manual's
two routes — the twelve-step rebuild, or the nine-step `PRAGMA writable_schema`
edit for removing a CHECK — warn that a syntax error leaves the database
unreadable. Before tenants this is a scratch-database exercise; after ten live
rows it is production schema surgery. Do not build A's ceremony while the gate is
open.

## 6. Cost of being wrong

B red: three spike days sunk; fall back to A, which adds one nullable column and
a form field — about a week, no data loss. A built and B green: four cards, two
to three weeks, plus the claims-deck rewrite, plus the same schema change under
live tenants. The state names and the token contract survive both; only the CHECK
and the enum move, and the door closes when the first real claim row exists, not
when the code merges.

## 7. Cross-cutting concerns

**Security.** No HQ boot credential in either option; the token is hashed and
single-use; under B the SNP-derived key never leaves the guest. Residual: the
control plane can boot a different image (detectable by measurement) and controls
DNS (pin the install fingerprint). Owner: Security, M1 gate.
**Privacy.** `tenants` holds email, slug, state and timestamps; no IP history, no
prompt data; the claim route must not log the token or the email.
**Observability.** Every transition writes an event; `claim_expired` is the
missing one; alarms are `invited > 7 d`, `claimed > 7 d` and `provisioned with no
heartbeat > 1 h` — the last is the seal-break signal under B.

## 8. What would change this

ODL-310 red → A; §5 flips, §§3–4 stand. A tenant who needs SSH → an optional
recovery key, one nullable column, no CHECK.

## Sources

- Google, *Design Docs at Google*: <https://www.industrialempathy.com/posts/design-docs-at-google/>
- SQLite, *ALTER TABLE* — both schema-change procedures:
  <https://www.sqlite.org/lang_altertable.html#making_other_kinds_of_table_schema_changes>
- `docs/MVP-V0.1.md` §6.2–§6.5; `app.py` `SCHEMA`; `manage.py` `cmd_funnel`;
  `docs/ops/evidence/2026-11-07-architect/schema-probe.txt`.
