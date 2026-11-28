# Tenant backup and the restore drill

This is the tenant's backup: restic on the guest, to a repository whose
password is generated on the guest, downloaded by its owner on the first-visit
page, and held by nobody at Odile. HQ knows the repository URL, its size, the
last status and the latest snapshot id — and its own stores cannot open any of
it.

**Two clocks, stated once.** The nightly timer runs at the slug's own minute
inside **02:00–06:00 UTC** (`02:00 + int(sha256(slug)[:2], 16) % 240`, ADR-004
v3 §4.3), so the newest snapshot is at most **24 hours** old plus the run — that
is the RPO. The
drills measured both clocks: **0.931 s** from the deletion to a
checksum-verified file on the live tenant (Drill 1, one file); and for a whole
machine, **84m13s end-to-end** from the destroyed home's machine (A, destroyed
22:28:39Z) to a checksum-verified home in a rebuilt guest (23:52:52Z). That
84m13s contains an earlier rebuild attempt that the harness stopped before the
restore; the **timed rebuild-and-restore run** inside it is **38m03s** (B's
destroy at 23:14:49Z to the same verified home), and **14m24s** of the 38m03s
is the machine's own work (build 7m48s, boot to first-visit 6m29s, restic
restore 0.861s, the guest's transfer and checksum 6 s). The product sentence is
"Backed up nightly. Rebuildable in under an hour — without a key.": **"under an
hour" quotes the 38m03s post-start rebuild**, not the 84m13s end-to-end, whose
other 46m10s is the harness's own gap (8m48s from A's destroy to B's first
visit at 22:37:27Z, then 37m22s while B sat waiting for the timed run, because
the worker's 420 s per-call limit stopped the first attempt before the restore).

