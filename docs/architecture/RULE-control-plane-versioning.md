# RULE — versioning and compatibility for the control-plane interface

**Status:** rule v1 · 25 November 2026 · Architect · applies to `release.json`,
`manifest.json`, the seed document and the heartbeat — every message between HQ
and a guest.
**Source of the mechanism:** Stripe API versioning —
<https://docs.stripe.com/api/versioning>, read 2026-11-25. Stripe ships monthly
releases that are additive and safe to adopt, twice-yearly majors that may break,
pins the version per request (`Stripe-Version`) and per webhook endpoint, and
treats *open enums* as allowed to grow without a version change — so a client
must have a fallback branch. Odile has one peer, not many, but the same asymmetry
applies: one side is replaced by the other, and never at the same instant.

## The rule

1. **Read one ahead, write one behind.** A release that changes a document's
   `schema` is cut only after every consumer already accepts that integer; the
   release that teaches the reader keeps emitting the old integer. The reader is
   deployed first, the writer second, one release apart.
2. **Consumers accept a range; only the cut tool may compare for equality.**
   `min_schema ≤ schema ≤ SCHEMA`. `min_schema` rises only when a document that
   satisfies it can no longer be read correctly — never because a field was
   added.
3. **Facts are additive and ignorable; actions are exact.** A new fact is an
   optional field: an old document without it still verifies, and an unknown
   field is ignored. A new artifact name, or an enum value that triggers a
   write, is an action: it is refused until the reader knows it, and an open
   enum read gets a fallback branch rather than an exception.
4. **Every document names its version, and every sender reports the one it
   read.** A version nobody transmits is a version nobody can observe: the
   heartbeat carries the app release and the schema the guest is running, so a
   cut-over is scheduled against a count, not against a hope.
5. **A deprecation has three parts: a served minimum, one named refusal with
   the upgrade command, and a date.** The old integer is retired only after the
   last machine reports above it; until then the refusal names it.

## What would break it — today

Bumping `SCHEMA` in `update/odile_update.py` is a one-way latch, because the
reader that must accept `n+1` is the reader the `n+1` release cannot replace:

- `validate_release_document` compares `document.get("schema") == SCHEMA`
  (`update/odile_update.py:449`); `validate_manifest` does the same
  (`:480`). Both refuse with exit 4 before a single artifact is written.
- The updater is not one of the artifacts — `ARTIFACT_NAMES = ("installer",
  "skills", "webapp")` (`:73`) — and the installer never writes it
  (`grep -c 'odile_update\|odile-update' installer/odile_install.py` → `0`).
  The `installer` artifact replaces `/etc/odile/odile_install.py` only
  (`apply_change`, `:1096`).
- So a `schema: 2` release is refused by every installed reader, and no
  release that reader accepts can carry the reader that would accept it. The
  only exits are an out-of-band copy onto the guest or a rebuild-and-restore —
  a two-line change that becomes a fleet operation.

The live path is safe today only because the cut and the read use the same
constant in the same file; the trap opens on the first bump, which is exactly
when a schema changes for a real reason.

## Where the interface stands

| Document | Current gate | Rule 1 | Rule 2 | Rule 3 | Rule 4 |
|---|---|---|---|---|---|
| `release.json` | `== SCHEMA` (`odile_update.py:449`) | fails | fails | unknown artifact refused — correct | fails |
| `manifest.json` | `== SCHEMA` (`:480`) | fails | fails | unknown fields ignored | fails |
| seed | eight required keys; unknown keys accepted; no `schema` (`odile_install.py:13512`–`:13517`) | n/a | n/a | additive — passes | fails: no version in the document |
| heartbeat | optional `versions` map (`odilectl/health.py:964`); the sending guest carries none (`installer/odile_install.py:1332`) | n/a | n/a | additive — passes | fails: the sender reports nothing |

## Evidence

`docs/ops/evidence/2026-11-25-architect/schema-probe.txt` — the probe loads
`update/odile_update.py`, prints `SCHEMA = 1`, accepts `schema: 1` and refuses
`schema: 2` with exit 4 on both the release and the manifest, and records the
two greps above. Rerunnable verbatim with `/usr/bin/python3`.

## What this rule does not decide

The interval between the reader release and the writer release, and whether the
updater becomes an artifact. Both are design decisions for the card that
installs `odile-update` on a tenant (ODL-332): the cheap fix is to add `update`
to `ARTIFACT_NAMES` and accept a range; the expensive fix is to discover the
latch after ten machines carry a frozen reader.
