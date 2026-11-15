# Incidents — the ten classes, the first five commands, and two drills

**Status:** v1.1 — corrected from two drills run for real on 15 November 2026
(§6). §6.3 is every line the drills changed, with the measurement that changed
it. **Card:** ODL-244, M4.
**Read with:** `alerting.md` (the heartbeat and the five alerts), `support.md`
(the grant and the diag bundle), `backup.md` (restic and the restore),
`seed.md` (the token and `/seed`), `provision.md` (the provider API),
`install.md` (the installer and the manifest), `image.md` (the seal and the
boot).

The goal in one line: **at 2 a.m. the first five commands are already written,
and none of them is an operator SSH.** A tenant has no standing operator key
(`AGENTS.md`), so every class below is worked from HQ, from the user's browser,
or from the agent the user is already talking to. Where a shell is genuinely
needed, it is the grant the user starts (§2.4) — never a key we keep.

---

## 0. The 2 a.m. card

| # | Class | The first command | Who acts |
|---|---|---|---|
| 4.1 | Machine down (unexpected) | `odilectl status <slug> --json` | HQ |
| 4.2 | Machine waiting for owner | `odilectl status <slug> --json`, then the first-visit page | HQ + user |
| 4.3 | User locked out | `curl https://<slug>.odile.fyi/verify` | user |
| 4.4 | Lost recovery kit | the recovery page, then the backup repo | user + HQ |
| 4.5 | Seed failure | `seed.py token list --tenant <slug>` | HQ |
| 4.6 | Installer failure | the provider's console log | HQ |
| 4.7 | Disk full | the agent: `df -h /` | user (agent) |
| 4.8 | Spend spike | the NEAR console, then `odilectl status` | HQ |
| 4.9 | Provider outage | `odilectl reconcile --json` | HQ |
| 4.10 | Boot loop | the provider's console log | HQ |

**The three facts that decide most incidents.** Ask them in this order and write
the answers in the incident note; every class below is a consequence of one of
them.

1. **Is the machine up?** `odilectl status <slug> --json` for the provider's
   view, `https://<slug>.odile.fyi/verify` for the machine's own answer.
2. **Does the user still hold the two things?** The recovery code (resets the
   sign-in) and the recovery file (opens the backup). They are different files;
   a class that ignores the difference gives the wrong answer (§4.3 vs §4.4).
3. **Is it one tenant or all of them?** One is an incident; all of them is a
   provider or a release (§4.9), and the first command is different.

---

## 1. How a class is written

Every class carries the same six fields. If a field is empty, write `none` and
say why: an empty escalation is a decision, not an omission.

| Field | The question it answers |
|---|---|
| **Detect** | What fires, and what number proves it. The alert text is quoted as it arrives. |
| **Blast radius** | What is broken, what is not, and what the user loses if we do nothing. |
| **First five commands** | Copy-pasteable, in order. HQ, browser or agent; never an operator SSH. |
| **What the user is told** | A template from §3.1, under 120 words, naming only `docs/CLAIMS.md` rows. |
| **Escalate when** | The condition and the owner, not a mood. |
| **Exit** | What "over" looks like: the check that turns green. |

`<slug>` is the tenant name (`odl-7971a8`); `<id>` is the provider's server id
from `odilectl status` (`31a63bbb`). `$KEY` is
`$(cat /root/.odile/servury_reseller_key)` and never appears in output.

---

## 2. The doors (and the one that is a shell)

### 2.1 HQ: `odilectl` and the provider API

Both are read-mostly, both run on this box, and neither opens a session on the
tenant.

```sh
/usr/bin/python3 -m odilectl status <slug> --json      # provider view + backup facts
/usr/bin/python3 -m odilectl health --json             # classification + planned alerts
/usr/bin/python3 -m odilectl reconcile --json          # tenant rows vs provider
/usr/bin/python3 -m odilectl restore <slug> --verify --password-file <file>
/usr/bin/python3 -m odilectl support <slug>            # the support.status plan; §2.4
```

The provider calls, from `provision.md` §2–§4:

```sh
curl -sS -H "X-Reseller-Key: $KEY" 'https://servury.com/api/reseller.php?action=servers'
curl -sS -H "X-Reseller-Key: $KEY" 'https://servury.com/api/reseller.php?action=server&id=<id>'
# the console log, for one restart: on, restart, read, off
curl -sS -X POST -H "X-Reseller-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"log": true}' 'https://servury.com/api/reseller.php?action=serial&id=<id>'
curl -sS -X POST -H "X-Reseller-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"action": "restart"}' 'https://servury.com/api/reseller.php?action=power&id=<id>'
curl -sS -H "X-Reseller-Key: $KEY" \
  'https://servury.com/api/reseller.php?action=serial&id=<id>&bytes=65536'
```

