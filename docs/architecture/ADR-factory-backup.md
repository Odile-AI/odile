# ADR-003 — The factory has an external copy: restic to a Cloudflare R2 bucket

**Status:** Accepted — 10 November 2026. Savio chose **C, Cloudflare R2**, on
card `t_4fa3415f`; the design was re-cut for it by `t_88f4df89`. Implemented by
card `t_63299b60` (ODL-450b): its first live snapshot `97f6c108` and first live
drill were measured on **11 November 2026** against the `hermes-hq-backup`
bucket, and `docs/runbooks/backup-hq.md` §9 carries the numbers. Savio cut
retention to the latest snapshot only on 11 November (card `t_4fa3415f`); §3.4
and §4 state it.
**Answers:** ODL-450 — "name everything on HQ that exists in exactly one place,
contract an external copy that survives a Servury wipe, and specify the restore
drill that proves the copy is real."
**Short answer:** one `restic` repository in a Cloudflare R2 bucket, written
nightly by a systemd timer from this box, encrypted with a passphrase Savio holds
off-box. Every SQLite file is copied with the SQLite backup API into a staging
directory before it is read, so the live board and the eleven seat state
databases are consistent in the snapshot. The set is **2.15 GiB across 34,907
files** measured live on 11 November 2026 (the 10 November rehearsal was 1.14
GiB / 19,381 files; §3.1 has both) — not the 83 GB of disk in use — because the
toolchains, caches and build roots are reproducible. A monthly drill restores a
file set and verifies the board, the waitlist and the signing key against the
manifest; a bare-machine rebuild is runbooked with a 2-hour target. The Chief of
Staff's watchdog runs one command every five minutes and stays silent while the
last snapshot is under 26 hours old.
**Related:** `docs/MVP-V0.1.md` §6.7 (tenant backups — not this), §8 (cost),
§12 D19; `docs/CLAIMS.md` §1 "Reliability" (tenant claim, unchanged);
`docs/AGENT-ORG.md` §3 (a new provider is Savio's); `docs/runbooks/backup-hq.md`
(the executable procedure); `docs/runbooks/deploy.md` §1 (the WAL-safe copy
pattern reused here); ODL-215 (tenant restic backup), ODL-450.
**Decision owner:** Architect for the mechanism; **Savio for the destination and
its credential** — a copy of the secrets outside this machine is a new trust
boundary (`docs/AGENT-ORG.md` §3). He answered C on 2026-11-10; he still owns
the bucket and the token that make it real (runbook §2 steps 1–2).

---

## 1. The invariant at risk

Three sentences the product depends on, and what this machine is worth to each:

1. **The published trust anchor stays signable.** The public half of
   `installer/odile-signing-key.pub` is published at
   <https://odile.fyi/odile-signing-key.pub> and travels in every tenant's
   recovery kit. Its private half exists in exactly one file on this disk. Lose
   that file and the anchor is dead: no future release can be signed under the
   key tenants already pinned, and the only repair is a key rotation announced to
   every tenant.
2. **The factory can be rebuilt without asking anyone for a secret.** The product
   history, the board's decisions, eleven seats' memory and the mail/DNS/provider
   credentials are all HQ-local.
3. **Nothing new is trusted by accident.** The copy leaves this machine, so a
   provider now holds ciphertext of HQ's secrets. That is a new trust boundary,
   and it is why this ADR cannot be accepted by an agent.

**What a Servury wipe costs today, in order:** the installer signing key (one
file), the product history (no remote), the board (166 cards, 123 done, the only
record of why), the seats (memory and sessions), the waitlist (real email
addresses), then the public services (`odile.fyi`, `mail.odile.fyi`,
`demo.odile.fyi`).

**What this ADR does not change.** No tenant machine is touched, no operator key
is created, and no public sentence moves: the "Backed up nightly. Rebuildable in
under an hour" row in `docs/CLAIMS.md` is about a **tenant** guest and ODL-215's
restic repository. This ADR is the factory, and its numbers are internal.

## 2. What exists in exactly one place

Measured on 2026-11-10 against this machine. **Bytes are file bytes**
(`du -sb`: the sum of file sizes, the measure restic reports as a snapshot's
size), not allocated blocks — `/etc` is 2.6 MB here and 6.9 MB on disk, `.git`
98 MB and 112 MB. The staging manifest and the rehearsal in §8 are the other two
sources. "Reproducible" means *without this disk*: from the repo, a vendor
console, or a package archive. A **no** is why the backup exists.

| Path | Bytes | What it is | Reproducible? |
|---|---:|---|---|
| `<checkout>/.git` | 98 MB | the whole product history, 5–10 Nov; no remote exists | **no** |
| `<checkout>` working tree | 78 MB | tracked files plus the host-only `docs/growth/` workspace | **no** (uncommitted edits, growth logs) |
| `/root/.odile/installer-signing.pem` | 119 B | **the one-of-one** private half of the published anchor | **no — never** |
| `/root/.odile/cloudflare_dns_token` | 54 B | DNS automation for every tenant record | vendor console (Cloudflare login) |
| `/root/.odile/servury_reseller_key` | 65 B | creates and destroys tenant machines | vendor console (Servury login) |
| `/root/.odile/gmail_app_password` | 17 B | mail relay login | vendor console (Google) |
| `/root/.odile/{cloudflare_account_id,seed-config.json,config.yaml,tenants/,mail_test_account.json,hackathon-submission.json}` | 3.0 KB | account id, tenant seed template, tenant rows, submission record | partly vendor, partly **no** |
| `/root/.odile/{first-tenant,review-evidence,spike-310,installer-sim,installer-sim-396}` | 2.5 MB | first-tenant evidence, review artefacts, ODL-310 spike, installer simulations | partly (repo + runbooks) |
| `/root/.hermes/kanban/boards/odile/kanban.db` | 6.4 MB | the board: 166 tasks, 123 done, every decision and comment | **no** |
| `/root/.hermes/kanban/boards/odile/attachments/` | 15 MB | files cards carry (reports, images, exports) | **no** |
| `/root/.hermes/profiles/*/` (state.db, projects.db, cron and Matrix E2EE databases — 36 databases) | 800 MB | each seat's sessions, memory, notes, tool history, and its Matrix device keys | **no** |
| `/root/.hermes/state.db{,-wal}` | 102 MB | the default profile's sessions and state | **no** |
| `/root/.hermes/{config.yaml,.env,auth.json,SOUL.md}` | 164 KB | model pins, platform tokens (Matrix, Telegram, API keys), identity | **no** |
| `/root/.hermes/cron/` | 835 KB | every scheduled job and its run history | **no** (the schedule is the product's heartbeat) |
| `/root/.hermes/{skills,platforms,sessions,projects.db,shared-state.db,runs_idempotency.db}` | 4.1 MB | shared skills, Matrix E2EE keys (`platforms/*/store/crypto.db`), sessions | **no** for the E2EE keys; skills partly in the repo |
| `/root/.hermes/profiles/*/{skills,memories,sessions,cron,config.yaml,.env,projects.db,platforms}` | 110 MB | the seats' own playbooks, memories and credentials | **no** |
| `/root/.ssh/` | 967 B | HQ host keys, `authorized_keys`, and the ed25519 key pair generated for the rejected SFTP option (unused since Savio chose R2 — it opens nothing) | **no** (host keys change every SSH fingerprint) |
| `/root/.config/` | 2.0 MB | the gateway's systemd **user** unit, the GitHub CLI token (`gh/hosts.yml`), Caddy and Cloudflare state | **no** |
| `/root/.gitconfig` | 276 B | the git author identity (`savio-agent`) and the `gh` credential helper | **no** (the name and address are in no repo) |
| `/root/bin/` | 34 KB | the host's operator scripts (verification and probe helpers) | **no** (host-only) |
| `/etc/` | 2.5 MB | Caddyfile, DKIM private key, mail TLS key, Postfix maps, systemd units, SSH host keys, machine-id | **no** (only the unit files are in the repo) |
| `/var/lib/caddy/` | 35 KB | the `odile.fyi` certificate and key | yes via ACME, but only after DNS points here |
| `/var/lib/odile/waitlist.db` | 172 KB | the waitlist: real email addresses (2 rows today) | **no** |
| `/var/lib/odile/{attestation.json,mail,outbox}` | 64 KB | the last real SNP report, mail spool | partly |
| `/home/odiledemo/` (state only) | 11 MB | the demo machine's config, key and workspace; resets daily | rebuildable from `deploy/demo-host/` + `seed-config.json`, minus its spendable NEAR key |
| `/var/lib/odile-demo/meter.db` | 20 KB | demo inference usage | **no** |
| `/srv/odile-demo/` | 39 KB | the demo's static sign-in page, served by Caddy before the gate | yes — `deploy/demo-host/login.html`, byte-identical |
| `/srv/odile-testing/` | 121 KB | the audited M-1 prototype **as served**: `index.html` (`9ab262d8…`) and `robots.txt` — **absent on this box since 2026-11-11**, §3.1 | yes from git history (`git show 744b53a:prototype/userflow.html`), **no from `main`** — the source moved to `4cf9f20…` in ODL-237; a `file_server` block makes the deployer pin the audited hash and refuse the difference, and the committed block is the retirement redirect, so it stages nothing |

**Held by a vendor, therefore not a single copy on this box:** the DNS zone
(Cloudflare: 5 A records — four on HQ, one a Servury tenant guest — 1 AAAA,
1 CNAME, 2 MX, 3 TXT: SPF, DMARC, DKIM), the tenant VMs (Servury; ODL-215 backs
up the guest disk), the Hermes source (GitHub, commit `2bd378ed1b8d`, v0.23.0),
and every Ubuntu package.

**The rule this table states.** A file earns its place in the backup only by
being *irreplaceable without this disk **and part of the product or its
operation***. Google Drive, a repo clone and an apt archive already hold the rest;
copying them nightly would quadruple the set and teach nobody anything.

## 3. The backup set: include, exclude, and the live-database problem

### 3.1 Include

The set is defined by roots, not by an itemised file list, so a new seat, a new
cron job or a new skill is captured without a code change:

```
<checkout>  /root/.hermes  /root/.odile  /root/.ssh  /root/bin  /root/.config
/root/.gitconfig  /etc  /var/lib/caddy  /var/lib/odile  /srv/odile-testing
```

**Amendment, 2026-11-14 (t_9d7ee536).** The three HQ-demo roots —
`/var/lib/odile-demo/meter.db`, `/home/odiledemo`, `/srv/odile-demo` — left the
set with the HQ demo's code; the demo itself was retired on 2026-11-13. The
per-path rows above are the 2026-11-10
measurement; a snapshot taken before this amendment still carries them, and
`run` skips a missing root by design.

`/root/.config` is a root because the gateway is a root **user** service: its
unit file, the GitHub CLI token (`gh/hosts.yml`) and the Caddy/Cloudflare state
live there, and a rebuilt box without them has no dispatcher and no `gh`.
`/root/.gitconfig` is a root because it is the authorship of every commit the
box makes; 276 bytes to keep the product history's author rather than
`root <root@mail.odile.fyi>`.

The `/srv` deploy target is a root for the same reason a deploy target is
usually *not*: it is the bytes a rebuilt box must serve immediately, and it is no
longer reproducible from `main`. `/srv/odile-testing/` is the audited prototype
as served — `prototype/userflow.html` on `main` moved to `4cf9f20…` after
ODL-237. When the site block serves files (`file_server`),
`deploy/caddy/deploy_testing_host.py` pins the audited `9ab262d8…` and exits 2
on the difference, so it deliberately refuses to rebuild the page from `main`;
the committed block is now the retirement redirect, so against it the deployer
stages nothing and exits 0. 160 KB total; the site is then up before any
deployer runs.

**That root is now absent here.** `/srv/odile-testing` was removed with
`testing.odile.fyi` on 2026-11-11 (main `82925f1`, 06:15 UTC; the live
`/etc/caddy/Caddyfile` answers a permanent redirect to `demo.odile.fyi`), before
the first live snapshot at 09:51 UTC. The repository therefore holds **13 of the
14 roots** — `run` prints `root missing, skipped: /srv/odile-testing` and
snapshots the rest — and **no snapshot in it carries the audited page**. The
root stays in the list because the list is the box's definition, not a census:
a box that still serves the page captures it, and re-creating the directory
before a run puts it back in the set. What a rebuilt box gets depends on the
snapshot it restores: from a **pre-retirement** snapshot that still holds the
root, the audited page comes back with everything else (it was never deployed);
from the current repository it gets the redirect restored from `/etc/caddy` and
thirteen roots. The audited bytes are always in git history —
`git show 744b53a:prototype/userflow.html` — and that is not the backup.

The 2026-11-10 rehearsal measured **1.14 GiB in 19,381 files**, of which **856
MiB (897.4 MB) is the staged database set** (§3.2) and **314 MiB is the tree**.

Live result, first consolidated snapshot on 2026-11-11: **2.15 GiB in 34,907
files**, of which **1.22 GB is the staged database set** (68 databases, up from
61) and **~0.94 GiB is the tree**. The set definition did not change; the box
grew — `/home/odiledemo/probe-home`, a 417 MB probe fixture with 24,340 entries,
sits inside the `/home/odiledemo` root and is therefore captured. The snapshot
stores **643 MB** (compression 2.91x), still well inside R2's 10 GB-month free
tier.

### 3.2 The live-database problem, and the WAL-safe answer

Eleven Hermes profiles and the board run with SQLite in WAL mode; the gateway
writes continuously. `cp` of a live `state.db` can capture a torn page set, and
restoring it gives a database that opens but fails `integrity_check` months
later, when it matters. Restic reading the file has the same problem.

So the run is two-phase, and the tree snapshot never contains a live database:

1. **Stage.** `/usr/bin/python3` walks the included roots and, for every
   `*.db`/`*.sqlite`, opens the source read-only and copies it with the SQLite
   **backup API** (`sqlite3.Connection.backup`), the same mechanism
   `docs/runbooks/deploy.md` §1 uses for the waitlist. The copy lands in
   `/var/backups/odile/live/` under a flat name derived from its absolute path
   (`root__.hermes__kanban__boards__odile__kanban.db`), so two boards can never
   collide. A non-SQLite `*.db` (Postfix's `aliases.db`, `virtual.db` hash maps)
   is copied whole and marked `mode: copy`. Each staged copy is switched to
   `journal_mode=DELETE` and any `-wal`/`-shm` sidecar is removed, so the stage
   holds **exactly one file per database plus `manifest.json`** — the `dbs`
   snapshot has no torn WAL sibling next to a consistent copy.
2. **Record.** `manifest.json` holds, per database: source path, staged name,
   bytes, sha256, `PRAGMA integrity_check`, and row counts — `waitlist`, and for
   a board `tasks` **and** `done` (`count(*) … where status='done'`), plus
   `sessions`, `messages`, `memories` where the table exists. A board judged by
   `tasks` alone cannot tell a full board from one that restored without its
   statuses, so `done` is a manifest field, not a transcript number. The
   manifest also records `git_head`, the commit at staging time; the drill
   accepts that commit **or a descendant of it**, because a commit landing in the
   seconds between staging and the tree snapshot is benign and the recorded
   commit is in the snapshot's history. The drill compares the restored database
   against the manifest — not against today's live numbers.
3. **Snapshot.** One restic snapshot per run (tag `hq`): the staging directory
   and the roots in a single call, with the database patterns anchored to each
   root (`/root/.hermes/**/*.db*`, `/root/.hermes/**/*.sqlite*`, …) so the tree
   carries no live database while the staged copies survive. Exactly one copy of
   every database exists in the repository: the consistent one. One snapshot is
   also why `restic restore latest --target /` is a complete restore — Savio's
   decision on 2026-11-11.

Measured cost of staging on this box: **11.4 s, 61 databases, 897.4 MB** (§8);
live on 2026-11-11, **14.3 s, 68 databases, 1.22 GB** (runbook §9).

### 3.3 Exclude — every exclusion with its reason

| Excluded | Bytes (allocated) | Why |
|---|---:|---|
| `<checkout>/.worktrees` | 1.4 GB | derived worktrees; the branch is the durable unit. Uncommitted work in flight is lost by design — RPO for agent work is its last commit, and the board says what was in flight. |
| `/root/.hermes/{tools,installs}` | 2.2 GB | pinned toolchains and plugin venvs, reinstalled by the Hermes installer from pins in `config.yaml`. |
| `/root/.hermes/hermes-agent` | 2.3 GB | a git clone of `NousResearch/hermes-agent` at `2bd378ed1b8d` / v0.23.0 — reproducible from GitHub. |
| `/root/.hermes/{cache,images,audio_cache}` and `profiles/*/{cache,lsp,logs,image_cache,audio_cache}` | 3.8 GB | caches, language servers, logs and generated media; deliverables live in the board's `attachments/`, which is included. |
| `models_dev_cache.json` (root and per profile) | 64 MB | the model catalogue; refetched. |
| `/root/.hermes/kanban/boards/*/workspaces` | 240 MB | task worktrees, re-created by the dispatcher from the branch. |
| `/root/.odile/meter-v0` | 1.2 GB | ODL-205's test fixtures (four copies of a Hermes home); reproducible from the meter runbook. |
| `/root/.odile/x-profile` | 190 MB | a Chromium profile for the X poster; a cache with a login. |
| `/root/.odile/installer-sim{,-396}` | 240 KB | installer simulation fixtures, reproduced by the installer's own tests. |
| `/srv/probe-hermes-home`, `/srv/probe2` | 930 MB | ODL-310 spike Hermes homes. |
| `/home/odiledemo/.hermes/{tools,hermes-agent,installs,cache,logs}`, `/home/odiledemo/.npm` | 3.9 GB | the demo's copy of the same toolchains — inside the loop image below |
| `/var/lib/odile-demo/home.img` | 4.1 GB (6 GiB sparse) | the demo's loop-mounted home image; its unique files are captured from the mount instead. |
| `/opt/odile` | 345 MB | the deploy target; `./scripts/deploy.sh` recreates it from the repo. |
| `/root/.cache`, `/root/.npm`, `/root/.local/share/{uv,zed}` | 400 MB | tool caches. |
| `/var/tmp` build and scenario roots (`bb`, `odile-sim*`, `odl214-*`, `verifier-odl*`, rehearsal dirs) | 48 GB | disposable scratch: rebuilt by the install, manifest and image runbooks; the evidence they produced is committed in `docs/evidence/`. The largest and most variable item on the disk, and never product state. |
| `/var/lib/containers` | 4.3 GB | the `localhost/odile-tenant:v0.23.0` release image and the Debian base build — built from `deploy/image/Containerfile`. |
| other `/root` trees outside the roots (`epic-automation`, `.zed_server`, `jobsearch`, `odile-paintings`, `odile-review`, hand-saved `*.db` snapshots) | 190 MB | personal or one-off trees with no product role; this is not a home-directory copy. |
| `/root/.local/bin` | 1.0 KB | 13 one-line wrappers (`exec hermes -p <seat> "$@"`); the dispatcher addresses profiles by name and works without them. |
| `/root/backups`, `/var/backups` (including the ~870 MB nightly stage) | 4 MB + stage | local staging and the deploy-time DB copies — on the same disk, therefore not a backup. |
| `node_modules`, `__pycache__`, `*.pyc`, `*.sock`, `*.pid`, `*.lock` | — | rebuildable or transient. |

Root-level `/root/.hermes/{lsp,logs}` (86 MB) stay in the set: small enough that
another exclusion pattern is not worth the risk of an over-broad one.

Total excluded: **about 82 GB of the 98 GB this disk reports** (83 GB in use on
2026-11-10) — toolchains, caches, build roots, container layers and the demo's
loop-mounted home image (its 3.9 GB of toolchain is inside the 4.1 GB image,
counted once) — which is why the nightly set is 1.1 GiB and not a copy of the
disk.

### 3.4 Growth, retention, and the one measurement that matters

Retention is `restic forget --keep-last 1 --prune --group-by host`: **the latest
snapshot only**, Savio's decision on 2026-11-11 ("I just need the latest
snapshot, no need to keep older ones"). `run` writes one snapshot per night, so
the policy keeps exactly one, whichever path set it holds; `--group-by host` is
what makes that "one for this host" rather than one per path set (it also
forgets the `dbs`/`tree` pair written before the consolidation). Growth is new
and changed bytes, not kept generations: restic stores each file once and
dedupes across snapshots, so the repository stays roughly the size of the newest
snapshot. Measured live on 2026-11-11: **643 MB stored** for 2.15 GiB of input
(compression 2.91x), pruned each night. R2's 10 GB-month free tier is roughly
fifteen times the set; above 10 GB it is $0.015/GB-month.

The measurement that matters is not the size but the **age**: the freshness check
in §6 alarms when the newest snapshot is 26 hours old, whatever its size.

### 3.5 Idempotency and failure behaviour

`run` is a function of the disk, not of a cursor: running it twice produces two
valid snapshots and no corruption; running it after a failure produces one
complete snapshot (the staging directory is wiped and rebuilt each time). If the
R2 endpoint is unreachable the run exits non-zero and **does not update**
`status.json`, so the watchdog alarms on age rather than on a lie. If staging
fails `integrity_check` the run exits before uploading, and the previous night's
snapshot remains restorable.

## 4. The destination

### 4.1 One destination: a Cloudflare R2 bucket

Live set from §3.1: **2.15 GiB across 34,907 files**, **643 MB stored** after the
first consolidated snapshot, and the latest-only retention keeps it flat. That
number is what the destination has to fit, and it is small enough that the right
answer is one bucket, one credential and one drill.

| | **Cloudflare R2** |
|---|---|
| Vendor, location | Cloudflare; **Location** left automatic (or a region hint) and **Jurisdiction** default or **EU**, both chosen at bucket creation |
| Protocol | S3 API over **HTTPS 443** at `https://<account_id>.r2.cloudflarestorage.com`, bucket region `auto`; restic's built-in `s3` backend |
| Capacity, minimum | none; 10 GB-month of storage free, 100,000 buckets per account included |
| Monthly cost at this size | **$0.00** today: **643 MB stored** for a 2.15 GiB set sits inside the 10 GB-month free tier. Retention is latest-only (§3.4), so the bill is the size of the newest snapshot, not a growing shelf — at the measured 2.91x compression the free tier holds a set roughly fifteen times today's. Above 10 GB it is $0.015/GB-month: 18 GB stored would be **$0.12/month**. Egress is free; 1 M Class A and 10 M Class B operations are free per month, and a nightly run touches hundreds of objects. |
| Vendor-side durability | R2 Standard storage; object versioning exists and stays **off** unless Savio turns it on (restic's own history is the versioning we use) |
| Who can read the ciphertext | Cloudflare (storage operator); anyone holding the repository passphrase |
| Who can delete it | Cloudflare; Savio in the dashboard; Odile root on HQ via the token |
| Credential on HQ | one R2 API token — Access Key ID + Secret Access Key, permission **Object Read & Write**, restricted to the one bucket |
| Card on file | Cloudflare requires a payment method to enable R2 even at $0.00 spend — Savio only |
| Signup friction | none new: the Cloudflare account already exists for DNS; enabling R2 and creating one bucket is minutes |

**Why one destination, not two.** A second copy doubles the credentials, the
network paths and the things that can be wrong at 03:30, and it still needs the
same monthly drill to prove either copy is real. At 2.15 GiB of input and 643 MB
stored, the second copy buys nothing that the drill does not buy more cheaply.

**What the token must be, exactly.** It is minted in the R2 dashboard; it is not
the DNS token and cannot be derived from it. `/root/.odile/cloudflare_dns_token`
is zone-scoped, and `GET /client/v4/accounts/<id>/r2/buckets` answers **403
Authentication error** with it (§8). The token must be **Object Read & Write** —
read-only cannot `init` or `prune` — and restricted to the backup bucket, so a
compromise of HQ can overwrite or delete the backup but cannot read or write any
other bucket and cannot touch DNS.

### 4.2 Alternatives considered and rejected

| Alternative | Why not |
|---|---|
| `rsync -a` to a box, no restic | No client-side encryption (the host reads the signing key), no snapshots (a deleted or corrupted file is gone with the next sync), no dedupe. A partial transfer is a plausible-looking corrupt backup. |
| `git push` the repo to GitHub and call the rest done | Covers 173 MB of the 1.1 GiB — not the board, not the seats, not the secrets. Putting `/root/.odile` in a git history means the signing key sits in a third party's object store and can never be rotated out of history. |
| Borg to the same destination | Equivalent tool, one more binary and a different repository format; restic is in Ubuntu's archive and ODL-215 already uses it for tenant guests — one tool, two uses, one runbook idiom. |
| **Hetzner Storage Box BX11, or rsync.net** — SFTP on port 23, restic's native backend | Rejected by Savio's decision C on 2026-11-10. It is €3.20/month (≈$3.58 net) for 1 TB of capacity roughly 900× the set, against $0.00 on R2, and SFTP brings an SSH key, an SSH alias, a pinned host-key fingerprint and port 23 through whatever network the restore machine sits on — four moving parts where HTTPS has one. Its one real advantage, a key confined to one directory, is matched by an R2 token confined to one bucket. Kept in this table because it is the option to revisit if the S3 credential ever becomes the thing that breaks at 03:30. |
| **Backblaze B2** — the documented fallback, not the choice here | The same restic tool and the same S3 backend, so switching is one line and a fresh `restic init` (§7). 10 GB free, $6.95/TB-month, egress free up to 3× stored, free API calls. Not chosen here because Cloudflare already has an account, a card and the DNS zone, and B2 is a second console for a saving of nothing at this size. It is the fallback precisely because it shares **no** account with our DNS. **19 November 2026:** B2 is now the **tenant** target (ADR-004 §4) once Savio's suspended account is restored; R2 is the tenant interim, not this bucket. |
| A second Servury box | The same provider, the same reseller API, the same account: a Servury wipe or suspension takes both. The backup must survive the provider, not duplicate it. |

### 4.3 The trust boundary this creates

- **What Cloudflare holds:** ciphertext (restic AES-256 in CTR mode with
  Poly1305-AES authentication, scrypt-derived key), the repository's size, the
  bucket's name, and the times of access. It learns that a machine backs up
  nightly and roughly how much changes.
- **What Cloudflare does not hold:** the passphrase, the plaintext, the file
  names (encrypted), or any way to produce a valid snapshot — a malicious host
  can delete or withhold, not forge.
- **The coupling this adds, stated plainly.** Cloudflare already held the DNS
  zone — five A records, one AAAA, a CNAME, two MX and three TXT (§2), including
  the record that makes `odile.fyi` resolve here. It now holds the backup
  ciphertext too, under the same account and the same login. **What it does not
  widen:** Cloudflare gains no ability to read a backup — the passphrase stays in
  Savio's kit — and it could already darken the site by editing DNS, so the
  *availability* blast radius is unchanged. **What it does widen:** the recovery
  path now shares a fate with the thing it exists to survive. One compromised
  Cloudflare login, or one billing suspension, can both repoint the records and
  delete the only off-site copy — two failures that used to need two vendors. The
  mitigations are the ones this ADR already requires: 2FA on the Cloudflare
  account (Savio), a token scoped to one bucket, a passphrase that lives off-box,
  and a monthly drill that proves the bucket restores. If that shared fate ever
  becomes unacceptable, B2 is the fallback and the switch is one line (§7).
- **Where the passphrase lives off-box:** Savio's password manager **and** a
  printed card he keeps. That is the only copy that survives a wipe.
- **What lives on HQ:** `/root/.odile/hq-backup.pass` (root, 0600) so the
  nightly job can run unattended, and `/root/.odile/r2-backup.env` (root, 0600)
  holding `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `R2_ENDPOINT`. Both
  are inside the encrypted snapshot, and that snapshot cannot decrypt itself: the
  copies inside it are reachable only with the passphrase from Savio's kit. A
  compromise of the running box reads both; a compromise of the bucket alone
  yields neither, and cannot read the passphrase. This is stated, not hidden.
- **The token:** one R2 API token, Object Read & Write, scoped to the
  `hermes-hq-backup` bucket, in `/root/.odile/r2-backup.env` (root, 0600). It
  opens one bucket. "Read & Write" includes delete, so it can destroy the
  repository's objects; it cannot reach another bucket and cannot touch DNS. It
  is not a tenant credential and does not touch the access model.
- **The key that is no longer used:** the ed25519 pair generated for the SFTP
  option (`/root/.ssh/odile-backup_ed25519`, 411 B plus a 97 B public half) was
  never installed anywhere. It opens nothing; it may be deleted or kept, and §2
  lists it so that row stays a measurement rather than a guess.

### 4.4 19 November 2026 — the tenant interim shares this account, and what that changes

**ADR-004 was re-cut on 19 November 2026** (`ODL-915`): Savio's Backblaze B2
account was suspended at signup, so the tenant's interim destination is
**Cloudflare R2, in this same account**, for at most three tenant buckets, under two
constraints he set — HQ's `hermes-hq-backup` is never modified or deleted, and
total R2 usage stays at or below **9 GB**.

**What this ADR's decision still is.** `hermes-hq-backup` remains one bucket in
the **EU jurisdiction**, with one **Object Read & Write** token in
`/root/.odile/r2-backup.env` scoped to it, one nightly job, one drill, one
passphrase in Savio's kit (§4.1, §4.3, §7). Nothing in this section changes that
token, that bucket or that schedule; the interim does not mint a second copy of
HQ's backup and does not touch the existing one.

**What the interim adds, and how it is kept apart.** Up to three tenant buckets
named `odile-t-<slug>` in the account's **default jurisdiction**, with two
bucket-scoped tokens each, minted by Savio in the dashboard. Three separations
hold at once: the **name** (`odile-t-*` vs `hermes-hq-backup`), the
**jurisdiction endpoint** (`<account_id>.r2.cloudflarestorage.com` vs
`<account_id>.eu.r2.cloudflarestorage.com`) and the **token resource string**
(`…_default_odile-t-<slug>` vs `…_eu_hermes-hq-backup`). No tenant token can
name HQ's bucket, and HQ's software holds **no account-wide credential** — only
its own bucket token and each tenant's bucket token.

**What the shared fate now covers.** §4.3's sentence — one compromised login or
one billing suspension can repoint the records and delete the only off-site copy
— now applies to the tenant copies too. Savio accepted that for a bounded interim;
ADR-004 §4.1 caps the blast radius (2.0 GB per repository, three buckets, alert at
8.0 GB, critical at 9.0 GB) and the B2 cutover ends it. HQ's 1.15 GB counts
against that 9 GB, so this ADR's bucket growth is now inside the same cap.

**The readback that proves the bucket is untouched** is in ADR-004 §4.2: on
19 November 2026, 75 objects / 1,149,539,702 bytes, one snapshot `8d521571…` at
03:35:00Z, with the HQ token answering `AccessDenied` to `ListBuckets`.

**When B2 is restored** the tenant buckets migrate to B2 (ADR-004 §10) and this
account holds only HQ's backup again; §4.3's original two-provider argument then
describes the live state once more.

## 5. The restore drill

Two drills, because they prove different things: a **file restore** proves the
repository is readable and the bytes are the bytes; a **bare-machine rebuild**
proves the machine is disposable.

### 5.1 File restore (monthly, and the first live run)

Executable by a stranger who has the repository passphrase and the R2 token from
Savio's kit. The exact commands and expected output are in
`docs/runbooks/backup-hq.md` §4; the shape is:

```sh
export RESTIC_REPOSITORY='s3:https://<account_id>.eu.r2.cloudflarestorage.com/hermes-hq-backup'
export RESTIC_PASSWORD_FILE=/root/.odile/hq-backup.pass
export AWS_DEFAULT_REGION=auto
export AWS_ACCESS_KEY_ID=<from /root/.odile/r2-backup.env>
export AWS_SECRET_ACCESS_KEY=<from /root/.odile/r2-backup.env>
restic snapshots --latest 1
restic restore latest --target /var/tmp/odile-drill
<checkout>/tools/backup_hq.py drill --from /var/tmp/odile-drill/var/backups/odile/live
```

`drill` checks, from the restored copies: `integrity_check` = `ok` on the board
and the waitlist; the board's `tasks`/`done` counts equal the manifest's; the
waitlist count equals the manifest's; `git -C <restore><checkout> rev-parse
HEAD` is the commit the run recorded **or a descendant of it** (a commit landing
between staging and the tree snapshot is benign; the recorded commit is in the
snapshot's history); and the restored `installer-signing.pem` derives a public
half hashing to
`13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2` — the
published anchor. It writes `/var/lib/odile/backup/drill.json` with the time,
the snapshot ids and the verdict. **A drill that fails is a backup that does not
exist**, and the freshness check says so (§6).

**Measured, 2026-11-10, from a local repository with the same commands:** restore
of 1.14 GiB / 19,381 files in **6.7 s** (3.75 s tree + 2.94 s databases);
`restic check` 0.83 s; `check --read-data-subset=10%` 1.0 s. From R2 the same
restore adds the download: this box measured **40.2 MB/s down / 66.8 MB/s up**
to Cloudflare's speed endpoint on 2026-11-10, so 1.1 GiB is ~30 s at the
measured rate and ~2 minutes at a pessimistic 10 MB/s.

**Measured live, 2026-11-11, from R2:** the one-snapshot restore of 2.15 GiB /
34,907 files took **16 s** and the whole drill **29.3 s**, both checks included;
`restic check --read-data-subset=10%` took **7.5 s** and found no errors. See
runbook §9 for the transcript.

### 5.2 Bare-machine rebuild (quarterly, and after any real loss)

The runbook (`docs/runbooks/backup-hq.md` §5) carries every command: a fresh
Ubuntu 26.04 box, `restic` from apt, the same passphrase, `restic restore
--target /`, the database restore step, the Hermes installer at
`2bd378ed1b8d`, the selected `/etc` paths, the `/srv` deploy targets the
snapshot carries (`/srv/odile-demo` today; §3.1 explains the other), the
systemd units, the demo redeploy and site convergence, and the DNS repoint in
Cloudflare (the four HQ A records, one AAAA, the SPF `ip4:`/`ip6:` tokens).
Expected end state: `https://odile.fyi/` 200, `/attestation` serving,
`https://demo.odile.fyi/login` 200 with Odile's sign-in page and the gate at
302/401, `https://testing.odile.fyi/` answering a **301** to
`https://demo.odile.fyi/` (retired 2026-11-11; a snapshot that still carries
`/srv/odile-testing` serves the audited page instead), `hermes kanban --board
odile list` showing the same task and done counts as the drill, and the same
signing-key hash.

**RTO target: 2 hours** from "the box is gone" to "the site serves again",
excluding the time Savio takes to create the replacement box. Measured
components on this hardware: restore ~2 min over the network, apt + Caddy +
Postfix/OpenDKIM ~5 min, Hermes install ~10 min, the demo redeploy ~10 min (its
toolchain is excluded and reinstalled), service start and verification ~10 min,
DNS repoint and TTL ~5 min (plus Savio's 5 minutes). That is ~45 minutes of work
in a 2-hour budget; the extra hour is the honest allowance for a step that
surprises us.

**RPO: 24 hours**, and the bound is measured, not assumed — at any moment the
newest snapshot is at most 26 hours old or the watchdog has already spoken
(§6). The worst case is a loss at 03:29 UTC: the previous night's snapshot,
24 h of board comments and state.db writes. The worktree rule means agent work
has a **commit-level** RPO, not 24 h: an uncommitted worktree is not in the
backup and is re-dispatched from the board.

### 5.3 What the drill proves, and what it does not

Proves: the repository decrypts, the databases are consistent, the board and the
waitlist survived, the signing key still matches the published anchor, and this
runbook is executable by someone who did not write it.

Does not prove: that the snapshot contains *everything* (the exclusions in §3.3
are a judgement, re-read them when a new kind of state appears), that the vendor
keeps the bytes (the vendor-side snapshots and `restic check` are the evidence),
or that a rebuilt box is byte-identical to this one (it is not, and does not need
to be).

## 6. Freshness: what the Chief of Staff's watchdog runs

**One command, every five-minute tick, no network, no model:**

```sh
<checkout>/tools/backup_hq.py status
```

It reads `/var/lib/odile/backup/status.json` (written by the last `run`),
`check.json` (weekly `restic check`) and `drill.json` (last drill), and prints:

- **nothing, exit 0** — when the newest snapshot is ≤ **26 h** old, the last
  `restic check` ≤ **8 d**, the last drill ≤ **35 d and its verdict is `pass`**;
- **one line, exit 1** — naming only the clauses that failed, e.g.

  ```
  HQ-BACKUP: snapshot 31.2 h old (max 26 h); drill 38 d old (max 35 d) — docs/runbooks/backup-hq.md §6
  ```

`odile-watch.py` appends that line to its alert list (deduped for 6 hours like
every other alert), so a stale backup is one line, not 288 a day. **Silent when
fresh** is the contract: an empty stdout means cron delivers nothing and the
pulse has nothing to report.

Thresholds are chosen from the schedule, not from taste: the timer fires at
03:30 UTC with up to 10 minutes of jitter, so 26 h tolerates one missed run plus
a slow one. The 8-day check and 35-day drill tolerances are one missed weekly or
monthly slot.

## 7. Decision, consequences, reversibility

**Decision.** Adopt restic to a Cloudflare R2 bucket —
`RESTIC_REPOSITORY='s3:https://<account_id>.eu.r2.cloudflarestorage.com/hermes-hq-backup'`,
bucket region `auto`, credentials in `/root/.odile/r2-backup.env` — nightly at
03:30 UTC, with the include/exclude set, staging manifest, retention and drills
above, and the freshness check in the Chief of Staff's watchdog. **Savio chose C
(Cloudflare R2) on 10 November 2026** on card `t_4fa3415f`; Hetzner BX11 and
rsync.net were the alternatives priced for him, and Backblaze B2 is the
documented fallback.

**Consequences.**

- A second copy of HQ's secrets exists off this box, in ciphertext, under a
  passphrase Savio holds. That is the point, and it is a new trust boundary: the
  vendor can delete or withhold the copy, and Savio can lose the passphrase.
- It is not a new company: Cloudflare already holds the DNS zone and now holds
  the backup ciphertext under the same account. §4.3 says what that does and does
  not widen; the short version is that recovery now shares a fate with DNS, and
  the monthly drill is what keeps that honest.
- Savio holds one new standing credential — the R2 token — beside the one he
  already had, the repository passphrase. Cloudflare needs a card on file even at
  $0.00/month. Nothing is added to a tenant machine.
- The box now runs one more nightly job and reaches one more host:
  `<account_id>.r2.cloudflarestorage.com` over HTTPS 443. That host is the only
  new network path — no port 23, no SSH, no shell anywhere.
- `restic` becomes a dependency (in Ubuntu's archive, 0.18.1 already installed
  here). One binary, no daemon, S3 backend compiled in.

**Reversibility: two-way, cheaply.** The repository format is restic and the
destination is one environment variable. Changing destination = write the new
`RESTIC_REPOSITORY` line in `/etc/odile/backup.env`, put the new key pair in
`/root/.odile/r2-backup.env`, `restic init`, one full snapshot, one drill; keep
the old bucket for a month. **The switch back to B2 is exactly that one line:**
`RESTIC_REPOSITORY='s3:https://s3.<region>.backblazeb2.com/<bucket>'`,
`AWS_DEFAULT_REGION=<region>`, a B2 application key in the same two variables, a
fresh `restic init`, one full snapshot and one drill — no code change, no
runbook rewrite, minutes rather than a migration.

It stops being cheap at the first moment HQ's only copy of something exists off
this box and nowhere else *and* the passphrase is lost — that is a one-way door
through Savio's password manager, not through the code. The destination also
stops being cheap once an invoice or a DPA names the vendor, which is a Savio
contract question, not an architecture one.

## 8. Evidence

All commands run on HQ, 2026-11-10 UTC.

**The anchor is one-of-one and matches what is published.** Private key
`/root/.odile/installer-signing.pem`; public half derived locally:

```
$ openssl pkey -in /root/.odile/installer-signing.pem -pubout -out /tmp/hq-pub.pem
$ sha256sum /tmp/hq-pub.pem
13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2  /tmp/hq-pub.pem
$ curl -fsS https://odile.fyi/odile-signing-key.pub -o /tmp/published.pub
$ sha256sum /tmp/published.pub
13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2  /tmp/published.pub
$ sha256sum installer/odile-signing-key.pub
13e8c2aee8263f550033448c8193a82b89498f533f52943e09c14d22e5b036e2  installer/odile-signing-key.pub
```

The derived public half, the published file and the repo copy are the same
bytes; the private half exists once.

**The set is smaller than the disk by design.** Rehearsal against a local restic
repository with the runbook's exact commands — the staging script quoted here is
the draft of `tools/backup_hq.py run` (card `t_63299b60`). The rehearsal wrote
two snapshots, `dbs` and `tree`; the implementation writes one, which is why
`restic restore latest` is a complete restore.

```
$ stage-dbs.py /var/tmp/odile-hq-rehearsal/stage
staged 61 database(s), 897.4 MB   (11.4 s; every integrity_check = ok)

$ restic backup --tag hq --tag dbs <stage>       processed 62 files, 855.840 MiB   (4.4 s)
$ restic backup --tag hq --tag tree <roots -excludes>  processed 15,112 files, 314.335 MiB  (4.7 s)
$ restic stats
     Total File Count:  19381
           Total Size:  1.143 GiB
$ du -sh repo                                     427M
$ restic check                                    no errors were found   (0.83 s)
$ restic check --read-data-subset=10%             no errors were found   (1.0 s)
```

The `dbs` snapshot holds 62 files for 61 databases: one consistent copy each
plus `manifest.json`, no `-wal`/`-shm` sidecar.

**The restore proves the board, the waitlist, the gateway unit, both deploy
targets and the key.**

```
$ restic restore <tree> --target /var/tmp/odile-hq-rehearsal/restore
Summary: Restored 19315 files/dirs (314.335 MiB) in 0:03        (3.75 s)
$ restic restore <dbs>  --target /var/tmp/odile-hq-rehearsal/restore
Summary: Restored 66 files/dirs (855.840 MiB) in 0:02           (2.94 s)
$ verify.py <restore>                     # the drill's five clauses, against the manifest
board/waitlist kanban.db: integrity ok; {'tasks': 166, 'done': 123} (manifest agrees)
board/waitlist waitlist.db: integrity ok; {'waitlist': 2} (manifest agrees)
git HEAD f4b1f95 (equals the manifest)
signing key 13e8c2aee826… (the published anchor)
PASS
$ ls <restore>/root/.config/systemd/user/hermes-gateway.service
hermes-gateway.service                                           (1070 B)
$ sha256sum <restore>/srv/odile-demo/login.html <restore>/srv/odile-testing/index.html
0d1f20ea683a6691551ed50ac0b2e00c0d79ab68c21e10f89c4e77449c39c9f6  …/srv/odile-demo/login.html
9ab262d8aeae08c855fd894cb5d1cad5d491f8597f42e7795c7748e74af0c95e  …/srv/odile-testing/index.html
```

**The demo gate and the testing page, measured on the live hosts on 2026-11-10**
(the commands runbook §5 step 10 carried then):

```
$ curl -s -o /dev/null -w '%{http_code}\n' https://demo.odile.fyi/login            → 200
$ curl -s https://demo.odile.fyi/login | grep -o '<title>[^<]*</title>'             → <title>Sign in — Odile</title>
$ curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://demo.odile.fyi/  → 302 …/login?next=%2F
$ curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
    --data '{"provider":"basic","username":"odile","password":"wrong"}' \
    https://demo.odile.fyi/auth/password-login                                       → 401
$ curl -s https://testing.odile.fyi/ | sha256sum
9ab262d8aeae08c855fd894cb5d1cad5d491f8597f42e7795c7748e74af0c95e  -              (the audited page)
```

Since 2026-11-11 that host is retired: `curl -s -o /dev/null -w '%{http_code}
%{redirect_url}' https://testing.odile.fyi/` answers `301
https://demo.odile.fyi/` with an empty body, and `/srv/odile-testing` is gone
(§3.1). The audited bytes live in git history (`git show
744b53a:prototype/userflow.html`), not in the current repository.

**The R2 endpoint and the credential, measured on HQ 2026-11-10:**

```
$ ACC=$(cat /root/.odile/cloudflare_account_id); echo ${#ACC}
32
$ curl -s -H "Authorization: Bearer $(cat /root/.odile/cloudflare_dns_token)" \
    "https://api.cloudflare.com/client/v4/accounts/$ACC/r2/buckets" -w ' http=%{http_code}\n'
{"success":false,"errors":[{"code":10000,"message":"Authentication error", …}]}  http=403
$ curl -sS -o /dev/null -w '%{http_code}\n' "https://$ACC.r2.cloudflarestorage.com/odile-hq-backup"
curl: (35) TLS connect error: … ssl/tls alert handshake failure   000
$ openssl s_client -connect "$ACC.r2.cloudflarestorage.com:443" \
    -servername "$ACC.r2.cloudflarestorage.com" </dev/null 2>&1 | grep -E 'alert|Cipher'
… ssl/tls alert handshake failure (SSL alert number 40) … Cipher is (NONE)
$ restic version
restic 0.18.1 compiled with go1.25.0 on linux/amd64
```

The DNS token cannot see R2 — `403 Authentication error` — which is why the
human card asks Savio for a bucket and an Object Read & Write token scoped to
it. The S3 hostname resolves (172.64.66.1, 172.64.190.1, 2606:4700:113::1) but
answers TLS with alert 40, which is also what three arbitrary account ids answer
on the same day: that is what an account with R2 not yet enabled looks like.
**The first connectivity test is `restic init` once the bucket exists**, not a
TLS investigation.

**Live evidence, 2026-11-11.** The first consolidated snapshot `97f6c108`
(09:51:40 UTC) holds **34,907 files / 2.15 GiB in one snapshot**; the
repository stores **643 MB**; `restic check --read-data-subset=10%` found no
errors in 7.5 s; the drill restored it in 16 s and passed every clause — board
205 tasks / 162 done and waitlist 2 against the manifest, HEAD `a9450b4` equal
to the manifest, signing key `13e8c2ae…` — in 29.3 s total. After a second
nightly run, `restic snapshots` shows **one** snapshot: the latest-only
retention of §3.4. The full transcript is `docs/runbooks/backup-hq.md` §9.

**The destination prices are vendor pages, read on 2026-11-10:**
<https://developers.cloudflare.com/r2/pricing/> (10 GB-month storage free,
$0.015/GB-month above it, egress free, 1 M Class A and 10 M Class B operations
free per month, `DeleteObject` free);
<https://developers.cloudflare.com/r2/api/s3/api/> (the S3 API endpoint is
`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`; the bucket region is `auto`,
and an empty value or `us-east-1` aliases to it); and
<https://www.backblaze.com/cloud-storage/pricing> for the fallback ($6.95/TB-mo,
first 10 GB free, egress free up to 3× stored, API calls free).

## 9. What this ADR does not do

- **No tenant change.** ODL-215's restic backup of a customer's guest, its
  user-held password and its 30-day retention are untouched; this is the factory.
- **No `docs/CLAIMS.md` change.** "Backed up nightly. Rebuildable in under an
  hour — with your key" is a tenant sentence and stays one; the HQ numbers here
  are internal and must not be quoted as that row.
- **No code in this ADR.** `tools/backup_hq.py`, the timers under
  `deploy/backup/` and `tests/test_backup_hq.py` are card `t_63299b60`; the live
  first snapshot and drill are measured in `docs/runbooks/backup-hq.md` §9.
- **No new boot, access or key-escrow story.** The one new credential is an R2
  token on HQ and a passphrase in Savio's pocket; no tenant gains anything.
- **No claim that the exclusions are complete forever.** When a new kind of state
  appears on HQ, §3.3 is the file to re-read — an exclusion without a reason is
  how a backup quietly stops being one.