**What this is not.** It is not the factory backup. HQ's own backup is
`docs/runbooks/backup-hq.md` (Cloudflare R2, Savio's passphrase). The two share
restic and nothing else: a tenant repository has a different password, a
different destination and a different owner.

**19 November 2026 — interim destination.** The tenant destination is
**Cloudflare R2**, not the SFTP box described below, until Savio's Backblaze B2
account is restored (`docs/architecture/ADR-backup.md` §4). Everything in this
runbook about the program, the password, the include set, retention and the
restore path is unchanged; the SFTP material in §3, §5 and §8 is the **v5
record** of the drill that measured the restore. The interim's bucket, keys and
9 GB cap are §3.1; the migration back to B2 is ADR-004 §10. **HQ's own
`hermes-hq-backup` bucket is not part of the interim and must never be written
to by a tenant** — the two bucket namespaces, jurisdictions and tokens are
disjoint.

All guest commands run as root on the tenant. The operator has no SSH: the
drill drove the machine through its own Webapp (the tenant's sign-in) and its
own Hermes cron, which is the same path the user has.

**Vintage of the R2 code paths (20 November 2026).** The destination, the
bucket layout and the R2 rows in §1 and §3.1 are the interim **design**
(ADR-004 §8); the code that operates them is **not shipped at this head**. The
`s3:` branch of `scripts/backup.py` and the guest's `/etc/odile/backup-s3.env`
were planned as **ODL-933** and never shipped (ODL-933 shipped the `sftp:`
shape and the cap check instead); the `odilectl backup-usage` readback and its
800 GiB warn / 950 GiB critical byte ceilings are **ODL-934**, shipped at this
head against the Hetzner box rather than R2 (§3.2; the R2 design's 8.0/9.0 GB
account line is superseded); the provisioning that records the BX11 pair, its
store of record and the Console API token is **ODL-924**, shipped (merged
82fb9ba; the token was minted on t_a548cb0a and is the one this card's exit
uses). `odilectl purge`'s
bucket half was planned as **ODL-935** and was re-cut to the BX11 before it
shipped: the exit is now the SFTP/Console-API path of §7 "Exit" and
`docs/runbooks/offboard.md` §4.1 (measured 2026-11-26), and no `s3:` purge
path exists. Read every R2 sentence below as those cards' design until they
land; the SFTP material stays the v5 record.

**24 November 2026 — the destination is a Hetzner Storage Box BX11.** Savio
chose it for all Odile user backups, and `docs/architecture/ADR-backup.md` v3
replaces the R2 interim **and** the B2 target with one box and a **sub-account
pair per tenant** (`odile-<slug>-rw`, its key on the guest; `odile-<slug>-ro`,
its key in the recovery file), both confined to the box's `tenants/<slug>`
directory. So:
**every R2 sentence in §1, §3.1, §8 and §9, the bucket token, the 9 GB cap and
the three-bucket ceiling are superseded on that date**; the SFTP shape in §3 is
live again with a different host (a sub-account on the box, not HQ's `sshd`)
and **one change to §3's own words** — under v3 the keypair is generated at
provision and the private half reaches the guest in the seed, so `init` finds it
and only the public half is installed on the box. The last `install-ssh-key`
step is a provisioning act, not a human one. The box is ordered (id 709870,
`hel1`) and **`t_12b3720a` has since landed** (merged at `131214e`): its nested
pair and restic drill are in place, and the box's `stats` reads 1,048,576 bytes
— the drill's own repository, no tenant byte. The code re-cuts are ADR-004 §8.

---

## 1. The trust boundary, in one table

| Fact | Where it lives | Who can read it |
|---|---|---|
| Repository password (32 chars) | `/etc/odile/recovery.txt` on the guest, mode 0600; the only copy the user holds | the user (first-visit download), root on the guest, and `scripts/backup.py` for one restic call |
| Repository password during a rebuild | nowhere at HQ: the user's own recovery file, pasted at the rebuilt machine's first visit and written there to `/etc/odile/recovery.txt` for **this** machine (§2.2) | the user, who holds the file; root on the guest after the paste |
| Repository URL | `/etc/odile/backup-repo.txt` (from the seed, 0644) and the tenant row | everyone; it is an address, not a secret |
| Repository contents | the SFTP box (v5) / the tenant's R2 bucket `odile-t-<slug>` (interim), restic ciphertext | anyone can copy the bytes; only the password decrypts them |
| SFTP transport key | `/etc/odile/backup-ssh/id_ed25519` (0600 root, **written from the seed** since ODL-924/933: the key is generated at provision and the machine has none of its own); its public half is installed on the box at provision. `known_hosts` (0600 root) holds the box's pinned host keys, also from the seed | root on the guest; the backup box sees the public half |
| R2 transport token (interim — superseded by BX11; the `s3:` branch was never shipped) | `/etc/odile/backup-s3.env` (0600 root): endpoint, bucket, `AWS_DEFAULT_REGION=auto`, cap and the **Object Read & Write** token scoped to `odile-t-<slug>`; the read-only twin is in the user's recovery file | root on the guest; Cloudflare; HQ holds a copy only to re-seed a rebuild |
| Last status, size, snapshot id | `/var/lib/odile/backup.json` on the guest; the signed heartbeat carries the same fields to HQ | the user, HQ (the fields only) |
| File names, file contents | inside the encrypted repository | nobody without the password |

`scripts/backup.py` reads the `Backup password:` line, writes it to a 0600
file for exactly one restic invocation, and never prints it. `ps` shows no
secret: the password is not on argv.

The 30-day purge (`docs/runbooks/offboard.md` §4, **t_9d6fb595**) is the one
command that touches a destroyed tenant's repository: `odilectl purge <slug>`
reads only the row's `detail.backup`, runs `restic snapshots` as the control
check (with the user's recovery file when the operator has it, and without it
otherwise — restic's "wrong password" still proves the directory is a restic
repository), deletes `/srv/odile-backup/tenants/<slug>` and the guest's
`authorized_keys` line, and records the purge on the row. **That is the HQ-root branch, kept byte for
byte. The BX11 exit shipped with ODL-935 on 2026-11-26:** a row whose URL is the
pair's account-relative `data/restic` (or a `repo-<4 hex>` start-fresh) is
pruned over SFTP with the tenant's read-write key and both sub-accounts are
deleted through the Console API — §7 "Exit" and `docs/runbooks/offboard.md`
§4.1 carry the measured transcript, the two survivors and the refusals. No
`s3:` path exists; the R2 bucket sentences are history. The command reads no
file name, decrypts nothing, and prints no password.

**HQ cannot read the repository.** Measured 2026-11-12T23:24Z on the host that
stores the bytes (the password is the live tenant's, from the recovery file
ODL-332 downloaded):

```
$ grep -rlF -e "$(sed -n 's/^Backup password:[[:space:]]*//p' \
      /var/tmp/odile-332-run/recovery-final.txt)" \
      /root/.odile /etc/odile /opt/odile; echo rc=$?
rc=1
$ RESTIC_PASSWORD=wrong-password-000000000000 \
      restic -r /srv/odile-backup/tenants/odl-fake01 snapshots
Fatal: wrong password or no key found
$ strings /srv/odile-backup/tenants/odl-fake01/data/77/77c52743… | grep -c odile
0
```

The control plane's stores — `/root/.odile`, `/etc/odile`, `/opt/odile` — hold
no copy of the password, which is why the exact-password grep above is `rc=1`;
a data pack carries no plaintext name. A **rebuild** does not change that: the
rebuilt machine has no recovery file until the user pastes theirs at the first
visit (§2.2), and the paste writes it on the machine, not at HQ — so the same
grep finds nothing there either, before or after the paste. The host that runs
the control plane does hold the user's own recovery files under `/var/tmp`,
because the drill acted as the user and kept the download there:
`/var/tmp/odile-332-run/recovery-final.txt` opens the live tenant's repository
and `/var/tmp/odile-215-run/a-recovery.txt` opens the bench one (both mode
0600). They are not Odile stores: no HQ process reads them, and "HQ cannot read
the repository" means the control plane, not that the box's disk never saw the
user's own download. (The field *name* `Backup password:` is in the installer
template and the docs under `/opt/odile`; the exact 32-character value is in
none of the three control-plane trees.)

---

## 2. The nightly job

**v5 (SFTP), and since ODL-933 the BX11 form.** The block below is the live
tenant's unit as `systemctl cat` prints it with `--key` and `--known-hosts`; a
BX11 tenant's unit is the same shape with the installer's extra
`--sftp-connections 2` and `ConditionPathExists=/etc/odile/recovery.txt`. The
R2 `s3:` branch (§3.1) was **never shipped** and is cancelled, not adapted: the
repository is `sftp:u<box>-sub<N>@u<box>-sub<N>.your-storagebox.de:repo`.

```
/usr/local/lib/odile/backup.py run
  --repo sftp://odile-backup@203.0.113.139:2222/tenants/odl-fake01
  --repo-file /etc/odile/backup-repo.txt      # the seed's URL; --repo wins
  --recovery  /etc/odile/recovery.txt
  --home      /home/tenant
  --key       /etc/odile/backup-ssh/id_ed25519
  --known-hosts /etc/odile/backup-ssh/known_hosts
  --status    /var/lib/odile/backup.json
  --slug      odl-fake01
ExecStartPost=-/usr/local/lib/odile/backup.py heartbeat --status … --slug odl-fake01
  --url http://203.0.113.139:8099/heartbeat --key-dir /etc/odile/heartbeat
```

That is the live tenant's unit as `systemctl cat` prints it (the second
`--status` path is elided). A unit the installer or `backup.py install` writes
without `--heartbeat-url` is the same body with the production URL:
`--url https://odile.fyi/heartbeat`, plus `--sftp-connections 2` and the
service's `ConditionPathExists` — see the subsection below for why the timer is
no longer a constant.

One run does, in order (the order ODL-933 shipped):

1. `restic stats --mode raw-data --json` — the size the run **starts** from.
   Over `BACKUP_TENANT_CAP_BYTES` (default `2000000000`) the run writes
   `status: over_cap`, prints `nothing uploaded`, and exits 0 **without
   uploading**: the cap stops growth instead of measuring it.
2. `restic backup /home/tenant --tag tenant --host odl-fake01 --json`
   — `--host` is the **slug**, not the kernel hostname, so a rebuilt guest
   keeps one retention group.
3. `restic forget --keep-daily 7 --keep-weekly 4 --group-by host --host odl-fake01 --prune`
   — the retention the card specifies.
4. `restic stats --mode raw-data --json` — **after the prune**, so the number
   is the size the next run starts from; over the cap the record is
   `status: over_cap` with `repo_bytes`, and the next run uploads nothing. The
   cap never deletes: the last good snapshot stays until an operator prunes.
5. Writes `/var/lib/odile/backup.json` atomically, mode 0600, the HQ-visible
   fields only (`status`, `repo_bytes`, `cap_bytes`, `snapshot`,
   `last_success_at`). A failed run writes `status: failed` and keeps the
   previous `last_success_at`, so a silent failure becomes a freshness alert
   instead of a lie (the 36-hour alert is ODL-216's, `odilectl health`).
6. `ExecStartPost` posts a signed heartbeat carrying `last_backup_at` and a
   `backup` object (repo, snapshot, status, bytes). HQ stores it in the
   heartbeat database; `odilectl status` renders it.

### The schedule, the cap, and a rebuilt machine (ADR-004 v3 §4.1/§4.3/§7.4)

**The timer is the slug's, not a constant.** The box publishes **10 concurrent
connections** (25 measured; the 26th refused, box-wide), one restic run asks
for **2** (`-o sftp.connections=2`, restic's own default is 5), so **five runs
share the published 10** and twelve share the measured 25. To keep the runs
apart, the installer renders

```
OnCalendar=*-*-* <HH:MM>:00     # 02:00 UTC + int(sha256(slug)[:2], 16) % 240
RandomizedDelaySec=0
Persistent=true
```

The offset is stable across a rebuild (same slug, same minute) and needs no
state at HQ. `RandomizedDelaySec` is 0 because the fixed offset **is** the
schedule; `Persistent=true` stays, so a machine that was off at its minute still
gets its snapshot (the stampede after an outage is a bounded failure: some runs
are refused, the next night retries, the freshness watchdog names the tenant).

**The cap is read twice.** `BACKUP_TENANT_CAP_BYTES` (or `--cap-bytes`) is the
per-repository envelope; `run` reads `restic stats` before the upload and after
the prune. Over the cap the status record is `status: over_cap` with the size
and the cap, and the run exits 0 so the heartbeat still reaches HQ — the
watchdog reports it, a prune by an operator is what clears it.

**A rebuilt machine has no password until the user pastes.** The backup
**service** carries `ConditionPathExists=/etc/odile/recovery.txt`; the
installer's `ensure_recovery` mints the file on a fresh install and **skips it
when the seed names the previous instance and the file is absent** (a freshly
minted password would not open the old repository). So a rebuilt machine's
timer fires, the condition fails, the unit is skipped silently, and no false
"backup failed" reaches the watchdog. The first-visit paste writes the file;
once it exists a later installer run keeps its password and refreshes only the
instance and certificate lines.

**The seed's half.** An `sftp:` seed makes the installer write
`/etc/odile/backup-repo.txt` (the no-port colon URL), `/etc/odile/backup-ssh/id_ed25519`
(0600 root) and `/etc/odile/backup-ssh/known_hosts` (0600 root, the box's pinned
host keys), and **no** `backup-s3.env` and no AWS variable. A converged second
run prints `same` for all three. The colon form carries **no port**:
`sftp:user@host:22/path` is the path `22/path` (the drill measured a literal
`22/` directory), so both `scripts/backup.py` and the installer refuse it **by
name**; 22 lives in `sftp.command` as `-p 22`.

### Installing it on a guest

The signed installer places the program and both units (ODL-215 follow-up): it
carries `scripts/backup.py` byte for byte, writes it to
`/usr/local/lib/odile/backup.py` (0755 root), writes
`odile-backup-tenant.service` and `.timer` with the seed-written repository URL
(`/etc/odile/backup-repo.txt`) and the recovery file
(`/etc/odile/recovery.txt`), and enables the timer. A converged scratch root
reads `same` for the program and both units (`/var/tmp/odile-215b-rc`,
installer `8ab356cf…`, the third run; the first run's `write` lines are in the
QEMU transcript at §2.1):

```
same         /var/tmp/odile-215b-rc/usr/local/lib/odile/backup.py
same         /var/tmp/odile-215b-rc/etc/systemd/system/odile-backup-tenant.service
same         /var/tmp/odile-215b-rc/etc/systemd/system/odile-backup-tenant.timer
skip         odile-backup-tenant.timer (the simulation has no systemd)
```

The only skipped step is `systemctl enable` — a scratch root has no systemd.

A fresh QEMU first boot, where the release installer ran as root, prints
`is-enabled` = `enabled`, a unit run that writes `/var/lib/odile/backup.json`,
and the heartbeat the receiver accepted — §2.1.

### 2.1 On a fresh bench guest

`deploy/image/backup-probe.sh` injects `deploy/image/backup-probe-guest.sh` and
its unit into a sealed bench disk and boots it; the probe runs the release
installer the first boot placed and asserts what it left. Measured
2026-11-13T12:53Z on the sealed-image bench, on its **first boot**: the guest
installed (`odile-firstboot: installed odl-bench … · 353s`), the probe read
`not-found` for the three artifacts (the bench image's own stack installer does
not place them), and then ran the release installer and asserted what it left:

```text
=== before the install
not-found
ls: cannot access '/usr/local/lib/odile/backup.py': No such file or directory
=== remove the three artifacts, so the installer's own write lines are visible
not-found
=== the release installer the first boot placed
8ab356cf6276e06664fdbc974660333d2a12543759353888b5cb0faeaa4617a8  /usr/local/bin/odile-install.sh
placed 8ab356cf6276e06664fdbc974660333d2a12543759353888b5cb0faeaa4617a8
manifest installer_sha256 8ab356cf6276e06664fdbc974660333d2a12543759353888b5cb0faeaa4617a8
same
=== release installer run (the tenant install step)
odile installer 0.1.0 — tenant · sha256 8ab356cf6276e066…
signature    OK (sha256 8ab356cf6276e06664fdbc974660333d2a12543759353888b5cb0faeaa4617a8)
write        /usr/local/lib/odile/backup.py (sha256 2f465af25388…)
write        /etc/systemd/system/odile-backup-tenant.service (nightly restic, 03:00)
write        /etc/systemd/system/odile-backup-tenant.timer (daily 03:00)
enable       odile-backup-tenant.timer --now (daily 03:00)
=== is-enabled
enabled
active
=== files
root:root 755 41239 /usr/local/lib/odile/backup.py
root:root 644 707 /etc/systemd/system/odile-backup-tenant.service
root:root 644 163 /etc/systemd/system/odile-backup-tenant.timer
2f465af25388dbaa3b19855fef33d32232d2aa646b59db8efdba35ad1e8cbda1  /usr/local/lib/odile/backup.py
=== unit
ExecStart=/usr/bin/python3 /usr/local/lib/odile/backup.py run --repo-file /etc/odile/backup-repo.txt --recovery /etc/odile/recovery.txt --home /home/tenant --key /etc/odile/backup-ssh/id_ed25519 --known-hosts /etc/odile/backup-ssh/known_hosts --status /var/lib/odile/backup.json --slug odl-bench
ExecStartPost=-/usr/bin/python3 /usr/local/lib/odile/backup.py heartbeat --status /var/lib/odile/backup.json --slug odl-bench --url https://odile.fyi/heartbeat --key-dir /etc/odile/heartbeat
=== timer
NEXT                 LEFT LAST PASSED UNIT                 ACTIVATES
Sat 2026-11-14 03:0…  14h -         - odile-backup-tenant… odile-backup-tenant.…

1 timers listed.
=== unit run
Result=success
ExecMainStatus=0
=== status record
"snapshot": "c7576a63868a9b20ae1fb46f600a246be4e22207e261c4baf7a017b65e43cbfc",
"snapshot_short": "c7576a63",
"status": "ok"
=== journal
Nov 13 12:53:35 odile-bench python3[9359]: ok           snapshot c7576a63 of /home/tenant in 5.0s (3679094 bytes processed, 376 new files)
Nov 13 12:53:37 odile-bench python3[9420]: created      /etc/odile/heartbeat/heartbeat.key (the private half never leaves this machine)
Nov 13 12:53:37 odile-bench python3[9420]: posted       heartbeat for odl-bench at 1794574416 -> https://odile.fyi/heartbeat
Nov 13 12:53:37 odile-bench python3[9420]: HQ said      {"at": 1794574416, "ok": true, "received_at": "2026-11-13T12:53:37Z", "slug": "odl-bench"}
=== odile-backup-probe PASS 2026-11-13T12:53:37Z
```

The stored beat renders the backup line through the real command (the bench
guest is not a provider machine, so the one machine row is a stub provider;
the heartbeat database is the live `/var/lib/odile/waitlist.db`):

```sh
$ python3 /var/tmp/odile-215b/status-proof.py
status: 1 machine(s) for odl-bench
  SERVER     HOSTNAME                 STATE      IPV4             EXPIRES
  odl-bench  odl-bench                active     10.0.2.15        2026-12-14
  release:   intended not recorded, booted no report (unknown)
  backup:    last 2026-11-13T12:53:35Z, (79s ago), snapshot c7576a63, 1288214 bytes in the repository, /var/lib/odile/backup-repo
note: release drift is reported, never repaired
```

Without the stub the real CLI prints `no machine named odl-bench` for this
slug: the guest is not in the provider's account, and the backup line above is
the receiver's stored row, not the provider's.

The one substitution is the repository: the bench seed's URL names
`sftp:bench@backup.odile.test:/srv/odl-bench`, and that box does not exist yet
(t_8eb17a54). The probe points `/etc/odile/backup-repo.txt` at a local restic
repository, creates it with the password in the guest's own
`/etc/odile/recovery.txt`, runs the unit, and puts the seed's URL back.
Everything else — the program, both units, their paths, the heartbeat URL — is
the installer's. The full serial transcript is the card's evidence.

`backup.py install` remains the manual path for a tenant that predates the
installer's placement (the live tenant is one: the drill installed it through
the Webapp). On a guest that has the file:

```sh
install -m 0755 scripts/backup.py /usr/local/lib/odile/backup.py
/usr/local/lib/odile/backup.py init \
    --repo 'sftp://***@backup.odile.fyi/tenants/odl-fake01' --dry-run
/usr/local/lib/odile/backup.py install \
    --repo 'sftp://***@backup.odile.fyi/tenants/odl-fake01' \
    --slug odl-fake01 --home /home/tenant
systemctl list-timers odile-backup-tenant.timer
```

The installer writes both units from its own literal copy of the program's
templates; `tests/test_installer_copy.py` formats the program's template and
the installer's copy and asserts the bytes are equal, so the two paths cannot
drift.

`--heartbeat-url` is not decoration. The generated unit posts to
`https://odile.fyi/heartbeat`, and the Caddy route is live (measured
2026-11-13T12:54Z: `GET /heartbeat/health` → **200** with
`{"heartbeats": 28, "ok": true, "service": "odile-heartbeat"}`, an empty `POST /heartbeat` → **400**
from the receiver). The route is committed in `deploy/seed/odile-seed.caddy`,
the one fragment `./scripts/deploy.sh` splices into the `odile.fyi` site, so a rebuilt
HQ gets it. A `GET` of `/heartbeat` is the receiver's own 404 — it answers
`POST /heartbeat` and `GET /health` only; the app never sees the path. The live tenant's hand-installed unit still
names `http://203.0.113.139:8099/heartbeat`; a re-provision (ODL-218) replaces
it with the generated unit, whose URL is the production name.

`install` writes two units, reloads systemd and enables the timer. It is
idempotent: a second run prints `same` for both files. A fresh unit's first run
posts an accepted heartbeat (the unit directory is a throwaway here so no second
timer lands on this host; the service file is started unchanged under a
throwaway name):

```
$ backup.py install --unit-dir /var/tmp/odile-215-fresh/units \
    --install-path /var/tmp/odile-215-fresh/backup.py \
    --repo /var/tmp/odile-215-fresh/repo --recovery /var/tmp/odile-215-fresh/recovery.txt \
    --home /var/tmp/odile-215-fresh/home --status /var/tmp/odile-215-fresh/backup.json \
    --slug odl-fresh-215 --heartbeat-url http://203.0.113.139:8099/heartbeat \
    --heartbeat-key-dir /var/tmp/odile-215-fresh/heartbeat
installed    /var/tmp/odile-215-fresh/backup.py
wrote        /var/tmp/odile-215-fresh/units/odile-backup-tenant.service
wrote        /var/tmp/odile-215-fresh/units/odile-backup-tenant.timer
skip         systemctl (the unit directory is not /etc/systemd/system)
$ grep '^ExecStartPost' /var/tmp/odile-215-fresh/units/odile-backup-tenant.service
ExecStartPost=-/usr/bin/python3 /var/tmp/odile-215-fresh/backup.py heartbeat --status /var/tmp/odile-215-fresh/backup.json --slug odl-fresh-215 --url http://203.0.113.139:8099/heartbeat --key-dir /var/tmp/odile-215-fresh/heartbeat
$ cp /var/tmp/odile-215-fresh/units/odile-backup-tenant.service \
     /etc/systemd/system/odile-215-fresh.service    # a throwaway name; the body is the unit above
$ systemctl daemon-reload && systemctl start odile-215-fresh.service
$ systemctl show odile-215-fresh.service -p Result -p ExecMainStatus
Result=success
ExecMainStatus=0
$ journalctl -u odile-215-fresh.service --no-pager --since '-2 min' \
    | grep -E 'ok |created|posted|HQ said'
Nov 13 01:41:26 python3[3688428]: ok           snapshot 1a80216c of /var/tmp/odile-215-fresh/home in 0.6s (28 bytes processed, 1 new files)
Nov 13 01:41:26 python3[3688559]: created      /var/tmp/odile-215-fresh/heartbeat/heartbeat.key (the private half never leaves this machine)
Nov 13 01:41:26 python3[3688559]: posted       heartbeat for odl-fresh-215 at 1794534086 -> http://203.0.113.139:8099/heartbeat
Nov 13 01:41:26 python3[3688559]: HQ said      {"at": 1794534086, "ok": true, "received_at": "2026-11-13T01:41:26Z", "slug": "odl-fresh-215"}
$ /usr/bin/python3 scripts/heartbeat.py show --db /var/lib/odile/waitlist.db --slug odl-fresh-215
{
  "at": 1794534086,
  "at_iso": "2026-11-13T01:41:26Z",
  "boot": null,
  "facts": {
    "backup": {
      "at": 1794534085,
      "bytes": 28,
      "repo": "/var/tmp/odile-215-fresh/repo",
      "repo_bytes": 1334,
      "snapshot": "1a80216c8eab7a9d2b48bbd40ae6029996fc70cd6193b22afaf853d655f70bd8",
      "status": "ok"
    },
    "last_backup_at": 1794534085,
    "slug": "odl-fresh-215"
  },
  "id": 24,
  "received_at": "2026-11-13T01:41:26Z",
  "slug": "odl-fresh-215"
}
```

Then the throwaway unit is removed (`rm /etc/systemd/system/odile-215-fresh.service
&& systemctl daemon-reload`); the evidence dir `/var/tmp/odile-215-fresh` stays.

```
$ systemctl list-timers odile-backup-tenant.timer --no-pager
NEXT                        LEFT LAST PASSED UNIT                      ACTIVATES
Fri 2026-11-13 03:00:17 UTC   7h -         - odile-backup-tenant.timer odile-backup-tenant.service
```

The unit is a oneshot. Measured on the live tenant, 2026-11-12T22:24:54Z:

```
$ systemctl start odile-backup-tenant.service       # waits for the oneshot
$ journalctl -u odile-backup-tenant.service -n 5 --no-pager
Nov 12 22:24:54 odile-bench systemd[1]: Starting odile-backup-tenant.service - Odile tenant backup (restic, nightly)...
Nov 12 22:25:00 odile-bench python3[8104]: ok           snapshot fb0f6694 of /home/tenant in 1.0s (47366363 bytes processed, 0 new files)
Nov 12 22:25:00 odile-bench python3[8104]: status       /var/lib/odile/backup.json
Nov 12 22:25:00 odile-bench python3[8153]: posted       heartbeat for odl-fake01 at 1794522300 -> http://203.0.113.139:8099/heartbeat
Nov 12 22:25:00 odile-bench python3[8153]: HQ said      {"at": 1794522300, "ok": true, "received_at": "2026-11-12T22:25:04Z", "slug": "odl-fake01"}
```

The timer is armed for the next 03:00, and it has fired on its own before the
manual runs (`LastTriggerUSec` is the 19:07 catch-up when the unit was first
enabled, which produced snapshot `08c1103b`):

```
$ systemctl list-timers 'odile-backup*' --no-pager --all
NEXT                        LEFT     LAST                        PASSED UNIT                      ACTIVATES
Fri 2026-11-13 03:03:14 UTC 3h 39min Thu 2026-11-12 19:07:55 UTC      - odile-backup-tenant.timer odile-backup-tenant.service
$ systemctl is-enabled odile-backup-tenant.timer
enabled
```

### What HQ shows

```
$ odilectl status odl-fake01
status: 1 machine(s) for odl-fake01
  SERVER     HOSTNAME                 STATE      IPV4             EXPIRES
  0badc0de   odl-fake01               active     203.0.113.87     2026-12-10
  release:   intended not recorded, booted no report (unknown)
  backup:    last 2026-11-12T22:25:00Z, (37m ago), snapshot fb0f6694, 1017507922 bytes in the repository, sftp://odile-backup@203.0.113.139:2222/tenants/odl-fake01
```

(The repository URL prints with the account masked as `***@` in some
terminals; the account is `odile-backup`, an address, not a secret.)

`--json` carries the same fields under `machines[].backup`:
`last_backup_at`, `age_seconds`, `snapshot`, `repo`, `repo_bytes`,
`backup_status`. No file name ever leaves the guest.

### 2.2 The rebuilt machine: the paste, the restore, and the snapshot (ODL-937)

A rebuilt machine's home is empty and its seed names the machine it replaced
(`backup.previous_instance`), so the installer writes no recovery file and the
first-visit page asks for one **before** it offers the sign-in (ADR-004 §5.3,
ADR-backup §7.2). The page's copy is fixed by the card: `Your machine is back.`
/ `Paste the recovery file you saved at your first sign-in. It opens your
backup.` / `Recovery file` / `Restore`; while the unit runs it shows
`Restoring your data…` with a five-second meta refresh (no JavaScript); and on
success `Your data is back. Snapshot <id> of <time>.` with the first 12 hex
characters of the snapshot the restore named — the same substring the account
page shows. The words `Installing upgrades` and `Upgrading` never appear: the
old machine was replaced, not upgraded.

The paste is refused **before** anything runs: the installer's own
`RECOVERY_PASSWORD_RE` grammar, the file's `Tenant:` line (another tenant's file
reads `This file is for a different machine.`) and its `Repository:` line (a
different repository for the same tenant is the same answer), and a truncated
paste fails at the page with `This file does not open this backup.` A wrong
password reaches restic, which exits **12**; `backup.py restore` propagates
that code, and the page shows the same sentence. The home is untouched on every
refusal, and a retry converges: restic overwrites into the same target.

`odile-restore.service` is one oneshot, root, admitted only by
`ConditionPathExists=/etc/odile/recovery.txt` (the paste). It runs
`/usr/local/lib/odile/first_visit.py --restore`, which, in order: stops
`odile-backup-tenant.timer` (so a 02:00–06:00 UTC run cannot snapshot a
half-restored home), stops the agent, resolves the snapshot (the seed's
`backup_snapshot`, or `latest` when the seed names none — resolved **before**
the restore, so the id shown is the one restored and not the fresh one), runs

```
backup.py restore <snapshot> --target / --include <home>
```

writes the seed's `sign_in_hash` back over the restored `.env` (the backup
night's copy is the verifier of the password the user may have retired), starts
the agent, runs one `backup.py run` so the newest snapshot is this machine's,
and starts the timer. `/etc/odile` is never in the restore. A failure leaves
the timer stopped — the leaves-the-freshness-watchdog signal ADR-backup §7.4
names — and a wrong password starts the agent again so the machine is never
worse than before the paste.

`backup.py snapshot-info <id|latest> --json` is the read-only verb the restore
uses to resolve the id and its time: it runs `restic snapshots --json
--no-lock` and prints `{id, short_id, time, host, paths}`; no match is exit 3.
Its `--no-lock` is the read-only key's rule too: the two commands in the
recovery file carry it, because a read-only sub-account refuses restic's lock.

---

## 3. The repository

Since ODL-924 every tenant's seed carries **its own pair's URL** on the Hetzner
Storage Box BX11 (ADR-004 v3): `sftp:u<box>-subN@u<box>-subN.your-storagebox.de:data/restic`
for the read-write half, `…:restic` for the read-only half, SFTP port 22, the
host key pinned in `/etc/odile/backup-ssh/known_hosts`. The repository lives at
`tenants/<slug>/data/restic` under the sub-account's home; the box is in `hel1`
(Helsinki, FI, the EU). Measured by the BX11 drill, 2026-11-28
(`docs/evidence/a3-bx11-drill.md`): `init` 14.7 s, the first run 23.2 s for 515
files / 16.4 MB, repository 2,808,994 bytes, and 16.3 MB/s up / 15.9 MB/s down
for a 1.0 GiB transfer.

**History — the HQ stand-in (port 2222).** Before the box, the seed named
`sftp:odile-backup@backup.odile.fyi:/tenants/{slug}`, a name that does not
resolve: the drill used one dedicated `sshd` instance on **port 2222**, one
account, chrooted, sftp only, no shell and no forwarding, kept apart from the
system sshd so a mistake there cannot lock anyone out of HQ:

```
/etc/ssh/sshd_odile_backup.conf        Port 2222; AllowUsers odile-backup;
                                        ChrootDirectory /srv/odile-backup;
                                        ForceCommand internal-sftp
/etc/systemd/system/odile-backup-sftp.service
/srv/odile-backup/tenants/<slug>/       mode 0700, owner odile-backup
/srv/odile-backup/.ssh/authorized_keys  the guests' public keys only
```

This is the same ciphertext an off-site box would hold; what changes when the
box is bought is the URL and the host key, not the program, the password or
the restore command. **The stand-in is honest about its limit:** the backup
survives the tenant, not the loss of HQ. A real box is a follow-up (§9).

The SFTP key is generated **on the guest** (`init`), and only its public half
travels:

```
$ backup.py init --repo 'sftp://odile-backup@203.0.113.139:2222/tenants/odl-fake01'
created      /etc/odile/backup-ssh/id_ed25519 (ed25519, no passphrase)
recorded     203.0.113.139 in /etc/odile/backup-ssh/known_hosts (6 line(s))
created      the restic repository
public key   /etc/odile/backup-ssh/id_ed25519.pub (register this one line on the backup box)
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGunOdY4nNo8XxigEMHXBH9RogzBZXjXNsYzaZ3RQ9H6 odile-backup odile-bench
```

The first `init` is expected to fail at `restic init` until the public half is
appended to the backup box's `authorized_keys`; a second `init` is `same`.
`known_hosts` is `ssh-keyscan` output, hashed, and the transport is
`StrictHostKeyChecking=yes` — the box's host key is pinned, not trusted on
first sight.

The repository is created **once**. A second `init` answers
`the repository already answers; init is a no-op`. A wrong recovery-file
password against an existing repository is refused by name, never re-initialised.

### 3.1 The interim destination — Cloudflare R2, 19 November 2026

> **Superseded, and never shipped.** On 24 November 2026 the destination became
> the Hetzner Storage Box BX11 (ADR-004 v3), and the `s3:` branch, the
> `backup-s3.env` file and the R2 names below are **history**: no code ever
> loaded them, and ODL-933 adapted nothing — it shipped the `sftp:` shape in §2.
> Kept because the drill records and the cap arithmetic still read correctly.

The interim destination until B2 is restored — designed for R2 and never built
(**ODL-924** shipped the BX11 pair instead; see §7 "Exit"). A tenant repository
is an R2 bucket,
**one bucket per tenant**, and the URL is the provider's, not ours:

```
s3:https://<account_id>.r2.cloudflarestorage.com/odile-t-<slug>/repo
```

The `repo` segment is the restic repository's prefix inside the bucket; a fresh
start without a restore (`repo-<4 hex>`) uses the same bucket, so there is no
dashboard step in that branch.

- **Bucket:** `odile-t-<slug>` (the `t` is tenant; for example
  `odile-t-odl-7f3a2c`), **default
  jurisdiction**, `locationHint=enam`, no public access. It is disjoint from HQ's
  `hermes-hq-backup` in name, in endpoint (`<account_id>.r2.cloudflarestorage.com`
  vs `<account_id>.eu.r2.cloudflarestorage.com`) and in token resource. **Never
  touch `hermes-hq-backup`; it is HQ's only copy.**
- **Keys:** two bucket-scoped tokens, both minted by **Savio in the Cloudflare
  dashboard** (there is no account-wide R2 credential at HQ): **Object Read &
  Write** for the guest, **Object Read only** for the user's recovery file. The
  guest's pair, the endpoint, the bucket and `AWS_DEFAULT_REGION=auto` live in
  `/etc/odile/backup-s3.env` (0600 root; written by **ODL-933**'s installer from
  the seed **ODL-924** builds — neither is shipped at this head); the same pair
  is kept on HQ at
  `/root/.odile/r2/odile-t-<slug>.env` (0600) only so a rebuild can
  re-deliver it. Neither token can create or delete a bucket.
- **Cap:** **2.0 GB per repository, at most three buckets in total (the demo
  `odl-7971a8`, the bench `odl-bench`, the first paying tenant), 9 GB across the
  account** (which already holds HQ's 1.15 GB). `backup.py run` reads
  `R2_TENANT_CAP_BYTES=2000000000` and reports `status: over_cap` with the size
  when the repository after prune exceeds it; a run that starts over cap does not
  upload. The R2-era readback was designed to list all four buckets daily and
  alert at **8.0 GB (warn) / 9.0 GB (critical)**; **ODL-934 shipped at this
  head as the box's readback instead** (§3.2): two box `GET`s plus each
  tenant's last restic number, warning at **800 GiB** and critical at
  **950 GiB**. The **fourth bucket — the second paying tenant — waits for
  B2.** The cap check is shipped for the **BX11** shape as
  `BACKUP_TENANT_CAP_BYTES` (§2); the R2 `R2_TENANT_CAP_BYTES` name never
  shipped.
- **Verification after minting:** `HeadBucket` on `odile-t-<slug>` must answer,
  and `HeadBucket` on `hermes-hq-backup` with the tenant token must **not** be
  200. Record both codes in the handoff — that is the proof the token is scoped.
- **Purge (ODL-935; shipped 2026-11-26 on the BX11, not on R2):** the R2
  design emptied the bucket through the tenant token; no `s3:` path shipped
  and the bucket row is history. The exit is the BX11 one — §7 "Exit" and
  `docs/runbooks/offboard.md` §4.1 carry the measured transcript: prune with
  the tenant's read-write key over SFTP, `DELETE` both sub-accounts, record
  `detail.backup.subaccount_deleted_at`, and state the two survivors (the
  `tenants/<slug>` directory with no user data, and the box's own 04:30 × 10
  snapshots, up to about ten days). The 30-day promise is unchanged.

**Init — ODL-933; not shipped at this head.** No code loads
`/etc/odile/backup-s3.env` today — `git grep -n 'backup-s3' -- '*.py' '*.sh' '*.service'` matches nothing — and `scripts/backup.py`'s `_environment()` sets
`RESTIC_*` only, so the command below has no AWS credentials until ODL-933
lands. The first `init` is §3's second half removed: no SSH key, no
`known_hosts`, no human on the provider's side. With `/etc/odile/backup-s3.env`
in place (0600 root), the guest runs

```sh
backup.py init --repo 's3:https://<account_id>.r2.cloudflarestorage.com/odile-t-<slug>/repo'
```

and a second run answers `same`, exactly as in §3.

The readback that proves HQ's bucket was untouched at the re-cut is in
`docs/architecture/ADR-backup.md` §4.2 (75 objects / 1,149,539,702 bytes, one
snapshot `8d521571…`), and the migration back to B2 is that ADR's §10.

### 3.2 The box's usage readback (ODL-934)

**Shipped at this head.** `odilectl backup-usage` reads the box's own `stats`
and each tenant's last restic `repo_bytes` from its heartbeat, prints one row
per tenant, and with `--apply` writes the record the watchdog reads. It is a
command, not a systemd unit: run it **daily**. The watchdog reads whatever the
last run wrote and prints the box's bytes, share, sub-account count and the
readback's time; the alert's symptom carries its age, so a readback that
stopped running is visible as an old time, not a fresh one.

The sample below was measured on 26 November 2026 at 07:28Z, while the drill's
bench pair still existed; the exit card purged it later the same day (ODL-935),
so a re-run today prints `unaccounted: 0 sub-account(s)` and
`sub-accounts: 2 of 100 (1 of 50 tenant pairs)`. The rule is the same either
way: a sub-account whose slug has no tenant row is printed by name, never
folded into a tenant's number.

```
$ python3 -m odilectl backup-usage --apply
backup usage: box 709870 odile-backups (hel1, HEL1-BX192), 2.0 MiB of 1.0 TiB used (0.0%)
  stats:     size_data 2097152 bytes, size_snapshots 0 bytes, total 2097152 bytes
  tenants:   2 row(s); 0 with their own restic number, 0 bytes (0 B)
    odl-7971a8: no restic number (heartbeat backup status: failed); pair odile-odl-7971a8-ro, odile-odl-7971a8-rw
    odl-aa3442: no restic number (heartbeat backup status: no backup reported)
  unaccounted: 2 sub-account(s) on the box with no tenant row
    odile-odl-bench-ro u728180-sub2 (id 392556, tenants/odl-bench/data)
    odile-odl-bench-rw u728180-sub1 (id 392550, tenants/odl-bench)
  sub-accounts: 4 of 100 (2 of 50 tenant pairs)
  ceiling:   ok (warn at 800.0 GiB = 858993459200 bytes, critical at 950.0 GiB = 1020054732800 bytes)
  token:     /root/.odile/hetzner-api-token (mode 0600, project-scoped; no new credential)
  calls:     GET /storage_boxes, GET /storage_boxes/709870/subaccounts
  readback:  /var/lib/odile/backup/usage.json (wrote, mode 0600)
```

At 08:49Z the same command read `size_data 1048576 bytes, size_snapshots
1048576 bytes, total 2097152 bytes`: the purged directory still counted in the
box's own snapshot, which is why both stats are printed and only their sum is
the box's usage.

Without `--apply` it prints and writes nothing; `--dry-run` wins when both are
given. The write is atomic (`os.replace` of a 0600 temp file in the same
directory) to `/var/lib/odile/backup/usage.json`:

```
$ stat -c '%a %U:%G %s %n' /var/lib/odile/backup/usage.json
600 root:root 1685 /var/lib/odile/backup/usage.json
```

The record's schema is `odile.backup-usage/1`:

- `box{id,name,location,system,size_bytes,size_data,size_snapshots,total_bytes,subaccounts,subaccounts_limit,pairs,pair_limit}`
- `tenants[{slug,state,subaccounts,repo_bytes,backup_at,status,source}]`
- `unaccounted[{name,username,id,home_directory,slug}]`
- `totals{tenants,tenants_with_a_number,tenant_bytes,box_bytes}`
- `thresholds{warn_bytes,critical_bytes,level}`
- `credential{token_path,token_mode,api_calls}`

`backup_at` is the time the tenant's own `restic stats` ran, read from the
heartbeat fact's `last_success_at` (what `scripts/heartbeat.py collect`
forwards), falling back to the older `at` and then `at_iso` parsed as an epoch,
so a real heartbeat renders `… from its last restic stats at <time>`. A record
whose `at` is not an integer epoch is not usable: `odilectl health` prints
`backup usage: <path> carries at <value>, not an integer epoch` instead of
dying inside the poll, and the next daily run rewrites the record.

Flags: `--token`, `--box-id`, `--api-base`, `--db`, `--heartbeat-db`, `--out`,
`--now`.

The box is one fixed 1,024 GiB, and the ceilings are the byte counts the
record carries: **800 GiB warn = 858,993,459,200 bytes**, **950 GiB critical =
1,020,054,732,800 bytes**. Both are inclusive (`>=`): unlike the age
thresholds, the byte line is a place, not a duration. At 2.0 MiB the readback
prints `ceiling: ok`. A fake box served on `127.0.0.1` answers the same two
`GET`s at exactly 800.0 GiB; the same command prints `ceiling: warn`, and the
`odilectl health` poll fed that record prints:

```
  alert backup_usage backup-box-709870 open at 2026-11-26T07:29:39Z: the backup box 709870 odile-backups uses 800.0 GiB (78.1% of 1.0 TiB); 2 of 100 sub-accounts; readback 0m 0s ago; 800.0 GiB warn line reached
```

At exactly 950.0 GiB the readback prints `ceiling: critical`; a poll at that
level resolves the warn row and opens the critical one:

```
  alert backup_usage backup-box-709870 resolved at 2026-11-26T07:29:39Z: the backup box 709870 odile-backups uses 800.0 GiB (78.1% of 1.0 TiB); 2 of 100 sub-accounts; readback 0m 0s ago; 800.0 GiB warn line reached
  alert backup_usage_critical backup-box-709870 open at 2026-11-26T07:30:30Z: the backup box 709870 odile-backups uses 950.0 GiB (92.8% of 1.0 TiB); 2 of 100 sub-accounts; readback 0m 0s ago; 950.0 GiB critical line reached
```

The 2 unaccounted sub-accounts are the bench pair, by name:
`odile-odl-bench-ro u728180-sub2` (id 392556, `tenants/odl-bench/data`) and
`odile-odl-bench-rw u728180-sub1` (id 392550, `tenants/odl-bench`). They have
no tenant row, so the command names them instead of folding their bytes into a
tenant's. The box counts 4 of 100 sub-accounts (2 of 50 tenant pairs); the
pair limit is half the sub-account limit because each tenant takes one `-rw`
and one `-ro`.

The command needs **no new credential** and never writes to the box: the two
calls are `GET /storage_boxes` and `GET /storage_boxes/709870/subaccounts`,
with the existing project-scoped token `/root/.odile/hetzner-api-token`
(0600). Tenant bytes come from the heartbeat database the poll already opens;
the provider reports the box as one number, so a per-tenant figure can only
come from the tenant's own last restic stats.

---

## 4. Drill 1 — delete a file, restore it, compare

This is the drill the card asks for, on the live tenant, 2026-11-12. It is
`backup.py drill`: it creates a canary, backs the home up, deletes the canary,
restores it from the snapshot into a temporary directory, compares SHA-256, and
puts the file back in place. A second run is safe and still passes.

```
$ backup.py drill --repo 'sftp://odile-backup@203.0.113.139:2222/tenants/odl-fake01' \
    --slug odl-fake01 --home /home/tenant
drill        backed up /home/tenant/backup-drill/canary.txt as 74e1507d, deleted it, restored it
sha256       before 896bcb5bc104dac7c9bb351eeba3c852f4929d781d907dd98eb04cc219cf0fde
             after  896bcb5bc104dac7c9bb351eeba3c852f4929d781d907dd98eb04cc219cf0fde
verdict      PASS in 7.6s -> /var/lib/odile/backup-drill.json
```

The record is written where `--drill` names it (default
`/var/lib/odile/backup-drill.json`, 0600); the tenant kept a copy inside the
home at `/home/tenant/backup/drill.json`, which is the one a restore carries.
It is a **point-in-time** record: this one is the 19:04 drill, and a later
drill rewrote the canary in the home, so the newest snapshot's
`backup-drill/canary.txt` hashes `bb0a59a3…` while the record below still
holds `896bcb5b…`. It holds the snapshot id, all three hashes, and
`rpo_seconds`/`rto_seconds`:

```
$ cat /home/tenant/backup/drill.json
{
  "at": 1794510255,
  "at_iso": "2026-11-12T19:04:15Z",
  "duration_seconds": 7.553,
  "file": "/home/tenant/backup-drill/canary.txt",
  "host": "odl-fake01",
  "repo": "sftp://odile-backup@203.0.113.139:2222/tenants/odl-fake01",
  "rpo_seconds": 0.0,
  "rto_seconds": 0.931,
  "schema": 1,
  "sha256_before": "896bcb5bc104dac7c9bb351eeba3c852f4929d781d907dd98eb04cc219cf0fde",
  "sha256_in_place": "896bcb5bc104dac7c9bb351eeba3c852f4929d781d907dd98eb04cc219cf0fde",
  "sha256_restored": "896bcb5bc104dac7c9bb351eeba3c852f4929d781d907dd98eb04cc219cf0fde",
  "snapshot": "74e1507d66e8e6b6f270d7b0745b6c58ae9381154b38caf4d2867b27af90c906",
  "verdict": "pass"
}
```

Measured: **RTO 0.931 s** from the deletion to a checksum-verified file;
**RPO 0.0 s** for this drill, because the snapshot was taken immediately before
the deletion — a real crash at that second loses nothing, and the nightly timer
is what bounds the production worst case at 24 h. The 7.553 s is the whole
drill (backup + delete + restore), not the RTO.

The machine's status record, written by the 19:02 run (the drill does not
update it):

