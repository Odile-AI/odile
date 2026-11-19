# ADR-004 — The copy that outlives HQ: restic to Cloudflare R2 now, Backblaze B2 when the account returns

**Status:** Proposed (re-cut) · 19 November 2026 · Architect. **The 17 November
2026 cut named Backblaze B2 in Canada East as the destination; this re-cut makes
Cloudflare R2 the interim destination because Savio's B2 account was suspended at
signup, and keeps B2 as the target for the day the account is restored.** Savio
directed the **shape** on 17 November 2026 — the copy lives at a third provider,
neither the tenant's machine nor HQ, Odile pays that provider, and the
subscription price carries it — and on 19 November 2026 he directed the
**interim**: use Cloudflare "to unblock R&D", **never modify or delete HQ's
existing R2 backup**, and **never exceed the free limit (aim for 9 GB total)**.
The **provider, the account, the region and the two standing credentials on a
tenant** remain his to approve (`docs/AGENT-ORG.md` §3: a new provider; a
standing credential on a tenant, default no).
**Answers:** ODL-570 — "which shape does the backup take, which third provider
keeps the copy, and how is a whole machine restored when the instance is already
gone?" — re-cut by ODL-915.
**Short answer:** keep **restic** and keep the **user's password**; move the
destination off HQ to **Cloudflare R2** — one bucket per tenant named
`odile-t-<slug>`, in the account's default jurisdiction, with two bucket-scoped
keys (a read-write one on the guest and a **read-only** one in the user's
recovery file) — **until the Backblaze B2 account is restored**, at which point
the same repository shape moves to **B2, Canada East (Toronto)** with no change
to the program, the password or the restore path. A whole machine is restored by
rebuilding the image and having the user **paste the recovery file into one page
on the new machine**: one paste, one new sign-in, no CLI, no operator key, and
nothing at HQ can open the repository. Cost: **$0.00** at the measured repository
size (1.017 GB), inside R2's 10 GB-month free tier; above it, $0.015/GB-month.
The B2 target price stays **$0.01 per tenant per month** at the measured size;
the plan's existing $0.60 line covers 86 GB per tenant and does not move.

### Change log

| Date | Cut | What changed | Why |
|---|---|---|---|
| 17 November 2026 | v1 | restic → **Backblaze B2, Canada East (Toronto)**; R2 rejected because it shares the Cloudflare account with `odile.fyi` DNS and HQ's only backup | Savio's shape; the lowest-risk third provider |
| 19 November 2026 | v2 — this re-cut | Destination re-cut to **Cloudflare R2 interim**: `odile-t-<slug>` buckets, default jurisdiction, bucket-scoped keys, 2 GB per tenant, at most 3 tenants, a 9 GB account cap and alert; **B2 retained as the target**; R2's shared fate accepted and time-boxed | Savio answered `t_02b8df9d`: the B2 account was suspended at signup; unblock R&D without touching HQ's backup or the free tier |
| 19 November 2026 | v2.1 | Review corrections, no decision change: the `git grep` proof names all three Markdown files that carry the bucket name; the bucket-name rule scopes the hex pattern to paying tenants and lists `odile-t-odl-bench`; the three buckets are named by slug, so the demo `odl-7971a8` is counted once; installer line anchors refreshed | Round-1 review: an evidence row must reproduce at the head it ships with |