The restart call may answer `504` after 30 s and still power-cycle the machine
about two minutes later; the initramfs ignores ACPI and the node cuts power
(`provision.md` §4). Do not restart twice.

Two `odilectl` habits that bite:

* `status --json` on a slug that does not exist prints `"ok": true` with
  `"machines": []`; read `count`, not the exit code.
* `health` without `--apply` is a dry run: it prints `planned_alerts` and
  writes nothing. `--apply` is what sends.

### 2.2 The tenant's own pages

| URL | What it proves |
|---|---|
| `https://<slug>.odile.fyi/verify` | The machine's own access report: measurement, boot key, seal, sign-in hash, operator key, grant. `200` with `PASS — five checks` means the machine is up and serving. |
| `https://<slug>.odile.fyi/first-visit` | `200` the sign-in form; `410` the token is spent; `404` the token is unknown. |
| `https://<slug>.odile.fyi/first-visit/reset` | The sign-in reset form; it wants the recovery code. |
| `https://<slug>.odile.fyi/first-visit/recovery?c=<code>` | The recovery file, with the code as the key. |
| `https://odile.fyi/manifest.json` | The signed manifest: release, versions, hashes. (`/manifest` answers 404.) |

`/health` on a tenant is the Webapp route; it answers `302` to `/login`, not a
`200`. The poll records it as a symptom and classifies on the heartbeat alone
(`alerting.md` §2).

### 2.3 The agent chat

The agent runs as `odile` with `NOPASSWD sudo` (`CLAIMS.md` §1), so the user can
ask it to run anything below. This is the user's channel, not ours: we ask the
user, in the comms template, to ask the agent. Nothing here needs a grant.

```sh
df -h /; sudo du -sh /var/lib/odile /var/log /home/odile
sudo odile-diag                     # the bundle to email to help@odile.fyi
sudo cat /etc/odile/recovery.txt    # the file the user lost (§4.4)
sudo odile-support status
systemctl list-timers --all; systemctl --failed
```

### 2.4 The grant — the only shell

A grant installs Odile's support public key behind a forced command for a fixed
number of hours, logs every use, and expires on its own (`support.md` §3). **The
user starts it; HQ cannot.** `odilectl support --apply` is not wired in v0.1
(`NotWired`, ODL-217 owns the grant code), so the only form is the user asking
the agent:

```sh
sudo odile-support grant --hours 24 --reason "disk full after an update"
sudo odile-support revoke
```

The grant prints the expiry and the revoke command. A reboot does **not** close
it: the image rewrites `authorized_keys` at boot and the grant's timer puts the
line back within 30 seconds. The closures are the expiry and `revoke`; an open
session survives both until it exits (`support.md` §1, §6).

**The post-revoke checks are five commands, and they are the device-agnostic
form ODL-217 measured** — `crontab` is not installed on the tenant and the LUKS
device is `sda3`, not the guessed `vda2`:

```sh
sudo cat /root/.ssh/authorized_keys
DEV=$(sudo cryptsetup status root_crypt | sed -n 's/^ *device: *//p')
sudo cryptsetup luksDump "$DEV"
if command -v crontab >/dev/null; then sudo crontab -l; else echo 'crontab not installed'; fi
sudo systemctl list-timers --all
```

The user runs these, or asks the agent to. They are the incident-runbook form of
`support.md` §5 and `CLAIMS.md` A10. `alerting.md` §7 still shows the older
first command `ssh -i ~/.ssh/odile …`; that key does not exist in v0.1, so this
page uses the agent or the grant instead.

### 2.5 What no door can do

* **No operator SSH.** There is no key at HQ, and the boot key is a public key
  with no private half.
* **No HQ reset of the sign-in and no disk unlock.** The recovery code resets
  the sign-in on the machine; a disk that will not open is a rebuild.
* **No reading of the backup without the user's recovery file.** Measured in
  §6.1: restic answers `wrong password or no key found`, and HQ's own key is not
  accepted by the backup box.
* **No repair of a broken seal in place.** Change the image or the firmware and
  the disk stays locked; the exit is rebuild + restore (`image.md` §8).

---

## 3. Comms

Three rules. (1) Under 120 words; count them. (2) Only sentences that are
already in `docs/CLAIMS.md`; a new public sentence is Savio's. (3) Say what is
**known**, what is **next**, and the one thing the user must do — in that order,
and never promise a time we have not measured ("target under an hour" is the
approved reliability row; "by 3 a.m." is not).

A template is a draft the operator may edit; it is not a channel. Send from
`help@odile.fyi`, to the tenant's address, and copy the incident note.

### 3.1 The templates

Each is a draft the operator edits. The word count is measured on the text as
written (placeholders count as one word each); every one is under the 120-word
limit.