```
snapshot    b50aa5e5…  (3,139,596,963 bytes processed, 156,311 new files, 27.3 s)
repo bytes  1,017,111,156
retention   keep_daily 7, keep_weekly 4
restic      restic 0.18.0 (guest)
```

The password check, also on the live tenant:

```
$ backup.py verify --repo 'sftp://odile-backup@203.0.113.139:2222/tenants/odl-fake01'
repo         sftp://…@203.0.113.139:2222/tenants/odl-fake01
password     opens the repository (2 snapshot(s))
wrong one    refused
```

---

## 5. Drill 2 — destroy a VM, rebuild it, restore the home

Two machines and one repository. **A** (the bench guest `odl-bench`) held the
home and made the repository `tenants/odl-bench`; its recovery file is the one
the user keeps. **C** is a different machine, built from the same signed image
and the same seed, with no key, no passphrase and no copy of A's disk. The only
thing the restore needs from A is the recovery file.

Drill 1 ran on the live Servury tenant. Drill 2 needs a machine it may destroy,
so it ran on the QEMU bench (`deploy/image/firstboot-bench.sh`), which is the
same signed UKI, the same first boot and the same first-visit page; QEMU TCG is
the only stand-in for SEV.

The transcript below is one continuous sequence, 2026-11-12T23:14:49Z to
23:52:52Z. A first attempt at 22:28:39Z destroyed A and rebuilt it as B
(first-visit at 22:37:27Z, 8m48s); the worker's per-call limit then stopped it
before the restore, so the timed run starts from B's destroy. The re-run is the
evidence: the home it restores is still A's, the password is still A's, and
neither build touched A's disk. **Two RTO anchors follow from this:** the timed
run is 38m03s (B's destroy → verified home), while the machine whose home was
actually destroyed died at 22:28:39Z, so the end-to-end number is 84m13s;
§5.5 states both.

### 5.1 Destroy (T0 23:14:49Z)

The machine destroyed here is **B**, the first attempt's rebuild of A. The home
restored below is A's, because B never held the repository or the home that
matters.

```
$ ls -l /var/tmp/odile-215-rebuild/out/odile-tenant-sealed.raw
-rw-r--r-- 1 root root 8589934592 … odile-tenant-sealed.raw
$ pgrep -af 'qemu-system-x86_64 -name odile-tenant-B'
3138487 qemu-system-x86_64 -name odile-tenant-B -machine q35 …
$ pkill -f 'qemu-system-x86_64 -name odile-tenant-B'; sleep 2
$ pgrep -af 'qemu-system-x86_64 -name odile-tenant-B' || echo gone
gone
$ rm -f /var/tmp/odile-215-rebuild/out/odile-tenant-sealed.raw
$ test ! -e /var/tmp/odile-215-rebuild/out/odile-tenant-sealed.raw && echo disk-removed
disk-removed
```

### 5.2 Rebuild (build 7m48s, boot to first-visit 6m29s)

The fixture is a new seed (`prepare`); the image is built with that token in the
measured command line, signed, and booted with the two surfaces a user has —
the first-visit page and the Webapp — forwarded to the host:

```
$ firstboot-bench.sh prepare --dir /var/tmp/odile-215-drill2
tenant     odl-bench
instance   45445445-0000-4000-8000-000000000454
expires    2026-11-13T23:14:50Z
token      9180aa26d8c6…    (printed once; only its SHA-256 is stored)
$ firstboot-bench.sh build --dir … --out … --force
… sbsign --key /root/.odile/image-signing.key --cert deploy/image/odile-image-signing.crt …
Wrote signed /var/tmp/odile-215-drill2/out/odile-tenant.efi
$ boot_guest.sh C /var/tmp/odile-215-drill2/out 19145 19143
…
2026-11-12T23:39:38Z T1       first-visit page -> 200
2026-11-12T23:39:40Z first    POST /first-visit -> 200; recovery file -> 200, 638 bytes
```

The guest's own first-boot line, from its serial console:

```
[  377.653967] python3[628]: odile-firstboot: installed odl-bench (instance 45445445-0000-4000-8000-000000000454) · first-visit 200 · 296s
```

On the code before t_fec9f699 the rebuilt machine minted a **new** backup
password into its own recovery file, exactly as a first install does: C's file,
kept from this drill, does not open A's repository (`restic` exit 12,
`Fatal: wrong password or no key found`), so a rebuilt machine's `backup.py
init` would meet the old repository with a key it cannot use.

**A rebuilt machine continues the same repository — the sealed path this drill
measured, retired by ODL-937 (t_fec9f699).** Until ODL-937 the fixture that
minted the rebuilt machine's setup token also took A's recovery file; the
provision sealed A's password under that token, and the seed carried the sealed
value. The rebuilt guest's own first boot opened it and wrote
`/etc/odile/recovery.txt` before the installer ran. That code is gone from the
tracked tree at this head: the same-repository rebuild is the first-visit paste
(§2.2), and the transcript below is the 2026-11-13 measurement of the drill —
`--backup-password-file` and `/var/lib/odile/seed-rebuilds.json` are the
retired names. Measured on the QEMU bench, 2026-11-13:

```
$ firstboot-fixture.py prepare --dir … --instance 45445445-…-000000000699 \
    --backup-password-file /var/tmp/odile-215-run/a-recovery.txt
