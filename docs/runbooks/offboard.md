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

The live tenant `odl-7971a8` measured on 2026-11-13: home 4.1 GB, root disk
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
only: `odile-export odl-7971a8`.

The first drill run failed because the staging directory landed in `TMPDIR`
(`${HOME}/.hermes/cache/scratch` on a tenant) and `tar .hermes` then copied a
directory into itself; the program stages in `/var/tmp` and the second run is
the one quoted here.

### The run, as the user sees it

```
$ odile-export odl-7971a8
wrote /home/tenant/odile-export-odl-7971a8-20261113T032107Z.tar.gz
bytes 1429556
sha256 478890ecf75a5227275a28204a48197ab4d984c27c24a7524fb604fa49700988
members AGENTS.md documents workspace .hermes
```

### The download

The Files pane reads the same file the browser does; the request behind a
download is `GET /api/fs/download?path=…`, and the drill measured the round
trip rather than trusting the page:

```
GET /api/fs/download?path=%2Fhome%2Ftenant%2Fodile-export-odl-7971a8-20261113T032107Z.tar.gz
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
$ /usr/bin/python3 -m odilectl export odl-7971a8 --verify /var/tmp/odile-218-run/odile-export-odl-7971a8-20261113T032107Z.tar.gz
export: odl-7971a8
  bundle:    /var/tmp/odile-218-run/odile-export-odl-7971a8-20261113T032107Z.tar.gz
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
$ odilectl destroy odl-7971a8 --export odile-export-odl-7971a8-20261113T032107Z.tar.gz --dry-run
destroy: dry run (pass --apply to execute)
  1. export.verify: file="odile-export-odl-7971a8-20261113T032107Z.tar.gz", sha256="478890ecf75a…", bytes=1429556, entries=702, members="ok"
  2. support.check: url="https://odl-7971a8.odile.fyi/verify", result="none", line="operator key PASS none (authorized_keys: 1 line(s), the measured throwaway)"
  3. dns.delete: zone="odile.fyi", name="odl-7971a8.odile.fyi", type="A", record_id="fixture-record-id-1", result="present"
  4. provider.terminate: hostname="odl-7971a8", server_id="31a63bbb", ipv4="203.0.113.87"
  5. provider.absent: server_id="31a63bbb", until="not listed by ?action=servers", timeout_seconds=120.0
  6. state.destroy: slug="odl-7971a8", from="ready", detail="bundle, grant, DNS, machine and retention"
  7. backup.retain: repo="sftp://***@203.0.113.139:2222/tenants/odl-7971a8", snapshot="828e8fab…", status="reported", keep_until="2026-12-13T03:32:59Z", days=30, then="delete the repository on the backup box"
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

The live tenant `odl-7971a8` is Savio's own agent and its destroy + rebuild is
**t_8d50106b** (the $18.99 order and the panel's disk image are his: see §5). The
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
| account after | `?action=servers` → `total 2` (`odl-7971a8`, `oqwaclzm` — the live tenant and HQ) | same |

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
a dated operator step, not a memory:

- `keep_until` is on the destroyed row's `detail.backup.keep_until` and in
  `destroy --json` (live tenant: `2026-12-13T03:32:59Z`, snapshot `828e8fab…`,
  in the 03:32:59Z text run; the 03:22:13Z `--json` run printed its own clock,
  `2026-12-13T03:22:13Z` — the date is 30 days from the run that made it).
- On the backup box, after that date: `restic -r <repo> snapshots` as the
  control check, then remove the repository directory
  (`/srv/odile-backup/tenants/<slug>` on the drill box), then drop the SFTP
  account's access. The bytes are ciphertext; nothing else has to be rotated.
- Before the date, nothing touches it. `destroy` never deletes the repository —
  it only prints the date and the path.

V0.1 leaves the deletion to the operator: the box is off-site and its owner is
the only one with a shell there. Automating it is **t_9d6fb595** (a command that
reads `keep_until` and purges past it); the date on the row is what makes it
checkable in the meantime.

---

## 5. Re-provision after a destroy

This is the step that `docs/runbooks/backup.md` §9 calls "ODL-218's path", and
it has two halves.

### The mechanics, measured

After a destroy, `odilectl provision <slug> --key <user.pub> --pin
odile-verify.pin --backup-password-file <the user's old
odile-recovery-<slug>.txt> --apply` orders the machine and writes the row.
Without the flag the rebuilt machine mints a new repository password (Drill 2's
C); with it the guest's own recovery file carries the old password and
`backup.py init` says `same` (`docs/runbooks/backup.md` §5.2, §9). The drill's
order was **plan 120 / 7 days** — the drill config's `default_plan_id: 120` and
`default_days: 7`, the panel's Blackbox Core (the Netherlands; $9.49 per 31 days,
billed $2.15 for the drill's 7 days), invoice `e0e6fbce` (§3). The live rebuild
orders the live config's default, **plan 113 / 31 days**: `/root/.odile/config.yaml`
carries `default_plan_id: 113` and `default_days: 31`, and the live tenant itself
is on plan 113 (`?action=server&id=31a63bbb` → `plan_id: 113`, Blackbox Plus,
Canada, $18.99 reseller / $19.99 retail per 31 days, active to 2026-12-10;
`evidence/plans.txt`). Plan 121 is also Blackbox Plus at $18.99 per 31 days,
but in the Netherlands — it is not what the live config orders. The flags
differ, the path does not: drill 2 ordered `fbf0f1f0` after drill 1's destroy,
and the row went `invited → claimed → provisioning` with release `2026.11.1` from the
pin, the machine came up active with its address, and a DNS-only A record was
written for the slug (drill 1's `882f002b…` in §3, TTL 120; the destroy deleted
it). The command is `odilectl dns create <slug> --ip <addr> --apply`. When
the drill ran, `odilectl dns …` did not exist yet (it exited 2, `invalid
choice: 'dns'`), so the replay used the module's own entry point
(`python3 -m odilectl.dns`); **t_1520be38** registered `dns` in
`cli.MODULES`. The command was replayed on 2026-11-13T08:03Z with drill 1's
own slug and address: dry run → applied (record `83269c0c…`) → one A record
read back → deleted → `result_count 0`; `evidence/dns-create.txt` (§7). The
route works; what it cannot do is keep the **slug**:

```
$ python3 -c '… provision._record_machine(ctx, spec, machine, "2026.11.1")'
StateError: odl-218a1b is destroyed; its release cannot be recorded
```

The refusal is not free on the normal path: `provision --apply` orders the
machine (`provider.create`) and binds the seed token *before* `_record` raises
(`odilectl/provision.py`), so `provision <destroyed-slug> --apply` pays for an
orphan no row names. Use a new slug until **t_ce52bb8d** decides the edge back.

`destroyed` has no outgoing edge (`odilectl/state.py`, `TRANSITIONS`), so a
destroyed slug cannot be provisioned again. **A rebuild today takes a new slug**
and therefore a new URL — which is the one thing a rebuilt machine should not
have to change. **t_ce52bb8d** is the card that decides the way back (an edge
out of `destroyed`, or a new slug as the rule, with the URL consequence
written down). Until it lands, the re-provision is:

1. `odilectl provision <new-slug> --key <the user's public key> --pin
   odile-verify.pin --backup-password-file <the user's old
   odile-recovery-<old-slug>.txt> --apply` — the order, the row, the release
   from the pin; the flag above keeps the old password, so the rebuilt machine
   does not mint a second one.
2. `odilectl dns create <new-slug> --ip <the machine's address> --apply` —
   the A record the claim path would have written (the replay that used the
   module entry point is `evidence/dns-create.txt`, §7).