**C-0.** We are looking at it. What we know: <one fact>. What we are checking next:
<one check>. Nothing you do is needed for now, and we will write again within
<time>. If the machine is down, your disk is encrypted and untouched.

**C-1.** Your Odile machine (<slug>) has stopped answering. Status: <powered on and
restarting / powered off, with the provider>. Your disk is encrypted and
untouched, and we hold no key to it. Next: we restart it and read its boot log;
if it does not come back by <time>, we rebuild it and restore the last backup
with your recovery file, so keep odile-recovery-<slug>.txt within reach. Nothing
is needed from you yet; we will write again when it is back or when the rebuild
starts.

**C-2.** Your machine is built and waiting for one thing: the sign-in you set on it.
The link in your ready email was good for 24 hours and has expired. Here is a
new one: <link>. Open it, choose a password of at least 16 characters, and your
workspace opens. The password is set on your machine and hashed there; we never
see it. If the link says it was already used, the sign-in is already set — open
https://<slug>.odile.fyi and tell us if that fails.

**C-3.** You cannot sign in; the machine is fine and your files are untouched. The
only reset is on the machine, with the recovery code you saved at your first
visit: open https://<slug>.odile.fyi/first-visit/reset and enter it. There is no
reset at HQ and we never see your password. If you cannot find the code, tell
us: the way back is a rebuild, and your recovery file still opens the backup.
Nothing is lost except the time it takes to rebuild.

**C-4.** Your backup is encrypted with a password that only your recovery file holds;
Odile keeps no copy and cannot read it. Two cases. If the machine is still
running, the agent can show the file again — ask it: "show me my recovery file".
If the machine is gone and the file is lost, the last backup cannot be opened;
we rebuild the machine empty and you start from today. Your URL and slug stay
yours. Tell us which case you are in and we will do the next step.

**C-5.** The machine fetched its one-time configuration token too late: the token
expires after 24 hours and this one is spent. Nothing is lost; the machine
simply has no configuration yet. We have issued a new token: run the same
install command again and it will go through. If the machine fetched its
configuration once before failing, it does not need the token at all — tell us
and we will use the copy it already has. The new token works once and expires in
24 hours.

**C-6.** The machine's install stopped before it reached your workspace. We are reading
its boot log and will restart it once; the installer runs again by itself on the
next boot. If it stops at the same line, we rebuild the machine, which takes
under an hour and needs no key from you. Nothing on the disk is at risk: the
disk is encrypted and the install writes no user data before it finishes. We
will tell you which of the two happened.

**C-7.** The machine's disk is <n>% full, past the 80% warning line. The agent keeps
running, but new writes can fail, including the morning brief and the nightly
backup. The likely cause is a job writing files faster than they are cleaned up.
Ask the agent to run "df -h /" and "sudo du -sh /var/lib/odile /var/log
/home/odile", and send us the output, or let it run odile-diag. Nothing is
deleted without your word: we will name the files and the space they free
before anything is removed.

**C-8.** The agent's inference allowance is used up. The machine keeps running and your
scheduled jobs keep firing; only chat answers stop, and they resume when the
allowance is topped up. The allowance is the key's prepaid credit — $12 a month,
and it cannot be exceeded. Top up here: <link>. Your files, the disk and the
backups are untouched. If you think the spend is wrong, tell us and we will read
the key's usage with you before you add credit.

**C-9.** The provider that hosts your machine reports a problem in <region>; more than
one Odile machine is affected, so this is theirs, not yours. Your disk is
encrypted, your data is untouched, and we cannot read it while it is down. We
are in contact with the provider and will write again when machines start
returning; no action from you. If it lasts past an hour we will name a time for
a rebuild on a different host.

**C-10.** Your machine powers on but never reaches your workspace: it is stuck in a boot
loop. The disk is intact and encrypted; nothing has been written since it
stopped. A loop is fixed by a rebuild, not a repair — a changed image or
firmware breaks the seal and the disk stays locked — so we will rebuild it and
restore last night's backup with your recovery file. Keep
odile-recovery-<slug>.txt within reach. Target under an hour once the rebuild
starts; the last day may be lost.

**The counts.**

| Template | Words |
|---|---|
| C-0 | 42 |
| C-1 | 84 |
| C-2 | 85 |
| C-3 | 79 |
| C-4 | 90 |
| C-5 | 86 |
| C-6 | 82 |
| C-7 | 87 |
| C-8 | 81 |
| C-9 | 78 |
| C-10 | 86 |

### 3.2 The rule behind the wording

Each template names the machine or the user, one fact, one next step and one
thing the user must do (or "nothing"). None of them promises a time that has not
been measured; "target under an hour" and "your last day may be lost" are the
approved reliability rows. None of them names a mechanism that is not in
`docs/CLAIMS.md`: the disk is encrypted, Odile holds no key, the sign-in is
hashed on the machine, the recovery file opens the backup and Odile keeps no
copy, the allowance is the key's prepaid credit.