**Related:** `docs/MVP-V0.1.md` §6.1, §6.7, §8, §12 (D19); `docs/CLAIMS.md` §1
(Access 65–66, Money and place 122–123, Reliability 201); `docs/architecture/ADR-003`
(`ADR-factory-backup.md`, the factory's copy — restic to Cloudflare R2) and its
§4.3 (the shared-fate argument this interim accepts for a fixed period);
`docs/runbooks/backup.md` §1–§9; `docs/runbooks/offboard.md` §4–§5;
`docs/RESTORE.md` (HQ's own restore, not this); cards ODL-215, ODL-218,
`t_fec9f699`, `t_8eb17a54`, `t_ce52bb8d`, `t_9d6fb595`, `t_02b8df9d`, ODL-570,
ODL-915.
**Decision owner:** Architect for the mechanism, the include set and the restore
path; **Savio for the provider, the account, the region, the interim and the
credentials** (§3 of the authority matrix). Product lands any sentence that moves
(`docs/CLAIMS.md`) — this ADR proposes one and changes none.

---

## 1. The invariant at risk

Five sentences the product depends on: four are internal invariants, and the
fifth is a deck sentence still **held from the public pages** (`docs/CLAIMS.md`
§1a, line 219).

1. **The copy survives the machine.** *True, drilled.* ODL-215's nightly restic
   snapshot and a destroy-and-restore were measured on 2026-11-12 (§2).
2. **The copy survives HQ.** **False today, and the interim only half-fixes it.**
   The repository's destination is
   HQ's own disk: `backup.odile.fyi` does not resolve, and the drill wrote to a
   dedicated chrooted `sshd` on HQ's port 2222, under `/srv/odile-backup/tenants/<slug>`
   (`docs/runbooks/backup.md` §3). HQ is itself one Servury guest: the address
   behind `mail.odile.fyi` — the public tree writes it as `203.0.113.139`
   (`docs/ops/PUBLIC-TREE.md`:85) — resolves to **AS395904 Servury, Montréal**,
   the same provider and the same city as the tenant machines. One Servury
   suspension, one dead host or one billing failure loses the tenant's machine, the
   control plane and the only copy at the same moment. **This is the invariant the
   card exists for**, and it is Savio's sentence of 17 November 2026 restated.
   The 17 November cut answered it with B2; the 19 November interim answers the
   **host** half with R2 — a different provider from Servury and from HQ's box —
   while accepting that the **account** half is still shared with Cloudflare's DNS
   and HQ's own backup. That shared fate is Savio's explicit interim call,
   bounded by §4's two constraints and ended by the B2 cutover; it is a
   time-boxed exception, not the destination's final shape.
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
   practice.* `docs/MVP-V0.1.md` §6.7 and `docs/CLAIMS.md`:123 promise a download
   "before" the 30 days; nothing today gives the user a credential that reaches
   the bytes. They hold a password and a repository URL, and an SFTP account only
   the guest can authenticate to.
5. **"Backups are off-site."** *In the deck, held from the page.*
   `docs/CLAIMS.md`:122 carries it and §1a line 245 holds it; it lands with
   `t_5f10dd14`. No page quotes the clause — `web/faq.html:131` has only "The
   machine is in Montreal, Canada." — but one page carries the *substance* in a
   different sentence: `web/privacy.html:149` lists the nightly backup's
   destination as "an off-site SFTP storage box". That is wrong twice today (the
   bytes are on HQ's disk, and the box was never bought) and stale after this ADR
   (the destination becomes an R2 bucket now and a B2 bucket at cutover, not
   SFTP). Both surfaces are Product's to land; **this ADR publishes nothing**. And
   the deck sentence is not true in fact at this head: the bytes are on HQ's
   disk, in the same provider and the same city as the machine they are supposed
   to survive.

**What this ADR preserves:** the shape (restic), the password (guest-generated,
user-held, never at HQ), the nightly timer, the status record and the heartbeat,
and every measured number's meaning. **What it changes:** the destination, the
credential that reaches it, and the path a rebuilt machine takes to its own data.
**What the 19 November re-cut changes:** only the destination — R2 for a bounded
interim, B2 as the target — and with it the shared-account risk the 17 November
cut rejected, accepted for a fixed period under two hard constraints. **What it
must not do:** add an operator key, an escrow, or a recovery account; touch HQ's
`hermes-hq-backup` bucket; exceed 9 GB of R2 storage; or sell a sentence the deck
does not already own.

## 2. What exists today, measured

The demo tenant `odl-7971a8` — the runbooks' "live tenant", and the only
machine with a measured repository. Nothing in this table is a projection; each
line is a measurement with a date and a source.

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
   `installer/odile_install.py:12210–12230`), which for a tenant is
   `/home/tenant` (`Layout.tenant`, `:10119`; `DEFAULT_HOME`, `:580`;
   the image runs the installer with `--user tenant`,
   `deploy/image/odile-release-setup.sh:169`) — the same string the live unit
   the drill measured names (`backup.md` §2.1). The `/home/odile` string in
   the tree is `installer/first_visit.py`'s standalone `DEFAULT_ENV` (`:114`),
   which the unit overrides with `--env-file` (`:9315`) filled from the tenant
   layout (`installer/odile_install.py:12295`, and the simulation's launch at
   `:12490`). So today the two
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
that) with the rates in §4: R2 at $0.015/GB-month above a **shared** 10 GB-month
free tier that already holds HQ's 1.15 GB backup; B2 at $6.95/TB-month =
$0.0068/GB-month.

| | **A. restic repository + password** | **B. one encrypted archive a night** | **C. user-driven download only** |
|---|---|---|---|
| What the provider holds | ciphertext (restic: AES-256-CTR + Poly1305-AES, scrypt KDF), object sizes, access times | one opaque blob per night; same visibility, no dedup so 11 blobs for 7 daily + 4 weekly | the archive, for as long as the download window is open |
| The secret the user holds | a 32-character repository password (the recovery file) | an age/tar passphrase (or an identity file) — the same shape in the user's hands | the passphrase, **and the file itself** |
| What Odile can do | delete, withhold, or write garbage (the bucket key); never forge a snapshot restic will accept (no password) | same | same, plus delete the copy the user never downloaded |
| Restore machinery | `restic restore` on the guest; partial restore and `restic check` exist | `age -d \| tar -x` — no partial restore, no integrity check, and a torn `state.db` unless the same WAL-safe staging is rebuilt by hand | the user's laptop must still have the file; no machine-side machinery at all |
| Nightly cost of a run | only the delta (measured: 27.3 s for 3.14 GB on the first run, seconds after) | a full 2–2.5 GB upload every night | none until the user asks |
| Cost per tenant / month | **$0.00** at the measured 1.017 GB (inside the shared 10 GB-month free tier); **≈$0.09** at a 15 GB envelope beside HQ's 1.15 GB | **≈$0.23** at 11 full archives ≈ 24 GB | **$0.00** (served from the guest) |
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

The shape is settled; the destination is the card's second question, and after 19
November 2026 it has **two answers in sequence**: Cloudflare R2 for the interim
and Backblaze B2 for the target. Savio's constraint rules out the tenant machine
and HQ, so the candidates are third providers. Prices are list prices read on 17
November 2026 (B2, Hetzner) and 19 November 2026 (R2), from the vendors' own
pages where marked.

| | **Cloudflare R2 — interim** | **Backblaze B2, Canada East — target** | Hetzner Storage Box BX11 |
|---|---|---|---|
| Protocol and path | S3 API over HTTPS/443 at `https://<account_id>.r2.cloudflarestorage.com`, bucket region `auto`; restic's `s3` backend | S3 API over HTTPS/443 at `s3.ca-east-006.backblazeb2.com`; restic's `s3` backend | SFTP on **port 23**, hostname `your-storagebox.de`, one username per sub-account; restic's `sftp` backend |
| Where the bytes are | **Default jurisdiction**, Cloudflare's choice of location; `locationHint=enam` asks for Eastern North America and is a **best effort, not a guarantee** — not guaranteed to be in Canada | **Toronto, Ontario** — `ca-east-006`; the region is chosen at account creation and **cannot be changed** | Germany (FSN1) or Finland (HEL1) only |
| Capacity and cost at our size | **10 GB-month free across the whole account**, then $0.015/GB-mo; free egress; 1 M Class A / 10 M Class B operations a month free; **no per-bucket cap** — the 9 GB ceiling is ours (§4.1) | no minimum; **first 10 GB free**; $6.95/TB-mo; egress free up to 3× stored; free transactions | 1 TB flat, **€3.20/mo** (≈$3.58 at the rate `ADR-003` §4.2 already uses), unlimited traffic, no minimum term |
| Cost per tenant / month at 1.017 GB | **$0.00** — HQ's 1.15 GB and one 1.017 GB repository sit inside the shared free tier | **$0.01** (10 tenants = 10.17 GB ≈ the free tier, ≈$0.001 total) | **$0.36** at 10 tenants on one box ($3.58 ÷ 10) |
| Durability of the only surviving copy | 11 nines claimed (multi-device object storage) — **in the same account as HQ's DNS and HQ's only backup** | 11 nines claimed; 17 data + 3 parity shards across 20 storage pods | **RAID, "the data is not mirrored onto other servers"** — Hetzner's own FAQ |
| Per-tenant scope | one bucket per tenant, token restricted to that bucket: the token's resource string pins account, jurisdiction and bucket name; 100,000 buckets/account included; bucket names are unique **within the account**, so `odile-t-<slug>` needs no suffix | one bucket per tenant; app keys restricted to that bucket; 100 buckets/account; **bucket names are globally unique across all B2 accounts**, so the name is recorded in the tenant row, never derived | a sub-account per tenant, directory-scoped; **100 sub-accounts, 10 concurrent connections per box** |
| The credential the guest needs | a bucket-scoped **Object Read & Write** token (Access Key ID + Secret Access Key), **minted by Savio in the Cloudflare dashboard and delivered in the seed** (0600 `/etc/odile/backup-s3.env`); read-only twin in the recovery file | an S3 application key **minted by HQ from the account master key and delivered in the seed** (0600 `/etc/odile/backup-s3.env`) | an SSH key **generated on the guest**, its public half registered on the box **after** the guest boots |
| New network path from a tenant | HTTPS/443 to `<account_id>.r2.cloudflarestorage.com` (the image already talks 443) | HTTPS/443 to one hostname | **port 23 out**, plus an SSH client, a pinned host key and a `known_hosts` file |
| Shared fate with today's copies | **the same Cloudflare account that holds `odile.fyi` DNS and HQ's only backup** — accepted for the interim under C1/C2, ended by the B2 cutover | none: not Servury, not HQ, not Cloudflare | none |
| Verdict | **Interim, chosen by Savio on 19 November 2026** | **Target; blocked on the account being restored** | Rejected |

**Savio's two constraints bound the R2 row, and they are requirements, not
goals: C1 — HQ's `hermes-hq-backup` is never modified or deleted; C2 — total R2
usage stays at or below 9 GB.** The design makes both structural (§4.1, §4.2).

**Why R2 for the interim.** On 19 November 2026 Savio answered `t_02b8df9d`:
the Backblaze account was suspended at signup, so B2 cannot be bought today. The
demo tenant's only surviving copy is still on HQ's disk (§1 item 2), which is the
invariant this card exists for; waiting for B2 leaves it false. R2 is already
enabled on the account that carries the DNS zone, already has a payment method on
file, and speaks the **same restic `s3` backend** as B2 — so the program, the
`restic` command, the password, the retention and the restore path are identical
at both destinations, and the B2 cutover changes an endpoint, a region and a key,
not code. What the 17 November cut rejected R2 **for** is real and is not denied:
it shares one Cloudflare account with the DNS zone and HQ's only backup. Savio
accepted that risk for a **bounded interim**, under the two constraints below,
and the B2 cutover ends it. The interim buys the one thing that cannot wait: the
tenant's copy stops sharing a fate with HQ's host.

**Why not the alternatives for the interim.** The SFTP stand-in on HQ fails the
invariant outright (it is the thing being replaced) and needs a human to register
each guest's public key. A Hetzner Storage Box has the durability and
connection-limit problems the 17 November cut named, plus port 23. A second
Servury box shares the provider whose suspension is the failure being insured
against. All three are slower to stand up than a bucket on an account that is
already live.

**The two constraints, and how the design makes them structural.**

- **C1 — HQ's existing R2 backup is never modified or deleted.** HQ's
  `hermes-hq-backup` bucket holds HQ's only off-site copy. The interim adds **no
  account-wide write credential at HQ**: the only R2 credentials HQ's software
  holds are the existing bucket-scoped token for `hermes-hq-backup` and one
  bucket-scoped token per tenant. Tenant buckets live in a disjoint name
  namespace (`odile-t-*` vs `hermes-hq-backup`) and in a **different
  jurisdiction endpoint** (`<account_id>.r2.cloudflarestorage.com` vs
  `<account_id>.eu.r2.cloudflarestorage.com`), so a tenant token's resource
  string cannot name HQ's bucket even by accident. Bucket creation and token
  minting are **dashboard steps by Savio**, not an API call from HQ; the HQ
  backup job and its runbook are untouched. §4.2 is the readback and the proof.
- **C2 — total R2 usage stays at or below 9 GB.** The cap is
  **9,000,000,000 bytes (9 GB decimal, 8.38 GiB)** — one gigabyte below R2's
  shared 10 GB-month free tier, which already carries HQ's 1.15 GB. The
  per-repository envelope is **2.0 GB** and **at most three buckets in total —
  the demo `odl-7971a8`, the bench `odl-bench` and the first paying tenant**,
  so the total is bounded by construction:
  `1.15 GB (HQ) + 3 × (2.0 GB + 0.2 GB un-pruned delta) ≈ 7.75 GB`, leaving
  ~1.25 GB of headroom. A **fourth bucket — the second paying tenant — is the
  signal that B2 must land**; the cap is arithmetic, not a hope. §4.1 names the
  enforcement and the alert.

### 4.1 The R2 layout

**Bucket naming.** `odile-t-<slug>` (the `t` is tenant) — for example
`odile-t-odl-7f3a2c` — where the slug is the machine's existing `odl-…`
identifier: a paying tenant's is `odl-` plus six hex characters, the demo's is
`odl-7971a8`, and the bench's is `odl-bench`. A paying tenant's bucket
therefore matches `^odile-t-odl-[0-9a-f]{6}$`; the bench is the one name
outside that pattern — `odile-t-odl-bench`, listed here rather than derived
(`deploy/image/firstboot-fixture.py:50`). Every name fits R2's 3–63 lowercase
letters/digits/hyphens rule, is unique within the account, and **cannot equal
`hermes-hq-backup`**. It is
recorded in `tenant_state.detail` (`backup_bucket`, `backup_token_id`,
`backup_readonly_token_id`) — the existing JSON column, so no schema migration —
and is never derived from a user-supplied string. `detail` is the cache, not the
store of record: `reopen` (ADR-007) rewrites it on a destroy→rebuild, so the
durable copy is `/root/.odile/r2/odile-t-<slug>.env` (0600), and the
reopen path re-reads it and re-records the three keys. The repository URL is
`s3:https://<account_id>.r2.cloudflarestorage.com/odile-t-<slug>/repo`. The
final `repo` segment is the restic repository's prefix inside the bucket: one
bucket holds one tenant's objects, and a fresh start without a restore (§7.4) is
a new `repo-<4 hex>` prefix in the **same** bucket — no new bucket, no dashboard
step — with the old prefix kept 30 days and then emptied. The account id is an
address, not a secret, but it now appears in the user's recovery file.

**Jurisdiction, endpoint and location.** Tenant buckets use the **default
jurisdiction** with `locationHint=enam` (Eastern North America, the closest
hint to Montréal); R2 does not offer a Canadian jurisdiction, and a hint is best
effort. The jurisdiction is fixed at bucket creation, which is acceptable
because these buckets are disposable at cutover. HQ's bucket stays EU-jurisdiction
and its own endpoint. **Consequence to state, not hide:** under the interim a
tenant's ciphertext may sit outside Canada; the B2 Canada East cutover restores
the "same country as the machine" property the 17 November cut chose.

**Two keys per bucket, both bucket-scoped.** Savio creates, in the dashboard, an
**account-owned token** (an Account API token) with **Object Read & Write** and a
second with **Object Read only**, each restricted to the one `odile-t-<slug>`
bucket (Cloudflare's access-policy resource is
`com.cloudflare.edge.r2.bucket.<ACCOUNT_ID>_default_<BUCKET>`; the permission
groups are `Workers R2 Storage Bucket Item Write` and `… Item Read`). The
read-write pair goes into the seed and lands at `/etc/odile/backup-s3.env`
(0600 root) with the endpoint, the bucket and `AWS_DEFAULT_REGION=auto`; HQ keeps
a copy at `/root/.odile/r2/odile-t-<slug>.env` (0600) because a rebuild
has to re-deliver it. The read-only pair goes into the seed too — the guest
renders the recovery file at install — and from there into the user's file; the
backup job never uses it. No token is account-wide: an Object Read & Write token
cannot create or delete a bucket, and it cannot name another bucket.

**The 9 GB cap and the alert.** Three legs, in order of strength:

1. **Per-tenant ceiling on the guest (hard).** `backup.py run` reads
   `R2_TENANT_CAP_BYTES=2000000000` from `/etc/odile/backup-s3.env`, runs
   `restic stats --mode raw-data` **after prune**, and writes
   `status: over_cap` — with the size and the cap — when the repository exceeds
   it. A run that starts over cap does not upload; growth stops at one night's
   delta above 2.0 GB.
2. **Bucket count on HQ (hard).** The provisioning path (`odilectl provision`,
   the bench and demo deploy) counts the `odile-t-*` env files under
   `/root/.odile/r2/` and **refuses a fourth** bucket while three are recorded;
   it then verifies with `HeadBucket` that the new bucket answers and that the
   tenant token gets no 200 from `hermes-hq-backup`. Those two checks are the
   acceptance test for each bucket.
3. **The account readback and alert (observability).** A daily HQ job
   (`odilectl backup-usage`) lists `hermes-hq-backup` with its existing token and
   every bucket with an env file under `/root/.odile/r2/`, using that bucket's
   own token, sums the
   object payload sizes, writes `/var/lib/odile/backup/usage.json`, and feeds
   the existing watchdog: **warn at 8.0 GB, critical at 9.0 GB**. The listing is
   the authoritative number; Cloudflare's own alerts are not used — its
   usage-based notification is a paid-plan, **dollar**-threshold alert, and R2
   spend is $0.00 below 10 GB, so a dollar alert would never fire before the cap
   did. The arithmetic bound above is what makes the cap hold when the alert is
   late; the alert is how an unexpected growth gets noticed. **One blind spot,
   named:** a bucket Savio created but the tenant row does not yet record is
   invisible to this sum; the provisioning check refuses to proceed until the
   row, the env file and `HeadBucket` agree, so the blind spot lasts only between
   the dashboard click and the `odilectl` run.

**Operations are not the constraint.** A nightly restic run touches hundreds of
Class A operations (PUT/LIST) and a restore a handful of Class B reads; three
tenants at ~500 Class A a night is ≈45,000 a month against the **1,000,000**
free, and egress is free. The 9 GB storage cap is the only free-tier line the
interim can approach.

**Deletion and the 30-day exit.** An Object Read & Write token can empty a
bucket but **cannot delete the bucket** (bucket deletion is account-level). At
`keep_until` the purge empties `odile-t-<slug>` through the tenant's own token,
records `bucket_emptied_at`, and the empty bucket is deleted in the Cloudflare
dashboard — one manual click at day 30 with no data left in it. The promise
("kept 30 days after cancellation, then deleted") is unchanged; only the last
click moves from `odilectl purge` to the dashboard during the interim.

### 4.2 HQ's existing backup: the readback and the proof

The constraint is "never modified or deleted", so the proof is threefold: the
bucket's own state at this head, the absence of a credential that could reach it,
and the absence of a code path that names it.

**The readback, 19 November 2026.** Read-only, with the existing bucket-scoped
token, `ListObjectsV2` over `hermes-hq-backup` (EU endpoint) and
`restic snapshots --no-lock`:

| Fact | Value |
|---|---|
| Snapshot | **`8d5215719afbaa1c756fb63d9234050ad1003d6883346d2742349e742dead652`** at **2026-11-19T03:35:00Z**, host `oqwaclzm`, tag `hq` — one snapshot, as latest-only retention intends |
| Objects and bytes | **75 objects / 1,149,539,702 bytes**, across the restic prefixes `config`, `data/`, `index/`, `keys/`, `snapshots/` |
| Listing digest | `sha256` of the sorted `key<TAB>size<TAB>etag` listing (newline-joined, no trailing newline, ETag unquoted) = **`0298996b894a416afca8e163637808022140d48acd7fe513708f9a49b0767a49`**. It moves on the next HQ run **by design** — the HQ job is the only writer — so it is a baseline, not a frozen value |
| The HQ token's reach | `ListBuckets` → **`AccessDenied`**: the token is confined to its one bucket and cannot even enumerate the account |
| The DNS token's reach | `GET /accounts/<id>/r2/buckets` with the zone-scoped token → **401 Authentication error** (ADR-003 recorded a 403; the shape is the same — it cannot touch R2) |
| The code's reach | `git grep hermes-hq-backup` matches **only ADR-003, this ADR and `docs/runbooks/backup.md`** — every match is Markdown, and no code in the repo names the bucket; HQ's own backup tool reads `RESTIC_REPOSITORY` from `/etc/odile/backup.env`, and the tenant path uses `odile-t-*` |

**What could still delete it, stated plainly.** Savio's own dashboard login can;
so can a Cloudflare account suspension. Neither is new, and neither is
Odile-the-software. The interim adds no credential and no code that can name that
bucket, which is the strongest form C1 can take without a second Cloudflare
account.

**What Savio is being asked to approve, exactly.**

- **Keep the interim on Cloudflare R2** (his 19 November direction), and accept
  the **shared-account risk** for a bounded period, ended by the B2 cutover.
- **Create the tenant buckets and mint the tokens**, one at a time, in the
  dashboard: bucket `odile-t-<slug>`, default jurisdiction, `locationHint=enam`,
  no public access; then the two bucket-scoped tokens above. At most three during
  the interim. This is the manual step the interim accepts in exchange for
  holding **no account-wide token at HQ**.
- **Approve two standing credentials on a tenant** (§5): the guest's read-write
  bucket token and the read-only token in the recovery file. This is the same
  ask as 17 November; only the provider changed.
- **Approve the 9 GB cap** — 2.0 GB per repository, three buckets in total
  (the demo `odl-7971a8`, the bench `odl-bench`, the first paying tenant),
  alert at 8.0 GB — and the rule that the **fourth bucket — the second paying
  tenant — waits for B2**.
- **Keep the B2 account opening as the standing target.** When it is restored:
  open Canada East, save the master key at `/root/.odile/b2-backup.env` (0600,
  root), approve the same two standing credentials, and the migration paragraph
  in §10 runs. The region remains **the one one-way door** in this ADR.

**The board card for this question:** `t_02b8df9d` (lane `human`, assignee
`savio`, blocked), linked from ODL-570. Its 19 November answer is the interim
above; the cards in §8 wait for the bucket steps, not for B2.

## 5. The trust boundary this creates

**What Cloudflare holds under the interim:** ciphertext, the repository's size
and object count, the bucket name (`odile-t-<slug>` — the slug is already public
in the URL), the times of access, and the fact that a machine writes nightly.
**What it does not hold:** the password, the plaintext, file names (restic
encrypts them), or any way to produce a snapshot restic will accept. It can
delete or withhold; it cannot forge. **What is new and uncomfortable:** the same
Cloudflare account also holds `odile.fyi` DNS and HQ's only backup, so one
account suspension or one compromised login reaches all three. That is the
shared-fate argument ADR-003 §4.3 made, accepted here for a bounded interim
(§4, C1/C2) and ended by the B2 cutover. **What B2 will hold at the target:** the
same list, and nothing more, in a different account, a different provider and
Toronto.

**What HQ holds, and what it can do.** For the interim, **no R2 master key**: HQ
holds its existing bucket-scoped token for `hermes-hq-backup` and each tenant's
bucket-scoped read-write token (root, 0600, at
`/root/.odile/r2/odile-t-<slug>.env`), because the seed that provisions
the guest has to carry it and a rebuild has to deliver it again. That means HQ
**can delete, and can write garbage into, any tenant's repository** — and cannot
read one byte of plaintext, because the password is the user's and is not in the
seed. It could already delete any tenant's machine through the Servury API, so
this does not widen the availability power; it does add "can overwrite", which is
worth stating: an operator who wanted to destroy a backup could, and an operator
who wanted to read one could not. **No HQ credential can reach
`hermes-hq-backup`**; under B2 the same paragraph holds with one master key
(`/root/.odile/b2-backup.env`) instead of the bucket tokens.

**The rules this amends, named.** `AGENTS.md` line 61 says HQ stores no tenant
passphrase, private key or Webapp password — "Only the public key and a hashed
setup token." A bucket-scoped token is none of those three: it is not a key *to*
the machine (no door in), it opens one bucket of the tenant's own ciphertext, and
it has to be in the seed because the guest cannot mint it. **`docs/MVP-V0.1.md`
§6.1 invariant 5 is stronger and is amended in the same way:** "Tenant backup
keys never reach HQ at all" is false under this design — the bucket token does
reach HQ, because a rebuild has to re-deliver it. What stays true is the
invariant's *purpose*: **the password never reaches HQ, so HQ cannot read a
backup.** This ADR amends the letter with Savio's yes (§4) and names the stale
sentence; `docs/MVP-V0.1.md` is outside this card's `touches`, so it lands with
the provisioning card (§8), not here. The recovery file's read-only key is the
same class of credential, held by the user; it also sits in the seed so the
guest can render the file, which gives HQ nothing the read-write token does not
already give.

**What the guest holds.** `/etc/odile/backup-s3.env` (0600 root): the endpoint,
the bucket, `AWS_DEFAULT_REGION=auto`, the tenant cap, and the read-write
**Object Read & Write** token, whose capabilities are exactly read, write, list
and delete objects **on that one bucket**. It cannot create or delete a bucket,
cannot reach another bucket, and cannot name `hermes-hq-backup` (different name,
different jurisdiction resource). It is a credential **to the tenant's own
bucket, outbound only** — it opens no door *into* the machine and no other
tenant's bucket, and the machine is already root over itself. It is the same
class of thing as today's guest-generated SFTP key, except that Savio mints it in
the dashboard instead of the guest. Under `docs/AGENT-ORG.md` §3 this is "a
standing credential on a tenant" and needs Savio's yes; it is granted on the
argument that it is a way *out* to the tenant's own ciphertext, not a way *in*.

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
Repository:          s3:https://<account_id>.r2.cloudflarestorage.com/odile-t-odl-7f3a2c/repo
Read-only key:       <Access Key ID>:<Secret Access Key>
Certificate SHA-256: <the certificate the browser saw>

This file is the only copy of the backup password. ...
```

With those three things — the password, the repository and the read-only key — a
user, or their lawyer, or their next provider, can pull the repository with
`restic -r <Repository> --no-lock snapshots` and `restore`, on any machine with
restic and the two `AWS_*` variables set, **without Odile, without HQ and without
a support grant**. (The endpoint is the one for the bucket's jurisdiction — the
default form is `<account_id>.r2.cloudflarestorage.com`, HQ's EU bucket uses
`<account_id>.eu.r2.cloudflarestorage.com` — and it is recorded in the tenant row
with the bucket, not guessed by the guest.) That closes invariant 4, and it makes
the 30-day exit a thing the user already has rather than a thing Odile promises to
build. It also makes `docs/CLAIMS.md`:65's "the file opens your backup" literally
true instead of figuratively true, which is a **stronger sentence than the deck
owns** and goes to Product and Savio (§9).

**What the read-only key deliberately cannot do:** write, delete or lock. A
recovery file that is printed, emailed to a new laptop or dropped in a password
manager cannot destroy the repository it opens. The read-write key never leaves
the guest (and the seed).

**A grant gains nothing new, which is why no access claim moves.** A support grant
could already read `/etc/odile/recovery.txt` (`docs/CLAIMS.md`:115 says so) and the
bucket token beside it, so a grantee could already copy the repository's
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
| 2 | the machine | the signed image boots and the installer runs zero-touch; the seed carries the endpoint, the bucket, the read-write token and the tenant row's **previous instance id**, so the page knows this is a rebuild from the row and not from an operator flag somebody forgot | included above |
| 3 | the user | opens `https://<slug>.odile.fyi/first-visit?t=<token>` from the "your machine is back" email and **pastes the whole recovery file** into one textarea | under a minute |
| 4 | the machine | the first-visit service parses `Backup password:` with the installer's own grammar (`RECOVERY_PASSWORD_RE`), writes `/etc/odile/recovery.txt` (0600) **regenerated for this machine** — same password, new `Instance:`, new `Certificate SHA-256:`, the repository and the read-only key — starts `odile-restore.service`, and the page polls a text status endpoint with a meta refresh (no JavaScript) | — |
| 5 | the machine | `odile-restore.service`: stop the backup timer and the agent unit, `backup.py restore latest --target / --include <home>`, start the agent unit, run one `backup.py run` so a fresh snapshot exists immediately, re-enable the timer, write the status. The timer is stopped first so a 03:00 run cannot snapshot a half-restored home; the restore's own run is the newest snapshot either way | repo ÷ throughput; the measured repository is **1.017 GB** |
| 6 | the user | the page says "Restored snapshot `<id>` of 2026-11-17T03:00Z — 156,311 files, 3.14 GB"; they set a **new sign-in** (the page's normal step) and sign in | under a minute |
| 7 | the user | checks one thing they care about — a file, the last brief, the agent's memory of yesterday — and the page's own report names the snapshot and its time | a minute |
| 8 | HQ | records the rebuild on the row: snapshot id, restore time, size; the next 03:00 run lands under the same `--host <slug>`, so the retention group stays one | — |

**The transfer term, with its assumption stated.** The measured 38m03s restored a
3.630 MiB home from a repository on the same host, so it says nothing about a
3 GB home from a third provider. The repository is **1.017 GB**; at 5 MB/s that is
**3.5 minutes**, at 20 MB/s it is **55 seconds**. The interim path was measured
on 19 November 2026: this box — a Servury VM in Montreal, the same provider as a
tenant — pulled a 17,544,671-byte data pack from the R2 bucket in **1.293 s
(13.6 MB/s, single stream)**; restic runs several transfers in parallel, so the
restore's download term for a 1.017 GB repository is **≈75 seconds at that
single-stream rate**. The end-to-end estimate is therefore **15–25 minutes**,
comfortably inside the published hour — but the number that replaces 38m03s has to
come from the drill in §8, not from this paragraph.

### 7.3 The user's four actions, in one paragraph

Open the link in the "your machine is back" email; paste the recovery file into
the box; wait for the page to say which snapshot came back; set a new sign-in and
look at one thing that matters. No terminal, no command, no SSH key, no chat
message containing a secret.

### 7.4 The branches, and the failures this shape has to answer

| Branch or failure | What happens |
|---|---|
| **Wrong password** | restic exits 12 (`wrong password or no key found`); the page says "that file does not open your backup", the home is untouched, the paste can be retried. A file from another tenant is caught by its own `Tenant:` and `Repository:` lines before restic runs, and a truncated paste fails at the page — the installer's `parse_recovery` grammar is reused, so a bad file never reaches a restic call |
| **The user lost the recovery file** | the page offers "start fresh without a restore": the guest mints a new password into a new recovery file, uses a new repository prefix (`repo-<4 hex>`) in the **same** bucket, and the old prefix is kept 30 days and then emptied (`t_9d6fb595`); the bucket, and therefore the manual dashboard step, does not change. The page says, in one sentence, that the old data is unrecoverable without the old file — which is the promise, not a bug |
| **The restore is interrupted** | `odile-restore.service` is a oneshot and the page re-offers the step; restic restore overwrites into the same target, so a second run converges. The backup timer stays stopped until a run completes, and if the user never comes back the 36-hour freshness watchdog is the signal. Nothing else in the machine depends on it, so the user is never blocked |
| **The process is killed mid-write** | `state.db` is overwritten under a live gateway — the torn-database failure `docs/RESTORE.md` §2 already refuses. The unit stops the agent first and starts it after; the drill asserts the session list is readable afterwards |
| **The nightly timer fires before the user pastes** | The backup **service** carries `ConditionPathExists=/etc/odile/recovery.txt`, and on a rebuild the installer **does not create that file**: `ensure_recovery` (`installer/odile_install.py:10988`) skips when the seed carries the previous instance id **and the file is absent**, because a freshly minted password would not open the old repository. So a rebuilt-but-not-yet-restored machine's timer fires, the condition fails, the service is skipped silently, and no false "backup failed" reaches the watchdog. The paste writes `/etc/odile/recovery.txt`, and `odile-restore.service` runs one snapshot before the next 03:00. Once the file exists, a later installer run keeps its password and refreshes only the instance and certificate lines, exactly as today. On a **fresh** tenant there is no previous instance id, the installer mints the recovery file at install as today, and the first backup is not suppressed |
| **A password typed into the chat instead of the page** | it travels to NEAR AI's TEE in the prompt **and** lands in `state.db`, which is inside the backup it opens. The restore step is a page POST for exactly this reason, and the help text says so |
| **The destination is down, or the bucket is over the 9 GB cap** | the restore waits and retries; the machine is usable without it. A failed upload is a `status: failed` heartbeat, and a repository over `R2_TENANT_CAP_BYTES` is `status: over_cap`; the freshness watchdog reports either. The cap never silently deletes: the last good snapshot stays until an operator prunes or moves the tenant to B2 |
| **HQ is gone** | the bucket, the user's recovery file and the signed image are enough to rebuild the machine by hand on any provider; what is lost is the convenience of `odilectl`, not the data. This is the first time that sentence has been true |

## 8. What this changes in the code — cards, not prose

The ADR decides; the board builds. Each row is one card. The provisioning card
and the drill are the two that make the destination worth paying for.

| Card | What it must do | Depends on |
|---|---|---|
| **Provisioning — interim (R2)** | For each of at most three buckets (the demo `odl-7971a8`, the bench `odl-bench`, the first paying tenant): record the bucket Savio created (`odile-t-<slug>`, default jurisdiction, `enam`), verify with `HeadBucket` that it answers **and** that the tenant token gets no 200 from `hermes-hq-backup`, use no credential above the bucket, write `/root/.odile/r2/odile-t-<slug>.env` (0600) and the row's `detail` (`backup_bucket`, `backup_token_id`, `backup_readonly_token_id`; the env file is the store of record and the reopen path re-records `detail` from it), and put the endpoint, bucket, read-write token, the read-only pair and `R2_TENANT_CAP_BYTES` in the seed **with the previous instance id** (empty on a fresh tenant; the rebuild marker §7.4 reads). Refuses a fourth `odile-t-*` bucket. **Retires `--backup-password-file`** and with it the sealed password in `seed-rebuilds.json` | Savio's bucket/token steps (§4; `t_02b8df9d`) |
| **`backup.py` + installer** | an `s3:` repository: load `/etc/odile/backup-s3.env` into restic's environment (never argv), keep the SFTP branch for one release, enforce `R2_TENANT_CAP_BYTES` with `restic stats` after prune (`status: over_cap`), add `ConditionPathExists=/etc/odile/recovery.txt` to the backup **service**, and **do not mint a recovery file on a rebuild** — `ensure_recovery` skips only while the file is absent and the seed carries the previous instance id; once the paste wrote it, a later run keeps the password and refreshes the certificate line. That is the suppression which makes the condition true before the paste (§7.4). Update `BACKUP_SHA256` and re-sign the installer | the seed fields |
| **The 9 GB readback and alert** | `odilectl backup-usage`: list `hermes-hq-backup` (with `/root/.odile/r2-backup.env`) and every bucket with an env file under `/root/.odile/r2/`, using that bucket's own token, sum payload sizes, write `/var/lib/odile/backup/usage.json`, and feed the watchdog at **8.0 GB warn / 9.0 GB critical**. No account-wide credential: the per-bucket tokens are enough, and the HQ bucket's readback is the daily proof C1 still holds | the provisioning card |
| **First visit** | the rebuild step: detect the previous instance id in the seed, ask for the recovery file before the sign-in step, write `/etc/odile/recovery.txt`, run `odile-restore.service`, show the snapshot and its time (no JavaScript) | `backup.py`'s `restore` (already supports `--target /` and `--include`) |
| **Recovery file** | two new lines (the repository URL and the read-only key) and the parse that tolerates them; the `first_visit.py` copy of the grammar stays the one source | the provisioning card |
| **The drill** | destroy → rebuild → paste → restore against the **real R2 bucket**, with `state.db` and `cron/jobs.json` asserted, and both clocks stated (the lesson of ODL-215's two T0s). This replaces 38m03s and decides whether "under an hour" stays | all of the above |
| **The exit** | `t_9d6fb595` grows teeth: at `keep_until`, **empty** the bucket through the tenant's own token, record `bucket_emptied_at`, and have Savio delete the empty bucket in the dashboard (an Object Read & Write token cannot delete a bucket); before it, the user already holds the key and the URL | provisioning |
| **The demo and bench repositories** | one command in the Webapp terminal (`backup.py init` against the new R2 URL, then `run`) for the demo and the bench; the HQ `sshd` on 2222 stays 30 days and is then deleted. **This card replaces `t_8eb17a54`'s purchase**: that card becomes "delete the stand-in", not "buy a storage box" | the drill |
| **The B2 cutover** | the paragraph in §10, run once when the B2 account is restored: `restic copy` each R2 repository into its B2 bucket, prove a restore, re-issue the recovery file, keep the R2 bucket 30 days, delete it. Automation of bucket/key minting returns with the B2 master key | Savio's B2 account (§4) |
| **The same-slug rebuild** | `t_ce52bb8d`. Without it the rebuilt machine has a new URL, and the user's recovery file names a tenant that no longer exists | — |

## 9. Claims

| Sentence | Today | After this ADR |
|---|---|---|
| "Backups are off-site." (`CLAIMS.md`:122) | **in the deck, held from the page** (`CLAIMS.md` §1a line 245, `t_5f10dd14`); not true in fact — the bytes are on HQ's disk | **true in fact when the migration card (§8) runs**: a bucket at Cloudflare R2, a different provider from the machine and from HQ (the same account as the DNS zone, which is the interim's accepted cost); the B2 cutover puts it in Toronto at a provider that shares nothing. The page sentence stays held until Product lands it — **this ADR does not publish it** |
| "The encrypted backup is kept 30 days after cancellation, then deleted. Odile can delete it, not read it." (`:123`) | true on the storage path; conditional during a rebuild | true on both paths, because the password never transits HQ: the rebuild is a paste on the machine and `--backup-password-file` is retired. On the interim the repository is emptied at `keep_until` and the empty bucket deleted in the dashboard (§4.1); the 30 days and the deletion are unchanged |
| "A recovery code and a recovery file are the way back: … the file opens your backup." (`:65`) | true through the agent | **literally true**: the file carries the URL and a read-only key, so it opens the backup on any machine. **This is stronger than the deck owns** — Product lands the wording, Savio approves it (§3, a published sentence not in `CLAIMS.md`) |
| "The recovery file holds the backup password and the certificate fingerprint." (`:66`) | true | it now also holds the repository and a read-only key; the sentence needs the extra clause |
| "Backed up nightly. Rebuildable in under an hour — without a key." (`:201`) | measured at 38m03s against a repository on the same host | **at risk**: the number must be re-measured against the real R2 bucket. The model says 15–25 minutes and the sentence probably survives; the drill decides, and if it does not, the sentence moves |
| `docs/MVP-V0.1.md` §6.7 "A restore needs the recovery file and one paste of its password in the browser" | already true at this head | unchanged by this ADR; it is the path §7.2–§7.3 describe |

Nothing here makes the *access* story stronger: no operator key, no escrow, no
recovery account, and the disk and the prompts are untouched. The one new claim
is about the user's own copy, and it is theirs to make.

## 10. Reversibility

**Two-way, with one exception.** Moving a repository is `--repo` on the next
`init`/`run` and a new seed value; the old bucket is kept 30 days and deleted
(`backup.md` §7). The program, the password and the restore command do not change.
The read-only key in a recovery file is additive: a user who never uses it is
unaffected, and a file without the two new lines still parses. The R2 interim is
deliberately the cheap direction of travel: its buckets are disposable, its
jurisdiction is fixed at creation but nothing depends on it after the cutover, and
no account-wide credential exists to unwind.

**The migration back to B2, in one paragraph.** When Savio's B2 account is
restored, per tenant and in this order: create the Canada East bucket
`odile-<slug>` (a different name from the R2 bucket, so the two coexist during
the overlap), `restic init` it with the **tenant's existing password** (so the
recovery file's password line stays valid), `restic copy --from-repo <the R2
repository>` to move every snapshot, run `restic check` and one restore drill,
then update the tenant row and have the machine re-render the recovery file with
the new `Repository:` URL and read-only key — the password line unchanged — keep
the R2 bucket for 30 days, and empty and delete it; new tenants go straight to B2,
`odilectl provision` resumes minting bucket and keys from the master key at
`/root/.odile/b2-backup.env`, and `BACKUP_DESTINATION` flips in
`/root/.odile/seed-config.json`. `restic copy` re-encrypts into the destination
repository's key, and because both repositories use the same password the user's
one secret opens both during the overlap.

**The exception is the account's region**, chosen at signup and unchangeable for
the account's life (Backblaze's own documentation). Getting that wrong costs a
second account and a re-upload, not the data.

**It stops being cheap** at the first tenant whose only surviving copy is a B2
bucket — i.e. at the first real restore or the first cancellation, whichever comes
first. After that, changing providers is a migration with a 30-day window, not a
config change. During the interim it stops being cheap at the first tenant whose
repository outgrows 2.0 GB, because the cap is a ceiling, not a suggestion.

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
- **The R2 bucket at this head, 19 November 2026** (read-only): `ListObjectsV2`
  over `hermes-hq-backup` at the EU endpoint → **75 objects / 1,149,539,702
  bytes** across `config`, `data/`, `index/`, `keys/`, `snapshots/`; the sorted
  `key<TAB>size<TAB>etag` listing (newline-joined, no trailing newline, ETag
  unquoted) hashes to
  `0298996b894a416afca8e163637808022140d48acd7fe513708f9a49b0767a49`;
  `restic snapshots --no-lock --json` → **one snapshot**
  `8d5215719afbaa1c756fb63d9234050ad1003d6883346d2742349e742dead652` at
  `2026-11-19T03:35:00.780240736Z`, host `oqwaclzm`, tag `hq`. The same token's
  `ListBuckets` → **AccessDenied**; the DNS token's
  `GET /accounts/<id>/r2/buckets` → **401 Authentication error**. `git grep
  hermes-hq-backup` matches only ADR-003, this ADR and
  `docs/runbooks/backup.md` — every match is Markdown, and **no code path**
  names the bucket.
- **The R2 transfer bound, 19 November 2026:** `boto3 get_object` of data pack
  `data/c8/c82f11ab…` (17,544,671 bytes) from this box → **1.293059 s**, i.e.
  13.57 MB/s single stream. The repo is 1.017 GB, so ≈75 s at that rate before
  restic's parallelism.
- Prices, read on 17 November 2026: Backblaze B2 $6.95/TB-month, first 10 GB
  free, egress free to 3× stored, no minimum duration (backblaze.com pricing
  page); Canada East = Toronto, `ca-east-006`, region fixed at account creation
  (Backblaze data-centres page, updated 2026-11-13); 11 nines and 17+3 erasure
  coding (Backblaze resiliency page); 100 buckets per account and bucket-scoped
  app keys (Backblaze S3-API and app-key pages). Hetzner BX11 €3.20/month, 1 TB,
  100 sub-accounts, 10 concurrent connections, Germany or Finland, "the data is
  not mirrored onto other servers" (hetzner.com Storage Box page and FAQ).
- **Cloudflare R2, read on 19 November 2026** (developers.cloudflare.com):
  pricing page updated 30 October 2026 — **10 GB-month free**, $0.015/GB-month
  Standard, 1 M Class A / 10 M Class B operations free, egress free; limits
  page — **data storage per bucket unlimited**, 100,000 buckets per account
  included; create-buckets page — names 3–63 characters of lowercase letters,
  digits and hyphens, and **delete requires emptying the bucket first**; data
  location page — `enam` is a location **hint**, best effort, and jurisdictions
  are the only residency guarantee; tokens page — **Object Read & Write** and
  **Object Read only** can be restricted to a set of buckets, and only
  `Workers R2 Storage Write` creates/deletes buckets. R2's account-level metrics
  endpoint (`GET /accounts/<id>/r2/metrics`) exists but needs an account token;
  the interim reads per-bucket listings instead, so no account-wide credential is
  held at HQ.
- *Modelled, not measured:* the 15–25 minute end-to-end estimate; the 2–2.5 GB
  size of a hypothetical full archive; the 15 GB envelope; every "at N GB" price
  except the 1.017 GB row, which is the measured repository × the list rate.

## 12. What this ADR does not do

- **No code, no tenant bucket, no B2 account.** The first honest sentence about
  this re-cut is that it is still a recommendation; the second is that Savio has
  to create the three buckets and mint the tokens before a tenant byte moves, and
  the B2 account is blocked at signup.
- **No new claim in the deck.** §9 names the one sentence that gets stronger; it
  lands through Product and Savio or not at all. The interim makes "Backups are
  off-site" true in fact before the B2 cutover, but the page sentence stays held.
- **No `docs/MVP-V0.1.md` edit** for §6.1's diagram, §6.7's "off-site SFTP box",
  §8's $0.60 line, §6.1 invariant 5 ("tenant backup keys never reach HQ") or
  `AGENTS.md`'s "off-site SFTP storage box": all five are outside this card's
  `touches` and are named in §8's card list instead of smuggled in here. **No
  `web/privacy.html` edit either**: its destination cell (line 149, "an off-site
  SFTP storage box") is wrong today and staler after this ADR; it lands with
  `t_5f10dd14` (§1 item 5).
- **No second copy.** One destination, one credential set, one drill — the same
  call ADR-003 §4.1 made for HQ, for the same reason: a second copy doubles what
  can be wrong at 03:00 and still needs a drill to prove it. The read-only key is
  not a second copy; it is the user's own way to make one whenever they like.
- **No `backup.odile.fyi`.** The name that never resolved is retired with the SFTP
  box, and the storage-box purchase in `t_8eb17a54` becomes "delete the stand-in".
  An R2 or B2 repository is addressed by the provider's endpoint, so the seed
  carries a URL with no DNS record of ours in it: one fewer name to own and one
  fewer name that can point at the wrong box.
- **No promise that the destination cannot lose the data.** B2 can (billing, abuse
  review, a regional outage); R2 can, and under the interim the same account
  holds the DNS zone and HQ's only backup. The residual risk is that the only
  surviving copy is a third party's, and the mitigation is the user's read-only
  key — a copy they can take at any time without asking.
- **No change to ADR-003.** HQ keeps its own Cloudflare R2 bucket, token and
  drill, and the factory's copy is untouched. During the interim the two copies
  live in the **same account** — the shared fate ADR-003 §4.3 named, accepted
  here for a bounded period; after the B2 cutover they live at two different
  providers again, which is the property Savio asked for, and neither of them is
  Servury.