3. The **signed image** pair in the panel (below).
4. First boot → first visit → the sign-in and the recovery file
   (`docs/runbooks/install.md`, `docs/runbooks/backup.md` §5.2).
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

### The live tenant

`odl-7971a8` is Savio's own agent and was **left running**: destroying it costs
nothing but rebuilding it needs a **plan 113 / 31-day** order (**$18.99**) and
the panel's two images, which is a human action. **t_8d50106b** asks for that
one decision (A: destroy and rebuild now, recommended for the M2 exit criterion;
B: keep it, the scratch drills stand as the evidence). The plan for the live
tenant is §2 — complete, with the record id, the server id and the backup date.

---

## 6. What this card leaves open

| Gap | Why it is not here | Card |
|---|---|---|
| a destroyed slug cannot come back | `TRANSITIONS["destroyed"]` is empty; the decision and its URL consequence belong to the state machine | **t_ce52bb8d** |
| the live tenant's destroy + rebuild | $18.99 and the panel's disk image — a real-world action | **t_8d50106b** |
| the live tenant's Webapp refuses `/login` (`400 Invalid Host header`) | measured 2026-11-13T05:33Z after a boot-time release install; the same boot's release setup ended `rc=1` at `odile-access-report` — installer/release work, not the offboard path | **t_d95b8c13** |
| the 30-day purge is an operator step | the box is off-site and has no agent; the date on the row makes it checkable | **t_9d6fb595** |

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
| `evidence/plans.txt` | **attached**: `?action=plans` rows 113 / 120 / 121 and `?action=server&id=31a63bbb` (`plan_id: 113`), read 2026-11-13T06:39Z — the plan facts behind §5 |
| `evidence/provider-after.txt` | **attached**: the card's required 404 — `?action=server&id=0b3234a2` and `?action=server&id=fbf0f1f0` both HTTP 404 `{"success":false,"error":"Server not found."}` — and `?action=servers` `total 2`, read 2026-11-13T05:45Z |
| `evidence/dns-after.txt` | **attached**: the zone's `GET dns_records?type=A&name=…` — `result_count 0` for `odl-218a1b.odile.fyi` and `odl-218c1d.odile.fyi`, `result_count 1` for the live `odl-7971a8.odile.fyi` (control), same run |
| `evidence/dns-create.txt` | **attached**: §5's way-back DNS command replayed 2026-11-13T08:03Z with drill 1's own slug and address — dry run (exit 0), apply (exit 0, record `83269c0c…`), an independent zone read (`result_count 1`, DNS-only, TTL 120), delete (exit 0), a second read (`result_count 0`); plus the `odilectl dns …` form it replaces, exiting 2 `invalid choice: 'dns'` |
| `evidence/program-current.txt` | **attached**: the checkout's `EXPORT_PROGRAM` printed by `--script` (`c635c432…`) and run on a fixture home; the bundle's members and `export --verify` verdict |
| `odile-export-odl-7971a8-20261113T032107Z.tar.gz` | the bundle, 1,429,556 bytes, sha256 `478890ec…` (the user's copy) |
| `dry-run-live.txt` (03:32:59Z), `dry-run-live.json` (03:22:13Z) | §2, the plan against the live tenant — two separate runs; the text one is quoted, the JSON one carries the same seven steps with its own `keep_until` clock |
| `provision-a.json`, `destroy-a.json` | drill 1: the order, the destroy's JSON |
| `provision-b.json`, `destroy-b.json` | drill 2 (the re-provision): the order, the destroy's JSON |
| `same-slug-refusal.txt` | the `StateError` on a destroyed slug |
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
| the row's record | `tenant_state.detail` + one `destroyed` event | — |
