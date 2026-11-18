# ADR-004 — The copy that outlives HQ: restic to a Backblaze B2 bucket in Canada East

**Status:** Proposed · 17 November 2026 · Architect. Savio directed the **shape** on
17 November 2026 — the copy lives at a third provider, neither the tenant's machine
nor HQ, Odile pays that provider, and the subscription price carries it. The
**provider, the account, the region and the two standing credentials on a tenant**
are his to approve (`docs/AGENT-ORG.md` §3: a new provider; a standing credential
on a tenant, default no). Nothing is built from this ADR until he answers.
**Answers:** ODL-570 — "which shape does the backup take, which third provider
keeps the copy, and how is a whole machine restored when the instance is already
gone?"
**Short answer:** keep **restic** and keep the **user's password**; move the
destination off HQ to **Backblaze B2, Canada East (Toronto)**, one bucket per
tenant, with two bucket-scoped keys — a read-write one on the guest and a
**read-only** one in the user's recovery file. A whole machine is restored by
rebuilding the image and having the user **paste the recovery file into one page
on the new machine**: one paste, one new sign-in, no CLI, no operator key, and
nothing at HQ can open the repository. Cost: **$0.01 per tenant per month** at the
measured repository size (1.017 GB), **$0.10** at a 15 GB envelope; the plan's
existing $0.60 line covers 86 GB per tenant and does not move.
**Related:** `docs/MVP-V0.1.md` §6.1, §6.7, §8, §12 (D19); `docs/CLAIMS.md` §1
*Reliability* and §4.5 (lines 64–65, 121–122, 180); `docs/architecture/ADR-003`
(`ADR-factory-backup.md`, the factory's copy — restic to Cloudflare R2) and §4.2
of it (B2 named as the fallback); `docs/runbooks/backup.md` §1–§9;
`docs/runbooks/offboard.md` §4–§5;
`docs/RESTORE.md` (HQ's own restore, not this); cards ODL-215, ODL-218,
`t_fec9f699`, `t_8eb17a54`, `t_ce52bb8d`, `t_9d6fb595`, `t_02b8df9d`, ODL-570.
**Decision owner:** Architect for the mechanism, the include set and the restore
path; **Savio for the provider, the account, the region and the credentials**
(§3 of the authority matrix). Product lands any sentence that moves
(`docs/CLAIMS.md`) — this ADR proposes one and changes none.

---

## 1. The invariant at risk

Five sentences the product depends on: four are internal invariants, and the
fifth is a deck sentence still **held from the public pages** (`docs/CLAIMS.md`
§1a, line 219).

1. **The copy survives the machine.** *True, drilled.* ODL-215's nightly restic
   snapshot and a destroy-and-restore were measured on 2026-11-12 (§2).
2. **The copy survives HQ.** **False today.** The repository's destination is
   HQ's own disk: `backup.odile.fyi` does not resolve, and the drill wrote to a
   dedicated chrooted `sshd` on HQ's port 2222, under `/srv/odile-backup/tenants/<slug>`
   (`docs/runbooks/backup.md` §3). HQ is itself one Servury guest: the address
   behind `mail.odile.fyi` — the public tree writes it as `203.0.113.139`
   (`docs/ops/PUBLIC-TREE.md`:85) — resolves to **AS395904 Servury, Montréal**,
   the same provider and the same city as the tenant machines. One Servury
   suspension, one dead host or one billing failure loses the tenant's machine, the
   control plane and the only copy at the same moment. **This is the invariant the
   card exists for**, and it is Savio's sentence of 17 November 2026 restated.
3. **Only the user can read it.** *True on the storage path, false on the rebuild
   path.* No repository password exists in `/root/.odile`, `/etc/odile` or
   `/opt/odile` (measured, §2), so the storage path is clean. But a rebuild is
   performed with `odilectl provision <slug> --backup-password-file <the user's
   recovery file>` (`docs/runbooks/offboard.md` §5), which reads the password on
   HQ and seals it under the new machine's setup token. The user's recovery file
   therefore sits on HQ's disk during a rebuild — and the "HQ cannot read" check
   greps three trees that file is not under. The claim is true on the path nobody
   exercises and conditional on the path every rebuild exercises.
4. **The user can get the bytes back without us.** *True in principle, unbacked in
   practice.* `docs/MVP-V0.1.md` §6.7 and `docs/CLAIMS.md`:122 promise a download
   "before" the 30 days; nothing today gives the user a credential that reaches
   the bytes. They hold a password and a repository URL, and an SFTP account only
   the guest can authenticate to.
5. **"Backups are off-site."** *In the deck, held from the page.*
   `docs/CLAIMS.md`:121 carries it and §1a line 219 holds it; it lands with
   `t_5f10dd14`. No page quotes the clause — `web/faq.html:131` has only "The
   machine is in Montreal, Canada." — but one page carries the *substance* in a
   different sentence: `web/privacy.html:149` lists the nightly backup's
   destination as "an off-site SFTP storage box". That is wrong twice today (the
   bytes are on HQ's disk, and the box was never bought) and stale after this ADR
   (the destination becomes a B2 bucket, not SFTP). Both surfaces are Product's
   to land; **this ADR publishes nothing**. And the deck sentence is not true in
   fact at this head: the bytes are on HQ's disk, in the same provider and the
   same city as the machine they are supposed to survive.

**What this ADR preserves:** the shape (restic), the password (guest-generated,
user-held, never at HQ), the nightly timer, the status record and the heartbeat,
and every measured number's meaning. **What it changes:** the destination, the
credential that reaches it, and the path a rebuilt machine takes to its own data.
**What it must not do:** add an operator key, an escrow, or a recovery account,
and sell a sentence the deck does not already own.

## 2. What exists today, measured

The live tenant `odl-7971a8`, as the runbooks record it. Nothing in this table is
a projection; each line is a measurement with a date and a source.

| Fact | Value | Where |
|---|---|---|
| The nightly snapshot | 156,311 files / **3,139,596,963 bytes** in 27.3 s, 2026-11-12T19:02:41Z | `backup.md` §8 |
| The repository after the first full run | **1,017,111,156 bytes** (3.09× compression + dedup) | `backup.md` §8 |
| The tenant's home | 4.1 GB; root disk 7.3 GB with 883 MB free, 2026-11-13 | `offboard.md` §1 |
| File restore (Drill 1) | canary deleted → checksum-verified: **0.931 s** | `backup.md` §4 |
| Whole-machine restore (Drill 2) | 550 files / 3.630 MiB into a rebuilt guest: **0.861 s**; timed rebuild-and-restore **38m03s**; end-to-end **84m13s**; of the 38m03s, **14m24s** is the machine's own work (build 7m48s, boot to first-visit 6m29s, restore 0.861 s, transfer and checksum 6 s) | `backup.md` §5.5 |
| HQ cannot read the repository | no password in `/root/.odile`, `/etc/odile`, `/opt/odile` (`rc=1`); `restic` with a wrong password exits 12; a data pack carries no plaintext name | `backup.md` §1, 2026-11-12T23:24Z |
| The repository password | 32 characters, in `/etc/odile/recovery.txt` (0600) on the guest and in the user's downloaded recovery file; nobody at HQ | `backup.md` §1 |
| The transport credential | an ed25519 key **generated on the guest**, public half registered on the box; the box's host key pinned in `known_hosts` | `backup.md` §3 |
| The destination | `sftp:odile-backup@backup.odile.fyi:/tenants/<slug>` — **a name that does not resolve**; the drill used `sftp://***@203.0.113.139:2222/tenants/<slug>` | `backup.md` §3, §9 |
| The include set | `--home` (which the installer sets to the tenant layout's home), minus `.hermes/hermes-agent`, `.hermes/tools`, `.hermes/installs`, `.hermes/cache`, `.hermes/logs`, `.hermes/audio_cache`, `.hermes/image_cache`, `.cache`, `.npm`, `.local/share/uv`, `node_modules`, `__pycache__` | `installer/odile_install.py` (`DEFAULT_EXCLUDES`) |
| Retention | 7 daily + 4 weekly, pruned on every run, grouped by `--host <slug>` so a rebuild stays one group | `backup.md` §2, §7 |
| The rebuild's password continuity | `odilectl provision --backup-password-file <the user's recovery file>` seals the password under the new setup token; only the token's SHA-256 is stored | `backup.md` §5.2, §9; `seed/seed.py` |

**Three facts in that table are worse than the table looks, and each one is a
failure the builder did not test.**

1. **The destination shares HQ's fate.** The runbook says it in its own words:
   "the backup survives the tenant, not the loss of HQ" (`backup.md` §3). The
   stand-in was honest; the card that buys the real box (`t_8eb17a54`) has not
   run.
2. **The rebuild path puts the user's recovery file on HQ's disk.** The
   `--backup-password-file` argument is a *path*, so the password is not on
   `argv`, but the file it names is on HQ while the rebuild runs — and the
   "HQ cannot read" measurement greps `/root/.odile`, `/etc/odile` and
   `/opt/odile`, none of which is a shell's `~`. A rebuild is also the one moment
   the repository password is *supposed* to be the user's alone.
3. **The include set is a flag, and the wrong flag succeeds.** The installer's
   backup unit is rendered from `layout.home` (`backup_unit_files`,
   `installer/odile_install.py:12194–12214`), which for a tenant is
   `/home/tenant` (`Layout.tenant`, `:10103–10113`; `DEFAULT_HOME`, `:578`;
   the image runs the installer with `--user tenant`,
   `deploy/image/odile-release-setup.sh:154`) — the same string the live unit
   the drill measured names (`backup.md` §2.1). The `/home/odile` string in
   the tree is `installer/first_visit.py`'s standalone `DEFAULT_ENV` (`:114`),
   which the unit overrides with `--env-file` (`:9299`) filled from the tenant
   layout (`installer/odile_install.py:12474`, `:12279`). So today the two
   homes agree, and **no migration or home-mapping is needed**: the failure is a
   rebuild whose unit or restore call drops `--home` or overrides it. A restore
   under a wrong `--home` succeeds, restores an empty tree, and reports `ok`.
   The drill must assert that `state.db` and `cron/jobs.json` came back, not
   that restic exited zero.

## 3. The three shapes the card asks about

The card names three: a restic repository plus a password, a single encrypted
archive, and a user-driven download. They differ in what the provider can do, what
the user holds, and what has to exist for a restore. Cost is priced at the
**measured** set (1.017 GB in the repository) and at a **15 GB envelope** (≈15×
that) with today's B2 rate, $6.95/TB-month = $0.0068/GB-month.

| | **A. restic repository + password** | **B. one encrypted archive a night** | **C. user-driven download only** |
|---|---|---|---|
| What the provider holds | ciphertext (restic: AES-256-CTR + Poly1305-AES, scrypt KDF), object sizes, access times | one opaque blob per night; same visibility, no dedup so 11 blobs for 7 daily + 4 weekly | the archive, for as long as the download window is open |
| The secret the user holds | a 32-character repository password (the recovery file) | an age/tar passphrase (or an identity file) — the same shape in the user's hands | the passphrase, **and the file itself** |
| What Odile can do | delete, withhold, or write garbage (the bucket key); never forge a snapshot restic will accept (no password) | same | same, plus delete the copy the user never downloaded |
| Restore machinery | `restic restore` on the guest; partial restore and `restic check` exist | `age -d \| tar -x` — no partial restore, no integrity check, and a torn `state.db` unless the same WAL-safe staging is rebuilt by hand | the user's laptop must still have the file; no machine-side machinery at all |
| Nightly cost of a run | only the delta (measured: 27.3 s for 3.14 GB on the first run, seconds after) | a full 2–2.5 GB upload every night | none until the user asks |
| Cost per tenant / month | **$0.01** measured; **$0.10** at 15 GB | **$0.16** at 11 full archives ≈ 24 GB | **$0.00** (served from the guest) |
| Verdict | **Chosen** | Rejected: cost is not the reason — integrity, partial restore and a full rewrite of the program and the drill are | Kept, but as the **exit** and the **second copy**, not as the backup |

**Why A.** It is the only one of the three with a measured restore, a measured
"HQ cannot read", a program in the installer and a runbook a stranger can follow.
B is a different program, a retired drill and a nightly full upload on a 4-vCPU
guest, to buy a single-file shape that a non-technical user cannot open without a
terminal anyway (`age`, `tar`). C alone fails the only case that matters — the
machine is gone and the user never downloaded — and it makes the RPO "whenever the
user last remembered" instead of "since 03:00". C is still the right *exit*: §6
puts the read-only key in the recovery file so the download needs no handover.

**What A does not buy, stated plainly.** A restic repository needs restic to read
it. That is fine here: restic is pinned in the image (0.18.0-1+b4) and any Linux
has it in its archive, and the exit path in §6 hands the user the URL, the key and
the password, not a tarball.

## 4. The destination

The shape is settled; the destination is the card's second question. Savio's
constraint rules out the tenant machine and HQ, so the candidates are third
providers. Prices are list prices read on 17 November 2026, from the vendor's own
pages where marked.

| | **Backblaze B2, Canada East** | Cloudflare R2 | Hetzner Storage Box BX11 |
|---|---|---|---|
| Protocol and path | S3 API over HTTPS/443 at `s3.ca-east-006.backblazeb2.com`; restic's `s3` backend | S3 API over HTTPS; restic's `s3` backend | SFTP on **port 23**, hostname `your-storagebox.de`, one username per sub-account; restic's `sftp` backend |
| Where the bytes are | **Toronto, Ontario** — `ca-east-006`; the region is chosen at account creation and **cannot be changed** | Cloudflare's choice of location hint/jurisdiction | Germany (FSN1) or Finland (HEL1) only |
| Capacity and cost at our size | no minimum; **first 10 GB free**; $6.95/TB-mo; egress free up to 3× stored; free transactions | 10 GB-month free, then $0.015/GB-mo; free egress | 1 TB flat, **€3.20/mo** (≈$3.58 at the rate `ADR-003` §4.2 already uses), unlimited traffic, no minimum term |
| Cost per tenant / month at 1.017 GB | **$0.01** (10 tenants = 10.17 GB ≈ the free tier, ≈$0.001 total) | $0.00 at 10 tenants; $1.50/tenant at 100 GB | **$0.36** at 10 tenants on one box ($3.58 ÷ 10) |
| Durability of the only surviving copy | 11 nines claimed; 17 data + 3 parity shards across 20 storage pods | 11 nines claimed (multi-device object storage) | **RAID, "the data is not mirrored onto other servers"** — Hetzner's own FAQ |
| Per-tenant scope | one bucket per tenant; app keys restricted to that bucket (B2's app-key model); 100 buckets/account; **bucket names are globally unique across all B2 accounts**, so the name is recorded in the tenant row, never derived | one token per bucket | a sub-account per tenant, directory-scoped; **100 sub-accounts, 10 concurrent connections per box** |
| The credential the guest needs | an S3 application key **minted by HQ and delivered in the seed** (0600 `/etc/odile/backup-s3.env`) | the same | an SSH key **generated on the guest**, its public half registered on the box **after** the guest boots |
| New network path from a tenant | HTTPS/443 to one hostname (the image already talks 443) | HTTPS/443 | **port 23 out**, plus an SSH client, a pinned host key and a `known_hosts` file |
| Shared fate with today's copies | none: not Servury, not HQ, not Cloudflare | **the same account that holds HQ's DNS and HQ's own backup** | none |
| Verdict | **Chosen** | Rejected | Rejected |

**Why B2.** It is the only candidate whose *last surviving copy* sits in
erasure-coded object storage (17+3 across 20 pods) rather than on one RAID
server; it is the cheapest at the size we actually have (under a cent per tenant
at the measured repository); and it is the one provider this house
has already written down as its fallback (`ADR-003` §4.2), so it adds no unknown
vendor to the risk register. Its per-tenant key is minted by HQ and delivered in
the seed, which removes the worst operational wart of today's SFTP path: a
`backup.py init` that fails until a human appends the guest's public key to a
box. And Toronto is a different provider, a different city and a different
datacentre from the machine, which is what "off-site" was always supposed to mean.

**Why not R2, though it is cheaper than B2 at 10 GB.** ADR-003 §4.3 already
wrote the argument against itself: Cloudflare holds the DNS zone *and* HQ's only
backup, so "one compromised Cloudflare login, or one billing suspension, can both
repoint the records and delete the only off-site copy". Adding every tenant's
backup to that account would turn a two-vendor problem into a one-vendor problem
for the whole product. Tenant data goes somewhere Cloudflare cannot reach.

**Why not a Hetzner Storage Box, though the SFTP code already exists.** Four
reasons, in order of weight. (1) **Durability:** a Storage Box is a RAID server,
and Hetzner says the data "is not mirrored onto other servers" — for the *last*
copy of a tenant's home that is the wrong class. (2) **Blast radius:** one 1 TB box
is one quota shared by every tenant, and one tenant's runaway directory fills it
for all ten; and the box allows **10 concurrent connections**, which ten tenants
with the same 03:00 timer reach exactly (`RandomizedDelaySec=300` spreads them, it
does not guarantee it). (3) **The registration race:** the guest generates its key
at `init`, so the public half has to reach the box *after* the guest boots and
*before* the first run — a human step per tenant, or a new HQ endpoint to carry a
public key. (4) **Price:** €3.20 flat only beats B2 above ≈500 GB total — about
500 tenants at today's measured repository and about 165 at a 3 GB one. The one
thing it wins — a guest-generated transport key, nothing secret at HQ — is real
(§5) and is the reason the SFTP code stays for one release rather than being
deleted in the same commit.

**What Savio is being asked to approve, exactly.**

- **Open a Backblaze account in the Canada East region.** The region is chosen at
  signup, is fixed for the account's life, and every bucket inherits it: **this is
  the one one-way door in this ADR.** EU Central (Amsterdam) is the alternative if
  he wants a different jurisdiction; Canada East keeps the copy in the same country
  as the machine, in the same time zone, and on a path short enough that the
  restore's transfer term is seconds (the model in §7.2).
- **Generate the master application key**, save it at `/root/.odile/b2-backup.env`
  (0600, root), and never use it from a tenant. It is `writeKeys`-class, i.e. it
  is the account.
- **Set an account-level cap and alert** (B2's *Caps & Alerts*) at 5× the expected
  total, so a runaway bucket is an alert and not an invoice.
- **Approve two standing credentials on a tenant** (§5): the guest's read-write
  bucket key and the read-only key in the recovery file.
- **Let Odile pay $6.95/TB-month.** The first bill, at ten tenants, is under a
  cent; at the plan's $0.60/tenant line the allowance is 86 GB per tenant.

**The board card for this question:** `t_02b8df9d` (lane `human`, assignee
`savio`, blocked), linked from ODL-570. The answer is recorded there, and the
cards in §8 wait for it.

## 5. The trust boundary this creates

**What Backblaze holds:** ciphertext, the repository's size and object count, the
bucket name (`odile-<slug>` — the slug is already public in the URL), the times of
access, and the fact that a machine writes nightly. **What it does not hold:** the
password, the plaintext, file names (restic encrypts them), or any way to produce
a snapshot restic will accept. It can delete or withhold; it cannot forge. This is
a new provider and a new vendor relationship, which is why it is Savio's to make.

**What HQ holds, and what it can do.** The master key (root, 0600) and each
tenant's read-write bucket key, because the seed that provisions the guest has to
carry it and a rebuild has to deliver it again. That means HQ **can delete, and can
write garbage into, any tenant's repository** — and cannot read one byte of
plaintext, because the password is the user's and is not in the seed. It could
already delete any tenant's machine through the Servury API, so this does not widen
the availability power; it does add "can overwrite", which is worth stating: an
operator who wanted to destroy a backup could, and an operator who wanted to read
one could not.

**The rule this amends, named.** `AGENTS.md` line 61 says HQ stores no tenant
passphrase, private key or Webapp password — "Only the public key and a hashed
setup token." A bucket-scoped application key is none of those three: it is not
a key *to* the machine (no door in), it opens one bucket of the tenant's own
ciphertext, and it has to be in the seed because HQ mints it at provision and
the guest cannot. This ADR amends the **letter** of that rule with Savio's yes
(§4) and leaves its purpose — nothing at HQ can log in or read plaintext —
exactly as it was. The recovery file's read-only key is the same class of
credential, held by the user instead of HQ.

**What the guest holds.** `/etc/odile/backup-s3.env` (0600 root): the bucket, the
region and the read-write application key, whose capabilities are exactly
`listFiles`, `readFiles`, `writeFiles` and `deleteFiles` on that one bucket (plus
`listAllBucketNames`, which some S3 SDKs need and which reveals bucket names — the
public slugs — and nothing else). It is a credential **to the tenant's own
bucket, outbound only** — it opens no door *into* the machine and no other tenant's
bucket, and the machine is already root over itself. It is the same class of thing
as today's guest-generated SFTP key, except that Odile mints it instead of the
guest. Under `docs/AGENT-ORG.md` §3 this is "a standing credential on a tenant"
and needs Savio's yes; it is granted on the argument that it is a way *out* to the
tenant's own ciphertext, not a way *in*.

**What the user holds, and what it now makes true.** The recovery file gains two
lines — the repository URL and a **read-only** bucket key — inside the file the
installer already renders (`render_recovery`):

```
Odile recovery file
===================
Tenant:              odl-7f3a2c
Instance:            <the instance this file was written on>
Written:             <date>

Backup password:     <32 characters>
Repository:          s3:https://s3.ca-east-006.backblazeb2.com/odile-odl-7f3a2c
Read-only key:       <keyID>:<applicationKey>
Certificate SHA-256: <the certificate the browser saw>

This file is the only copy of the backup password. ...
```

With those three things — the password, the repository and the read-only key — a
user, or their lawyer, or their next provider, can pull the repository with
`restic -r <Repository> --no-lock snapshots` and `restore`, on any machine,
**without Odile, without HQ and without a support grant**. (The cluster suffix in
the URL is whatever the bucket page names — `ca-east-006` today — and it is
recorded in the tenant row with the bucket, not guessed by the guest.) That closes
invariant 4, and it makes the 30-day exit a thing the user already has rather than
a thing Odile promises to build. It also makes `docs/CLAIMS.md`:64's "the file
opens your backup" literally true instead of figuratively true, which is a
**stronger sentence than the deck owns** and goes to Product and Savio (§9).

**What the read-only key deliberately cannot do:** write, delete or lock. A
recovery file that is printed, emailed to a new laptop or dropped in a password
manager cannot destroy the repository it opens. The read-write key never leaves the
guest (and the seed).

**A grant gains nothing new, which is why no access claim moves.** A support grant
could already read `/etc/odile/recovery.txt` (`docs/CLAIMS.md`:114 says so) and the
SFTP transport key beside it, so a grantee could already copy the repository's
bytes and its password. The read-only key is the same capability, in a tidier
form, and it widens the grant by nothing. A user who wants a grantee's copy to
stop opening the repository rotates the password — `restic key add`, prove a
restore, `restic key remove` — which `docs/runbooks/backup.md` §7 already
documents.

## 6. What "whole machine" means

The backup does not contain a machine. **A machine is the signed image plus one
home**, and the backup contains the home.

| In the repository (the home, `--home` from the layout) | Rebuilt, not restored |
|---|---|
| `.hermes/state.db` (the session store), `.hermes/memories/`, `.hermes/sessions/`, `.hermes/skills/`, `.hermes/config.yaml`, `.hermes/.env` (the sign-in hash), `cron/jobs.json`, `plugins/`, `skins/`, `platforms/`, `state/` | the OS, the kernel, the initramfs and the command line — the **signed, measured image** |
| the user's files, `workspace/`, `documents/`, `AGENTS.md`, `SOUL.md` | `.hermes/hermes-agent`, `.hermes/tools`, `.hermes/installs`, the caches (the pinned installer and `odile-update`) |
| | `/etc/odile/key.env` (the NEAR key) and the bucket key — **re-issued in the seed** at provision |
| | `/etc/odile/recovery.txt` — **the user's paste** (§7) |
| | the TLS certificate (ACME, on first request) and `/var/lib/odile/backup.json` (the first post-rebuild run) |

Two consequences a reviewer should check. **First, a restore is only as good as
its `--home`** — §2 fact 3: a restore under the wrong path writes an empty tree
and exits 0 (both units name `/home/tenant` today; the failure is a rebuild that
overrides it), so the drill asserts content, not an exit code: `state.db` opens
and holds the sessions the user had, `cron/jobs.json` is present, and the `.env`
hash the restore put back equals the one the backup carried (checked before the
page's sign-in step replaces it). **Second, the sign-in hash is in the home**, so the
order of the rebuilt machine's steps matters: restore first, then set the sign-in
(§7.3) — the other order lets the restore silently replace the password the user
just chose.

## 7. The restore path, step by step

This is the answer to the card's third question. It assumes the rebuild keeps the
tenant's **slug** — the same URL, the same recovery file — which today it does
not: a destroyed slug cannot be re-provisioned (`odilectl/state.py`,
`TRANSITIONS`), so `t_ce52bb8d` is a hard dependency of the user-facing promise,
not a nicety.

### 7.1 What has to be true before the machine dies

- The nightly run has succeeded: the heartbeat's `last_backup_at` is under **36
  hours** old (the freshness watchdog, ODL-216 — `odilectl/health.py:710`,
  `BACKUP_ALERT_SECONDS = 36 * 60 * 60`) and `backup.json` says `ok`.
- The user still has the recovery file, off the machine. Nothing else is needed
  from them — not a key, not a passphrase, not a shell.

### 7.2 Who does what, in order

| # | Who | What | Duration |
|---|---|---|---|
| 0 | the user / HQ | The URL fails or the heartbeat stops. The alpha path is an email to `help@odile.fyi`; the 36-hour check catches a silence nobody reported | detection, not a step |
| 1 | HQ | `odilectl provision <slug> --apply`: one machine in the same tenant row, the same slug, the published image, **no `--backup-password-file`** | the panel's order — **unmeasured for a rebuild**; the bench's build-to-first-visit term is 14m24s and the first boot's measured installer is 296–353 s |
| 2 | the machine | the signed image boots and the installer runs zero-touch; the seed carries the bucket, the read-write key and the tenant row's **previous instance id**, so the page knows this is a rebuild from the row and not from an operator flag somebody forgot | included above |
| 3 | the user | opens `https://<slug>.odile.fyi/first-visit?t=<token>` from the "your machine is back" email and **pastes the whole recovery file** into one textarea | under a minute |
| 4 | the machine | the first-visit service parses `Backup password:` with the installer's own grammar (`RECOVERY_PASSWORD_RE`), writes `/etc/odile/recovery.txt` (0600) **regenerated for this machine** — same password, new `Instance:`, new `Certificate SHA-256:`, the repository and the read-only key — starts `odile-restore.service`, and the page polls a text status endpoint with a meta refresh (no JavaScript) | — |
| 5 | the machine | `odile-restore.service`: stop the backup timer and the agent unit, `backup.py restore latest --target / --include <home>`, start the agent unit, run one `backup.py run` so a fresh snapshot exists immediately, re-enable the timer, write the status. The timer is stopped first so a 03:00 run cannot snapshot a half-restored home; the restore's own run is the newest snapshot either way | repo ÷ throughput; the measured repository is **1.017 GB** |
| 6 | the user | the page says "Restored snapshot `<id>` of 2026-11-17T03:00Z — 156,311 files, 3.14 GB"; they set a **new sign-in** (the page's normal step) and sign in | under a minute |
| 7 | the user | checks one thing they care about — a file, the last brief, the agent's memory of yesterday — and the page's own report names the snapshot and its time | a minute |
| 8 | HQ | records the rebuild on the row: snapshot id, restore time, size; the next 03:00 run lands under the same `--host <slug>`, so the retention group stays one | — |

**The transfer term, with its assumption stated.** The measured 38m03s restored a
3.630 MiB home from a repository on the same host, so it says nothing about a
3 GB home from a third provider. The repository is **1.017 GB**; at 5 MB/s that is
**3.5 minutes**, at 20 MB/s it is **55 seconds**. As a sanity bound on the path,
this box — a Servury VM in Montreal, the same provider as a tenant — pulled
100,000,000 bytes from Hetzner's Ashburn, Virginia endpoint in **0.624 s
(168 MB/s)** on 17 November 2026; a tenant's own link to Toronto is the same order
of magnitude. The end-to-end estimate is therefore **15–25 minutes**, comfortably
inside the published hour — but the number that replaces 38m03s has to come from
the drill in §8, not from this paragraph.

### 7.3 The user's four actions, in one paragraph

Open the link in the "your machine is back" email; paste the recovery file into
the box; wait for the page to say which snapshot came back; set a new sign-in and
look at one thing that matters. No terminal, no command, no SSH key, no chat
message containing a secret.

### 7.4 The branches, and the failures this shape has to answer

| Branch or failure | What happens |
|---|---|
| **Wrong password** | restic exits 12 (`wrong password or no key found`); the page says "that file does not open your backup", the home is untouched, the paste can be retried. A file from another tenant is caught by its own `Tenant:` and `Repository:` lines before restic runs, and a truncated paste fails at the page — the installer's `parse_recovery` grammar is reused, so a bad file never reaches a restic call |
| **The user lost the recovery file** | the page offers "start fresh without a restore": the guest mints a new password into a new recovery file, uses a new repository path, and the old bucket is kept 30 days and deleted (`t_9d6fb595`). The page says, in one sentence, that the old data is unrecoverable without the old file — which is the promise, not a bug |
| **The restore is interrupted** | `odile-restore.service` is a oneshot and the page re-offers the step; restic restore overwrites into the same target, so a second run converges. The backup timer stays stopped until a run completes, and if the user never comes back the 36-hour freshness watchdog is the signal. Nothing else in the machine depends on it, so the user is never blocked |
| **The process is killed mid-write** | `state.db` is overwritten under a live gateway — the torn-database failure `docs/RESTORE.md` §2 already refuses. The unit stops the agent first and starts it after; the drill asserts the session list is readable afterwards |
| **The nightly timer fires before the user pastes** | The backup **service** carries `ConditionPathExists=/etc/odile/recovery.txt`, and on a rebuild the installer **does not create that file**: `ensure_recovery` (`installer/odile_install.py:10972`) skips when the seed carries the previous instance id **and the file is absent**, because a freshly minted password would not open the old repository. So a rebuilt-but-not-yet-restored machine's timer fires, the condition fails, the service is skipped silently, and no false "backup failed" reaches the watchdog. The paste writes `/etc/odile/recovery.txt`, and `odile-restore.service` runs one snapshot before the next 03:00. Once the file exists, a later installer run keeps its password and refreshes only the instance and certificate lines, exactly as today. On a **fresh** tenant there is no previous instance id, the installer mints the recovery file at install as today, and the first backup is not suppressed |
| **A password typed into the chat instead of the page** | it travels to NEAR AI's TEE in the prompt **and** lands in `state.db`, which is inside the backup it opens. The restore step is a page POST for exactly this reason, and the help text says so |
| **Backblaze is down or the account is capped** | the restore waits and retries; the machine is usable without it. A failed upload is a `status: failed` heartbeat, which the freshness watchdog already reports |
| **HQ is gone** | the bucket, the user's recovery file and the signed image are enough to rebuild the machine by hand on any provider; what is lost is the convenience of `odilectl`, not the data. This is the first time that sentence has been true |

## 8. What this changes in the code — cards, not prose

The ADR decides; the board builds. Each row is one card. The provisioning card
and the drill are the two that make the destination worth paying for.

| Card | What it must do | Depends on |
|---|---|---|
| **Provisioning** | `odilectl provision` mints the tenant's bucket (a four-character suffix on a name collision, because B2 bucket names are globally unique), the read-write key and the read-only key, records the repository in the tenant row, and puts the read-write key in the seed **with the previous instance id** (empty on a fresh tenant; the rebuild marker §7.4 reads). **Retires `--backup-password-file`** and with it the sealed password in `seed-rebuilds.json` | the account (§4; `t_02b8df9d`) |
| **`backup.py` + installer** | an `s3:` repository: load `/etc/odile/backup-s3.env`, keep the SFTP branch for one release, add `ConditionPathExists=/etc/odile/recovery.txt` to the backup **service**, and **do not mint a recovery file on a rebuild** — `ensure_recovery` skips only while the file is absent and the seed carries the previous instance id; once the paste wrote it, a later run keeps the password and refreshes the certificate line. That is the suppression which makes the condition true before the paste (§7.4). Update `BACKUP_SHA256` and re-sign the installer | the seed field |
| **First visit** | the rebuild step: detect the previous instance id in the seed, ask for the recovery file before the sign-in step, write `/etc/odile/recovery.txt`, run `odile-restore.service`, show the snapshot and its time (no JavaScript) | `backup.py`'s `restore` (already supports `--target /` and `--include`) |
| **Recovery file** | two new lines (the repository URL and the read-only key) and the parse that tolerates them; the `first_visit.py` copy of the grammar stays the one source | the provisioning card |
| **The drill** | destroy → rebuild → paste → restore against the **real bucket**, with `state.db` and `cron/jobs.json` asserted, and both clocks stated (the lesson of ODL-215's two T0s). This replaces 38m03s and decides whether "under an hour" stays | all of the above |
| **The exit** | `t_9d6fb595` grows teeth: at `keep_until`, delete the bucket; before it, the user already holds the key and the URL | provisioning |
| **The live and demo repositories** | one command in the Webapp terminal (`backup.py init` against the new URL, then `run`); the HQ `sshd` on 2222 stays 30 days and is then deleted. **This card replaces `t_8eb17a54`'s purchase**: that card becomes "delete the stand-in", not "buy a storage box" | the drill |
| **The same-slug rebuild** | `t_ce52bb8d`. Without it the rebuilt machine has a new URL, and the user's recovery file names a tenant that no longer exists | — |

## 9. Claims

| Sentence | Today | After this ADR |
|---|---|---|
| "Backups are off-site." (`CLAIMS.md`:121) | **in the deck, held from the page** (`CLAIMS.md` §1a line 219, `t_5f10dd14`); not true in fact — the bytes are on HQ's disk | **true in fact when the migration card (§8) runs**: a bucket in Toronto at a different provider from the machine and from HQ. The page sentence stays held until Product lands it — **this ADR does not publish it** |
| "The encrypted backup is kept 30 days after cancellation, then deleted. Odile can delete it, not read it." (`:122`) | true on the storage path; conditional during a rebuild | true on both paths, because the password never transits HQ: the rebuild is a paste on the machine and `--backup-password-file` is retired |
| "A recovery code and a recovery file are the way back: … the file opens your backup." (`:64`) | true through the agent | **literally true**: the file carries the URL and a read-only key, so it opens the backup on any machine. **This is stronger than the deck owns** — Product lands the wording, Savio approves it (§3, a published sentence not in `CLAIMS.md`) |
| "The recovery file holds the backup password and the certificate fingerprint." (`:65`) | true | it now also holds the repository and a read-only key; the sentence needs the extra clause |
| "Backed up nightly. Rebuildable in under an hour — without a key." (`:180`) | measured at 38m03s against a repository on the same host | **at risk**: the number must be re-measured against the real bucket. The model says 15–25 minutes and the sentence probably survives; the drill decides, and if it does not, the sentence moves |
| `docs/MVP-V0.1.md`:301 "A restore requires the user's key and the user's recovery file" | stale: there is no user key under model A | one paste in the browser, no key |

Nothing here makes the *access* story stronger: no operator key, no escrow, no
recovery account, and the disk and the prompts are untouched. The one new claim
is about the user's own copy, and it is theirs to make.

## 10. Reversibility

**Two-way, with one exception.** Moving a repository is `--repo` on the next
`init`/`run` and a new seed value; the old bucket is kept 30 days and deleted
(`backup.md` §7). The program, the password and the restore command do not change.
The read-only key in a recovery file is additive: a user who never uses it is
unaffected, and a file without the two new lines still parses.

**The exception is the account's region**, chosen at signup and unchangeable for
the account's life (Backblaze's own documentation). Getting that wrong costs a
second account and a re-upload, not the data.

**It stops being cheap** at the first tenant whose only surviving copy is a B2
bucket — i.e. at the first real restore or the first cancellation, whichever comes
first. After that, changing providers is a migration with a 30-day window, not a
config change.

## 11. Evidence

Measured, at this head, unless marked *modelled*:

- The destination and its honesty: `docs/runbooks/backup.md` §3 (the repository
  name does not resolve; the stand-in is HQ's `sshd` on 2222; "the backup survives
  the tenant, not the loss of HQ"), §9 (the box is `t_8eb17a54`'s purchase).
- HQ's provider, read on 17 November 2026: `getent hosts mail.odile.fyi`, then
  `curl https://ipinfo.io/<that address>/json` → `"hostname": "mail.odile.fyi"`,
  `"city": "Montréal"`, `"org": "AS395904 Servury"`. The shipped tree carries the
  documentation-range alias for that address (`docs/ops/PUBLIC-TREE.md`:85), and
  `docs/runbooks/backup-hq.md` §5 states that HQ is "one vCPU-class box".
- The numbers in §2: `backup.md` §4 (0.931 s), §5.5 (38m03s / 84m13s / 14m24s /
  0.861 s), §8 (156,311 files / 3,139,596,963 bytes; repository
  1,017,111,156 bytes; the `rc=1` password grep; the snapshot ids).
- The include set and its home: `installer/odile_install.py` (`DEFAULT_EXCLUDES`;
  `backup_unit_files` with `home=layout.home`; `Layout.tenant` → `/home/<user>`;
  `DEFAULT_HOME`; the unit's `--env-file` filled from `layout.hermes_home`),
  `deploy/image/odile-release-setup.sh` (the installer runs with `--user tenant`),
  `backup.md` §2.1 (the installer-written unit names `--home /home/tenant`), and
  `installer/first_visit.py` (`DEFAULT_ENV = /home/odile/.hermes/.env`, which the
  unit overrides).
- The rebuild's password path: `docs/runbooks/offboard.md` §5 and
  `docs/runbooks/backup.md` §5.2, §9; `seed/seed.py:39`, `:383`, `:541`.
- The restore primitives that already exist: `command_restore` in
  `installer/odile_install.py` (`--target`, repeatable `--include`);
  `backup.py init` answering `same` against an existing repository.
- Prices, read on 17 November 2026: Backblaze B2 $6.95/TB-month, first 10 GB
  free, egress free to 3× stored, no minimum duration (backblaze.com pricing
  page); Canada East = Toronto, `ca-east-006`, region fixed at account creation
  (Backblaze data-centres page, updated 2026-11-13); 11 nines and 17+3 erasure
  coding (Backblaze resiliency page); 100 buckets per account and bucket-scoped
  app keys (Backblaze S3-API and app-key pages). Cloudflare R2 $0.015/GB-month,
  10 GB free, free egress (`ADR-003`, `docs/architecture/ADR-factory-backup.md`
  §4.1). Hetzner BX11 €3.20/month, 1 TB, 100
  sub-accounts, 10 concurrent connections, Germany or Finland, "the data is not
  mirrored onto other servers" (hetzner.com Storage Box page and FAQ).
- The transfer bound: `curl -o /dev/null -w '%{speed_download}'` against
  `https://ash-speed.hetzner.com/100MB.bin` on 17 November 2026 —
  `bytes=104857600 seconds=0.623830 bytes_per_s=168086818`.
- *Modelled, not measured:* the 15–25 minute end-to-end estimate; the 2–2.5 GB
  size of a hypothetical full archive; the 15 GB envelope; every "at N GB" price
  except the 1.017 GB row, which is the measured repository × the list rate.

## 12. What this ADR does not do

- **No code, no account, no bucket.** The first honest sentence about this ADR is
  that it is a recommendation; the second is that nothing here is worth a dollar
  until Savio opens the account.
- **No new claim in the deck.** §9 names the one sentence that gets stronger; it
  lands through Product and Savio or not at all.
- **No `docs/MVP-V0.1.md` edit** for §6.1's diagram, §6.7's "off-site SFTP box",
  §8's $0.60 line or `AGENTS.md`'s "off-site SFTP storage box": all four are
  outside this card's `touches` and are named in §8's card list instead of
  smuggled in here. **No `web/privacy.html` edit either**: its destination cell
  (line 149, "an off-site SFTP storage box") is wrong today and staler after this
  ADR; it lands with `t_5f10dd14` (§1 item 5).
- **No second copy.** One destination, one credential set, one drill — the same
  call ADR-003 §4.1 made for HQ, for the same reason: a second copy doubles what
  can be wrong at 03:00 and still needs a drill to prove it. The read-only key is
  not a second copy; it is the user's own way to make one whenever they like.
- **No `backup.odile.fyi`.** The name that never resolved is retired with the SFTP
  box, and the storage-box purchase in `t_8eb17a54` becomes "delete the stand-in".
  A B2 repository is addressed by the provider's endpoint, so the seed carries a
  URL with no DNS record of ours in it: one fewer name to own and one fewer name
  that can point at the wrong box.
- **No promise that Backblaze cannot lose the data.** It can (billing, abuse
  review, a regional outage). The residual risk is that the only surviving copy is
  a third party's, and the mitigation is the user's read-only key — a copy they can
  take at any time without asking.
- **No decision about the factory's copy.** HQ keeps its Cloudflare R2 bucket
  (`ADR-003`); after this ADR the two copies live at two different providers,
  which is the property Savio asked for, and neither of them is Servury.