tenant     odl-bench
instance   45445445-0000-4000-8000-000000000699
rebuild    sealed (…/seed-rebuilds.json)
token      74e4d5c5…    (printed once; only its SHA-256 is stored)
$ grep -c "$(sed -n 's/^Backup password:[[:space:]]*//p' a-recovery.txt)" \
      seed-rebuilds.json
0
$ ./deploy/image/firstboot-bench.sh build --dir … --out … --force
…
Wrote signed …/odile-tenant.efi
$ /usr/bin/python3 /var/tmp/odile-t_fec9f699-rebuild.py
2026-11-13T09:18:32Z T0       qemu … boots …/odile-tenant.efi
2026-11-13T09:29:18Z T1       first-visit page -> 200
2026-11-13T09:29:20Z T2       POST /first-visit -> 200; the rebuilt guest's own recovery
                              file -> 200, 636 bytes -> d-recovery.txt
serial:  odile-firstboot: installed odl-bench (instance 45445445-…-699) ·
         first-visit 200 · record … · 498s
```

Both files carry the same `Backup password:` line — 32 characters, compared
with `cmp`; no value is printed here:

```
$ for f in a-recovery.txt d-recovery.txt c-recovery.txt; do
    printf '%-16s %s chars\n' "$f" \
      "$(sed -n 's/^Backup password:[[:space:]]*//p' "$f" | tr -d '\n' | wc -c)"
  done