---

## 4. The ten classes

### 4.1 Machine down (unexpected)

**Detect.** The `down` alert: "heartbeat stopped 10m 0s ago; port 22 answers
host key … / port 22 did not answer; brief missed: yes|no" (`alerting.md` §2).
The classification is heartbeat age alone, so a stale heartbeat with a live
machine is this alert too — check the machine before believing the word "down".

**Blast radius.** One tenant. Chat, the morning brief and the nightly backup all
stop; the disk is encrypted and untouched. A missed 03:00 backup is the loss that
compounds.

**First five commands.**

```sh
1. /usr/bin/python3 -m odilectl status <slug> --json
2. curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/verify
3. /usr/bin/python3 -m odilectl health --json
4. curl -sS -H "X-Reseller-Key: $KEY" 'https://servury.com/api/reseller.php?action=server&id=<id>'
5. # console log on, restart, read (§2.1)
```

`status` gives `raw_status` and `powered_on`; `/verify` is the machine's own
answer and `health --json` is the poll's classification of the same tenant. If
`/verify` answers `200` with `PASS — five checks`, the machine is fine and the
work is to restart the heartbeat job, not the machine. `curl /health` is not the
test: on a tenant it answers `302` to `/login` (§2.2). The provider call is the
only view of the hypervisor.

**What the user is told.** C-1.

**Escalate when.** `powered_on: false` and a restart does not change it, or the
console log shows a failed build → provider ticket, and the rebuild path (§5).
All tenants at once → §4.9.

**Exit.** A heartbeat newer than the stopping beat; the next poll sets
`resolved_at`. `/verify` `200` alone is not the exit — it was `200` during the
incident.

---

### 4.2 Machine waiting for owner

**Detect.** The tenant is `ready` but the owner has not set a sign-in: the
first-visit page answers `200`, the ready email's link is unused, and the
machine heartbeats (or, if it does not, this is §4.1). `waiting_for_owner` is a
legacy state: the sealed image boots without a passphrase, so the poll no longer
parks a tenant there (`odilectl/state.py`, ODL-467).

**Blast radius.** None technically. The user paid for a machine they have not
opened; the setup token dies after 24 hours and the link stops working.

**First five commands.**

```sh
1. /usr/bin/python3 -m odilectl status <slug> --json
2. curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/first-visit
3. /usr/bin/python3 /opt/odile/seed/seed.py token list --tenant <slug>
4. /usr/bin/python3 /opt/odile/seed/seed.py token rotate --tenant <slug>
5. curl -sS https://<slug>.odile.fyi/verify | grep -i 'sign-in'
```

`200` on the first-visit page means the form is still waiting; `410` means the
token was spent (the sign-in is set, so the user has a different problem);
`404` means the token is unknown. The token ledger says `live`, `ended` or
`expired`. Step 4 mints a new token but sends nothing: the ready email
(`templates/emails/ready.txt`) is a separate send, with the token in it.

**What the user is told.** C-2.

**Escalate when.** The user says they never received the ready email and a
resend does not arrive → the mail relay (`email.md`), not the machine.

**Exit.** `https://<slug>.odile.fyi/first-visit` answers `410` (the token is
spent, the sign-in is set) and the user is in the workspace.

---

### 4.3 User locked out

**Detect.** The user says "I cannot sign in". The machine is healthy; the
`sign-in hash` row on `/verify` reads PASS.

**Blast radius.** The user, not the machine: the agent keeps running, cron jobs
fire, the backup runs. The recovery code resets the sign-in; without it there is
no reset at HQ.

**First five commands.**

```sh
1. curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/verify
2. curl -sS https://<slug>.odile.fyi/verify | grep -i 'sign-in'
3. # the user opens https://<slug>.odile.fyi/first-visit/reset with the code
4. curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/first-visit/reset
5. # code lost: §4.4's rebuild path; the recovery file still opens the backup
```

`/verify` answering `200` proves the machine and the Webapp are up; the reset
form is the only in-place way back. We never hold the password and there is no
HQ reset (`CLAIMS.md` §5).

**What the user is told.** C-3.

**Escalate when.** The code is lost *and* the user cannot reach the agent: this
becomes the rebuild path (§5), with §4.4's file question first.

**Exit.** The user signs in; the `sign-in hash` row on `/verify` changes (a
reset writes a new hash and rotates the code).

---

### 4.4 Lost recovery kit

**Detect.** The user says so, or a restore finds it. The kit is two things and
they fail differently: the **recovery code** resets the sign-in; the **recovery
file** (`odile-recovery-<slug>.txt`) holds the backup password and the
certificate fingerprint. Losing the code is annoying; losing the file makes the
backup unreadable.

