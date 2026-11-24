# ADR-004 — The copy that outlives HQ: restic over SFTP to a Hetzner Storage Box BX11

**Status:** Proposed (v3 re-cut) · 24 November 2026 · Architect. **The 17 November
2026 cut named Backblaze B2 in Canada East as the destination; the 19 November
2026 re-cut made Cloudflare R2 the interim and kept B2 as the target. Both are
superseded. On 24 November 2026 Savio chose the Hetzner Storage Box BX11, on a
new Hetzner account dedicated to backups, as the destination for all Odile user
backups** (card `t_a548cb0a`). **The box is ordered and active in Helsinki,
Finland (`hel1`) as `odile-backups` — id 709870, created 2026-11-24 06:25:41Z —
and holds no bytes yet.** He directed the **shape** on 17 November 2026 —
the copy lives at a third provider, neither the tenant's machine nor HQ, Odile
pays that provider and the subscription price carries it — and under
`docs/AGENT-ORG.md` §3 the **provider, the account, the region and the two
standing credentials on a tenant** are his to approve. He approved all four on
24 November 2026, and this ADR records what that buys, what it costs and what it
gives up.
**Answers:** ODL-570 — "which shape does the backup take, which third provider
keeps the copy, and how is a whole machine restored when the instance is already
gone?" — re-cut by ODL-915 (the R2 interim) and by this card (`t_8c2604c9`).
**Short answer:** keep **restic** and keep the **user's password**. Move the
destination to **one BX11 Storage Box** — 1 TB, **Helsinki, Finland (`hel1`)** —
reached over **SFTP on port 22** with restic's `sftp` backend. Each
tenant gets **two sub-accounts**, both confined to the box's `tenants/<slug>`
directory: **`odile-<slug>-rw`** (`readonly: false`; its private key is in the
seed and on the guest) and **`odile-<slug>-ro`** (`readonly: true`; its private
key is in the user's recovery file). A read-only sub-account can read, but it
**cannot upload, delete or modify**. HQ provisions the pair through the Hetzner
Console API with **one project-scoped token**; the main account's password never
leaves Savio's console. List price: **€3.20/month excl. VAT for the whole box**
(≈$3.58 at `ADR-factory-backup.md` §4.2's rate), **$0.36 per tenant at ten** and
**$0.07 at fifty** — and it is the **sub-account count, not the terabyte, that
caps a box at fifty tenants**. A whole machine is restored by rebuilding the
image and having the user **paste the recovery file into one page on the new
machine**: one paste, one new sign-in, no CLI, no operator key, and nothing at
HQ can decrypt the repository.

### Change log

| Date | Cut | What changed | Why |
|---|---|---|---|
| 17 November 2026 | v1 | restic → **Backblaze B2, Canada East (Toronto)**; R2 rejected because it shares the Cloudflare account with `odile.fyi` DNS and HQ's only backup | Savio's shape; the lowest-risk third provider then |
| 19 November 2026 | v2 | Destination re-cut to **Cloudflare R2 interim**: `odile-t-<slug>` buckets, bucket-scoped keys, 2 GB per tenant, at most 3 buckets, a 9 GB account cap; B2 retained as the target | The B2 account was suspended at signup; unblock R&D without touching HQ's backup or the free tier |
| 19–20 November 2026 | v2.1, v2.2 | Review corrections; §8's rows named the cards that own them (ODL-924/933/934/935) | Round-1 review: an evidence row must reproduce at the head it ships with |
| 24 November 2026 | **v3 — this re-cut** | **Destination re-cut to a Hetzner Storage Box BX11** on a new, backup-only Hetzner account: one **sub-account pair per tenant** (`readonly: false` / `readonly: true`), one project-scoped **Console API token** at HQ, SFTP on port 22, a **staggered 02:00–06:00 UTC** nightly window sized to **10 concurrent connections**. **R2 and B2 are superseded**; the three-bucket ceiling and the 9 GB cap are R2 history, not current design. The durability trade is stated, not hidden: one box, RAID, snapshots on the same box | Savio, 2026-11-24: the B2 account never came back, R2 was an interim with no tenant data in it, and the Hetzner box is the provider he chose for all user backups |

**Related:** `docs/MVP-V0.1.md` §6.1, §6.7, §8, §12 (D19); `docs/CLAIMS.md` §1
(Access 65–66, Money and place 122–123, Reliability 201) and §1a;
`docs/architecture/ADR-factory-backup.md` (ADR-003, the factory's own R2 copy)
§4.2–§4.4; `docs/architecture/ADR-007-destroyed-slug.md` (the same-slug
rebuild, landed); `docs/runbooks/backup.md` §1–§9; `docs/runbooks/offboard.md`
§4–§5; `docs/RESTORE.md` (HQ's own restore, not this); cards ODL-215, ODL-218,
`t_fec9f699`, `t_8eb17a54`, `t_ce52bb8d`, `t_9d6fb595`, `t_02b8df9d`, ODL-570,
ODL-915, ODL-924, ODL-933, ODL-934, ODL-935, ODL-937, ODL-938, `t_a548cb0a`,
`t_12b3720a`.
**Decision owner:** Architect for the mechanism, the include set, the schedule
and the restore path; **Savio for the provider, the account, the region, the
credentials and the durability trade** (§3 of the authority matrix). Product
lands any sentence that moves (`docs/CLAIMS.md`) — this ADR proposes one and
changes none.

---

## 1. The invariant at risk

Five sentences the product depends on: four are internal invariants, and the
fifth is a deck sentence still **held from the public pages** (`docs/CLAIMS.md`
§1a, line 219).

1. **The copy survives the machine.** *True, drilled.* ODL-215's nightly restic
   snapshot and a destroy-and-restore were measured on 2026-11-12 (§2).
2. **The copy survives HQ.** **False at this head; the destination this ADR
   names is what makes it true.** The repository's destination is still HQ's own
   disk: `backup.odile.fyi` does not resolve, and the drill wrote to a dedicated
   chrooted `sshd` on HQ's port 2222, under `/srv/odile-backup/tenants/<slug>`
   (`docs/runbooks/backup.md` §3). HQ is itself one Servury guest: the address
   behind `mail.odile.fyi` — the public tree writes it as `203.0.113.139`
   (`docs/ops/PUBLIC-TREE.md`:85) — resolves to **AS395904 Servury, Montréal**,
   the same provider and the same city as the tenant machines. One Servury
   suspension, one dead host or one billing failure loses the tenant's machine,
   the control plane and the only copy at the same moment. **This is the
   invariant the card exists for**, and it is Savio's sentence of 17 November
   2026 restated. The R2 interim did not reach it: two buckets were created on
   2026-11-22 and two read-write token pairs worked on 2026-11-23, but no
   read-only twin was ever minted, **no tenant byte was written to them**, and
   the provisioning card (`t_83d6fd7d`) never landed. Under v3 the repository
   moves to a Hetzner box: a different provider from Servury, a different
   account from Cloudflare, a different country from the machine. The box exists
   and is empty — `stats` 0 bytes at 2026-11-24 06:30Z (§2) — so the sentence
   becomes true **only when it holds the bytes**: `t_12b3720a` creates the first
   pair and runs the real drill.
3. **Only the user can read it.** *True on the storage path, false on the
   rebuild path.* No repository password exists in `/root/.odile`,
   `/etc/odile` or `/opt/odile` (measured, §2), so the storage path is clean.
   But a rebuild is today performed with
   `odilectl provision <slug> --backup-password-file <the user's recovery file>`
   (`docs/runbooks/offboard.md` §5), which reads the password on HQ and seals it
   under the new machine's setup token. The user's recovery file therefore sits
   on HQ's disk during a rebuild — and the "HQ cannot read" check greps three
   trees that file is not under. The claim is true on the path nobody exercises
   and conditional on the path every rebuild exercises. The provisioning card
   (§8) retires `--backup-password-file`, and a rebuild becomes a paste on the
   machine. **What HQ can do under v3, stated plainly:** the seed must carry the
   tenant's read-write **private key** and its read-only twin — a fresh tenant's
   recovery file is rendered on the guest, and a rebuild re-delivers both — so HQ
   holds both keys and can read the ciphertext and write garbage; it cannot read
   one byte of plaintext, because the password is the user's and is not in the
   seed. The API token adds one power the R2 token also had — it can reset the
   sub-account's password and then log in — and that power reaches **ciphertext,
   never plaintext**.
4. **The user can get the bytes back without us.** *True in principle, unbacked
   in practice.* `docs/MVP-V0.1.md` §6.7 and `docs/CLAIMS.md`:123 promise a
   download "before" the 30 days; until now nothing gave the user a credential
   that reaches the bytes. Under v3 the recovery file carries the repository
   URL, the host-key fingerprint and the **read-only private key**: with those
   and the password, a user — or their lawyer, or their next provider — can pull
   the repository with `restic` and `ssh`, without Odile, without HQ and without
   a support grant (§5). The restore reads with `--no-lock`, because a read-only
   sub-account cannot write restic's lock file — the one detail that turns "it
   should work" into a command that works. This is the first time the sentence
   is literally true; §8's drill has to prove it against the real box rather
   than assert it.
5. **"Backups are off-site."** *In the deck, held from the page.*
   `docs/CLAIMS.md`:122 carries it and §1a holds it. It becomes true in fact
   **when `t_12b3720a` lands** — the first sub-account pair, the
   destroy→rebuild→paste→restore drill against the real box (the box exists and
   is empty; it is the pair and the bytes that are missing), followed by the
   demo and bench repositories moving to it — **not when the R2 migration
   landed**, because the R2 migration never landed. `t_5f10dd14` landed the
   30-day retention row on 19 November 2026 and corrected `web/privacy.html`'s
   destination cell, which then said "an SFTP storage box at HQ" and now needs
   the Hetzner sentence (§4.4 carries the hand-off). Both surfaces are
   Product's; **this ADR publishes nothing**.

**What this ADR preserves:** the shape (restic), the password (guest-generated,
user-held, never at HQ), the nightly timer, the status record and the heartbeat,
the include set and every measured number's meaning. **What it changes:** the
destination, the credential that reaches it, the schedule, and the path a
rebuilt machine takes to its own data. **What it gives up, named in §4.5:** the
only surviving copy moves from object storage with erasure coding (B2's 17+3)
to **one box with RAID and snapshots on the same box**. Savio overrode that
rejection on 24 November 2026; this ADR states the residual risk and the
mitigation instead of calling it gone. **What it must not do:** add an operator
key, an escrow or a recovery account; touch HQ's `hermes-hq-backup` bucket; or
sell a sentence the deck does not already own.

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
| The timer | `OnCalendar=*-*-* 03:00:00`, `Persistent=true`, `RandomizedDelaySec=300` — **every tenant at the same clock time** | `installer/odile_install.py` (`BACKUP_TIMER_TEMPLATE`) |

**Three facts in that table are worse than the table looks, and each one is a
failure the builder did not test.**

1. **The destination shares HQ's fate.** The runbook says it in its own words:
   "the backup survives the tenant, not the loss of HQ" (`backup.md` §3). The
   stand-in was honest; the box that replaces it exists and is **empty**
   (`stats` 0 bytes), so **no tenant byte has left HQ yet**, and `t_8eb17a54` is
   now "retire the stand-in", not "buy a box". The R2 interim put nothing in its
   buckets either.
2. **The rebuild path puts the user's recovery file on HQ's disk.** The
   `--backup-password-file` argument is a *path*, so the password is not on
   `argv`, but the file it names is on HQ while the rebuild runs — and the
   "HQ cannot read" measurement greps `/root/.odile`, `/etc/odile` and
   `/opt/odile`, none of which is a shell's `~`. A rebuild is also the one moment
   the repository password is *supposed* to be the user's alone.
3. **The include set is a flag, and the wrong flag succeeds.** The installer's
   backup unit is rendered from `layout.home` (`backup_unit_files`,
   `installer/odile_install.py:14987`), which for a tenant is `/home/tenant`
   (`Layout.tenant`; `DEFAULT_HOME`; the image runs the installer with
   `--user tenant`, `deploy/image/odile-release-setup.sh:169`) — the same string
   the live unit the drill measured names (`backup.md` §2.1). So today the two
   homes agree, and **no migration or home-mapping is needed**: the failure is a
   rebuild whose unit or restore call drops `--home` or overrides it. A restore
   under a wrong `--home` succeeds, restores an empty tree, and reports `ok`.
   The drill must assert that `state.db` and `cron/jobs.json` came back, not
   that restic exited zero.

**The new destination, measured and read on 24 November 2026.** The box was
ordered at 06:25Z and answers the API; each row below says whether it is a
measurement of *our box* — `GET https://api.hetzner.com/v1/storage_boxes`,
recorded at `/root/.odile/hetzner-box.json` (0600, 06:30:36Z) — or a fact read
from the vendor's own pages.

| Fact | Value | Source, date |
|---|---|---|
| **The box, measured** | **id 709870, name `odile-backups`, username u728180, server `u728180.your-storagebox.de`, system `HEL1-BX192`, status `active`, created 2026-11-24T06:25:41Z**; `stats` **0 / 0 / 0 bytes** — it holds nothing yet | `GET /storage_boxes`, 2026-11-24 06:30:36Z; `/root/.odile/hetzner-box.json` (0600) |
| **Its settings as ordered, measured** | `location` **hel1 / Helsinki / FI**, network zone `eu-central`; `ssh_enabled: true`, `reachable_externally: true`, `samba`, `webdav`, `zfs` false; `snapshot_plan` **04:30 UTC, max 10**; `subaccounts_limit` **100**; `snapshot_limit` 10, `automatic_snapshot_limit` 10; size 1,099,511,627,776 bytes | same |
| **The API token, measured** | one project-scoped Console API token at `/root/.odile/hetzner-api-token` (**0600**, one `HETZNER_API_TOKEN` line); `GET /storage_boxes` with it answers the box above, so the scope works and HQ needs nothing else | measured 2026-11-24 06:27–06:30Z |
| The account's login, measured | the account password was accepted at `accounts.hetzner.com` and Hetzner demanded a **6-digit code mailed to the account address** (`/2fa`) at 04:57Z; the API token, not the password, is what HQ uses | measured 2026-11-24 04:57Z; credentials `/root/.odile/hetzner-account.env` (0600) |
| The product, read | **BX11: 1 TB, 100 sub-accounts, 10 snapshots (10 automated), 10 concurrent connections, unlimited traffic, Germany (FSN1) or Finland (HEL1), no minimum contract term, cancellation immediately, €3.20/month excl. VAT, no setup fee** | `hetzner.com/storage/storage-box/` and `/bx11/`, read 2026-11-24 |
| Protocols, read | SFTP, SCP, rsync over SSH, BorgBackup, rclone, FTP/FTPS, SMB, WebDAV, HTTPS — **restic is listed as supported** | same page, read 2026-11-24 |
| SFTP and SSH ports, read | **SFTP/SCP on port 22 is always active and cannot be disabled; it has no interactive access.** "SSH Support" enables port **23** (interactive, rsync, Borg). Port 22 takes the public key in **RFC4716** format; port 23 takes **OpenSSH** format | `docs.hetzner.com/storage/storage-box/general` and `.../getting-started/creating-a-storage-box`, read 2026-11-24 |
| Sub-accounts, read | a sub-account gets **its own username, its own hostname (`uXXXXX-subN.your-storagebox.de`) and its own `authorized_keys`**; it sees only its `home_directory`; **a read-only sub-account "prevents it from uploading, deleting, and modifying files"** | API reference + product page, read 2026-11-24 |
| Durability, read | **RAID "which can withstand several drive failures"; "the data is not mirrored onto other servers"**; block checksums detect and correct bit errors | Hetzner's FAQ on the Storage Box page, read 2026-11-24 |
| The API, read | `https://api.hetzner.com/v1`, Console API token, project-scoped: `GET|POST /storage_boxes`, `GET|POST /storage_boxes/{id}/subaccounts`, `GET|PUT|DELETE /storage_boxes/{id}/subaccounts/{subaccount_id}`, `POST /storage_boxes/{id}/actions/{change_protection,change_type,reset_password,update_access_settings}`, and `POST /storage_boxes/{id}/subaccounts/{subaccount_id}/actions/update_access_settings`; the returned object carries `location`, `access_settings`, `snapshot_plan`, **`stats {size, size_data, size_snapshots}`** and `protection {delete}` | `docs.hetzner.cloud/reference/hetzner`, read 2026-11-24 |

## 3. The three shapes the card asks about

The card names three: a restic repository plus a password, a single encrypted
archive, and a user-driven download. They differ in what the provider can do,
what the user holds, and what has to exist for a restore. **The destination
changed the cost argument:** on a **flat €3.20/month for 1 TB**, all three
shapes cost the same until the tenants' repositories pass 1 TB, so cost no
longer separates them. What separates them is integrity, partial restore and
whether a drill exists.

| | **A. restic repository + password** | **B. one encrypted archive a night** | **C. user-driven download only** |
|---|---|---|---|
| What the provider holds | ciphertext (restic: AES-256-CTR + Poly1305-AES, scrypt KDF), object sizes, access times | one opaque blob per night; same visibility, no dedup, so 11 blobs for 7 daily + 4 weekly | the archive, for as long as the download window is open |
| The secret the user holds | a 32-character repository password (the recovery file), plus the read-only key | an age/tar passphrase (or an identity file) — the same shape in the user's hands | the passphrase, **and the file itself** |
| What Odile can do | delete, withhold, or write garbage (the read-write key and the API token); never forge a snapshot restic will accept (no password) | same | same, plus delete the copy the user never downloaded |
| Restore machinery | `restic restore` on the guest; partial restore and `restic check` exist | `age -d \| tar -x` — no partial restore, no integrity check, and a torn `state.db` unless the same WAL-safe staging is rebuilt by hand | the user's laptop must still have the file; no machine-side machinery at all |
| Nightly cost of a run | only the delta (measured: 27.3 s for 3.14 GB on the first run, seconds after) | a full 2–2.5 GB upload every night | none until the user asks |
| Cost per tenant / month | **$0.36 at ten tenants, $0.07 at fifty** — one flat box either way | the same flat box, ~24 GB used instead of ~2 GB | the same flat box, ~2 GB used |
| Verdict | **Chosen** | Rejected: not cost — integrity, partial restore and a full rewrite of the program and the drill are | Kept, but as the **exit** and the **second copy**, not as the backup |

**Why A.** It is the only one of the three with a measured restore, a measured
"HQ cannot read", a program in the installer and a runbook a stranger can follow.
B is a different program, a retired drill and a nightly full upload on a small
guest, to buy a single-file shape that a non-technical user cannot open without a
terminal anyway (`age`, `tar`). C alone fails the only case that matters — the
machine is gone and the user never downloaded — and it makes the RPO "whenever the
user last remembered" instead of "since 03:00". C is still the right *exit*: §5
puts the read-only key and the URL in the recovery file so the download needs no
handover, and it is the user's own second copy whenever they want one.

**What A does not buy, stated plainly.** A restic repository needs restic to read
it. That is fine here: restic is pinned in the image (0.18.0-1+b4) and any Linux
has it in its archive, and the exit path in §5 hands the user the URL, the key
and the password, not a tarball.

## 4. The destination

The shape is settled; the destination is the card's second question, and after
24 November 2026 it has **one answer**: the Hetzner Storage Box BX11, on the new
Hetzner account whose only workload is backups. Prices are list prices read on
24 November 2026 from the vendors' own pages where marked.

| | **Hetzner Storage Box BX11 — the destination** | Cloudflare R2 — *superseded 2026-11-24* | Backblaze B2, Canada East — *superseded 2026-11-24* |
|---|---|---|---|
| Protocol and path | **SFTP on port 22** (always on, no interactive access), `sftp:<subaccount>@<subaccount>.your-storagebox.de:repo`; restic's `sftp` backend | S3 API over HTTPS/443; restic's `s3` backend | S3 API over HTTPS/443; restic's `s3` backend |
| Where the bytes are | **Helsinki, Finland (`hel1`)** — chosen at order, and the box is active there (`HEL1-BX192`) | Cloudflare's default jurisdiction, a best-effort `enam` hint; not guaranteed to be in Canada | Toronto, Ontario — `ca-east-006` |
| Capacity and cost at our size | **1 TB flat, €3.20/mo excl. VAT (≈$3.58), unlimited traffic, no minimum term**; 100 sub-accounts (measured); 10 snapshots; 10 concurrent connections | 10 GB-month free across the account, then $0.015/GB-mo; no per-bucket cap (the 9 GB ceiling was ours) | first 10 GB free, then $6.95/TB-mo; 100 buckets/account |
| Cost per tenant / month at 1.017 GB | **$0.36 at ten tenants; $0.07 at fifty** — the box is one fixed cost | $0.00 inside the shared free tier; ≈$0.09 at a 15 GB envelope | $0.01 |
| Durability of the only surviving copy | **RAID, "the data is not mirrored onto other servers"**; block checksums; **10 snapshots on the same box** | 11 nines claimed, in the same account as HQ's DNS and HQ's only backup | 11 nines claimed; 17 data + 3 parity shards |
| Per-tenant scope | **one sub-account pair per tenant**, each with its own username, hostname and `authorized_keys`, confined to `tenants/<slug>` | one bucket per tenant, token restricted to it | one bucket per tenant, app key restricted to it |
| The credential the guest needs | the tenant's **read-write private key** (ed25519), in the seed; its public half on the read-write sub-account | an Object Read & Write token in the seed | an S3 application key in the seed |
| New network path from a tenant | **SFTP/22 out** to one hostname, with a pinned host key | HTTPS/443 | HTTPS/443 |
| Shared fate with today's copies | **none**: not Servury, not HQ, not Cloudflare, not the same country | the same Cloudflare account as `odile.fyi` DNS and HQ's only backup | none |
| Verdict | **Chosen by Savio, 24 November 2026** | Superseded | Superseded |

**Why one box and not a second copy.** A second box in a second location is
€6.40/month and would restore the durability property §4.5 gives up. It is
rejected here for the same reason ADR-003 §4.1 rejected a second HQ copy: a
second copy doubles what can be wrong at 03:00 and still needs its own drill, and
at alpha.0's size the first copy is the one that has to exist. **This is the
first thing to revisit** (§4.5).

### 4.1 The per-tenant layout

**The pair, and what each can do.** One tenant, one pair, both created with the
same `home_directory`:

| Sub-account | `access_settings` | The key | What it can do |
|---|---|---|---|
| `odile-<slug>-rw` | `readonly: false`, `ssh_enabled: true`, `reachable_externally: true`, `samba_enabled: false`, `webdav_enabled: false` | generated at provision, private half in the seed → `/etc/odile/backup-ssh/id_ed25519` (0600) | `init`, `backup`, `prune`, `check`, `restore` — everything restic does |
| `odile-<slug>-ro` | `readonly: true`, same rest | generated at provision, private half in the user's recovery file and in the store of record | `snapshots`, `check`, `restore` — **it cannot upload, delete or modify**, and it cannot write restic's lock file, so every command carries `--no-lock` |

**Naming.** Our name for the pair is `odile-<slug>-rw` / `odile-<slug>-ro`,
carried in the sub-account's `description` and in the tenant row. **Hetzner
assigns the login**: the sub-account's login is `u<box>-sub<N>` and its
hostname is `u<box>-sub<N>.your-storagebox.de`, so the ADR never derives a login
from the slug — it records what the API returned. The pair is slug-scoped, and a
tenant cannot see another tenant's directory: the sub-account's home directory is
`tenants/<slug>` on the box, and the box's main account sees it under its own
`/home`.

**The repository URL** is the shipped program's grammar
(`scripts/backup.py:SFTP_HOST_RE`): `sftp:u<box>-sub<N>@u<box>-sub<N>.your-storagebox.de:repo`
— the path **relative to the sub-account's home**, so no absolute path and no
leading slash. That URL is one line in `/etc/odile/backup-repo.txt`, written
from the seed's `backup_repo_url` (`installer/odile_install.py:14914`). A fresh
start without a restore (§7.4) is a new `repo-<4 hex>` directory **inside the
same sub-account** — no API call, no new sub-account — with the old directory
kept 30 days and then emptied by the exit.

**The store of record.** `detail` in the tenant row is the cache, not the store
of record: `reopen` (ADR-007) rewrites it on a destroy→rebuild. The durable copy
is `/root/.odile/hetzner/odile-<slug>.env` (0600), holding the box id, the two
sub-account ids and usernames, the repository URL and **both private keys**; the
reopen path re-reads it and re-records the row. Both keys are in it for the same
reason: a fresh tenant's recovery file is rendered on the guest from the seed,
so the read-only private half has to travel there once, and a rebuild that
re-renders the file (the "start fresh" branch, §7.4) must be able to do it
without the user's paste.

**The 2 GB fair-use cap stays.** It is a per-repository envelope, not a
provider limit: `backup.py run` reads `BACKUP_TENANT_CAP_BYTES=2000000000`,
runs `restic stats --mode raw-data` **after prune**, and writes
`status: over_cap` — with the size and the cap — when the repository exceeds it.
A run that starts over cap does not upload, so growth stops at one night's delta
above 2.0 GB. **Fifty tenants at the cap use 100 GB of 1 TB**; the cap is a
fair-use guard, not a capacity plan.

**The ceiling that replaces the three-bucket cap.** The ordered box reports
**`subaccounts_limit: 100`** (measured 2026-11-24 06:30Z), and a pair per tenant
means **50 tenants per box — the sub-account count, not the terabyte, is the
binding constraint**. It does not move with an upgrade: the feature table lists
100 sub-accounts and 10 concurrent connections for every BX tier, so a bigger
box buys capacity, not more tenants. The provisioning path therefore **counts
the pairs and refuses the 51st** — the same structural check the R2 layout made
on buckets — and the 51st tenant is the signal that a **second box** is the next
purchase.

### 4.2 The credentials, and what each one can reach

**The API token at HQ: one, project-scoped, and only for provisioning.** Savio
creates it in the Hetzner Console (Project → Security → API Tokens, Read &
Write) — card `t_a548cb0a` — and it lands at
`/root/.odile/hetzner-api-token` (0600). It is used by provisioning and by
nothing else:

- `POST /storage_boxes/{id}/actions/change_protection` — set `delete` protection
  on the box (§4.5). **It is not set yet**: the box reports
  `protection.delete: false` at 2026-11-24 06:30Z, and `t_12b3720a` sets it
  before the first tenant byte. The `POST /storage_boxes` that ordered the box
  (id 709870, 06:25:41Z) is behind us and is not a code path.
- `GET /storage_boxes/{id}/subaccounts` and `POST …/subaccounts` — read the
  count and create the pair.
- `POST …/subaccounts/{subaccount_id}/actions/update_access_settings` — set or
  correct the access settings.
- `DELETE …/subaccounts/{subaccount_id}` — the exit and the rotation.
- `GET /storage_boxes` — the usage readback (`stats.size_data`,
  `stats.size_snapshots`).

**What it cannot reach.** A Hetzner Console API token is bound to its project.
The project (`odile-backups`) and the account are **dedicated to backups**, so
the token cannot reach HQ's Cloudflare account, Servury, or any other Hetzner
project or machine. It cannot read file contents — the API manages boxes,
sub-accounts and snapshots, not bytes — but it **can reset a sub-account's
password** and then log in with it, so it can reach the ciphertext. **It cannot
decrypt anything.** Two consequences follow, and both are wanted: the token is
"can delete, can roll back, cannot read plaintext", and it is the same class of
power the R2 bucket token had.

**The main account's password is not at HQ.** It stays in Savio's console. HQ
never needs it: the API token creates the sub-accounts, sets their passwords and
installs their keys. **`protection.delete` goes on the box** as `t_12b3720a`'s
first write (§4.5), so that the token — or a tired operator with `curl` — cannot
delete the only copy; deleting the box is then a two-step act in the console.

**The sub-account password is a one-time provisioning value.** The API requires
one to create a sub-account; it is generated at provision, used once to install
the public key, and then reset to a new random value and discarded. HQ's real
credential is the API token, which can reset it again whenever a key has to be
re-installed. **Nothing at HQ holds a password that can open a door into a
tenant machine** — this credential opens one directory of one tenant's
ciphertext, outbound only.

**The key install, and its one sharp edge.** Each sub-account has its own
`authorized_keys`, and the key format depends on the port: **RFC4716 for port
22, OpenSSH for port 23**. Provisioning installs each public half in both
formats — the port-23 `install-ssh-key` command does that in one step while
"SSH Support" is enabled — and the steady state is **port 22 only**, which
cannot be disabled and exposes no shell. A key uploaded in the wrong format
fails silently as `Permission denied (publickey)`; `t_12b3720a` proves both
halves with a real restic run and records the exact commands.

### 4.3 Concurrency, and the nightly window

**The box has 10 concurrent connections**, shared by every sub-account and the
main account. Two facts make that a design constraint rather than a footnote:

1. **A restic run opens up to 5 SFTP connections by default**
   (`sftp.connections`, restic's own option, default 5). Two simultaneous runs
   at defaults sit exactly on the ceiling.
2. **Today every tenant fires at 03:00** (`OnCalendar=*-*-* 03:00:00`,
   `RandomizedDelaySec=300`). Ten tenants at defaults would ask for 50
   connections in the same five minutes.

**The decision.** The guest's unit carries **`-o sftp.connections=2`**, so one
nightly run asks for **two** connections and **five runs can share the box**.
The installer renders the timer from the slug instead of a constant:

```
window:   02:00-06:00 UTC            (240 minutes)
offset:   int(sha256(slug)[:2], 16) % 240
start:    02:00 UTC + offset         (02:00 ... 05:59)
```

The offset is stable across a rebuild (same slug, same minute) and needs no
state at HQ: the installer computes it, the timer carries it. **At a full box of
50 tenants the binding number is the run length `D`: average concurrent runs =
50 x D / 240.** At D = 10 minutes that is 2.1; at D = 20 minutes, 4.2; the
window holds a nightly run of **about 24 minutes** before five runs overlap.
The measured first run took **27.3 s** on the same host; the transatlantic
upload term is the unknown, and `t_12b3720a` measures it. **`RandomizedDelaySec`
goes to 0** — the fixed offset is the whole schedule and a random delay on top
of it only blurs the arithmetic.

**The failures this leaves, named.**

- **A run that is longer than the window assumes.** Five runs is the safe
  number, not a lock; a sixth connection is refused, restic exits non-zero, the
  run is `status: failed`, and the 36-hour freshness watchdog (ODL-216) catches
  two in a row. Nothing corrupts: restic's last good snapshot stays.
- **`Persistent=true` replays missed runs at boot.** After a box outage or a
  guest reboot every tenant that missed its window starts at once, offset
  included, and the ceiling can be exceeded. The consequence is the same bounded
  failure above: some runs fail, the next night retries, the watchdog reports a
  tenant that fails twice. Keeping `Persistent=true` is the right trade (a
  machine that was off at 02:13 still gets its snapshot); the stampede is the
  price and it is visible.
- **A restore is outside the window.** `odile-restore.service` stops the backup
  timer first (§7.2, step 5), so the one operation that legitimately needs more
  connections never competes with the nightly set.
- **The 11th connection's error is measured, not guessed.** `t_12b3720a` opens
  11 parallel SFTP sessions and records the exact refusal.

### 4.4 Location: the bytes are in the EU, and that is a change

The machine is in **Montréal, Canada** (`docs/CLAIMS.md`:122). The box is in
**Helsinki, Finland (`hel1`)** — `location: {name: hel1, city: Helsinki,
country: FI}`, `system: HEL1-BX192`, ordered on 24 November 2026. Germany
(`fsn1`) was the other location Hetzner offers and is **not** used; the location
is fixed at order. **Nothing about a restic repository depends on it**, and
moving it is `restic copy` to a second box (§10) — but the "where the bytes
are" answer changes, and the privacy page's destination cell must say so.

**The hand-off to Product Lead / Growth, in one sentence.** The cell that today
reads "an SFTP storage box at HQ" should become:

> **"an SFTP storage box in the European Union (Finland), operated by Hetzner"**

and the place row's "Backups are off-site" stays **held** until `t_12b3720a`
lands. That is the exact wording this card hands over; this ADR edits no page
and no `docs/CLAIMS.md` row.

**What it means for the trust boundary:** the ciphertext leaves Canada. A
tenant's data is protected by restic's encryption, not by jurisdiction, so the
move costs no security property — it costs the sentence "the bytes are in the
same country as the machine", which v1 (B2 Toronto) had and v3 does not. Stated
once, here, so no page has to discover it.

### 4.5 Durability: one box, RAID, snapshots on the same box

**This is the reason the 17 November cut rejected Hetzner, and Savio overrode it
on 24 November 2026.** The ADR does not pretend the objection was wrong; it
records what the risk is and what makes it bearable.

**The residual risk, in one paragraph.** The only surviving copy of a tenant's
home lives on **one box, in one Hetzner location, on one RAID set**. RAID
survives drive failures and its checksums correct bit errors, but **"the data is
not mirrored onto other servers"** — a rack, a datacentre, a filesystem or an
account-level failure takes the copy with it, and the 10 snapshots are **on the
same box**, so they protect against a bad prune, a wrong delete or a corrupted
file, not against losing the box. B2's erasure coding (17+3 across 20 pods) and
R2's multi-device storage both survived more than this. **What Savio bought with
that trade:** a provider that does not share an account, a country or a fate with
HQ, at a flat €3.20, with an API that automates provisioning and no per-bucket
credential to mint by hand.

**The mitigation, in four parts, none of which is "it will not happen":**

1. **Automatic snapshots at 04:30 UTC, 10 kept.** The plan is already set —
   measured `snapshot_plan: {hour: 4, minute: 30, max_snapshots: 10}` — and it
   covers the failure that actually happens in practice: an operator's bad
   `restic forget`, a tenant's wrong directory, a torn write. It does not cover
   the box dying; it covers us.
2. **`protection.delete` on the box.** Not set at 06:30Z (`false`);
   `t_12b3720a` sets it before the first tenant byte. After that the API token
   cannot delete the box, and a human has to turn protection off first.
3. **The 30-day exit.** The promise does not change: after cancellation the
   repository is kept 30 days and then deleted (`docs/CLAIMS.md`:123). During
   those 30 days the user holds the password, the URL and the read-only key, so
   they can take a copy without asking. That is the mechanism by which a tenant
   is never trapped by our single box.
4. **No minimum contract term, cancellation immediately.** Leaving Hetzner costs
   one month, not a term. A provider failure is a migration, not a lock-in.

**What would make us revisit this — named now, so the decision has a trigger:**
the first paying tenant whose only copy is the box; the box passing ~25 tenants
(half its sub-account ceiling); any drill that loses a snapshot; or Hetzner
changing the RAID or snapshot terms. At that point the answer is a second BX11 in
the other location (€6.40/month total) and `restic copy` per tenant — the same
paragraph §10 already writes.

## 5. The trust boundary this creates

**What Hetzner holds:** ciphertext, each repository's size and object count, the
sub-account names and their directories (the slug is already public in the URL),
the times of access, the box's snapshot images, and the fact that a machine
writes nightly. **What it does not hold:** the password, the plaintext, file
names (restic encrypts them), or any way to produce a snapshot restic will
accept. It can delete or withhold; it cannot forge. **The box's main account** —
Savio's — has full access to every sub-account directory, so it can read every
tenant's ciphertext and nothing more.

**What HQ holds, and what it can do.** One **project-scoped Console API token**
(§4.2), and each tenant's **read-write private key** and **read-only private
key** in `/root/.odile/hetzner/odile-<slug>.env` (0600) and in the seed, because
the seed that provisions the guest has to carry them and a rebuild has to deliver
them again. The read-only key is the one the user also holds; HQ's copy is what
renders a *fresh* tenant's recovery file, and it adds no capability — it opens
the same ciphertext the read-write key already opens. That means HQ **can read a
tenant's ciphertext, delete the repository and write garbage into it** — and
cannot read one byte of plaintext, because the password is the user's and is not
in the seed. It could already delete any tenant's machine through the Servury
API, so this does not widen the availability power; it does add "can overwrite",
which is worth stating: an operator who wanted to destroy a backup could, and an
operator who wanted to read one could not. **No HQ credential can reach
`hermes-hq-backup`** — a different provider, a different account, a different
console.

**The rules this amends, named.** `AGENTS.md` line 61 says HQ stores no tenant
passphrase, private key or Webapp password — "Only the public key and a hashed
setup token." **Under v3 the letter is amended, not satisfied: HQ holds a backup
private key.** It is not a key *to* the machine (no door in) — it opens one
directory of the tenant's own ciphertext, and it has to be in the seed because
the guest cannot register it before it boots. **`docs/MVP-V0.1.md` §6.1 invariant
5 is the stronger sentence and is amended again by this cut:** as corrected on
20 November 2026 (`t_2522fa72`) it names "the bucket-scoped read-write token" as
the thing that reaches HQ; under v3 the thing is **the tenant's read-write SFTP
private key**. What stays true is the invariant's *purpose*: **the password never
reaches HQ, so HQ cannot read a backup.** The MVP sentence is outside this
card's `touches`; §8 names the card that re-cuts it.

**What the guest holds.** `/etc/odile/backup-ssh/id_ed25519` (0600) and its
`known_hosts`, `/etc/odile/backup-repo.txt`, `/etc/odile/recovery.txt` (0600)
and the cap in the unit's environment. The key is **to the tenant's own
directory, outbound only** — it opens no door *into* the machine and no other
tenant's directory, and the machine is already root over itself. It is the same
class of thing as the SFTP key the v5 drill generated on the guest, except that
HQ generates it so the box can be ready before the guest boots. Under
`docs/AGENT-ORG.md` §3 this is "a standing credential on a tenant" and needs
Savio's yes; it is granted on the argument that it is a way *out* to the
tenant's own ciphertext, not a way *in*.

**What the user holds, and what it now makes true.** The recovery file gains
three things — the repository URL, the host-key fingerprint and a **read-only
private key** — inside the file the installer already renders
(`render_recovery`):

```
Odile recovery file
===================
Tenant:              odl-7f3a2c
Instance:            <the instance this file was written on>
Written:             <date>

Backup password:     <32 characters>
Repository:          sftp:u<box>-sub<N>@u<box>-sub<N>.your-storagebox.de:repo
Box host key:        SHA256:<the fingerprint of the storage box's host key>
Read-only key:       <the read-only private key block>
Certificate SHA-256: <the certificate the browser saw>

This file is the only copy of the backup password. ...
```

With those four things — the password, the repository, the pinned host key and
the read-only key — a user can pull the repository on any machine with restic
and ssh. The key goes into a 0600 file, its host-key line goes into
`~/.ssh/known_hosts`, and the two commands are:

```
restic --no-lock -o sftp.args="-i ~/odile-ro -o IdentitiesOnly=yes" snapshots
restic --no-lock -o sftp.args="-i ~/odile-ro -o IdentitiesOnly=yes" restore latest --target ~/odile-restore
```

**`--no-lock` is not optional for a read-only sub-account**: restic writes a
lock file unless told not to, and a read-only account refuses the write. That
one flag is the difference between "the file opens your backup" and a user
watching a restore fail with a permission error, and it goes in the recovery
file's own words as well as the page's. That closes invariant 4, and it makes
the 30-day exit a thing the user already has rather than a thing Odile promises
to build. It also makes `docs/CLAIMS.md`:65's "the file opens your backup"
literally true instead of figuratively true, which is a **stronger sentence than
the deck owns** and goes to Product and Savio (§9).

**What the read-only key deliberately cannot do:** write, delete or lock. A
recovery file that is printed, emailed to a new laptop or dropped in a password
manager cannot destroy the repository it opens. The read-write key is on the
guest and in the seed; it never goes into the user's file.

**The parser and the file.** `parse_recovery` is a set of line-anchored
regexes, so a multi-line key block between the other fields is tolerated — but
the card that adds it must pin that with a test, because the file is pasted
whole into the first-visit page and the page's grammar is the same one. The
private key is **not** the file's only secret any more: the file already carries
the repository password, and together they open the repository. Separately,
neither does.

**A grant gains nothing new, which is why no access claim moves.** A support
grant could already read `/etc/odile/recovery.txt` (`docs/CLAIMS.md`:115 says
so) and the read-write key beside it, so a grantee could already copy the
repository's bytes and its password. The read-only key is the same capability,
in a tidier form, and it widens the grant by nothing. A user who wants a
grantee's copy to stop opening the repository rotates the password —
`restic key add`, prove a restore, `restic key remove` — which
`docs/runbooks/backup.md` §7 already documents; the read-only key is rotated by
deleting the `-ro` sub-account and creating a new one on the same directory
(§4.2).

## 6. What "whole machine" means

The backup does not contain a machine. **A machine is the signed image plus one
home**, and the backup contains the home.

| In the repository (the home, `--home` from the layout) | Rebuilt, not restored |
|---|---|
| `.hermes/state.db` (the session store), `.hermes/memories/`, `.hermes/sessions/`, `.hermes/skills/`, `.hermes/config.yaml`, `.hermes/.env` (the sign-in hash), `cron/jobs.json`, `plugins/`, `skins/`, `platforms/`, `state/` | the OS, the kernel, the initramfs and the command line — the **signed, measured image** |
| the user's files, `workspace/`, `documents/`, `AGENTS.md`, `SOUL.md` | `.hermes/hermes-agent`, `.hermes/tools`, `.hermes/installs`, the caches (the pinned installer and `odile-update`) |
| | `/etc/odile/key.env` (the NEAR key) and the backup key — **re-issued in the seed** at provision |
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

This is the answer to the card's third question. **The rebuild keeps the
tenant's slug** — the same URL, the same recovery file: ADR-007 landed the
`destroyed → reopened` edge (`t_ce52bb8d`, merged at `4dc34ca`), so
`odilectl provision` on a destroyed row records the new machine and the URL does
not move.

### 7.1 What has to be true before the machine dies

- The nightly run has succeeded: the heartbeat's `last_backup_at` is under **36
  hours** old (the freshness watchdog, ODL-216 — `odilectl/health.py`,
  `BACKUP_ALERT_SECONDS = 36 * 60 * 60`) and `backup.json` says `ok`.
- The user still has the recovery file, off the machine. Nothing else is needed
  from them — not a key, not a passphrase, not a shell.

### 7.2 Who does what, in order

| # | Who | What | Duration |
|---|---|---|---|
| 0 | the user / HQ | The URL fails or the heartbeat stops. The alpha path is an email to `help@odile.fyi`; the 36-hour check catches a silence nobody reported | detection, not a step |
| 1 | HQ | `odilectl provision <slug> --apply`: one machine in the same tenant row, the same slug, the published image, **no `--backup-password-file`**; the API token already holds the pair, and the seed carries the read-write key and the repository URL | the panel's order — **unmeasured for a rebuild**; the bench's build-to-first-visit term is 14m24s and the first boot's measured installer is 296–353 s |
| 2 | the machine | the signed image boots and the installer runs zero-touch; the seed carries the repository URL, the read-write key, the pinned host key and the tenant row's **previous instance id**, so the page knows this is a rebuild from the row and not from an operator flag somebody forgot | included above |
| 3 | the user | opens `https://<slug>.odile.fyi/first-visit?t=<token>` from the "your machine is back" email and **pastes the whole recovery file** into one textarea | under a minute |
| 4 | the machine | the first-visit service parses `Backup password:` with the installer's own grammar (`RECOVERY_PASSWORD_RE`), writes `/etc/odile/recovery.txt` (0600) **regenerated for this machine** — same password, new `Instance:`, new `Certificate SHA-256:`, the repository, the host key and the read-only key — starts `odile-restore.service`, and the page polls a text status endpoint with a meta refresh (no JavaScript) | — |
| 5 | the machine | `odile-restore.service`: stop the backup timer and the agent unit, `backup.py restore latest --target / --include <home>`, start the agent unit, run one `backup.py run` so a fresh snapshot exists immediately, re-enable the timer, write the status. The timer is stopped first so a 02:00–06:00 run cannot snapshot a half-restored home; the restore's own run is the newest snapshot either way | repo ÷ throughput; the measured repository is **1.017 GB** |
| 6 | the user | the page says "Restored snapshot `<id>` of 2026-11-17T03:00Z — 156,311 files, 3.14 GB"; they set a **new sign-in** (the page's normal step) and sign in | under a minute |
| 7 | the user | checks one thing they care about — a file, the last brief, the agent's memory of yesterday — and the page's own report names the snapshot and its time | a minute |
| 8 | HQ | records the rebuild on the row: snapshot id, restore time, size; the next nightly run lands under the same `--host <slug>`, so the retention group stays one | — |

**The transfer term, with its assumption stated.** The measured 38m03s restored a
3.630 MiB home from a repository on the same host, so it says nothing about a
3 GB home from a third provider. The repository is **1.017 GB**. The R2 transfer
measured on 19 November 2026 (13.6 MB/s single stream from Cloudflare) is
**history, not a Hetzner number**: the box is in the EU and the machine is in
Montréal, so the term is a transatlantic one. At 5 MB/s a 1.017 GB repository is
**3.5 minutes**; at 20 MB/s it is **55 seconds**; restic runs its transfers in
parallel across those two connections (§4.3). The end-to-end estimate is
therefore **15–25 minutes**, comfortably inside the published hour — but **the
number that replaces 38m03s has to come from the drill in §8**, not from this
paragraph, and if it does not hold, `docs/CLAIMS.md`:201's "under an hour" is
re-cut rather than stretched.

### 7.3 The user's four actions, in one paragraph

Open the link in the "your machine is back" email; paste the recovery file into
the box; wait for the page to say which snapshot came back; set a new sign-in and
look at one thing that matters. No terminal, no command, no SSH key, no chat
message containing a secret.

### 7.4 The branches, and the failures this shape has to answer

| Branch or failure | What happens |
|---|---|
| **Wrong password** | restic exits 12 (`wrong password or no key found`); the page says "that file does not open your backup", the home is untouched, the paste can be retried. A file from another tenant is caught by its own `Tenant:` and `Repository:` lines before restic runs, and a truncated paste fails at the page — the installer's `parse_recovery` grammar is reused, so a bad file never reaches a restic call |
| **The read-only key is used to restore** | it works, with `--no-lock`; without it restic fails to write the lock. This is the failure the user will actually hit, so the command in the recovery file carries the flag and the page's help text repeats it |
| **The user lost the recovery file** | the page offers "start fresh without a restore": the guest mints a new password into a new recovery file, uses a new repository directory (`repo-<4 hex>`) in the **same** sub-account, and the old directory is kept 30 days and then emptied by the exit (ODL-935's re-cut); no API call and no new sub-account. The page says, in one sentence, that the old data is unrecoverable without the old file — which is the promise, not a bug |
| **The restore is interrupted** | `odile-restore.service` is a oneshot and the page re-offers the step; restic restore overwrites into the same target, so a second run converges. The backup timer stays stopped until a run completes, and if the user never comes back the 36-hour freshness watchdog is the signal. Nothing else in the machine depends on it, so the user is never blocked |
| **The process is killed mid-write** | `state.db` is overwritten under a live gateway — the torn-database failure `docs/RESTORE.md` §2 already refuses. The unit stops the agent first and starts it after; the drill asserts the session list is readable afterwards |
| **The nightly timer fires before the user pastes** | **Designed here; shipped by the `backup.py` + installer card (§8) — at this head neither the unit template nor `ensure_recovery` carries it.** The backup **service** carries `ConditionPathExists=/etc/odile/recovery.txt`, and on a rebuild the installer **does not create that file**: `ensure_recovery` skips when the seed carries the previous instance id **and the file is absent**, because a freshly minted password would not open the old repository. So a rebuilt-but-not-yet-restored machine's timer fires, the condition fails, the service is skipped silently, and no false "backup failed" reaches the watchdog. The paste writes `/etc/odile/recovery.txt`, and `odile-restore.service` runs one snapshot before the next window. Once the file exists, a later installer run keeps its password and refreshes only the instance and certificate lines, exactly as today. On a **fresh** tenant there is no previous instance id, the installer mints the recovery file at install as today, and the first backup is not suppressed |
| **The box's host key changes** | `StrictHostKeyChecking=yes` with the pinned `known_hosts` fails the run with a host-key error — the safe direction, and every tenant fails at once because they share one box. The fix is one re-seed: the page or an installer re-run rewrites `known_hosts` from the box, and the runbook records `ssh-keyscan`'s output before and after. `t_12b3720a` records the box's fingerprint so the first change is detectable |
| **The sub-account is deleted, or the box is full** | restic fails with an authentication or quota error; the run is `status: failed` and the watchdog reports it. The provisioning path refuses the 51st pair (§4.1), so "full" is caught before a tenant exists rather than at 03:00 |
| **A password typed into the chat instead of the page** | it travels to NEAR AI's TEE in the prompt **and** lands in `state.db`, which is inside the backup it opens. The restore step is a page POST for exactly this reason, and the help text says so |
| **The destination is down, or the repository is over the 2 GB cap** | the restore waits and retries; the machine is usable without it. A failed upload is a `status: failed` heartbeat, and a repository over `BACKUP_TENANT_CAP_BYTES` is `status: over_cap`; the freshness watchdog reports either. The cap never silently deletes: the last good snapshot stays until an operator prunes |
| **HQ is gone** | the off-site repository, the user's recovery file and the signed image are enough to rebuild the machine by hand on any provider; what is lost is the convenience of `odilectl`, not the data. This is the first time that sentence has been true of a destination that is not HQ itself |

## 8. What this changes in the code — cards, not prose

The ADR decides; the board builds. **The shipped program already speaks SFTP**
— `scripts/backup.py` parses `sftp:user@host:port/path`, builds an
`ssh -i <key> -s sftp` command with `BatchMode`, `IdentitiesOnly` and a pinned
`known_hosts`, and the installer places it — so this cut **adds no restic
backend**. The `s3:` branch the R2 cards planned was never shipped; it is
cancelled, not adapted. What is new is the credential, the schedule, the
recovery file and the provisioning.

| Card | What it must do | Depends on |
|---|---|---|
| **The box and the first pair — `t_12b3720a`** (new; supersedes **ODL-938** / `t_7010af06`) | The box is ordered and active (id 709870, `hel1`, snapshots already 04:30 UTC × 10, `stats` empty); **set `protection.delete`** — still `false` — then create `odile-odl-bench-rw`/`-ro` on `tenants/odl-bench`, install both public keys, run a real `init`/`backup`/`snapshots`/`restore` with the read-write key, read the same repository with the read-only key (`--no-lock`), measure **11 parallel sessions'** refusal, record the box fingerprint, and write `docs/evidence/hetzner-bx11/` with the commands and raw output (token redacted) | `t_a548cb0a` is **satisfied**: the token exists at `/root/.odile/hetzner-api-token` (0600) and answers the API |
| **Provisioning — the re-cut of ODL-924 (`t_83d6fd7d`)** | For each tenant: create the pair with the same `home_directory`, generate both keypairs, install the public halves, reset and discard the sub-account passwords, write `/root/.odile/hetzner/odile-<slug>.env` (0600) and the row's `detail` (`backup_box_id`, `backup_subaccount_id`, `backup_readonly_subaccount_id`; the env file is the store of record and the reopen path re-records `detail` from it), put the repository URL, the read-write key, the host-key line and the read-only key in the seed **with the previous instance id**, and **refuse the 51st pair**. **Retires `--backup-password-file`** and with it the sealed password in `seed-rebuilds.json` | the evidence from `t_12b3720a` |
| **`backup.py` + installer — the re-cut of ODL-933 (`t_fda54dd4`)** | Add **`-o sftp.connections=2`** to the SFTP options; render the timer from the slug (§4.3's offset, `RandomizedDelaySec=0`), keep `Persistent=true`; rename the cap to `BACKUP_TENANT_CAP_BYTES` and keep the `restic stats` check and `status: over_cap`; add `ConditionPathExists=/etc/odile/recovery.txt` to the backup **service**; **do not mint a recovery file on a rebuild** (`ensure_recovery` skips only while the file is absent and the seed carries the previous instance id). No `s3:` branch. Update `BACKUP_SHA256` and re-sign the installer | the seed fields |
| **The usage readback — the re-cut of ODL-934 (`t_a316c2fe`)** | `odilectl backup-usage`: read `stats.size_data` and `stats.size_snapshots` from `GET /storage_boxes` with the existing API token, sum each tenant's last `restic stats` from its heartbeat, write `/var/lib/odile/backup/usage.json`, and feed the watchdog at **800 GB warn / 950 GB critical** (1 TB box, 2 GB cap, 50 tenants = 100 GB at the cap). No new credential: the API token is already there | provisioning |
| **First visit — ODL-937 (`t_c31fb2aa`) stands** | the rebuild step: detect the previous instance id in the seed, ask for the recovery file before the sign-in step, write `/etc/odile/recovery.txt`, run `odile-restore.service`, show the snapshot and its time (no JavaScript). Its §8 row's bucket wording is re-cut to the repository URL | `backup.py`'s `restore` (already supports `--target /` and `--include`) |
| **Recovery file** | three new lines (the repository URL, the box host-key fingerprint and the read-only private key) and the parse that tolerates the key block; `first_visit.py`'s copy of the grammar stays the one source; the page's help text carries `--no-lock` | provisioning |
| **The exit — the re-cut of ODL-935 (`t_1b578030`)** | at `keep_until`: forget and prune **every** snapshot with the tenant's **read-write key** (`restic snapshots --json` → `restic forget --prune <id>` for each), which removes all snapshot data and leaves only the repository's config and key files — no user data — then **`DELETE` both sub-accounts** with the API token and record `subaccount_deleted_at`. The shipped `odilectl purge` is a `shutil.rmtree` on HQ's `--root` and has **no SFTP path today**; that is the re-cut. **One constraint the API imposes:** Hetzner lets a sub-account's home directory be deleted only *after* the sub-account is gone, and the only credential that could then remove it is the box's **main account**, which HQ deliberately does not hold — so the empty `tenants/<slug>` directory stays, and the runbook says so. The promise is about the data, and the data is gone when the last snapshot is pruned and the two keys that could read it are deleted | provisioning |
| **The demo and bench repositories** | one command (`backup.py init` against the Hetzner URL, then `run`) for the demo and the bench; the HQ `sshd` on 2222 stays 30 days and is then deleted. **`t_8eb17a54` becomes "delete the stand-in"**, not "buy a storage box" | the drill |
| **The same-slug rebuild** | ADR-007 landed it (`t_ce52bb8d`, `4dc34ca`); the rebuild path's remaining dependency is the provisioning card's retirement of `--backup-password-file` | — |

**The sentences this cut leaves outside its own `touches`, handed over rather
than smuggled in:**

| Where | What is stale | Who re-cuts it |
|---|---|---|
| `docs/MVP-V0.1.md` §6.1 invariant 5 | names "the bucket-scoped read-write token" as what reaches HQ; under v3 it is the tenant's read-write **private key** (same purpose: the password never reaches HQ) | Product lead, with the provisioning card |
| `docs/MVP-V0.1.md` §6.7 | "the destination is A3's decision … a bucket at a third provider (Toronto)" — Toronto was B2; the destination is a Hetzner box in the EU | Product lead |
| `docs/MVP-V0.1.md` §8 | the $0.60 backup line still covers the box at six or more tenants ($3.60 ≥ €3.20 ≈ $3.58); below six the box's €3.20 is a fixed cost the alpha absorbs, and at fifty the share is $0.07 | Finance / Product lead |
| `docs/runbooks/backup.md` §1, §3.1, §8, §9 | the R2 interim rows, the bucket token and the 9 GB cap are history; §3's SFTP material is live again with a different host and a sub-account instead of a main account | the `backup.py` card |
| `docs/architecture/ADR-factory-backup.md` §4.2, §4.4 | the Hetzner row says "rejected by decision C"; §4.4 says the tenant copies share Cloudflare's account and migrate to B2 | this card, a dated correction in that ADR only |
| `web/privacy.html`'s destination cell, `docs/CLAIMS.md` §1a's held row | the cell's wording and the date "Backups are off-site" lands | Product lead / Growth (§4.4 carries the exact sentence) |

## 9. Claims

| Sentence | Today | After this ADR |
|---|---|---|
| "Backups are off-site." (`CLAIMS.md`:122) | **in the deck, held from the page** (`CLAIMS.md` §1a, `t_66fcaa02`); not true in fact — the bytes are on HQ's disk | **true in fact when `t_12b3720a` lands** — a box at Hetzner, a different provider from the machine and from HQ, a different account from the DNS zone, in the EU. The page sentence stays held until Product lands it — **this ADR does not publish it** |
| "The encrypted backup is kept 30 days after cancellation, then deleted. Odile can delete it, not read it." (`:123`) | true on the storage path; conditional during a rebuild | true on both paths, because the password never transits HQ: the rebuild is a paste on the machine and `--backup-password-file` is retired. At `keep_until` the repository directory and both sub-accounts are deleted (ODL-935's re-cut); the 30 days and the deletion are unchanged |
| "A recovery code and a recovery file are the way back: … the file opens your backup." (`:65`) | true through the agent | **literally true**: the file carries the URL, the pinned host key and a read-only key, so it opens the backup on any machine. **This is stronger than the deck owns** — Product lands the wording, Savio approves it (§3, a published sentence not in `CLAIMS.md`) |
| "The recovery file holds the backup password and the certificate fingerprint." (`:66`) | true | it now also holds the repository, the box's host-key fingerprint and a read-only key; the sentence needs the extra clause |
| "Backed up nightly. Rebuildable in under an hour — without a key." (`:201`) | measured at 38m03s against a repository on the same host | **at risk**: the number must be re-measured against the real box over a transatlantic link. The model says 15–25 minutes and the sentence probably survives; the drill decides, and if it does not, the sentence moves |
| `docs/MVP-V0.1.md` §6.7 "A restore needs the recovery file and one paste of its password in the browser" | already true at this head | unchanged by this ADR; it is the path §7.2–§7.3 describe, and under v3 the paste carries the key the user's own copy needs |

Nothing here makes the *access* story stronger: no operator key, no escrow, no
recovery account, and the disk and the prompts are untouched. **One property gets
weaker in fact and must not be published as stronger:** "the bytes are in the
same country as the machine" was true of B2 Toronto and is **false** of the
Hetzner box in Finland. No approved row carries it, and this ADR
keeps it that way. The one new claim is about the user's own copy, and it is
theirs to make.

## 10. Reversibility

**Two-way, with two named one-way doors.** Moving a repository is `--repo` (or a
new line in `/etc/odile/backup-repo.txt`) on the next `init`/`run` and a new
seed value; the old directory is kept 30 days and deleted (`backup.md` §7). The
program, the password and the restore command do not change. The read-only key in
a recovery file is additive: a user who never uses it is unaffected, and a file
without the three new lines still parses. **Provisioning is API-driven**, so the
direction of travel v2 called cheap is now *scripted* cheap: deleting a pair is
`DELETE` on two sub-accounts, creating one is `POST` on two, and there is no
dashboard step and no hand-minted credential.

**The one-way doors.** (1) **The box's location** is **Helsinki (`hel1`)**,
fixed at order; moving it to `fsn1` is a new box and a `restic copy`.
(2) **The sub-account ceiling** — 100 per box, fixed across every BX tier — is
why the *tenant* ceiling is 50 per box; scaling tenants is a second box, not an
upgrade. Getting either wrong costs a `restic copy` and a re-seeded URL, not the
data.

**Moving to a second box, in one paragraph.** Per tenant and in this order:
create `odile-<slug>-rw`/`-ro` on the second box with the same
`home_directory`, install the new public keys, `restic init` with the **tenant's
existing password** (so the recovery file's password line stays valid), `restic
copy --from-repo <the first box's repository>` to move every snapshot, run
`restic check` and one restore drill, then update the tenant row and the seed and
have the machine re-render the recovery file with the new `Repository:`, host
key and read-only key — the password line unchanged — keep the first box's
directory for 30 days, and delete the two old sub-accounts. `restic copy`
re-encrypts into the destination repository's key, and because both repositories
use the same password the user's one secret opens both during the overlap. The
same paragraph is the migration from the HQ stand-in in §8, with `--from-repo`
naming the chrooted `sshd`.

**It stops being cheap** at the first tenant whose only surviving copy is a
Hetzner box — i.e. at the first real restore or the first cancellation,
whichever comes first. After that, changing providers is a migration with a
30-day window, not a config change.

## 11. Evidence

Measured, at this head, unless marked *read* or *modelled*:

- The destination and its honesty: `docs/runbooks/backup.md` §3 (the repository
  name does not resolve; the stand-in is HQ's `sshd` on 2222; "the backup survives
  the tenant, not the loss of HQ"), §9.
- HQ's provider, read on 17 November 2026: `getent hosts mail.odile.fyi`, then
  `curl https://ipinfo.io/<that address>/json` → `"hostname": "mail.odile.fyi"`,
  `"city": "Montréal"`, `"org": "AS395904 Servury"`. The shipped tree carries the
  documentation-range alias for that address (`docs/ops/PUBLIC-TREE.md`:85), and
  `docs/runbooks/backup-hq.md` §5 states that HQ is "one vCPU-class box".
- The numbers in §2: `backup.md` §4 (0.931 s), §5.5 (38m03s / 84m13s / 14m24s /
  0.861 s), §8 (156,311 files / 3,139,596,963 bytes; repository
  1,017,111,156 bytes; the `rc=1` password grep; the snapshot ids).
- The include set and its home: `installer/odile_install.py`
  (`DEFAULT_EXCLUDES`; `backup_unit_files` with `home=layout.home`;
  `Layout.tenant` → `/home/<user>`; `DEFAULT_HOME`; the unit's `--env-file`
  filled from `layout.hermes_home`), `deploy/image/odile-release-setup.sh` (the
  installer runs with `--user tenant`), `backup.md` §2.1, and
  `installer/first_visit.py`.
- The timer and the shipped SFTP path: `installer/odile_install.py`
  (`BACKUP_TIMER_TEMPLATE`, `03:00:00` + `RandomizedDelaySec=300`;
  `DEFAULT_KEY_DIR = /etc/odile/backup-ssh`; `backup_repo_file` →
  `/etc/odile/backup-repo.txt`), `scripts/backup.py` (`SFTP_HOST_RE`,
  `sftp_args`, `resolve_repo`, and `command_init`'s `ssh-keygen`/`ssh-keyscan`
  fallback), `seed/seed.py` (`backup_repo_url` templated with the slug).
- The rebuild's password path: `docs/runbooks/offboard.md` §5 and
  `docs/runbooks/backup.md` §5.2, §9; `seed/seed.py:331` (`seal_backup_password`),
  `:508` (`backup_repo_url`), `:883` (the same-slug fill).
- The restore primitives that already exist: `command_restore` in
  `installer/odile_install.py` (`--target`, repeatable `--include`);
  `backup.py init` answering `same` against an existing repository.
- The same-slug rebuild: `docs/architecture/ADR-007-destroyed-slug.md`;
  `t_ce52bb8d` merged at `4dc34ca`.
- **Hetzner, measured on 2026-11-24 06:30:36Z** (read-only, with the project
  token): `GET https://api.hetzner.com/v1/storage_boxes` → **one box: id
  709870, name `odile-backups`, `username` u728180, server
  `u728180.your-storagebox.de`, system `HEL1-BX192`, status `active`, created
  06:25:41Z, `stats` 0 / 0 / 0 bytes**; `GET /storage_boxes/709870` →
  `location` **hel1 / Helsinki / FI**, `subaccounts_limit` **100**,
  `snapshot_plan` **{hour 4, minute 30, max 10}**, `access_settings` {ssh true,
  reachable_externally true, samba/webdav/zfs false}, `protection.delete`
  **false**. Recorded at `/root/.odile/hetzner-box.json` (0600); the token is at
  `/root/.odile/hetzner-api-token` (0600, one `HETZNER_API_TOKEN` line) and the
  account login at `/root/.odile/hetzner-account.env` (0600). The account
  password plus a 6-digit email code were needed once, at 04:57Z; the token is
  what HQ uses, and `GET /storage_boxes` answering is the proof it works.
- **Hetzner, read on 2026-11-24:** `https://www.hetzner.com/storage/storage-box/`
  and `/storage/storage-box/bx11/` — BX11 1 TB, 100 sub-accounts, 10 snapshots
  (10 automated), 10 concurrent connections, unlimited traffic, Germany (FSN1)
  or Finland (HEL1), €3.20/month excl. VAT, €0.0051/hour, no setup fee, no
  minimum contract term, cancellation immediately, restic/Borg/rclone/rsync
  listed; the FAQ's RAID paragraph ("withstand several drive failures", "the
  data is not mirrored onto other servers", block checksums).
- **Hetzner's ports and keys, read on 2026-11-24:**
  `docs.hetzner.com/storage/storage-box/general` — "FTP/FTPS (port 21) and
  SFTP/SCP (SSH port 22) are always active and cannot be disabled"; "The option
  'SSH Support' only enables port 23"; port 22 takes RFC4716 keys, port 23
  OpenSSH keys — and `.../getting-started/creating-a-storage-box` for the same
  facts at order time.
- **The API, read on 2026-11-24:** `https://api.hetzner.com/v1` and
  `https://docs.hetzner.cloud/reference/hetzner` — the storage-box and
  sub-account endpoints (`GET|POST /storage_boxes`, `GET|POST
  /storage_boxes/{id}/subaccounts`, `GET|PUT|DELETE
  /storage_boxes/{id}/subaccounts/{subaccount_id}`), the `access_settings`
  fields (`readonly`, `ssh_enabled`, `reachable_externally`, `samba_enabled`,
  `webdav_enabled`), the "read-access … prevents it from uploading, deleting,
  and modifying files" paragraph, the per-sub-account `authorized_keys` and
  hostname, `stats {size, size_data, size_snapshots}`, `snapshot_plan`, and
  `POST /storage_boxes/{id}/actions/change_protection`.
- **restic's connection default, read on 2026-11-24:** the `sftp` backend's
  `connections` option, "set a limit for the number of concurrent connections
  (default: 5)" (`pkg.go.dev` for restic v0.19.1's
  `internal/backend/sftp`); the image pins 0.18.0, whose default is the same.
- **The R2 interim, 19–23 November 2026, as history:** two buckets
  (`odile-t-odl-7971a8`, `odile-t-odl-bench`) created 2026-11-22; two
  read-write token pairs working on 2026-11-23 (HeadBucket 200 · PUT/GET/DELETE
  200 · `hermes-hq-backup` 403 · `ListBuckets` AccessDenied); **no read-only
  twin minted, no tenant data written**, and the provisioning card never landed.
  **Both buckets measured empty on 2026-11-24 06:46Z — 0 objects, 0 bytes, 0
  multipart uploads — and no live config names them**: `/root/.odile/r2/` is
  absent, `seed-config.json` still points at the SFTP stand-in, there is no
  `backup-s3.env` on HQ, and the demo and bench repositories live under
  `/srv/odile-backup`. They are **slated for deletion in Savio's 24 November
  2026 cleanup, and were not yet deleted at this head**: `HeadBucket` still
  answers **200** on both at 06:56Z, so this ADR claims only what is measured —
  empty and unreferenced, not gone.
  HQ's own `hermes-hq-backup` readback of 19 November 2026 (75 objects /
  1,149,539,702 bytes, one snapshot `8d521571…`) stays in ADR-003, which owns
  that bucket.
- **B2, as history:** the account was suspended at signup; no key was ever
  minted, no byte written; `t_02b8df9d` closed as superseded on 2026-11-24.
- *Modelled, not measured:* the 15–25 minute end-to-end estimate; the
  transatlantic transfer term; the 2–2.5 GB size of a hypothetical full
  archive; the 800 GB / 950 GB readback thresholds; every per-tenant cost
  except the flat €3.20, which is the list price.

## 12. What this ADR does not do

- **No code, no tenant byte moved.** The first honest sentence about this re-cut
  is that it is still a recommendation: the box exists (id 709870, `hel1`,
  empty), the token exists, and **no tenant has a sub-account pair and no byte
  has been written**. `t_12b3720a` creates the first pair, sets
  `protection.delete` and proves the path; the provisioning re-cut automates it.
- **No new claim in the deck.** §9 names the one sentence that gets stronger and
  the one property that gets weaker; both land through Product and Savio or not
  at all.
- **No `docs/MVP-V0.1.md` edit, no `docs/CLAIMS.md` edit, no `web/` edit** —
  all three are outside this card's `touches`; §8's table names the exact
  sentences and the seat that owns each.
- **No second copy** — §4 and §4.5: one box, one credential pair per tenant, one
  drill, and the revisit trigger named. The read-only key is not a second copy;
  it is the user's own way to make one whenever they like.
- **No `backup.odile.fyi`.** The name that never resolved is retired with the
  SFTP stand-in, and the box is addressed by the sub-account's own hostname, so
  the seed carries a URL with no DNS record of ours in it: one fewer name to own
  and one fewer name that can point at the wrong box.
- **No promise that the destination cannot lose the data.** A single RAID box
  can (a rack, a datacentre, a filesystem, an account suspension), and §4.5 says
  so with the mitigation rather than a claim. The residual risk is that the only
  surviving copy is a third party's on one box, and the mitigations are the
  snapshots, the delete protection, the 30-day exit and the user's read-only key
  — a copy they can take at any time without asking.
- **No change to ADR-003's decision.** HQ keeps its own Cloudflare R2 bucket,
  token and drill, and the factory's copy is untouched. **The tenant copies
  leave that account entirely**, which is the property ADR-003 §4.3 argued for
  and §4.4 lost for the interim: after this cut the two copies are at two
  providers again, and neither of them is Servury. ADR-003 §4.4 carries the
  dated correction.