a-recovery.txt   32 chars      # the user's file (A)
d-recovery.txt   32 chars      # the rebuilt guest's own download
c-recovery.txt   32 chars      # the pre-fix rebuild's file (C)
$ cmp -s a-line d-line; echo rc=$?     # the two password lines
rc=0
$ cmp -s a-line c-line; echo rc=$?
rc=1
```

Then, with the rebuilt guest's own file and the repository A left:

```
$ backup.py init --repo /srv/odile-backup/tenants/odl-bench --recovery d-recovery.txt
same         the repository already answers; init is a no-op
$ backup.py verify --repo … --recovery d-recovery.txt
password     opens the repository (2 snapshot(s))
wrong one    refused
$ backup.py verify --repo … --recovery a-recovery.txt
password     opens the repository (2 snapshot(s))
$ backup.py verify --repo … --recovery c-recovery.txt
backup: the recovery password did not open the repository: … exit_error … code:12 …
$ backup.py run --repo <a byte-identical copy> --recovery d-recovery.txt --host odl-bench …
ok           snapshot 4cf563b0 of /var/tmp/odile-t_fec9f699-run/home in 0.4s
status       /var/tmp/odile-t_fec9f699-run/backup.json
$ restic snapshots
snapshot f07960bd host odl-bench 2026-11-12T22:07:44Z
snapshot fa0aead0 host odl-bench 2026-11-12T22:17:48Z
snapshot 4cf563b0 host odl-bench 2026-11-13T09:31:42Z
```

`init` and `verify` only read the repository (`restic snapshots --no-lock`); the
one `run` writes to a copy, so the bench repository stays exactly as A left it.
The copy carried a stale lock from the previous drill, so the first `run`
printed `warning retention/prune failed; the snapshot is still valid`; `restic
unlock` removed it and the run above is clean. The `record …` text in the
first-boot line is the measured-boot POST, which this bench fixture refuses
(400) because its `initramfs_host_key` is a placeholder; it is ODL-398's field,
not the backup password, and the guest installs regardless.

### 5.3 Restore (T2 23:46:37Z, 0.861 s)

On the machine that holds the recovery file — the user's laptop, or HQ acting
as the user for this test tenant — `odilectl restore` runs restic and needs no
Odile credential:

```
$ odilectl restore odl-bench --apply \
    --repo 'sftp://odile-backup@127.0.0.1:2222/tenants/odl-bench' \
    --password-file /var/tmp/odile-215-run/a-recovery.txt \
    --ssh-key /var/tmp/odile-215-run/restore-key \
    --known-hosts /var/tmp/odile-215-run/known_hosts \
    --target /var/tmp/odile-215-run/restore-c --include /home/tenant