**Blast radius.** With the file lost and the machine alive: no loss yet. With the
machine dead: the last backup is ciphertext nobody can open — Odile holds no
copy (`CLAIMS.md` A9; `backup.md` §1).

**First five commands.**

```sh
1. # machine alive, code in hand: the file is behind the code
   curl -sS -o recovery.txt -w '%{http_code}\n' 'https://<slug>.odile.fyi/first-visit/recovery?c=<code>'
2. # code lost too: ask the agent (the file stays on the machine)
   #   in the chat: "show me my recovery file"
3. # machine dead: does the file we have open the repo?
   restic -r <repo> snapshots          # with the file's password
4. # and can HQ open it?
   /usr/bin/python3 -m odilectl restore <slug> --verify --password-file <file>
5. # the honest answer when the file is gone: rebuild, and name what is lost
   /usr/bin/python3 -m odilectl status <slug> --json   # last_backup_at, repo_bytes
```

`restic` answers `Fatal: wrong password or no key found` (exit 12) for a wrong
password and `an empty password is not allowed` (exit 1) for none. `odilectl
restore --verify` without `--password-file` exits 2 and says the recovery file
is the only copy. Step 4 is the HQ check, and for a live tenant it fails before
the password is read: `backup.odile.fyi` does not resolve and HQ's key is not in
the backup box's `authorized_keys`. The restore is the rebuilt machine's job
(§5). All of it is measured in §6.1.

**What the user is told.** C-4.

**Escalate when.** The file is lost and the data matters: Savio decides whether
the rebuild is a courtesy or a paid restore; the technical answer does not
change.

**Exit.** Either the file is found and `restic snapshots` lists it, or the
rebuild completes and the incident note names the last backup that is now
unreadable.

---

### 4.5 Seed failure

**Detect.** The installer stops with `error: seed exchange refused with 410
(expired)…` or `error: seed exchange returned HTTP 5xx`, and the machine never
reaches first visit. HQ sees nothing until the user reports it, unless the token
ledger shows a token that expired unused.

**Blast radius.** One machine, no user data. The machine has no configuration, so
nothing else can work; the token is single-use, so a retry without a new token
cannot succeed on a fresh machine.

**First five commands.**

```sh
1. curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://odile.fyi/seed \
     -H 'content-type: application/json' -d '{}'          # 400 = the route is alive
2. systemctl is-active odile-seed; journalctl -u odile-seed -n 50 --no-pager
3. /usr/bin/python3 /opt/odile/seed/seed.py token list --tenant <slug>
4. /usr/bin/python3 /opt/odile/seed/seed.py token rotate --tenant <slug>
5. # the user reruns the installer with the new token, or restores the cache
```

The user-visible message names the two ways out itself: ask Odile for a new
token, or restore `/etc/odile/seed.json` and `/etc/odile/seed.sig` from the
first run — a machine that already holds a signed cache never re-exchanges
(`installer/odile_install.py` `load_or_fetch_seed`).

**What the user is told.** C-5.

**Escalate when.** `500` from `/seed` (the signing key or the config is broken)
→ Savio/security; the `odile-seed` unit down → restart it (HQ); a release-wide
signature problem → the release card.

**Exit.** The installer writes the seed cache (`write seed cache … signature
verified`) and the machine reaches first visit.

---

### 4.6 Installer failure

**Detect.** No first visit after a boot; the serial log shows the installer
stopping; `/verify` answers `500` or `FAIL`; the ready page never appears. The
installer runs itself on every boot (`odile-release-setup.service`), so a
transient failure is often a reboot away.

