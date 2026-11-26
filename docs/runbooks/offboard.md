# Offboard — the export, the destroy, and the way back

ODL-218. A tenant can leave with its data, and leaving bills nothing. One
command, `odilectl destroy`, runs the whole path in order, and prints every
step before it runs the first one.

The four things this page owns:

| The thing | Where it ends up | Who holds it |
|---|---|---|
| the data | one gzipped tar in the tenant's home, downloaded by the user | the user |
| the machine | terminated at the provider; `?action=server&id=` answers 404 | nobody |
| the DNS record | deleted from the zone; a second delete is a no-op | nobody |
| the encrypted backup | kept **30 days**, then deleted on the backup box | the user's password; the box's ciphertext |

**No operator SSH.** The export is the user's own command in the Webapp's
Terminal pane; the download is the Files pane. The only reads HQ makes are the
provider's API, the zone's API, the last signed heartbeat, and the tenant's own
`/verify` page over HTTPS. There is no shell on the machine, before or after.

**The rule that is easy to get wrong: the bundle belongs to the user.** It is
written on the machine by `odile-export`, downloaded to the user, and checked by
the operator on the copy the user holds. HQ's copy in this drill lived in
`/var/tmp/odile-218-run` (the card's scratch root, swept after the card). The
bundle carries every secret in the data — the agent's `.env` —
because it is the user's data; that is the point of leaving with it.

---

## 1. The bundle

### What is in it, and what is not

The live tenant `odl-fake01` measured on 2026-11-13: home 4.1 GB, root disk
7.3 GB with **883 MB free**. The whole home cannot be exported in place, so the
program takes the **data** and leaves the **runtime**:

| In the bundle (measured: 702 entries, 1,429,556 bytes) | Out of it (re-installed by a rebuild) |
|---|---|
| `AGENTS.md`, `documents/`, `workspace/` — the user's files | `.hermes/hermes-agent` (2.1 GB — the Hermes checkout) |
| `.hermes/state.db` — the session store, copied by SQLite's backup API | `.hermes/tools` (987 MB — the node/python toolchain) |
| `.hermes/memories/`, `.hermes/skills/`, `.hermes/sessions/` | `.hermes/cache` (461 MB), `.hermes/logs`, `.hermes/installs` (128 MB) |
| `.hermes/config.yaml`, `.hermes/.env`, `SOUL.md`, `cron/`, `skins/`, `plugins/`, `platforms/`, `state/`, `kanban.db`, `projects.db` | `models_dev_cache.json`, `runtime/`, `audio_cache/`, `image_cache/`, `*.lock`, `*.sock` |

Anything else in the home can be added by name:
`odile-export <name> backup bench` adds those two directories. The excluded
runtime is not the user's data, and a rebuilt guest gets it back from the
installer and `odile-update`.

The script is `odilectl/offboard.py`'s `EXPORT_PROGRAM`, printed byte-for-byte
by `odilectl export <slug> --script` (a test pins the two). The signed
installer carries those exact bytes — `installer/embed_export.py` embeds the
program, `--self-test` pins it and fails on a one-byte edit — and places it at
`/usr/local/bin/odile-export`, 0755 root, on every install (ODL-218 follow-up).
There is no hand placement.

The scratch install proves the placement and the bytes independently: the
root's copy and `--script`'s output are hashed separately and compared.

```
$ /usr/bin/python3 installer/sim_tenant.py --root /var/tmp/odile-218f-sim \
      --user odile218f --port 9346 --seed-port 9347 --reset --apply
== the guest export program (ODL-218 follow-up)
export       /var/tmp/odile-218f-sim/usr/local/bin/odile-export mode 0o755 sha256 864604aefc785d3a7e0f8dd7f7c5523fdcacc538ec21c47ccea1cf74036cdf96
odilectl export odl-sim-203 --script | sha256sum -> 864604aefc785d3a7e0f8dd7f7c5523fdcacc538ec21c47ccea1cf74036cdf96
ok   the installer places /usr/local/bin/odile-export
ok   the placed odile-export is 0755: 0o755
ok   the placed odile-export is byte-identical to `odilectl export <slug> --script`: 3897 vs 3897 bytes
```

The copy placed on the live tenant for the drill is
`f128a3c95bf25e9290fb607b8f5cd4df00403618bab890ae2ead950fad060f49`, equal to the
checkout's `odilectl export odl-fake01 --script | sha256sum` at the time. The
checkout now prints
`c635c4323ef404b4532e1cbb95bd9a80f1e69357b88c4e11fc598c6493132dae`: the review
added two fixes to the program — the staging tar is a file, not a pipe (so a
tar that exits 2 stops the run instead of letting `tar | tar` print "wrote"),
and `kanban.db` is copied through SQLite's backup API with its `-wal`/`-shm`
excluded, the same way as `state.db`. Both are pinned by
`tests/test_offboard.py` (a stub tar that exits 2; a task board whose row is
only in the WAL at copy time). The checkout's program run on a fixture home is
`evidence/program-current.txt`: its printed program is the same bytes as
`export --script` (`c635c432…`), and its bundle — 722 bytes, 13 entries, no
`kanban.db-wal`/`-shm` members — passes `export --verify` as complete. The live
tenant still holds the earlier copy: its Webapp began refusing `/login` with
`400 Invalid Host header` at 2026-11-13T05:33Z after a boot-time release
install (§6), which was an installer defect and is fixed and recovered
(t_d95b8c13, 2026-11-15T20:12Z: `=== rc=0`, `/login` 200, the sign-in 200 with
four `__Host-` cookies), and the new program now travels inside the signed
installer itself (**t_2efbd6e3**, merged), so it reaches a tenant on the next
install rather than by hand placement.

The first run prints the write and the second reads `same`:

```
write        /var/tmp/odile-218f-sim/usr/local/bin/odile-export (sha256 864604aefc78…)
same         /var/tmp/odile-218f-sim/usr/local/bin/odile-export
```

The bytes the installer now carries are `864604ae…`, the checkout's current
`--script` output. At ODL-218 the reviewed program was `c635c432…`: the review
made the staging tar a file instead of a `tar | tar` pipe (so a tar that exits
2 stops the run instead of letting the pipe print "wrote"), and copied
`kanban.db` through SQLite's backup API with its `-wal`/`-shm` excluded, the
same way as `state.db`; `tests/test_offboard.py` pins both. After that review
the tree changed one comment in the program — "(the meter key)" became
`/etc/odile/key.env` — so `--script` prints `864604ae…` today; the algorithm is
identical, and `installer/embed_export.py` pins whatever the checkout prints at
embed time. The live tenant still holds the drill's hand-placed copy,
`f128a3c9…`, which predates both review fixes: at 2026-11-13T05:33Z its Webapp
began refusing `/login` with `400 Invalid Host header` after a boot-time release
install (§6), and the installer-placed program reaches it when it is
re-provisioned (**t_8d50106b**). The quoted run below is the hand-placed
copy's; the data it carries is the same.

The first argument is the bundle's name (`name=${1:-$(hostname)}`), so `-h` and
`--help` are not special: `odile-export --help` writes a bundle named
`odile-export---help-<stamp>.tar.gz` rather than printing usage. Pass a name
only: `odile-export odl-fake01`.


The first drill run failed because the staging directory landed in `TMPDIR`
(`${HOME}/.hermes/cache/scratch` on a tenant) and `tar .hermes` then copied a
directory into itself; the program stages in `/var/tmp` and the second run is
the one quoted here.

### The run, as the user sees it

```
$ odile-export odl-fake01
wrote /home/tenant/odile-export-odl-fake01-20261113T032107Z.tar.gz
bytes 1429556
sha256 478890ecf75a5227275a28204a48197ab4d984c27c24a7524fb604fa49700988
members AGENTS.md documents workspace .hermes
```

### The download

The Files pane reads the same file the browser does; the request behind a
download is `GET /api/fs/download?path=…`, and the drill measured the round
trip rather than trusting the page:

```
GET /api/fs/download?path=%2Fhome%2Ftenant%2Fodile-export-odl-fake01-20261113T032107Z.tar.gz
status=200 bytes=1429556
sha256=478890ecf75a5227275a28204a48197ab4d984c27c24a7524fb604fa49700988
guest_said=478890ecf75a5227275a28204a48197ab4d984c27c24a7524fb604fa49700988
equal=True
```

The guest's copy is deleted after the download (`rm -f`); the user keeps the
one they saved.

### The inspection

`odilectl export <slug> --verify PATH` is the operator's inspection of their
copy, and the same function `destroy --export` refuses without:

```
$ /usr/bin/python3 -m odilectl export odl-fake01 --verify /var/tmp/odile-218-run/odile-export-odl-fake01-20261113T032107Z.tar.gz
export: odl-fake01
  bundle:    /var/tmp/odile-218-run/odile-export-odl-fake01-20261113T032107Z.tar.gz
  size:      1429556 bytes in 702 entries
  sha256:    478890ecf75a5227275a28204a48197ab4d984c27c24a7524fb604fa49700988
  top:       .hermes, AGENTS.md, documents, workspace
  members:   home, memory, skills and the session store are present
  listing:   the first 40 entries
    f AGENTS.md
    - workspace
    f workspace/cron-proof.log
    f workspace/README.md
    f workspace/smoke.txt
    - documents
    - .hermes
    f .hermes/gateway-starts.log
    - .hermes/backups
    - .hermes/backups/config
    f .hermes/backups/config/config.yaml.good.20261112-144610
    f .hermes/backups/config/config.yaml.good.20261112-102614
    f .hermes/backups/config/config.yaml.good.20261112-102530
    f .hermes/backups/config/config.yaml.good.20261112-102739
    f .hermes/.env
    f .hermes/kanban.db-shm
    f .hermes/spawn-ledger.json
    - .hermes/state
    f .hermes/state/dashboard_clients.heartbeat
    f .hermes/state/gateway.heartbeat
    f .hermes/state/gateway.lifecycle.json
    f .hermes/state.db
    - .hermes/skins
    f .hermes/skins/odile.yaml
    - .hermes/plugins
    - .hermes/hooks
    f .hermes/.gateway_state_zpyulwu3.tmp
    f .hermes/kanban.db-wal
    - .hermes/cron
    - .hermes/cron/output
    - .hermes/cron/output/d695ac56e581
    f .hermes/cron/output/d695ac56e581/2026-11-13_02-56-40.md
    f .hermes/cron/output/d695ac56e581/2026-11-13_03-20-40.md
    f .hermes/cron/output/d695ac56e581/2026-11-13_03-02-40.md
    f .hermes/cron/output/d695ac56e581/2026-11-13_03-08-40.md
    f .hermes/cron/output/d695ac56e581/2026-11-13_02-46-40.md
    f .hermes/cron/output/d695ac56e581/2026-11-13_01-56-40.md
    f .hermes/cron/output/d695ac56e581/2026-11-13_01-52-40.md
    f .hermes/cron/output/d695ac56e581/2026-11-13_02-54-40.md
    f .hermes/cron/output/d695ac56e581/2026-11-13_02-58-40.md
  verdict:   complete; destroy --export accepts this file
```

The block is the command's whole output, copied from `evidence/verify.txt`
(attached, 1,970 bytes): the command prints the **first 40** entries
(`LISTING_LINES` in `odilectl/offboard.py`), so that is the listing, not a
selection. The bundle is the placed copy's (`f128a3c9…`), so it carries
`.hermes/kanban.db-wal` and `-shm` beside the board; the checkout's program
(`c635c432…`) copies `kanban.db` through SQLite's backup API and excludes the
two, as `evidence/program-current.txt` shows. The data is the same.

An incomplete bundle is exit 2 with the missing members named; a file that is
not a gzipped tar is exit 2; nothing in the path reads the provider or the zone.

---

## 2. The plan — every action, before the first one

The dry run on the live tenant, 2026-11-13T03:32Z, with the bundle above:

```
$ odilectl destroy odl-fake01 --export odile-export-odl-fake01-20261113T032107Z.tar.gz --dry-run
destroy: dry run (pass --apply to execute)
  1. export.verify: file="odile-export-odl-fake01-20261113T032107Z.tar.gz", sha256="478890ecf75a…", bytes=1429556, entries=702, members="ok"
  2. support.check: url="https://odl-fake01.odile.fyi/verify", result="none", line="operator key PASS none (authorized_keys: 1 line(s), the measured throwaway)"
  3. dns.delete: zone="odile.fyi", name="odl-fake01.odile.fyi", type="A", record_id="fixture-record-id-1", result="present"
  4. provider.terminate: hostname="odl-fake01", server_id="0badc0de", ipv4="203.0.113.87"
  5. provider.absent: server_id="0badc0de", until="not listed by ?action=servers", timeout_seconds=120.0
  6. state.destroy: slug="odl-fake01", from="ready", detail="bundle, grant, DNS, machine and retention"
  7. backup.retain: repo="sftp://***@203.0.113.139:2222/tenants/odl-fake01", snapshot="828e8fab…", status="reported", keep_until="2026-12-13T03:32:59Z", days=30, then="delete the repository on the backup box"
```

Read it as a contract: the bundle is checked first, the grant before the
machine, the DNS record named by id, the machine named by server id and
address, the row's *from* state, and the date the backup is deleted. No step
guesses.

The dry run makes three read calls and no writes: `?action=servers` once, the
zone's `GET dns_records`, and the tenant's `/verify`. The plan does not build a
provider for a command that never needs one (`export --script`, `export
--verify`) and does not open the zone when the hostname is not a tenant slug.

Refusals, all before the first write:

| Refusal | Exit | When |
|---|---|---|
| no `--export` and no `--skip-export` | 2 | the bundle is an input; the plan says "no bundle given" and still lists every step |
| the bundle is missing a member | 2 | `.hermes`, `.hermes/memories`, `.hermes/skills`, `.hermes/state.db` or `AGENTS.md` absent |
| the tenant reports a **live support grant** | 2 | `FAIL authorized_keys carries …` on `/verify`; `--force` overrides |
| two machines answer to one hostname | 4 | never a guess |

The table is the **apply** path's exit codes. The dry run runs the same checks,
prints every step and exits 0, so a dry run that names a missing bundle or a
live grant still shows the plan.

---

## 3. The destroy — measured on two scratch tenants

The live tenant `odl-fake01` is Savio's own agent and its destroy + rebuild is
**t_8d50106b** (the $47.44 order and the panel's disk image are his: see §5). The
destroy path itself was measured end to end on two scratch tenants ordered for
this card, on the host account, at $2.15 each:

| Fact | Drill 1 | Drill 2 (the re-provision, §5) |
|---|---|---|
| order | invoice `b473060c`, Blackbox Core, **7 days, $2.15** | invoice `e0e6fbce`, same |
| machine | `0b3234a2`, `odl-218a1b`, 203.0.113.182 | `fbf0f1f0`, `odl-218c1d`, 203.0.113.182 |
| row | `invited → claimed → provisioning`, release `2026.11.1` | same |
| DNS record | `fixture-record-id-2` → the address, TTL 120 | `fixture-record-id-3` |
| destroy | `destroyed: true`, `absent_at 2026-11-13T03:28:51Z`, DNS `deleted`, `keep_until 2026-12-13T03:28:40Z` | `destroyed: true`, `absent_at 03:31:50Z`, DNS `deleted`, `keep_until 2026-12-13T03:31:39Z` |
| provider after | `?action=server&id=0b3234a2` → **HTTP 404** `{"success":false,"error":"Server not found."}` | `?action=server&id=fbf0f1f0` → **HTTP 404**, the same body |
| zone after | `GET dns_records?type=A&name=odl-218a1b.odile.fyi` → `success true, result_count 0` | the same for `odl-218c1d` |
| account after | `?action=servers` → `total 2` (`odl-fake01`, `hostfake` — the live tenant and HQ) | same |

The two 404s and the zone GETs were re-read on 2026-11-13T05:45Z and are the
raw transcripts in `evidence/provider-after.txt` and `evidence/dns-after.txt`
(§7), not the table's retelling.

The row, after the second destroy, is the record of the whole run:

```
state: destroyed | server_id: fbf0f1f0 | release: 2026.11.1
detail: {"absent_at": "2026-11-13T03:31:50Z",
         "backup": {"days": 30, "keep_until": "2026-12-13T03:31:39Z", "repo": null, "snapshot": null},
         "dns": {"action": "deleted", "name": "odl-218c1d.odile.fyi",
                 "record_id": "fixture-record-id-3", "zone": "odile.fyi"},
         "export": {"skipped": true},
         "grant": {"source": "https://odl-218c1d.odile.fyi/verify", "status": "unknown"},
         "machine": {"ipv4": "203.0.113.182", "server_id": "fbf0f1f0"},
         "reason": "ODL-218 re-provision after a destroy"}
events: invited → claimed → provisioning → destroyed
```

The scratch tenants never booted an image, so `/verify` did not answer and the
grant report is `unknown` — a machine that does not answer is not a refusal. On
the live tenant the same step read the real page (`operator key PASS none …`,
§2 step 2).

A second run of the same command converges: `already_gone: true`, DNS `absent`,
`state: already destroyed`, one event, no new write (unit test
`test_a_second_apply_converges`). A terminate that answers 404 is
`already_gone: true` too. A terminate that is accepted but the id is still
listed after `--wait` is exit 4 and **the row stays live**, so the drift is
visible and the next run finishes the job.

### "No active grant"

A grant lives in one place: the tenant's own `authorized_keys` behind a forced
command, with its state file on the machine. It cannot outlive the machine, so
the destroy closes it by construction. The plan still reads the tenant's
`/verify` first, because a **live** grant means the user has an open handover
and the destroy would end it without their `odile-support revoke`:

The second run on the destroyed slug is the transcript that shows what "a
second run is a no-op" means in practice — real output, 2026-11-13T03:40Z:

```
$ odilectl destroy odl-218c1d --skip-export --apply
destroy: applied
  bundle:     skipped (--skip-export)
  grant:      unknown (https://odl-218c1d.odile.fyi/verify)
  dns:        odl-218c1d.odile.fyi was already absent in odile.fyi
  machine:    odl-218c1d is already gone (no machine at servury)
  state:      odl-218c1d was already destroyed
  backup:     kept until 2026-12-13T03:31:39Z (30 days), no heartbeat carried a repository
```

The date is the **first** run's, not the second's: a row that recorded
`keep_until` keeps it (`test_a_second_run_keeps_the_recorded_deletion_date`
shifts the clock a day between the two runs). The first run's own lines are the
same with the writes in place — `dns: removed … (record …)`, `machine:
terminate requested for …`, `machine: … is gone from the account at …`, `state:
… provisioning -> destroyed (one event)` — and its JSON is in the table above.

`--force` exists for the case where the grant belongs to a machine that is
going away anyway (an expiring tenant); the unit test
`test_force_destroys_under_a_live_grant` pins both halves.

---

## 4. The backup — 30 days, then delete

The encrypted repository is the tenant's, and HQ holds no password for it
(`docs/runbooks/backup.md` §1). The destroy records the **date** — 30 days from
the run — and the last heartbeat's repository and snapshot, so the deletion is
a dated step, not a memory:

- `keep_until` is on the destroyed row's `detail.backup.keep_until` and in
  `destroy --json` (live tenant: `2026-12-13T03:32:59Z`, snapshot `828e8fab…`,
  in the 03:32:59Z text run; the 03:22:13Z `--json` run printed its own clock,
  `2026-12-13T03:22:13Z` — the date is 30 days from the run that made it).
- Before the date, nothing touches it. `destroy` never deletes the repository —
  it only prints the date and the path.

**The BX11 exit — ODL-935, shipped and measured 2026-11-26.** Every tenant on
the Hetzner Storage Box (box 709870, `hel1` Helsinki — the EU) has one
`odile-<slug>-rw`/`-ro` sub-account pair, and the repository sits in the
read-write account's `data/restic` (ADR-004 v3 §4.1). At `keep_until`,
`odilectl purge <slug>` prunes that repository with the row's read-write key
over SFTP on port 22, deletes **both** sub-accounts through the Console API,
and records `detail.backup.subaccount_deleted_at`. §4.1 is the shipped shape
and the measured transcript. The HQ-root path below (`t_9d6fb595`) is still
the branch for a row whose URL names the old stand-in's absolute
`/tenants/<slug>` — the demo and bench migration — and it is kept byte for
byte.

### 4.1 The BX11 exit — measured on a bench pair, 2026-11-26T08:35Z

The command is a control-plane verb (`odilectl purge`), deliberately not a
`scripts/backup.py` verb: that program is embedded byte-for-byte in the signed
installer, and the fact this command reads (the tenant row) lives on HQ. It
runs on **HQ**, not on the box. The destination is **the row's repository
URL**, not a flag:

| The row's `detail.backup.repo` | Branch |
|---|---|
| `sftp:` with an **account-relative** path (`data/restic`, `data/repo-<4 hex>`) | the BX11 exit (this section) |
| `sftp:` with the old stand-in's **absolute** `/tenants/<slug>` | the HQ-root path (§4.2), byte for byte |
| a local path | the HQ-root path (§4.2) |

For a BX11 row the credentials and ids come from
`/root/.odile/hetzner/odile-<slug>.env` (0600) — the store of record
`odilectl provision` writes. A missing file refuses by name and path. The
read-write private key and the pinned host key are written to 0600 files for
the length of one call; the token is read from its 0600 file and sent as an
`Authorization` header. **No password, key or token reaches argv or a log.**

The order is the one the API imposes:

1. `restic snapshots --json` (over `sftp.command=ssh -i <key> … -p 22 <user>@<host> -s sftp`,
   with the pinned `known_hosts`) with the tenant's read-write key.
2. `restic forget --prune <id>` for **every** snapshot, which removes the
   snapshot data and leaves the repository's `config` and `keys/`.
3. `GET /v1/storage_boxes/709870` — the box's own snapshot plan, so the
   survivor note is measured, not remembered.
4. `DELETE /v1/storage_boxes/709870/subaccounts/<id>` for the **read-only**
   half, then the **read-write** half; each write answers an action that is
   polled to `success`.
5. `detail.backup.subaccount_deleted_at` on the row, with the per-half ids and
   results, the prune record and the two survivors. It is a detail, not a new
   event (`tenant_state_events.kind` is the state vocabulary). `keep_until`,
   the repository and the snapshot stay where `destroy` wrote them, and so do
   `dns`, `export`, `grant`, `machine` and `reason`.

**The repository password is the user's** (ADR-004 §3): HQ holds the keys, not
the password. With `--password-file` (the user's recovery file, or a bare
password file) the command runs the two restic calls above. Without it — the
production case at `keep_until` — restic answers "wrong password" (exit 12),
and the command deletes the repository's `snapshots/`, `index/` and `data/`
objects with the read-write key instead, which is the same end state: no user
data, `config` and `keys/` only. The snapshot ids are the snapshot files' own
names, so the record still names what was pruned. A **supplied** password that
does not open the repository is exit 4 and nothing is deleted.

**The two things that outlive the deletion, stated and recorded:**

- **`tenants/<slug>` stays, and it is not literally empty.** Measured on the
  bench pair: after both sub-accounts were deleted, a throwaway read-only
  sub-account on the surviving home listed
  `.ssh/authorized_keys`, `data/.ssh`, and the pruned repository's
  `config` plus `keys/<id>`, with `data/`, `index/`, `snapshots/` and
  `locks/` present and **empty** (`ls -1 data/restic/data/*/*` and the three
  other globs answer `not found`). "Empty" means **no user data**; the
  skeleton and the key files remain. Hetzner deletes the sub-account, not its
  home, and only the box's **main account** can delete the directory — which
  HQ deliberately does not hold (ADR-004 §4.2).
- **The box's own snapshots** (04:30 UTC, 10 kept — read back from
  `GET /storage_boxes/709870`) are not deleted by a repository prune, so the
  surviving directory can be recovered from them until the ten slots roll
  over: up to about ten days. The 30-day promise is about the repository, and
  it is unchanged.

**A `repo-<4 hex>` directory** is a lost-recovery-file start-fresh (ADR-004
§7.4): the old repository is pruned inside the same sub-account and the pair
**stays**, because the tenant still writes to it — so that branch makes **no
API call** and records `detail.backup.prunes` (one entry per pruned old
repository). A second run says `already pruned`.

Dry run and apply on a bench pair, 2026-11-26T08:34–08:35Z (a pair minted for
the exit — `odl-exit935`, both homes under `tenants/odl-exit935`, one snapshot
`02983ce3…` written by `restic init` + `backup` with a password minted for the
drill):

```
$ /usr/bin/python3 -m odilectl purge odl-exit935 --config /var/tmp/odile-ODL-935/config.yaml \
    --password-file /var/tmp/odile-ODL-935/odl-exit935-restic.pass
purge: dry run (pass --apply to execute)
  1. backup.verify: repository="sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic", via="restic snapshots --json", snapshots=1, ids=["02983ce3ceb6c4b332143c4ccf0562cffa122eab71a29c9da55390e06f3c60fa"]
  2. backup.prune: repository="sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic", via="restic forget --prune <id>", snapshots=1, ids=["02983ce3ceb6c4b332143c4ccf0562cffa122eab71a29c9da55390e06f3c60fa"]
  3. subaccount.delete: box=709870, id=395983, name="odile-odl-exit935-ro"
  4. subaccount.delete: box=709870, id=395979, name="odile-odl-exit935-rw"
  5. state.record: slug="odl-exit935", detail="detail.backup.subaccount_deleted_at", keep_until="2026-11-01T00:00:00Z"
note: the store of record is /root/.odile/hetzner/odile-odl-exit935.env (0600); its read-write key is the only credential used, and it never reaches argv
note: the password file opened the repository; the prune runs restic forget --prune for every snapshot
note: survives: tenants/odl-exit935 (stays, no user data: .ssh and the repository's config and keys/; only the box's main account can delete it, and HQ does not hold it)
note: the box's own snapshots: 04:30 UTC, 10 kept — the deleted directory can survive up to about ten days
note: the 30-day promise and keep_until 2026-11-01T00:00:00Z are unchanged

$ /usr/bin/python3 -m odilectl purge odl-exit935 --config … --password-file … --apply
purge: applied
  repository: sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic
  prune:      1 snapshot(s) via restic forget --prune: 02983ce3
  left:       config keys (data/, index/, locks/, snapshots/ empty)
  subaccount: odile-odl-exit935-ro 395983 deleted
  subaccount: odile-odl-exit935-rw 395979 deleted
  survives:   tenants/odl-exit935 (stays, no user data: .ssh and the repository's config and keys/; the box's main account would delete it)
  box:        the box's own snapshots: 04:30 UTC, 10 kept — the deleted directory can survive up to about ten days
  unchanged:  keep_until 2026-11-01T00:00:00Z and the 30-day promise
  state:      detail.backup.subaccount_deleted_at at 2026-11-26T08:35:20Z in /var/tmp/odile-ODL-935/waitlist-copy.db

$ /usr/bin/python3 -m odilectl purge odl-exit935 --config … --apply      # the second run
purge: already deleted
  deleted_at: 2026-11-26T08:35:20Z
  repository: sftp:u728180-sub1@u728180-sub1.your-storagebox.de:data/restic
  subaccount: odile-odl-exit935-rw 395979 (deleted)
  subaccount: odile-odl-exit935-ro 395983 (deleted)
  pruned:     1 snapshot(s) via restic
  survives:   tenants/odl-exit935 (stays, no user data: .ssh and the repository's config and keys/; the box's main account would delete it)
  box:        the box's own snapshots (04:30 UTC, 10 kept) are not deleted by this; the deleted directory can survive up to about ten days
```

The API readback after the apply: `GET /storage_boxes/709870/subaccounts`
lists only the demo pair (395540/395542), and `GET …/subaccounts/395979` and
`…/395983` both answer **404 `Sub Account with ID … not found`**. The copy of
the state store is byte-identical before and after the second run
(`sha256sum -c` OK). The same command deleted the earlier bench pair
(`odl-bench`, 392550/392556) in the dry-run cycle; both ids now answer 404 as
well. A missing store of record refuses before anything is touched:

```
$ /usr/bin/python3 -m odilectl purge odl-noenv --config … --apply   # no …/odile-odl-noenv.env
odilectl purge: odl-noenv's backup store of record is missing: /root/.odile/hetzner/odile-odl-noenv.env; the pair's ids, keys and host key live there
rc=2
```

The redaction check: the drill password appears **0** times in the run's
output and **0** times in a 0.05 s `ps -eo args` sample taken while restic was
live; the read-write key and the API token are not in either.

Refusals, each with the row's own reason:

| Refusal | Exit | When |
|---|---|---|
| the store of record is missing | 2 | `<slug>'s backup store of record is missing: <path>` — before restic and before the API |
| the store records another slug | 2 | `<path> records 'odl-other', not '<slug>'` |
| the row's URL is not the read-write account | 2 | `… is not the read-write sub-account in the store of record (…)` |
| a supplied password does not open it | 4 | restic's `wrong password or no key found`, nothing deleted |
| the row is not `destroyed` (and not a start-fresh) | 2 | `<slug> is ready, not destroyed; nothing is due` |
| `keep_until` is in the future | 2 | `not due: keep_until … (… days away)` |

The full transcripts are in the card's evidence, `docs/evidence/a3-bx11-purge.md`.

### 4.2 The HQ-root path — `odilectl purge <slug>` (`t_9d6fb595`, byte for byte)


**t_9d6fb595** is the command: `odilectl purge`, a control-plane verb beside
`destroy` (`odilectl/purge.py`). It is deliberately not a `scripts/backup.py`
verb — that program is embedded byte-for-byte in the signed installer, so a
purge verb there would ship to every tenant and force a re-signed installer,
and the fact this command reads (the tenant row) lives on HQ. It runs **where
the box's chroot is visible**: the drill box's chroot is `/srv/odile-backup` on
HQ; the real off-site box is **t_8eb17a54**'s (§6). It reads one destroyed row
from the state store (`tenant_state.detail.backup`), refuses before the row's
own date, and past it:

1. `restic -r <path> snapshots --no-lock` as the control check — the row
   records the guest's URL, and on the box the same bytes are the local path
   below `--root`. With `--password-file` (the user's recovery file) the
   repository opens and the recorded snapshot is looked for by id (reported, not
   required: `restic forget --prune` may have removed it); without the file
   restic answers "wrong password" (exit 12), which proves the directory is a
   restic repository that this password does not open — the key is the user's.
   The purge never reads a file name or a byte.
2. deletes the repository directory (the URL's `/tenants/<slug>` below
   `--root`; the last path component must be the slug, so a row naming another
   tenant's directory refuses).
3. drops the guest's line from the box's `authorized_keys` — the box registers
   each guest's key with the tenant in its comment (`odile-backup odl-…`), and
   the purge drops every line whose comment carries the slug. `backup.py init`
   writes the guest's kernel hostname instead (`odile-bench` on the image), so
   a box that registered that word names it with `--sftp-key-comment` — a
   token that names only this tenant, because the purge drops every line
   carrying the word. A key
   file with no line for the tenant **refuses** before the repository is
   deleted; a missing file, or a repository directory already gone, is the
   converged case and records what it found.
4. records `detail.backup.purged` on the row (`at`, `path`, `repo`, `snapshot`,
   `restic`, `snapshots`, `snapshot_present`, `sftp_keys_dropped`). It is a
   detail, not a new event: `tenant_state_events.kind` is the state vocabulary,
   so a `purged` event would be a schema change. `keep_until`, the repository
   and the snapshot stay where `destroy` wrote them, and so do `dns`, `export`,
   `grant`, `machine` and `reason`.

With no `--apply` the command is the dry run: it prints every action, numbered,
and changes nothing; `--apply` executes, and `--dry-run` wins over it. A second
run prints `already purged` and writes nothing, down to the byte. The date is
the **row's**: a re-run cannot push it forward.

The dry run on a scratch repository and a copy of the state store,
2026-11-16T02:38Z — the row's date 15 days past, four lines in the box's
`authorized_keys`, one of them this tenant's:

```
$ /usr/bin/python3 -m odilectl purge odl-218purge --config /var/tmp/odile-t_9d6fb595/config.yaml --db /var/tmp/odile-t_9d6fb595/waitlist-copy.db --root /var/tmp/odile-t_9d6fb595/box --dry-run
purge: dry run (pass --apply to execute)
  1. backup.verify: repository="sftp:odile-backup@backup.odile.fyi:/tenants/odl-218purge", path="/var/tmp/odile-t_9d6fb595/box/tenants/odl-218purge", snapshot="b806dcd180c0dcf0850fa2c772ee376df860fce81df95ce60a30a30e6743cd07", command="restic -r /var/tmp/odile-t_9d6fb595/box/tenants/odl-218purge snapshots --no-lock", password_file=null
  2. backup.delete: path="/var/tmp/odile-t_9d6fb595/box/tenants/odl-218purge", result="present"
  3. sftp.revoke: file="/var/tmp/odile-t_9d6fb595/box/.ssh/authorized_keys", lines=[4], comments=["odl-218purge"], result="1 line(s)"
  4. state.purge: slug="odl-218purge", detail="detail.backup.purged", keep_until="2026-11-01T00:00:00Z"
note: the repository password is the user's: --password-file opens the repository for the check, and without it restic's wrong-password answer still proves the directory is a restic repository
note: run: odilectl purge odl-218purge --root /var/tmp/odile-t_9d6fb595/box --apply
note: a second run says already purged; the date is the row's
```

The state store and the key file hash the same before and after it
(`64567dba…`, `c221d0a3…`; `dry-run.txt` carries both `sha256sum` runs). The
row, before the run, is the destroy's own record:

```
odl-218purge|destroyed|{"days":30,"keep_until":"2026-11-01T00:00:00Z","repo":"sftp:odile-backup@backup.odile.fyi:/tenants/odl-218purge","snapshot":"b806dcd180c0dcf0850fa2c772ee376df860fce81df95ce60a30a30e6743cd07"}
```

The purge, with the recovery file, then the second run:

```
$ /usr/bin/python3 -m odilectl purge odl-218purge --config /var/tmp/odile-t_9d6fb595/config.yaml --db /var/tmp/odile-t_9d6fb595/waitlist-copy.db --root /var/tmp/odile-t_9d6fb595/box --password-file /var/tmp/odile-t_9d6fb595/recovery.txt --apply
purge: applied
  repository: sftp:odile-backup@backup.odile.fyi:/tenants/odl-218purge
  path:       /var/tmp/odile-t_9d6fb595/box/tenants/odl-218purge
  restic:     opened, 1 snapshot(s); the recorded b806dcd1 is present
  delete:     removed /var/tmp/odile-t_9d6fb595/box/tenants/odl-218purge
  sftp:       dropped 1 line(s) from /var/tmp/odile-t_9d6fb595/box/.ssh/authorized_keys
  state:      detail.backup.purged at 2026-11-16T02:39:19Z in /var/tmp/odile-t_9d6fb595/waitlist-copy.db
$ /usr/bin/python3 -m odilectl purge odl-218purge --config /var/tmp/odile-t_9d6fb595/config.yaml --db /var/tmp/odile-t_9d6fb595/waitlist-copy.db --root /var/tmp/odile-t_9d6fb595/box --apply
purge: already purged
  purged_at:  2026-11-16T02:39:19Z
  repository: sftp:odile-backup@backup.odile.fyi:/tenants/odl-218purge
  path:       /var/tmp/odile-t_9d6fb595/box/tenants/odl-218purge
```

The directory is gone, the tenant's key line is gone and the other three lines
are unchanged; the state store and the key file hash the same before and after
the second run (`b287eaee…`, `9c6b964a…`; `second-run.txt`). The row now
carries the purge beside the retention, and the event count is still 1 (the
destroy's), with `dns`, `export`, `grant`, `machine` and `reason` untouched:

```
{"absent_at": "2026-11-02T00:00:00Z", "backup": {"days": 30, "keep_until": "2026-11-01T00:00:00Z", "purged": {"already_absent": false, "at": "2026-11-16T02:39:19Z", "path": "/var/tmp/odile-t_9d6fb595/box/tenants/odl-218purge", "repo": "sftp:odile-backup@backup.odile.fyi:/tenants/odl-218purge", "restic": "opened", "sftp_keys_dropped": 1, "snapshot": "b806dcd180c0dcf0850fa2c772ee376df860fce81df95ce60a30a30e6743cd07", "snapshot_present": true, "snapshots": 1}, "repo": "sftp:odile-backup@backup.odile.fyi:/tenants/odl-218purge", "snapshot": "b806dcd180c0dcf0850fa2c772ee376df860fce81df95ce60a30a30e6743cd07"}, "dns": {"action": "deleted", "name": "odl-218purge.odile.fyi", "record_id": "fixture-record-1", "zone": "odile.fyi"}, "export": {"skipped": true}, "grant": {"source": "https://odl-218purge.odile.fyi/verify", "status": "unknown"}, "machine": {"ipv4": "203.0.113.99", "server_id": "purge001"}, "reason": "ODL-218 follow-up drill"}
```

Without the recovery file — the production case, where the user keeps the
password — the same run says `restic: present, not opened (its password is the
user's)` and deletes anyway; it is the delete that Odile is allowed to do, not
the read (`docs/runbooks/backup.md` §7). The scratch run for `odl-218nopass`
did exactly that, and no password appeared in the output.

A box that registered the guest's kernel hostname refuses until the word is
named (`key-comment.txt`): `odl-218key`'s line says `odile-backup odile-bench`,
so the first run exits 2 with

```
odilectl purge: no line of /var/tmp/odile-t_9d6fb595/box/.ssh/authorized_keys-image names odl-218key; pass --sftp-key-comment TOKEN (a box that registered the image's hostname) or drop the line by hand
```

Re-measured 2026-11-18 after the wording was scoped (`odl-218key2`, a fresh
row, repository and key file in the same scratch root; the same
`key-comment.txt` carries the section), the refusal now names the scope:

```
odilectl purge: no line of /var/tmp/odile-t_9d6fb595/box/.ssh/authorized_keys-rekey names odl-218key2; pass --sftp-key-comment TOKEN (a token that names only this tenant — every line carrying it is dropped — for a box that registered the image's hostname) or drop the line by hand
```

and the same command with `--sftp-key-comment odile-bench` drops exactly that
line and the drill key stays. The refusal comes before the delete, so the
repository and the key are both unchanged when it fires.

Refusals, each with the row's own reason:

| Refusal | Exit | When |
|---|---|---|
| no tenant row | 2 | `no tenant named 'odl-…' in <db>` |
| the row is not `destroyed` | 2 | `<slug> is ready, not destroyed; nothing is due` |
| the row records no repository | 2 | `records no repository (detail.backup.repo is null)` — the machine never backed up |
| `keep_until` is in the future | 2 | `not due: keep_until 2099-01-01T00:00:00Z (26343.9 days away)` |
| no key line names the tenant | 2 | `no line of <file> names <slug>; pass --sftp-key-comment …` — before the delete |
| the repository path is not a directory | 4 | names it; restic 0.18 retries a non-directory path for hours, so the check refuses first |
| the directory is not a repository | 4 | names it (`it has no config file or no key`; or restic's own `Fatal: …` line) |
| the URL's path does not end in the slug | 2 | `<path> ends in 'odl-2', not 'odl-1'; refusing to delete it` |

A destroyed row whose directory is already gone converges: the check says
`already absent`, the key line is still dropped, and the purge is recorded — a
half-finished run is finished by the next one, and the next one says
`already purged`.

The transcripts are the card's evidence files on **t_9d6fb595**
(`dry-run.txt`, `purge.txt`, `second-run.txt`, `refusals.txt`,
`no-password.txt`, `key-comment.txt`).

The live box is **t_8eb17a54**'s. The drill's chroot is on HQ, so the command
runs on HQ and records on the live row; the real box has no agent (the card
that buys it owns how its chroot and HQ's state store meet), and until then no
tenant's repository is due — the live tenant is not destroyed, and its destroy
plan named `2026-12-13T03:32:59Z`.

---

## 5. Re-provision after a destroy

This is the step that `docs/runbooks/backup.md` §9 calls "ODL-218's path", and
it has two halves.

### The mechanics — the same slug comes back (ADR-007)

After a destroy, `odilectl provision <slug> --key <user.pub> --pin
odile-verify.pin --backup-password-file <the user's old
odile-recovery-<slug>.txt> --apply` reopens the row and orders the machine.
The reopen is the one edge back (`destroyed -> claimed`, `odilectl/state.py`);
it appends one `claimed` event with the reason, keeps every earlier event and
clears the dead machine's columns, so the order records `provisioning` on the
same row. The same write points the web `tenants` row at the new `server_id`
(address, expiry, release, `state='provisioned'`) and moves the state row on
to `first_boot`, so the machine's own first boot has a row to land on
(`odilectl/bootrecord.py` checks `tenants.server_id` and refuses a
`provisioning` spine). **The slug does not change**: the URL, the recovery
file's name and the `tenants/<slug>` repository survive (ADR-007;
`docs/CLAIMS.md` §1). A dry run on a destroyed slug lists `state.reopen` as
its first action and writes nothing. Without the `--backup-password-file` flag
the rebuilt machine mints a new repository password (Drill 2's C); with it the
guest's own recovery file carries the old password and `backup.py init` says
`same` (`docs/runbooks/backup.md` §5.2, §9). The drill's order was **plan 120
/ 7 days** — the drill config's `default_plan_id: 120` and `default_days: 7`,
the panel's Blackbox Core (the Netherlands; $9.49 per 31 days, billed $2.15
for the drill's 7 days), invoice `e0e6fbce` (§3). The live rebuild orders the
live config's default — **plan 113 / 31 days at the drill's date**, when
`/root/.odile/config.yaml` carried `default_plan_id: 113` and
`default_days: 31`. Since 2026-11-24 that file carries `default_plan_id: 138`
and `default_days: 365`, so a rebuild today orders a **Blackbox Nano at the
annual term** rather than a Plus (`docs/runbooks/deploy.md`; the default's
measurement is `docs/evidence/nano-baseline.md`). The live tenant itself is on
plan 113 (`?action=server&id=0badc0de` → `plan_id: 113`, Blackbox Plus, Canada,
$18.99 reseller / $19.99 retail per 31 days, active to 2026-12-10;
`evidence/plans.txt`). Plan 121 is also Blackbox Plus at $18.99
per 31 days, but in the Netherlands — it is not what the live config orders.
The flags differ, the path does not: a same-slug rebuild orders a new machine,
the row reopens and moves `claimed → provisioning → first_boot` with release
`2026.11.1` from the pin, and a DNS-only A record is written for the **same**
slug (`odilectl dns create <slug> --ip <addr> --apply`; the drill's replay
used the module entry point `python3 -m odilectl.dns` because `odilectl dns …`
exited 2 at the time, and **t_1520be38** registered `dns` in `cli.MODULES`).
The command was replayed on 2026-11-13T08:03Z (dry run → applied, record
`83269c0c…` → one A record read back → deleted → `result_count 0`;
`evidence/dns-create.txt`, §7).

**A machine that vanished is not a destroy.** Where the tenant is still live
(`ready`, `suspended`, …) and the provider no longer lists its machine,
`reconcile --apply` is the command that marks it `destroyed`; `provision` on
that live row is refused **before** the order and names the remedy, because a
live row has no edge back to `provisioning` and an order recorded there would be
a paid machine no row names (`docs/runbooks/provision.md` §9, ADR-007 §5). Run
it first — the finding names this same-slug rebuild — then the five steps below.
Both paths meet at the same `claimed -> provisioning -> first_boot`.

The old behaviour was the fork this card closed. On 2026-11-13, a destroyed
slug refused in `_record` **after** the order had been placed and billed:

```
$ python3 -c '… provision._record_machine(ctx, spec, machine, "2026.11.1")'
StateError: odl-218a1b is destroyed; its release cannot be recorded
```

ADR-007 removed that refusal before the first provider call: a destroyed row is
reopened to `claimed` first, so the order always lands on a row that can record
it. The re-provision is now:

1. `odilectl provision <slug> --key <the user's public key> --pin
   odile-verify.pin --backup-password-file <the user's old
   odile-recovery-<slug>.txt> --apply` — the reopen, the order, the new
   `server_id` on both the state row and the web row, the release from the
   pin, and the state row at `first_boot`; the flag above keeps the old
   password, so the rebuilt machine does not mint a second one.
2. `odilectl dns create <slug> --ip <the machine's address> --apply` — the A
   record the destroy deleted, for the **same** slug (the replay used the
   module entry point `python3 -m odilectl.dns` before **t_1520be38**
   registered `dns` in `cli.MODULES`; `evidence/dns-create.txt`, §7).
3. The **signed image** pair in the panel (below). The order is
   **boot image, then disk image**; the disk write is panel-only. The pair
   measured 6 s (the boot attach) and 77 s (the disk write, `18:50:35Z` → `18:51:52Z`,
   §6 of the spike, `docs/product/SPIKE-servury-reinstall.md`). Arm
   the seed row before the disk write — the first boot exchanges the token the
   image bakes, and an unarmed row fails closed (below; the spike's §5 #3).
4. First boot → first visit → the sign-in and the recovery file
   (`docs/runbooks/install.md`, `docs/runbooks/backup.md` §5.2). The first
   boot's measured line lands because step 1 left `tenants.server_id` naming
   this machine and the state row at `first_boot`; a record from the dead
   machine's instance is refused.
5. The home comes back from restic with the user's own recovery file
   (`docs/runbooks/backup.md` §5.3–§5.4, measured 38m03s for the timed rebuild).

### The signed image is the panel's, not the API's

`?action=create` and `?action=reinstall` take `os` (a stock image) plus the
`snp`/`fde`/`measured` flags; `?action=boot-image` sets the kernel (the UKI).
**The sealed disk image has no API verb** — it is the panel's "disk image"
field. A machine ordered without it boots stock Debian, not a tenant, which is
why the *live* rebuild is a human step (the account's panel login is not on this
box; the dev account's credential is a different account). The two values are
in the round-8 record (`docs/runbooks/install.md` §12.4), re-verified from HQ
before saving:

- boot `https://odile.fyi/releases/m1/odile-tenant.efi` — `5cb31808…6a5e`
- disk `https://odile.fyi/releases/m1/odile-tenant-sealed.raw` — `c1278242…b0072`
- command line: empty (the per-tenant line is inside the UKI)

**A reinstall clears both image fields, so the pair goes back boot first.**
Measured on the disposable box, 2026-11-17 — the spike's §4 and §6
(`docs/product/SPIKE-servury-reinstall.md`): the panel's record went boot image
`f7bc0ceb…` (the UKI **t_c1e0f336** built) → **none** and disk image
`24d8a1ff…` → **none**, the OS from
Debian 12 to Debian 13, while the address and the term did not move and the
balance showed no new row — a reinstall is free. (The rDNS is the vendor's
"kept", not a second reading.) The boot image lived on the disk the reinstall
replaces, which is why the pair is re-applied afterwards, in this order:
**boot image, then disk image**. At the end of its write the disk step starts
the machine itself, and the boot that follows must already be the tenant's UKI;
the other order is not exercised in the spike, but the boot image applies at
the next start, so a machine started between the two writes comes up on the
vendor's release and needs one more start before the pair is what runs. On §5's
path the first write is the `provision` order, not a reinstall — a new machine
with no image fields at all — and the pair keeps that order. A reinstall also
writes the machine's whole `authorized_keys` from the key it is sent (§3's
panel field is pre-filled with the keys the machine was *built* with), so a
machine recycled to another tenant must have that pre-fill replaced with the
new tenant's key; an operator key posted there is the standing credential the
access model forbids.

The three writes and the first boot, as §6 measures them:

| # | Write | Route today | Measured |
|---|---|---|---|
| 1 | reinstall: `os=debian13`, `snp`/`fde`/`measured`, the tenant's public key | API `POST ?action=reinstall` when the key owns the machine, or the panel's Danger Zone | **1 m 32 s** from POST to `Running`; the disk is a template and both image fields are cleared; address and term kept; free |
| 2 | boot image: URL + SHA-256, empty command line | API `POST ?action=boot-image` or the panel's Manage tab | **6 s**, including the node's 114.9 MiB download and hash check (`spike/servury-reinstall/evidence/admin-bootimage.txt:6`) |
| 3 | disk image: URL + SHA-256 + format | **panel only** — `?action=disk-image` answers 404 | **77 s** for the 8 GiB image (`18:50:35Z` submitted → `Written 18:51:52Z`); the machine starts itself |
| 4 | first boot: bootstrap key, re-seal to the chip, seed exchange | on the machine, no operator | kernel start **40 s** after `Written`, its release-setup line **30 s** into the boot (`spike/servury-reinstall/evidence/console-restored.txt:343,666`); boot record `ok`; the seal re-provisioned to the new chip; the seed exchange failed closed (below) |

The first boot is not a write and has no panel step: it opens the disk with the
image's first-boot bootstrap key, re-seals the blob to the machine's own chip
and shreds the bootstrap key. The disk image is portable, the seal is not — the
right behaviour, and the reason a reinstall alone never leaves a tenant.

**Arm the seed row before the rebuild.** The image's command line is baked, so
a rebuilt machine re-presents the *same* `odile.token` (`db669443…` on the
box), and the ledger must carry a row for exactly that token before the first
boot — bound to the tenant the machine will serve (that `tenant_id` decides
whose seed the token returns) and to the `bb.iid` baked in the same line, with
an expiry after the boot. A token the ledger had no row for answered
`404 unknown_token` (`{"error":"no such token","reason":"unknown_token"}`; the
spike's §5 #3, `docs/runbooks/seed.md`'s `404 unknown_token` row) — **not**
§6a's `410 used`, so §6a's un-end recipe is a no-op here: its `UPDATE … WHERE
token_hash = …` matches nothing on an absent row. A row a previous boot spent
is §6a's case. No seed CLI verb adopts a plaintext (`token issue` and
`token rotate` mint a random one and print it once; `odilectl provision` binds
one through `seed.bind`), so arming
is either that hash inserted by hand in the same shape or, the path the spike
calls the one that scales, a line built around a fresh per-machine token
(`token issue --tenant <slug> --instance <id>`, on a UKI whose `.cmdline` is
empty — recommended in the spike's §6 and recorded as unmeasured in its §10).

The fence and the one-command sequence for the three writes landed as
**t_6d956aee** (merged `1665cca`) in `tools/servury_panel.py --recycle`
(`docs/runbooks/recycle.md`). The disk write is still a human panel step for
the live tenant: that account's panel login is not on this box (§5, *The signed
image is the panel's, not the API's*).

### The live tenant

`odl-fake01` is Savio's own agent and was **left running**: destroying it costs
nothing but rebuilding it needs a **plan 138 / 365-day** order (**$47.44**, the
partner price after the term and partner discounts) and
the panel's two images, which is a human action. **t_8d50106b** asks for that
one decision (A: destroy and rebuild now, recommended for the M2 exit criterion;
B: keep it, the scratch drills stand as the evidence). The plan for the live
tenant is §2 — complete, with the record id, the server id and the backup date.

---

## 6. What this card leaves open

| Gap | Why it is not here | Card |
|---|---|---|
| ~~a destroyed slug cannot come back~~ **resolved** | ADR-007: `destroyed -> claimed`, the named `reopen()`, and `provision` reopens before the order, points the web row and the state row at the new machine and reaches `first_boot` (so its boot record lands); the slug, the URL and the `tenants/<slug>` repository keep their names | **t_ce52bb8d** |
| the live tenant's destroy + rebuild | $47.44 (plan 138 / 365 days) and the panel's disk image — a real-world action | **t_8d50106b** |
| the live tenant's Webapp refuses `/login` (`400 Invalid Host header`) | measured 2026-11-13T05:33Z after a boot-time release install; that boot's release setup ended `rc=1` at `odile-access-report` and the 10:29Z retry ended `rc=4` at the recovery confirmation. It was installer/release work, not the offboard path: the agent is restarted whenever a file the gateway reads at start changed (so a rewritten `webapp.public_url` goes live), a failed access report no longer aborts that restart, and the boot half passes `--i-have-copied-it`. **Recovered on the live tenant 2026-11-15T20:12Z**: `=== rc=0`, `/login?next=%2F` 200, `POST /auth/password-login` 200 with four `__Host-` cookies, `/verify` 200. The machine's own name `odl-fake01.odile.fyi` still answers 400 by design (`demo-public.md` §Host check) | **t_d95b8c13** fixed and recovered 2026-11-15 |
| the purge on the real box | the command is `odilectl purge` (**t_9d6fb595**, §4), drilled on a scratch repository and a copy of the state store; the off-site box, and how its chroot meets HQ's state store, are the purchase's | **t_8eb17a54** |

---

## 7. Evidence log

Every command and its raw output quoted in this page is in `/var/tmp/odile-218-run`
(the card's scratch root, left in place for the reviewer; the disk-care sweeper
removes it an hour after the card is done), and the files marked **attached**
are in the copy attached to the card. The ODL-218 follow-up's placement
evidence is in `/var/tmp/odile-218f-evidence/` (this card's scratch root,
attached to the card): `self-test.txt` (PASS), `one-byte.txt` (a copy with one
byte flipped inside the embedded program: `FAIL the embedded odile-export
matches its pinned hash`, `FAIL this installer's shipped signature verifies`,
exit 1), `signature.txt` (`--verify-signature` OK, sha256 `2cdf0801…`),
`sim.txt` (the scratch install's placement and the SHA-256 pair),
`release-check.txt` (`tools/recut_release.py --check`: five `ok` hash lines and
four `ok` signatures) and `manifest-pair.txt` (the installer, its release copy,
the manifest, `release.json` and the skills tarball — sha256 and size; the
manifest's `installer_sha256` is the installer's own hash). `odilectl` in the blocks is
`/usr/bin/python3 -m odilectl` run from the checkout; the name is not installed
as a console script. The DNS replay in `evidence/dns-create.txt` used the
module's own entry point (`python3 -m odilectl.dns`) because `odilectl dns …`
exited 2 at the time; **t_1520be38** registered `dns` in `cli.MODULES`, so
every DNS command in this page is `odilectl dns <verb> …` and the module form
remains only for scripts that already call it.

| File | What it is |
|---|---|
| `evidence/export.txt` | **attached**: the whole export drill: the tenant as the user sees it, the placed program's two sha256s, the three printed lines, the download round trip, the removal |
| `evidence/verify.txt` | **attached**: the `export --verify` run at the head of this branch, byte for byte — 1,970 bytes, 702 entries, the first-40 listing and the verdict; §1 quotes it whole |
| `evidence/plans.txt` | **attached**: `?action=plans` rows 113 / 120 / 121 and `?action=server&id=0badc0de` (`plan_id: 113`), read 2026-11-13T06:39Z — the plan facts behind §5 |
| `evidence/provider-after.txt` | **attached**: the card's required 404 — `?action=server&id=0b3234a2` and `?action=server&id=fbf0f1f0` both HTTP 404 `{"success":false,"error":"Server not found."}` — and `?action=servers` `total 2`, read 2026-11-13T05:45Z |
| `evidence/dns-after.txt` | **attached**: the zone's `GET dns_records?type=A&name=…` — `result_count 0` for `odl-218a1b.odile.fyi` and `odl-218c1d.odile.fyi`, `result_count 1` for the live `odl-fake01.odile.fyi` (control), same run |
| `evidence/dns-create.txt` | **attached**: §5's way-back DNS command replayed 2026-11-13T08:03Z with drill 1's own slug and address — dry run (exit 0), apply (exit 0, record `83269c0c…`), an independent zone read (`result_count 1`, DNS-only, TTL 120), delete (exit 0), a second read (`result_count 0`); plus the `odilectl dns …` form it replaces, exiting 2 `invalid choice: 'dns'` |
| `evidence/program-current.txt` | **attached**: the checkout's `EXPORT_PROGRAM` printed by `--script` (`c635c432…`) and run on a fixture home; the bundle's members and `export --verify` verdict |
| `odile-export-odl-fake01-20261113T032107Z.tar.gz` | the bundle, 1,429,556 bytes, sha256 `478890ec…` (the user's copy) |
| `dry-run-live.txt` (03:32:59Z), `dry-run-live.json` (03:22:13Z) | §2, the plan against the live tenant — two separate runs; the text one is quoted, the JSON one carries the same seven steps with its own `keep_until` clock |
| `provision-a.json`, `destroy-a.json` | drill 1: the order, the destroy's JSON |
| `provision-b.json`, `destroy-b.json` | drill 2 (the re-provision): the order, the destroy's JSON |
| `same-slug-refusal.txt` | the `StateError` a destroyed slug raised in `_record` after the order — the **pre-ADR-007** behaviour §5 removes |
| `servers-before.txt`, `servers-after.txt` | `?action=servers`, before and after (the same account read as `evidence/provider-after.txt`) |
| `scratch.yaml` | the drill's config (0600): the host account, its own state DB in `/var/tmp`, plan 120 / 7 days |

The suite: `/usr/bin/python3 -m unittest tests.test_offboard -v` (24 tests,
hermetic: recorded provider and zone transports, a string for `/verify`, a fixed
clock; two tests run `EXPORT_PROGRAM` against a fixture home — a stub tar that
exits 2, and a `kanban.db` whose row is only in the WAL at copy time).
`make check` on the merged head (main at `3e9fcb0`): **1559 tests + 20
meter tests, OK**, self-test PASS, claims 21/14, brand 17/21.

---

## 8. Where the pieces live

| Piece | Path | Mode |
|---|---|---|
| the guest program | `/usr/local/bin/odile-export` | 0755, root — placed by the signed installer (`installer/embed_export.py`; a re-run reads `same`) |
| the bundle (guest) | `/home/tenant/odile-export-<name>-<UTC>.tar.gz` | 0644, the tenant |
| the offboard path | `odilectl/offboard.py` | — |
| the command | `odilectl destroy <slug>` | — |
| the exported script | `odilectl export <slug> --script` | — |
| the inspection | `odilectl export <slug> --verify PATH` | — |
| the 30-day purge | `odilectl purge <slug>` | — |
| the row's record | `tenant_state.detail` + one `destroyed` event | — |