restore: applied
  repository: sftp://odile-backup@127.0.0.1:2222/tenants/odl-bench
  snapshot:   latest
  target:     /var/tmp/odile-215-run/restore-c
  restic:     Summary: Restored 550 / 549 files/dirs (3.630 MiB / 3.630 MiB) in 0:00
```

The checks pass in the restored tree before anything travels:

```
$ cd /var/tmp/odile-215-run/restore-c/home/tenant
$ sha256sum -c backup/drill-manifest.txt
drill/alpha.txt: OK
drill/beta.txt: OK
drill/nested/random.bin: OK
$ sha256sum backup-drill/canary.txt
174695432a9daf5a2d345c0ef57a531e8859792a7fc2d86f0c7bf8bd9c69cfb4  backup-drill/canary.txt
```

The manifest is A's own record, written before the destroy and carried inside
the snapshot: the restored files are checked against a hash list that predates
the machine's death, not against the restore's own output.

### 5.4 Into the rebuilt guest (T3 23:52:52Z)

The rebuilt guest has no user key and no operator key, so it pulls the restored
home through its own Webapp and its own Hermes cron, as the tenant:

```
2026-11-12T23:52:43Z == start host=odile-bench cmd=…
… urlretrieve("http://10.0.2.2:8098/drill2-home.tar.gz", "home.tar.gz") | tar -xzf home.tar.gz | cd tenant | sha256sum -c backup/drill-manifest.txt | sha256sum backup-drill/canary.txt
VERIFY-AT 2026-11-12T23:52:49Z
drill/alpha.txt: OK
drill/beta.txt: OK
drill/nested/random.bin: OK
174695432a9daf5a2d345c0ef57a531e8859792a7fc2d86f0c7bf8bd9c69cfb4  backup-drill/canary.txt
DRILL2-OK
2026-11-12T23:52:49Z == end rc=0
```

The home lands at `/home/tenant/restored/tenant` (the rebuilt guest keeps its
own live `.hermes`), and every file the manifest names matches the hash A
recorded before it died.

### 5.5 The numbers

| Number | Value | How it is measured |
|---|---|---|
| RTO, end-to-end | **84m13s** | A's destroy (22:28:39Z) to T3 (the guest's `DRILL2-OK`, 23:52:52Z) — the home restored is A's |
| — the timed rebuild run | **38m03s** | B's destroy (T0 23:14:49Z) to T3; this is the number "under an hour" quotes |
| — build | 7m48s | `prepare`/`build` start to the signed UKI |
| — boot to first-visit | 6m29s | qemu start to `GET /first-visit -> 200` |
| — restic restore | 0.861 s | the `odilectl restore` call |
| — transfer + checksum | 6 s | the guest's `VERIFY-AT` to `DRILL2-OK` |
| — operator + harness | 23m39s | the gaps between those steps inside the timed run, including the killed first build (7m) and its 1m13s restart |
| — harness gap before the timed run | 46m10s | 8m48s (A's destroy → B's first visit) + 37m22s (B idle until the timed run began); the worker's 420 s per-call limit, not the machine |
| RPO | **10m51s** | A died 10m51s after its newest snapshot (22:17:48Z → 22:28:39Z) |
| RPO worst case | 24 h | the nightly timer, if the machine dies just before a run |

The timed run's newest snapshot was already 57m01s old when B died, because B
had not been backed up; the home restored is A's, so the data-loss window that
matters is A's 10m51s. The 84m13s is the drill's end-to-end wall clock; the
38m03s is its post-start rebuild; the machine's own share of the 38m03s is
14m24s, and the other 46m10s of the 84m13s is harness time the product does not
own. The "under an hour" sentence quotes **38m03s** — say so wherever the
sentence is quoted (ODL-474 lands it).

## 6. Failure modes

| Symptom | Check | Fix |
|---|---|---|
| `backup: cannot read the recovery file` | `ls -l /etc/odile/recovery.txt` | the file is gone (the user shredded the local copy): restore it from the user's kit; the repository is unreadable without it |
| `the recovery file's password does not open the existing repository` | `sed -n 's/^Backup password:.*/present/p'` | the file is from another machine or truncated; use the user's file, never re-`init` |
| `restic init failed: … unable to start the sftp session` | `ssh -i /etc/odile/backup-ssh/id_ed25519 … -s sftp odile-backup@host` | the guest's public key is not in the box's `authorized_keys`; register it (§3) |
| `Host key verification failed` | `ssh-keygen -F host -f /etc/odile/backup-ssh/known_hosts` | the box was rebuilt; re-run `init` to record the new host key, then a run |
| `repository is already locked` | `restic list locks` | `restic unlock` when no run is active |
| `no space left on device` | `df -h /home /var/cache/odile` | the cache and the stage live under `/var/cache/odile`; prune the cache, not the repository |
| the status file is missing after a run | the run failed before restic finished | `systemctl status odile-backup-tenant.service` and `journalctl -u …`; the previous `last_success_at` is preserved |
| `odilectl status` says `no heartbeat yet` | the guest cannot reach HQ | the timer's `ExecStartPost` is best-effort; run `backup.py heartbeat` once the network is back |

