# Verdict — Hetzner BX11 box, first sub-account pair, restic drill

**Card:** `t_12b3720a` · **Seat:** Security · **Measured:** 2026-11-24
06:25–07:56 UTC, re-verified 09:18 UTC and again 2026-11-25 03:28 UTC
(`11-recheck-2026-11-25.txt`) · **Artifact:** Hetzner Storage Box id
709870, `odile-backups`,
BX11, Helsinki (`hel1`) · **Evidence:** `docs/evidence/hetzner-bx11/` (HQ-only)

This card is the first half of ADR-004 v3's destination: the box and one
sub-account pair, proved with a real restic restore. It is not the tenant
provisioning path (that is `t_83d6fd7d`) and it changes no application code.
The verdict below is on the access property the pair is meant to carry, not on
"the command exited zero".

## Verdict

| Claim the card makes | Verdict | Command and output |
|---|---|---|
| The drill passes with a real restic restore | **PASS** | `restic init` (12.0 s) → `backup` (8.7 s, snapshot `608ef125`, 4 files / 1.023 MiB) → `snapshots` → `restore` (8.6 s); `sha256sum -c` all four OK and `diff -r` identical — `05-restic-drill.txt` |
| The RO key is proven read-only | **PASS on the pair as shipped; the card's literal pair is a BLOCK** | With both homes `tenants/odl-bench` the RO key authenticated to the RW sub-account and wrote a file; after the RO home was moved to `tenants/odl-bench/data`, the RO key reads the snapshot (`--no-lock`) and its SFTP write/delete are refused (`sftp -b` exits 1), and it is denied on the RW account — `04-access-checks.txt`, `07-finding-shared-authorized-keys.md` |
| The 10-connection limit is measured | **BLOCK on the number, PASS on the measurement** | The 11th SFTP session **succeeds**; 25 hold; the 26th is refused with `Received disconnect … Maximum connections for host/user reached`; the ceiling is shared by the sub-accounts — `06-connection-ceiling.txt` |
| The token file is 0600 | **PASS** | `600 root root /root/.odile/hetzner-api-token` |
| No secret in the evidence | **PASS** | token, restic password and the (then live) sub-account passwords absent; no private-key marker; no `Authorization: Bearer <value>` — `09-secret-scan.txt` |

## The three findings

1. **A shared `home_directory` is a shared `authorized_keys`.** The card and
   ADR-004 v3 §4.2 specify `home_directory: tenants/odl-bench` for *both*
   sub-accounts and say each has its own `authorized_keys`. On this product the
   second sentence is false: both accounts are chrooted to the same directory,
   so `.ssh/authorized_keys` is one file. Reproduced with the first RO account
   (id 392552): the RO key wrote `/ro-key-on-rw-account.txt` through
   `u728180-sub1`. A holder of the user's read-only key could therefore write
   the backup. **Fixed in this card** by the nested home; **ADR-004 v3 and the
   provisioning card must carry the nesting**, not the literal pair.
2. **The ceiling is 25, not 10.** The product page and ADR §4.3 say 10
   simultaneous connections; the box accepted 25 and refused the 26th, with the
   refusal shared across the pair. The ADR's schedule (`sftp.connections=2`,
   five runs) is conservative by 2.5×; its "the sixth connection is refused"
   sentence is wrong. The measured sentence is "25 from HQ on 2026-11-24".
3. **restic's `sftp:` URL has no port field.** `sftp:user@host:22/path` is the
   path `22/path`; the first init created a literal `22/` directory and the RO
   account could not see the repository. The correct form is
   `sftp:user@host:path` with `-p 22` in `sftp.command`, which
   `scripts/backup.py` already builds. The runbook and the recovery file must
   use the no-port form.

A fourth, provisioning-facing fact: `POST /subaccounts` silently ignores
`readonly`, `ssh_enabled` and `reachable_externally`; every create needs a
follow-up `update_access_settings` and a readback, and a key must be installed
while the account is unlocked.

## Residual risks

* **RW can rewrite RO's `authorized_keys`.** Because the RO home is inside the
  RW home, the RW account can read/rewrite
  `tenants/odl-bench/data/.ssh/authorized_keys`. That is a denial of service on
  the read-only key (it already owns everything in the directory), not a
  privilege escalation. The inverse nesting (RO home the parent, RW home
  `tenants/<slug>/data`) removes even that and keeps the RO home exactly as
  the card wrote it — the Architect should choose one and record it. If Product
  wants this accepted explicitly, it is Savio's call.
* **25 is measured from HQ on 2026-11-24, not a product guarantee.** A
  per-source-IP limiter would look identical from one host. The ADR should date
  the number.
* **The create-time password quirk.** A recreated sub-account did not accept
  the password sent at create; the SDK's
  `…/actions/reset_subaccount_password` set it explicitly. Any provisioning
  code that assumes the create password is live will fail. Both passwords were
  reset to discarded random values; the keys are the credentials HQ keeps.

## What this proves, and what it does not

It proves the box exists, is delete-protected, is in Helsinki, holds bytes and
that a restic repository on it can be written, snapshotted, restored byte-for-
byte, and read with a genuinely read-only key (under the nested homes). It does
**not** prove the 3 GB tenant home over a transatlantic link, the end-to-end
rebuild time, or the tenant provisioning path; those are the bench's and
`t_83d6fd7d`'s.

No public sentence changes. The ADR is the only doc this feeds; "Backups are
off-site" is true in fact for this pair but stays Product's row to land
(`docs/CLAIMS.md` §1a, `t_66fcaa02`).