**Blast radius.** One machine, no user data. A bad release is all new machines
(§4.9's cousin) and is a rollback, not a repair.

**First five commands.**

```sh
1. /usr/bin/python3 -m odilectl status <slug> --json
2. # the serial log, one restart (§2.1)
3. curl -sS -o /dev/null -w '%{http_code}\n' https://odile.fyi/manifest.json
4. curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/verify
5. # restart once; if the log repeats, rebuild (§5)
```

`/manifest.json` on `odile.fyi` is the signed manifest the installer verifies
against (`/manifest` answers 404); `/release.json` and `/release.json.sig` are
beside it. The serial log is the only place the installer's own lines are
visible without a shell.

**What the user is told.** C-6.

**Escalate when.** The same installer line appears on two machines → a release
bug; one machine → rebuild.

**Exit.** The machine reaches first visit and `/verify` answers `200`.

---

### 4.7 Disk full

**Detect.** The `disk` alert: "disk 91.0% used (threshold 80%)" (`alerting.md`
§2), or the user says the brief stopped and the chat errors. The threshold is a
strict `>`: exactly 80% does not alert.

**Blast radius.** One tenant. Writes fail; the brief, the backup and the agent's
own state are at risk. The machine keeps answering until the disk is completely
full.

**First five commands.**

```sh
1. /usr/bin/python3 -m odilectl status <slug> --json
2. /usr/bin/python3 -m odilectl health --json | grep -i disk
3. # ask the user to ask the agent:
   df -h /; sudo du -sh /var/lib/odile /var/log /home/odile
4. # the bundle says what grew, without a shell of ours:
   sudo odile-diag        # disk section: df -hT, lsblk, du
5. # delete only with the owner's approval; then
   sudo journalctl --vacuum-size=200M
```

The usual cause is an agent loop writing files into the workspace; the second
usual cause is journal growth. Nothing is deleted without the owner's word.

**What the user is told.** C-7.

**Escalate when.** The agent cannot run (the disk is too full to fork) → the
grant (§2.4). Deletion of a file the user needs → the user decides, not us.

**Exit.** Disk below 80% and the next heartbeat reports it; the poll resolves the
alert.

---

### 4.8 Spend spike

**Detect.** The chat reports the provider's insufficient-balance message and
stops answering; the NEAR AI console shows the key's balance. The `spend` alert
exists (`alerting.md` §2) but no v0.1 heartbeat carries `facts.spend`, so it is
dormant; ODL-242 is the card that makes issue → top-up → zero → revoke real.

**Blast radius.** Inference only. The machine, the disk and cron jobs keep
running; chat answers stop. The allowance cannot be exceeded: the key's prepaid
credit is the ceiling (`CLAIMS.md` §1 Money).

**First five commands.**

```sh
1. /usr/bin/python3 -m odilectl status <slug> --json          # heartbeat facts
2. /usr/bin/python3 -m odilectl health --json                 # open alerts
3. # ask the agent: the agent's own line, without printing the key
   sudo journalctl -u odile-agent -n 80 --no-pager | grep -iE 'balance|insufficient|401'
4. # the NEAR AI console: the per-user key's balance (HQ, human step)
5. ./tools/spend.py --json                                    # HQ's own budget, not the tenant's
```

`tools/spend.py` is Odile's HQ inference budget ($100/day across profiles). It is
never the tenant's number; quoting it in a tenant incident is how the wrong
figure reaches a user.

**What the user is told.** C-8.

**Escalate when.** A top-up above the plan's $12 → Savio (money). A key that
spends with no tenant → revoke and rebuild (`ODL-242`'s revoke path).

**Exit.** The chat answers again; the console shows the topped-up balance.

---

### 4.9 Provider outage

**Detect.** Several tenants stop at once; `odilectl status` errors or shows
`raw_status` changes for more than one; the provider's status page says so. One
tenant is §4.1; the discriminator is the count.

**Blast radius.** Every tenant on the affected provider or region. Odile's own
site is a different machine and can stay up (`odile.fyi` is DNS + the waitlist,
not the tenants).

**First five commands.**

```sh
1. /usr/bin/python3 -m odilectl reconcile --json      # drift, per tenant
2. /usr/bin/python3 -m odilectl health --json         # how many are down
3. curl -sS -H "X-Reseller-Key: $KEY" 'https://servury.com/api/reseller.php?action=account'
4. curl -sS -H "X-Reseller-Key: $KEY" 'https://servury.com/api/reseller.php?action=servers'
5. # the provider's status page and ticket
```

`reconcile` returns findings, not guesses: `missing_vm`, `expired_vm`,
`unknown_vm`, `state_drift` (`odilectl/state.py`). An `unknown_vm` with no
tenant row is HQ's own machine, not a lost tenant.

**What the user is told.** C-9.

**Escalate when.** More than an hour, or the provider has no estimate → Savio.
A provider choice is a major change (new provider = Savio).

**Exit.** Every tenant heartbeats again and `reconcile --json` has no finding
but the known `unknown_vm`, if that still exists.

---

### 4.10 Boot loop

**Detect.** A machine that powers on and never reaches a heartbeat; the serial
log repeats the same lines; port 22 answers with the initramfs host key or not at
all. Under the sealed image a boot is supposed to unseal itself with no
passphrase (`image.md` §11.5), so a loop is a seal or image failure, not a
waiting owner.

**Blast radius.** One tenant, total: no workspace, no brief, no backup. The disk
is intact and encrypted; nothing is written during the loop.

**First five commands.**

```sh
1. /usr/bin/python3 -m odilectl status <slug> --json
2. # the serial log, one restart, read (§2.1)
3. # compare the log's release and measurement with the ready email and /verify
   curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/verify
4. curl -sS https://odile.fyi/release.json
5. # no repair in place: rebuild + restore (§5)
```

The measurement in the log must equal the one in the ready email and on
`/verify`; a different image or firmware breaks the seal and the disk stays
locked. No shell fixes that; `image.md` §8 says so.

**What the user is told.** C-10.

**Escalate when.** Two machines loop on the same release → rollback the release
(a release card, not this one).

**Exit.** The rebuilt machine reaches first visit; the restore drill (§5) passes;
the user signs in.

---

## 5. The rebuild path

Shared by 4.1 (`status: failed`), 4.4 (file lost, or the disk will not open),
4.6 and 4.10. A rebuild is a new machine with the same slug; addresses change,
the URL does not (`CLAIMS.md` §1: your slug is yours for the life of the
tenant).

```sh
# 1. the old machine: destroy, or terminate from the provider panel
/usr/bin/python3 -m odilectl destroy <slug> --apply
# 2. the new machine: provision on the same slug, one key (provision.md §3)
/usr/bin/python3 scripts/provision_one.py --slug <slug> --email <owner@example.com> \
  --key /root/.odile/tenants/<slug>.pub
# 3. wait for first visit; the image installs itself and fetches its seed
# 4. the user sets the sign-in on the new machine (the ready email's link)
# 5. the user asks the agent to restore, with the recovery file (the guest's
#    own copy of the backup program; it reads /etc/odile/recovery.txt):
#    /usr/bin/python3 /usr/local/lib/odile/backup.py restore latest \
#      --target /home/odile --include /home/odile
# 6. verify: the snapshot count, sha256sum -c against the drill manifest, the
#    workspace's files
```

The restore runs on the machine that holds the recovery file. From HQ,
`odilectl restore --apply` needs `--repo`, the backup box's `--ssh-key` and
`--known-hosts`; HQ holds neither for a live tenant (drill 1), so HQ is the
fallback for a bench, not the plan. The last day may be lost; the approved row
is "Rebuildable in under an hour — without a key" (`backup.md` §5).

---

## 6. The drills

Two drills, run for real on 15 November 2026, at the head of ODL-244's branch.
Each transcript is attached to the card. The rule for a drill: it must exercise
the **shipping** program, not a mock, and it must be able to fail.

### 6.1 Drill 1 — a lost key

**Scenario.** The user has lost the recovery file; one half with the machine
alive and the code in hand, one half with the machine gone.

**What ran.** `installer/first_visit.py` — the file the signed installer places
— against a scratch root with a real state file and a real 0600 recovery file;
then `restic` and `odilectl restore` against the live tenant's repository
(`odl-7971a8`, 1,017,737,148 bytes on the backup box).

**What it measured.**

| Check | Result |
|---|---|
| the code the user saved opens the file | `200`, `text/plain`, `attachment; filename="odile-recovery-odl-drill1a.txt"`, 538 bytes, `Backup password:` present |
| a mistyped code | `404` `The recovery file is not available on this machine.` |
| no code at all | `404`, the same body — the page does not distinguish |
| the code without dashes, lower case | `200` — the code is normalised, so the user may type it either way |
| the file on the machine | mode `0600`, root, 538 bytes — the agent can show it again |
| the live tenant's page without a code | `404` |
| the live repo with a wrong password | `restic`: exit 12, `Fatal: wrong password or no key found` |
| the live repo with no password | `restic`: exit 1, `an empty password is not allowed…` |
| the bench repo with the file's password | exit 0, one snapshot listed |
| the bench repo, one character changed | exit 12, the same fatal |
| `odilectl restore --verify` without a file | exit 2, `…the user's recovery file is the only copy of the repository password` |
| `odilectl restore` from HQ, any password | exit 5 — the transport fails first: `backup.odile.fyi` is `NXDOMAIN` and HQ's key is not in the backup box's `authorized_keys` |

**What it changed.** v1 said the recovery page answers `400` for a bad code, and
that an HQ restore proves the file opens the repository. v1.1 says `404` (the
page never says which part was wrong) and names the restore as the rebuilt
machine's job. See §6.3, rows 1–3.

### 6.2 Drill 2 — a failed seed

**Scenario.** A machine's setup token is expired before the install finishes;
then HQ rotates it and the same machine retries.

**What ran.** `seed/seed.py serve` on a scratch ledger with the real signing
key, driven by `curl` and by the shipping installer
(`installer/odile_install.py --root …`) against a scratch root.

**What it measured.**

| Exchange | Result |
|---|---|
| the right token, the right machine | `200`, `X-Odile-Signature: ed25519=…`, eight fields |
| the same token again | `410` `{"reason":"used"}` |
| an expired token | `410` `{"reason":"expired"}` |
| a live token for another machine | `403` `{"reason":"wrong_instance"}` — and the token still works afterwards |
| a token that never existed | `404` `{"reason":"unknown_token"}` |
| a malformed token | `400` `{"reason":"bad_token"}` |
| a body that is not JSON | `400` `{"reason":"bad_request"}` |
| a body over 4 KiB | `413` `{"reason":"too_large"}` |
| the service down | `URLError: [Errno 111] Connection refused` |
| the installer, expired token, fresh machine | `error: seed exchange refused with 410 (expired) and no cached seed exists.` … exit **1**, no seed cache |
| the installer, expired token, machine with a cache | `same seed cache …` — no exchange at all |
| HQ rotates the token | `revoked: 2 live token(s)`, a new token printed once |
| the installer, new token, fresh machine | `fetch seed` → `seed key … certified by …` → `write seed cache … (0600, signature verified)` |

**What it changed.** v1 said the refusal exits 2 and gave
`odilectl rotate-token --apply` as the rotation command. The refusal exits 1, the
command is `seed.py token rotate` (the `odilectl` wrapper is `NotWired` until
ODL-212), and the cache-first rule is the fastest way out. See §6.3, rows 4–7.

### 6.3 Every line the drills changed

| # | v1 said | The drill measured | v1.1 says |
|---|---|---|---|
| 1 | a wrong recovery code answers `400` | `404`, same body as no code | `404` |
| 2 | HQ can run the tenant's restore with `--repo` and the backup key | `NXDOMAIN`; HQ's key is not accepted by the backup box | the rebuilt machine restores |
| 3 | `/health` is `200` on a live tenant | `302` to `/login` | `/verify` is the liveness page |
| 4 | the seed refusal exits 2 | exit 1 | exit 1 |
| 5 | `odilectl rotate-token --apply` | `NotWired`; `seed.py token rotate` works | `seed.py token rotate` |
| 6 | an expired token blocks the install | cache-first: a machine that already fetched never re-exchanges | the cache is a way out |
| 7 | `/manifest` is the manifest URL | `404`; `/manifest.json` is `200` | `/manifest.json` |
| 8 | `status --json` proves a slug exists | exits 0, `ok: true`, `machines: []` | read `count` |
| 9 | a stopped heartbeat means the machine is down | the demo tenant's heartbeat is 2 d 7 h stale while `/verify` answers `200` with `PASS — five checks`; the poll plans `down` and `backup` alerts (15 Nov 10:37Z) | `/verify` first, then the heartbeat job |

### 6.4 A live measurement beside the drills

While the drills ran, the poll's own view of the demo tenant was recorded: its
last heartbeat is `2026-11-13 03:01:14Z` (`odilectl health --json`:
`heartbeat_age_seconds: 200,991`, classification `down`, brief missed), while
`/verify` answers `200` with `measurement 9f01725a…` and `PASS — five checks`.
The poll plans a `down` alert ("heartbeat stopped 2d 7h ago; port 22 answers
host key …") and a `backup` alert ("last backup 2d 7h ago (threshold 1d 12h)").
The machine is up; the heartbeat job is what stopped — on the day the demo was
reinstalled (13 November). The public `/status` page counts it: `Downtime 3,421
min` against `In-service time 2.4 machine-days`. That is the discriminator §4.1
now puts second, it is why "down" in this page means *the heartbeat stopped*,
and it is a live defect the follow-up card owns (§7.6).

## 7. Limits

1. **The drills are a scratch root and a live repository, not a live failed
   tenant.** Drill 1's first half runs the shipping first-visit program; its
   second half runs the real repo. Drill 2 runs the shipping seed service and
   the shipping installer. Neither drill powers off a tenant; the live
   stopped-VM check is ODL-332's.
2. **`waiting_for_owner` is legacy.** The state remains in the machine; the
   poll no longer parks a tenant there (ODL-467). §4.2 is about the sign-in,
   not about a parked boot.
3. **The `spend` alert is dormant** until a heartbeat carries `facts.spend`;
   ODL-242 owns the per-user key and the zero-balance message. §4.8 says what
   to read instead.
4. **A grant is root, and the log is on the machine.** A root shell can edit
   `authorized_keys` and the log; the five post-revoke checks are how the user
   sees what it left (`CLAIMS.md` A10).
5. **The provider console is a door we do not control.** It shows the boot and
   can restart or power off; it is not a shell and it does not decrypt anything
   (`image.md` §11.5, gap 2).
6. **The demo tenant is the counter-example in §6.4.** Its heartbeat stopped on
   13 November (2 d 7 h stale at the time of writing) while the machine serves
   `/verify`, so the poll reports the demo `down`, its backup stale, and the
   public `/status` page counts `3,421 min` of downtime for a working machine.
   A follow-up card owns the demo's heartbeat and backup timers and the status
   page's demo row. Two smaller follow-ups the same measurement exposed: the
   `down`/`backup` alert's `first_command` is still
   `ssh -i ~/.ssh/odile …` (the key does not exist; `odilectl/health.py`), and
   the `spend` alert is dormant (§4.8).