---

## 7. Retention, rotation and exit

- **Retention** is 7 daily and 4 weekly, pruned on every run. `restic snapshots`
  on the box shows the current set; a rebuild adds a snapshot under the same
  `--host`, so the group stays one.
- **Rotate the repository password:** `restic key add` with a new password,
  prove a restore with it, then `restic key remove` the old one, and update the
  user's recovery file. Never all three without a successful drill between.
- **Move the repository:** a new URL is `--repo` on the next `init`/`run` and
  the seed's `backup_repo_url`; the password and the program do not change.
  Keep the old box for 30 days, then delete it. **The interim → B2 move
  (19 November 2026)** is `restic init` on the B2 bucket with the tenant's existing
  password, `restic copy --from-repo <the R2 repository>`, one drill, a re-rendered
  recovery file (new URL and read-only key, same password line), then 30 days and
  the R2 bucket is emptied and deleted; ADR-004 §10 has the paragraph.
- **Exit:** the encrypted repository is kept 30 days after cancellation, then
  deleted. The user can download it with the password before then; Odile can
  delete it, not read it. The deletion is `odilectl purge <slug>`
  (`docs/runbooks/offboard.md` §4.1, **ODL-935**, measured 2026-11-26 on the
  bench pair). On the BX11 it forgets and prunes every snapshot with the
  tenant's read-write key over SFTP on port 22 — or, when the user's password
  is not at hand (the production case), deletes the snapshot objects with that
  key, leaving the repository's `config` and `keys/` — then deletes **both**
  sub-accounts through the Console API and records
  `detail.backup.subaccount_deleted_at`. Two things outlive the deletion: the
  `tenants/<slug>` directory stays, with no user data but with `.ssh` and the
  repository's `config` and `keys/` (only the box's main account can delete
  it, and HQ does not hold that credential), and the box's own snapshots
  (04:30 UTC, 10 kept) can carry the deleted directory for up to about ten
  days. The 30-day promise and `keep_until` are unchanged.

---

## 8. Evidence log

| Date | What | Result |
|---|---|---|
| 2026-11-12T19:01Z | First repository on the drill box (`init`, 3 calls: 2 failures before the key was registered) | created; `config`, `data`, `index`, `keys`, `snapshots` under `/srv/odile-backup/tenants/odl-fake01` |
| 2026-11-12T19:02:41Z | First nightly run (`backup.py run`) | snapshot `b50aa5e5`; 156,311 files / 3,139,596,963 bytes in 27.3 s; repository 1,017,111,156 bytes |
| 2026-11-12T19:04:08Z | **Drill 1** (`backup.py drill`) | PASS: canary `896bcb5b…` before = after; snapshot `74e1507d`; RTO 0.931 s; RPO 0.0 s; the whole drill 7.553 s |
| 2026-11-12T19:07:29Z | Password check (`backup.py verify`) | password opens (2 snapshots); a wrong password refused |
| 2026-11-12T19:08:34Z | Unit run (`systemctl start odile-backup-tenant.service`) | snapshot `08c1103b` in 6.5 s; heartbeat accepted `{"ok": true}`; timer next 2026-11-13T03:00:17Z |
| 2026-11-12T19:08:44Z | `odilectl status odl-fake01` | `backup: last 2026-11-12T19:08:44Z … snapshot 08c1103b, 1017383711 bytes` |
| 2026-11-12T19:14:56Z | HQ cannot read | no password in `/root/.odile`, `/etc/odile`, `/opt/odile`; the storing host gets `wrong password`; a pack has no plaintext name |
| 2026-11-12T22:25:00Z | Nightly unit again (`systemctl start`) | snapshot `fb0f6694` in 1.0 s; heartbeat accepted; `odilectl status` shows `last 22:25:00Z … snapshot fb0f6694` |
| 2026-11-12T22:28:39Z–23:52:52Z | **Drill 2** (destroy A, rebuild as B and then C, restore A's home with A's recovery file) | PASS: 550 files / 3.630 MiB restored in 0.861 s; `sha256sum -c` OK for all three manifest files; canary `17469543…`; end-to-end RTO 84m13s, of which the timed rebuild-and-restore is 38m03s; RPO 10m51s |
| 2026-11-13T09:18–09:31Z | **A rebuilt guest continues the repository** (t_fec9f699; the sealed path, retired by ODL-937) | the fixture sealed A's password under the new token (no plaintext in `seed-rebuilds.json`); the rebuilt guest's own download carries the same `Backup password:` line as A (`cmp` rc=0), C's pre-fix file differs; `backup.py init` → `same`; `verify` opens 2 snapshots with the guest's file and with A's, and refuses C's (rc 1, restic 12); a `run` writes snapshot `4cf563b0` under `--host odl-bench` beside A's two |

## 9. What this card leaves open

- **The interim destination (19 November 2026).** The items below describe the
  SFTP stand-in; the live interim is Cloudflare R2 (§3.1). What remains open:
  Savio creates at most three `odile-t-<slug>` buckets (the demo `odl-7971a8`,
  the bench `odl-bench`, the first paying tenant) and mints two bucket-scoped
  tokens each; `odilectl provision` records
  them and verifies the scope; the demo and bench repositories are re-initialised
  against the R2 URL; and the R2-era 9 GB readback/alert is superseded by the
  box's readback (**ODL-934**, shipped at this head: §3.2, 800 GiB warn /
  950 GiB critical). The return to B2 is ADR-004 §10, not a new design.
- **The repository and its key registration.** The installer now places the
  program and arms the timer (t_19148cba), so a fresh tenant has the job. The
  repository still needs its first `backup.py init`: it generates the SFTP key
  on the guest, the public half must be appended to the backup box, and the
  restic repository is created once. `backup.odile.fyi` is a seed value, not a
  machine — **t_8eb17a54** is the human step (Savio buys the storage box, the
  MVP budgets $0.60/month for a 100 GB share) and points `backup.odile.fyi` at
  it; the program needs only the new URL. The drill's destination is a
  dedicated chrooted sftp instance on HQ (port 2222): the bytes are restic
  ciphertext and the control plane holds no password (§1), but they share HQ's
  fate. The drill's copy of the user's file under `/var/tmp` is the only
  password beside the ciphertext, and it is the user's own download, not an
  Odile store.
- **A rebuilt machine continues the same repository — by the paste, not a seal
  (t_fec9f699 measured it; ODL-937 made the paste the live path).**
  `odilectl provision odl-7f3a2c --key odile-boot.pub --pin odile-verify.pin
  --apply` reopens the destroyed row and orders the machine, and passes no
  password. The reopen records the dead machine's id in the seed
  (`backup.previous_instance`); a seed that names the machine it replaces makes
  the installer write no recovery file, so the rebuilt machine's first visit is
  the paste (§2.2). The paste writes the user's file back for **this** machine
  — the same password, the new `Instance:` and certificate, the repository, the
  pinned host key and the read-only key — and starts the restore, so HQ holds
  no copy at any point. Measured on the bench (§5.2), on the sealed path this
  replaces: the rebuilt guest's recovery file opened the repository,
  `backup.py init` printed `same`, and the user's old file still opened it. The
  live tenant's rebuild is ODL-218's
  destroy-and-re-provision path (`docs/runbooks/offboard.md` §5), a human step.
  ADR-007 (`t_ce52bb8d`) makes it the same slug: `destroyed -> claimed` is the
  one edge back, `provision <slug>` reopens the row before it orders, points
  the web row and the state row at the new machine and moves the state row to
  `first_boot` (so the new machine's first boot records itself), and the URL,
  the recovery file's name and the `tenants/<slug>` repository all survive — so
  `backup-repo.txt` names the same repository and the paste is the live path,
  not a bench-only one. The sealed-password rebuild is retired: ODL-937 removed
  every writer and reader of `--backup-password-file` and
  `/var/lib/odile/seed-rebuilds.json` from the tracked tree. `recovery-seed.py
  --password-stdin` survives as the operator's tool, not as a rebuild step —
  its own docstring: "Nothing runs it automatically: a rebuild's recovery file
  comes from the first-visit paste, and this program leaves the machine without
  one until then (`rebuild_waiting_for_paste`, ODL-937)."
- **The Servury rebuild.** Drill 2 rebuilds a VM from the same signed recipe;
  destroying and re-provisioning the *live tenant* is ODL-218's path (destroy
  needs the provider API and a new order, i.e. Savio), and it reuses this
  runbook's §5.3–§5.4 unchanged.
- **The 30-day purge** is a command now (**t_9d6fb595**):
  `odilectl purge <slug>` keeps the row's `keep_until`, refuses before
  it, and past it runs `restic snapshots`, deletes the box's directory and
  drops the guest's key line; a second run says `already purged`
  (`docs/runbooks/offboard.md` §4.2). The BX11 exit is **ODL-935**'s and is
  shipped (2026-11-26): prune with the read-write key over SFTP, delete both
  sub-accounts, record `detail.backup.subaccount_deleted_at` — §7 "Exit" and
  `offboard.md` §4.1. The HQ-root path above is kept for the demo and bench
  migration; no bucket path shipped.

The follow-ups are board cards, not prose: **t_fec9f699** (rebuild keeps
the repository), **t_19148cba** (installer places the program and the units,
heartbeat reachable — this card), **t_8eb17a54** (Savio buys the box and
sets DNS), **t_9d6fb595** (the v5 30-day purge, §9) and **ODL-935** (the BX11
exit, §7 "Exit" — shipped 2026-11-26; no bucket half exists).
