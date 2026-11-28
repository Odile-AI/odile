# Incidents — the ten classes, the first five commands, and the drills

**Status:** v1.4 — 23 November 2026: after ODL-925 (B-once) §4.2 and C-2 are
re-cut to the landed first visit — a login with the password chosen at
registration, not a setup — and the `?t=`/`410` lines with them (D-1, D-7). v1.3
was the reset-copy re-cut to the v3.11 deck (D-2, D-4, D-19) and §6.2's
pre-ODL-926 label (D-20). v1.2 corrected four drills: two re-cut for alpha.0 and
run on 22 November 2026 (§6.1, §6.2), and two run on 15 November 2026 and kept
because the recovery and seed classes still cite them (§6.4). §6.3 is every line
a drill changed, with the measurement that changed it. **Card:** ODL-244, A2
(re-cut 17 November 2026, ODL-573); claims re-cut `t_47bceb77` (ODL-244
follow-up), v3.11 D-2/D-4/D-19/D-20, and `t_01ef88d9`, v3.11 D-1/D-7 after
ODL-925.
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
| 4.2 | Machine waiting for owner | `odilectl status <slug> --json`, then `/first-visit/reset` (`409` = never signed in) | HQ + user |
| 4.3 | User locked out | `curl https://<slug>.odile.fyi/first-visit/reset` (`200` = a code exists) | user |
| 4.4 | Lost recovery kit | the recovery page, then the backup repo | user + HQ |
| 4.5 | Seed failure | `seed.py token list --tenant <slug>` | HQ |
| 4.6 | Installer failure | the provider's console log | HQ |
| 4.7 | Disk full | the agent: `df -h /` | user (agent) |
| 4.8 | Spend spike | the NEAR console, then `odilectl status` | HQ |
| 4.9 | Provider outage | `odilectl reconcile --json` | HQ |
| 4.10 | Boot loop | the provider's console log | HQ |

**One state is not a class.** A build that failed before a machine existed — the
claim worker's `provision_failed` — is neither a down machine nor an installer
failure: there is no machine to restart and nothing to read a boot log from. The
first command is `odilectl claim status <slug> --json`, the alert carries the
retry line, and §5.0 is the whole path. It is A2's fourth exit criterion, and
drill 1 measures it (§6.1).

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
# off: empties the transcript on the host — read it before this call
curl -sS -X POST -H "X-Reseller-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"log": false}' 'https://servury.com/api/reseller.php?action=serial&id=<id>'
```

The restart call may answer `504` after 30 s and still power-cycle the machine
roughly 90 s to two minutes later; the initramfs ignores ACPI and the node cuts
power (`provision.md` §4, whose provider table records `~90s`). Do not restart
twice.

Two `odilectl` habits that bite:

* `status --json` on a slug that does not exist prints `"ok": true` with
  `"machines": []`; read `count`, not the exit code.
* `health` without `--apply` is a dry run: it prints `planned_alerts` and
  writes nothing. `--apply` is what sends.

### 2.2 The tenant's own pages

| URL | What it proves |
|---|---|
| `https://<slug>.odile.fyi/verify` | The machine's own access report: measurement, boot key, seal, sign-in hash, operator key, grant. `200` with `PASS — five checks` means the machine is up and serving. |
| `https://<slug>.odile.fyi/first-visit` | The page the ready email names: `200` the login form on a machine whose sign-in was written at provision (a classic install with no seed gets the setup form instead). `?t=` — any value — answers `410` (`This link is closed`), the stale A-era link: no page accepts the setup token any more (ODL-925). The bare path is the page, not a 404. |
| `https://<slug>.odile.fyi/first-visit/reset` | `200` the reset form once the first sign-in has run (a recovery hash exists); `409` (`This link is closed.`) when it has not — the state has no recovery hash, which is §4.2, not §4.3. |
| `https://<slug>.odile.fyi/first-visit/recovery` | `GET` the form; `POST` the code (no `?c=` any more) and get a page with a one-time download link (`?d=`, ten minutes, one use). The old permanent `?c=<code>` URL returns the form and never the file (ODL-579). |
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
user starts it; HQ cannot.** `odilectl support --apply` still raises `NotWired`
(the guest program ships; the wrapper does not), so the only form is the user
asking the agent:

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
`support.md` §5 and `CLAIMS.md` A10. `alerting.md` §2 and §7 still show the
older first command `ssh -i ~/.ssh/odile …`; that key does not exist in v0.1, so this
page uses the agent or the grant instead.

### 2.5 What no door can do

* **No operator SSH.** There is no key at HQ, and the boot key is a public key
  with no private half.
* **No HQ reset of the sign-in and no disk unlock.** The recovery code resets
  the sign-in on the machine; a disk that will not open is a rebuild.
* **No reading of the backup without the user's recovery file.** Measured in
  §6.4.1: restic answers `wrong password or no key found`, and HQ's own key is
  not accepted by the backup box.
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

**C-2.** A machine is reserved for you. When you claim it, it is built and starts
itself. Check the machine before you trust it: the verify page shows PASS or
FAIL. Report: https://<slug>.odile.fyi/verify. Your Odile password opens your
machine and your account page. You chose it when you registered. Sign in:
https://<slug>.odile.fyi/first-visit — the same page your ready email names, and
the page to use if that email's link says it is closed. If the password is
refused, tell us.

**C-3.** You cannot sign in; the machine is fine and your files are untouched. A
lost Odile sign-in is reset on the machine with your recovery code; HQ cannot
reset your machine's sign-in. Open https://<slug>.odile.fyi/first-visit/reset and
enter the code you saved at your first visit. There is no emailed reset. If you
cannot find the code, tell us: the way back is a rebuild, and your recovery file
still opens the backup. Nothing is lost except the time it takes to rebuild.

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

**C-6.** The build stopped before your workspace. If a machine was created, we read
its boot log and restart it once; the installer runs again by itself on the next
boot. If no machine was made, we retry the build. Either way you do not need to
do anything, nothing is billed while the build is failing, and nothing of yours
is lost: the disk is encrypted and the install writes no user data before it
finishes. We will tell you which happened. If it stops twice we rebuild, which
takes under an hour and needs no key from you.

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
| C-2 | 79 |
| C-3 | 81 |
| C-4 | 90 |
| C-5 | 86 |
| C-6 | 99 |
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
host key … / port 22 did not answer; brief missed: yes|no" (`alerting.md` §2),
or — for a tenant that has never sent a signed heartbeat — "no signed heartbeat
has ever arrived; /health did not answer; port 22 did not answer; …". The
classification is heartbeat age, or (with no heartbeats at all) both public
surfaces silent, so a stale heartbeat with a live machine is this alert too —
check the machine before believing the word "down".

**Blast radius.** One tenant. Chat, the morning brief and the nightly backup all
stop; the disk is encrypted and untouched. A missed nightly backup is the loss
that compounds.

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

**Detect.** The tenant is `ready` (or `installed`) and the machine heartbeats, but
the user has never signed in: `/first-visit/reset` answers `409` — the state has
no recovery hash, and the code is issued at the first sign-in — and `/verify`'s
sign-in row reads `PASS` (it proves a hash is present, not that a user typed it).
The ready email's link is `https://<slug>.odile.fyi/first-visit`, a login page;
any `?t=` answers `410` (`This link is closed`), because no page accepts the
setup token any more (ODL-925). `waiting_for_owner` is a legacy state: the sealed
image boots without a passphrase, so the poll no longer parks a tenant there
(`odilectl/health.py`, ODL-467). Measured on the bench tenant
`odl-340a1e.test.odile.fyi` on 22 November 2026, on the 21 November image:
`/first-visit/reset` `409`, bare `/first-visit` `404` (that image's A-era page),
`/login` `200`, and `/verify`'s sign-in row `PASS` — the row proves a hash is
present, not that a user chose it.

**Blast radius.** None technically: the machine is up, its disk is encrypted and
untouched, and nothing expires — the ready email's link is the machine's login
page. The user has a machine they have not opened.

**First five commands.**

```sh
1. /usr/bin/python3 -m odilectl status <slug> --json
2. curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/first-visit/reset   # 409 = never signed in
3. curl -sS https://<slug>.odile.fyi/first-visit | grep -o 'Sign in\|Choose your sign-in'   # which page the ready link opens
4. curl -sS https://<slug>.odile.fyi/verify | grep -i 'sign-in'
5. ls -l /var/lib/odile/outbox/ready/<slug>.eml   # the ready email as queued; resend it, or send C-2
```

`409` on the reset form is the discriminator: the state has no recovery hash, so
the first sign-in never ran. `/first-visit` is the page the ready email names;
any `?t=` is the stale A-era link and answers `410`, so the status alone proves
nothing about the sign-in. Step 3 tells the two pages apart: `Sign in` is the
B-once login, `Choose your sign-in` a classic install with no seed. Step 5 is the
queued message; a resend is a separate send, and C-2 is the same message when the
queue item is gone.

**What the user is told.** C-2.

**Escalate when.** The user says they never received the ready email and a
resend does not arrive → the mail relay (`email.md`), not the machine.

**Exit.** The user's first sign-in answers `POST` `200` with the done page (the
recovery code shown once) and `/first-visit/reset` answers `200` instead of
`409`.

---

### 4.3 User locked out

**Detect.** The user says "I cannot sign in". The machine is healthy and
`/first-visit/reset` answers `200` (a recovery hash exists — a `409` is §4.2,
the first sign-in never ran). The `/verify` sign-in row reads `PASS` in both
cases; it proves a hash is present, not that the user's password is the one.

**Blast radius.** The user, not the machine: the agent keeps running, cron jobs
fire, the backup runs. The reset is the machine's: "A lost Odile sign-in is reset
on the machine with your recovery code; HQ cannot reset your machine's sign-in."
(`CLAIMS.md` D-19)

**First five commands.**

```sh
1. curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/first-visit/reset   # 200 = a code exists
2. # the user opens https://<slug>.odile.fyi/first-visit/reset and enters the code
3. # a wrong code answers 200 with "That recovery code does not match." — test the text, not the status
4. curl -sS -o /dev/null -w '%{http_code}\n' https://<slug>.odile.fyi/verify
5. # code lost: §4.4's rebuild path; the recovery file still opens the backup
```

The machine's reset form is the only in-place way back. "One recovery code resets
both: your account password at the account page and your machine's sign-in on the
machine. There is no emailed reset." (`CLAIMS.md` D-4) D-2 is why an account
reset does not fix the machine: "Odile sees your password when you register and
when you sign in at the account page and keeps only a hash of it. Your machine is
built with a copy of that hash; after that, changing one does not change the
other." (D-2) D-19 is the qualified survivor: "…HQ cannot reset your machine's
sign-in." (D-19) Four things the drill measured (§6.2):

* at the **pre-ODL-926 head** a **wrong or used code** answered `200` and
  re-rendered the form with `That recovery code does not match.` — a check on the
  status code alone reads a refusal as a success;
* the **right code** sets the new sign-in; at the pre-ODL-926 head the page also
  showed a **new code once**, and the old code died immediately, because both
  `POST /first-visit/reset` and `POST /first-visit/recovery` spent a code and
  rotated it. That is the **pre-ODL-926 measurement**: D-20 is the current row —
  "Your recovery code works at both places until you replace it; using it does not
  spend it." (D-20) — and ODL-926 lands the stable code, whose reset response
  shows no new code;
* the reset **signs out every other browser** (the session secret is replaced)
  and **restarts the gateway** to load the new hash: the workspace answers `502`
  for a few seconds, so wait for `/` to answer `302` before calling a login a
  refusal (measured at 3.6–4.2 s in `docs/evidence/m1-tenant.md` §10);
* the **recovery page** is a `POST` now, and its answer carries a one-time
  download link (`?d=`, ten minutes); the old `?c=<code>` URL returns the form.

**What the user is told.** C-3.

**Escalate when.** The code is lost *and* the user cannot reach the agent: this
becomes the rebuild path (§5), with §4.4's file question first.

**Exit.** The user signs in with the new password — a `200` from the login, not
the `/verify` row (which never changes: it proves a hash is present) — and the
code they hold is the one the reset form accepts: the new code the page showed at
this head, and the unchanged code once ODL-926 lands.

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
1. # machine alive, code in hand: POST the code; the answer carries a one-time link
   curl -sS -o form.html -w '%{http_code}\n' -X POST -d 'code=<code>' \
     https://<slug>.odile.fyi/first-visit/recovery
   # then GET the ?d=<token> link the page shows (ten minutes, one use)
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
is the only copy. Step 1 is a `POST` since ODL-579: the old permanent
`?c=<code>` URL answers `200` with the *form* and never the file (measured on
the demo tenant, 22 November 2026). Step 4 is the HQ check, and for a live
tenant it fails before the password is read: `backup.odile.fyi` does not resolve
and HQ's key is not in the backup box's `authorized_keys`. The restore is the
rebuilt machine's job (§5). All of it is measured in §6.4.1.

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
transient failure is often a reboot away. If there is no machine at all, the
build failed before this class began: `claim status <slug> --json` says
`provision: "failed"` and §5.0 is the path.

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

`tools/spend.py` is Odile's HQ inference meter (today's total across every
profile; there is no standing cap since 2026-11-22). It is never the tenant's
number; quoting it in a tenant incident is how the wrong figure reaches a user.

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
`unknown_vm`, `zombie_vm` (`odilectl/state.py`). An `unknown_vm` with no
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
locked. No shell fixes that; `image.md` §7 says so.

**What the user is told.** C-10.

**Escalate when.** Two machines loop on the same release → rollback the release
(a release card, not this one).

**Exit.** The rebuilt machine reaches first visit; the restore drill (§5) passes;
the user signs in.

---

## 5. The rebuild and re-run paths

### 5.0 A build that failed before a machine existed

A2's claim worker builds a tenant in two runs (ODL-947). The first orders the
machine, polls it, writes the DNS record and mints the seed token, and stages
the ready mail's facts. If any step before the machine is recorded fails, that
run terminates the machine it created, removes any DNS record it wrote, puts
the tenant row back to `claimed`, and writes one `provision_failed` alert.
There is no machine to restart and no disk to rebuild; the fix is a retry. Once
the machine is recorded, a later run of the same timer asks its own
`/attestation` and `/verify`; only an answering machine gets the recovery kit
and the one ready email, and a failure in that second half keeps the machine and
retries on the next tick (`claim status` reads `waiting_for_guest`).

```sh
1. /usr/bin/python3 -m odilectl.claim status <slug> --json   # provision: "failed"
2. # the alert carries the retry line, e.g. under the operator's outbox:
   #   retry: python3 -m odilectl.claim run --apply <slug>
3. /usr/bin/python3 -m odilectl.claim run --apply <slug>
4. /usr/bin/python3 -m odilectl.claim status <slug> --json   # provision: "ready"
5. # the user's claim token still replays; the account page's `failed` state
   # (ODL-583) reads this event
```

`claim status` derives `provision` from the last append-only event, so a failed
attempt is visible without rewriting the row; `tenant_state` stays `claimed`,
which is the retryable state. The drill measured the whole path (§6.1): the
provider calls `create → poll → terminate`, zero live machines remain, no DNS
record and no recovery kit or ready email is left, one alert is written, and a
retry orders exactly one machine and produces the ready email. The alert prints
its retry as `python3 -m odilectl.claim run --apply <slug>`; at HQ `python3` is
the Hermes virtualenv, so run the `/usr/bin/python3` form above.

### 5.1 The rebuild: a new machine on the same slug

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
# 4. the user signs in on the new machine with their account password (the ready
#    email's link)
# 5. the user asks the agent to restore, with the recovery file (the guest's
#    own copy of the backup program; it reads /etc/odile/recovery.txt):
#    /usr/bin/python3 /usr/local/lib/odile/backup.py restore latest \
#      --target /home/odile --include /home/odile
# 6. verify: the snapshot count, sha256sum -c against the drill manifest, the
#    workspace's files
```

With no `--plan` / `--days` flags step 2 orders the script's own default, **plan
138 (Blackbox Nano) for 365 days** — the same tier and term as the live config
(`docs/runbooks/deploy.md`). Pass explicit flags only to deviate, and name the
measured reason on the card: Servury cannot downgrade a machine.

The restore runs on the machine that holds the recovery file. From HQ,
`odilectl restore --apply` needs `--repo`, the backup box's `--ssh-key` and
`--known-hosts`; HQ holds neither for a live tenant (§6.4.1), so HQ is the
fallback for a bench, not the plan. The last day may be lost; the approved row
is "Rebuildable in under an hour — without a key" (`backup.md` §5).

---

## 6. The drills

Four drills, each run for real against a shipping program and each able to fail.
The two re-cut for alpha.0 were run on 22 November 2026 (§6.1, §6.2); the two of
15 November 2026 are kept because §4.4 and §4.5 still cite them (§6.4). Every
transcript is attached to the card. The rule for a drill: it must exercise the
**shipping** program, not a mock, and it must be able to fail.

### 6.1 Drill 1 — a failed provision (re-cut for A2, 22 November 2026)

**Scenario.** A paid, claimed tenant's build fails after the machine exists. A2's
fourth exit criterion is what the drill is for: no billed machine remains, and
the tenant is back in a state the worker can retry.

**What ran.** The shipping claim spine — `odilectl/claim.py`'s
`provision_tenant` — against a throwaway SQLite database, with its own recovery
kit renderer, alert writer and outbox. The two external services are the
shipping suite's own fixtures (`tests/test_claim.py`): an in-memory provider and
a stateful Cloudflare transport. No vendor order was placed, so nothing was
billed. Three failure points: the poll raises after `create` returns a machine,
`create` itself raises, and the recovery-kit render raises after the DNS record
is written.

**What it measured.**

| Check | Result |
|---|---|
| the provider calls after a failed poll | `create`, `poll`, `terminate` — one terminate, no second order |
| machines the provider still bills | `0`: the created machine is `terminated` |
| the tenant row after the failure | `state: claimed`, `server_id: null`, `ipv4: null`, `setup_token_hash: null`, `release: null` |
| the DNS record | none after the run (`{}`); when the failure lands after the DNS write, the record is `DELETE`d (the kit-render row below) |
| the queued files | none: no ready email, no kit |
| the event the operator reads | `{"step": "provision_failed", "server_id": "srv-fixture0001", "terminated": true, "reason": "fixture: the build failed"}` |
| `claim status <slug> --json` | `provision: "failed"`, `tenant_state: "claimed"`, `server_id: null`, `ready_email: null` |
| the alert | one `[Odile] provision failed — <slug>` email: symptom, server, `cleanup: machine terminated; no billed resource remains`, and the retry line `python3 -m odilectl.claim run --apply <slug>` |
| the claim token after the failure | replays (`replay: true`) — the account is retryable |
| the retry on a healthy provider | `ok: true`, exactly one `create`, one live machine, one ready email, `provision: "ready"` |
| a `create` that never returns | provider calls `create` only, zero machines, `terminated: false`, the same `claimed` row, an alert saying `no machine was created` |
| a failure after the DNS write (the kit render) | `create`, `poll`, `terminate`; DNS `POST` then `DELETE`; the row `claimed`; the alert's reason is the kit error |

**What it changed.** The runbook had no path for a build that failed before a
machine existed: §4.1 restarts a machine, §4.6 reads a boot log, and neither
exists here. §0 now says the state is not a class, §5.0 is the retry, and C-6
carries the user copy. See §6.3.

### 6.2 Drill 2 — a lost sign-in (re-cut for A2, 22 November 2026)

**Measured before ODL-925 and ODL-926.** The drill ran before ODL-926, which
lands the stable code, so the "new code", "third code" and "the same code again"
rows below are the pre-ODL-926 measurement. D-20 is the current row: "Your
recovery code works at both places until you replace it; using it does not spend
it." (D-20) It also ran before ODL-925, which makes the first visit a login: the
bare `/first-visit` `404` rows below are that head's measurement, and at this
head the bare path is the login page (`200`) while any `?t=` answers `410`.

**Scenario.** A tester on the bench forgets the sign-in. The recovery code is the
only reset; "HQ cannot reset your machine's sign-in" (`CLAIMS.md` D-19).

**What ran.** Two halves.

* The live bench tenant `odl-340a1e.test.odile.fyi` and the live demo tenant
  `odl-7971a8.odile.fyi`, driven over HTTPS: the endpoints §4.2 and §4.3 use,
  and a wrong-code `POST` to the demo's reset form (non-destructive — a refused
  code changes nothing).
* The shipped `installer/first_visit.py` as a real HTTP service on a scratch
  guest root (real state file, real 0600 `.env`, real page — the classic setup
  branch, because a scratch guest root has no seed): set a sign-in, lose it,
  present a wrong code, present the right one, then read what changed.

**What it measured.**

| Check | Result |
|---|---|
| the bench tenant's `/first-visit/reset` | `409` — no recovery hash: its first visit was never completed after the 21 November image |
| the bench tenant's bare `/first-visit` | `404` — the token is the whole address |
| the bench tenant's `/login`, `/verify` | `200`, `200`; the sign-in row reads `PASS` even though no user set a sign-in |
| the demo tenant's `/first-visit/reset` (sign-in set) | `200` |
| a wrong code on the live demo tenant | `200` with `That recovery code does not match.` |
| `POST /first-visit` (set the sign-in) | `200`, the code shown once; the old password verifies |
| `GET /first-visit/reset` | `200`, the form |
| a wrong code on the scratch guest | `200`, `refused: true`, `That recovery code does not match.`; the old password still works |
| the right code | `200`, `Your sign-in is changed.`; the new password verifies, the old does not |
| the session secret | replaced (32 fresh bytes) — every other browser is signed out |
| the gateway reload | invoked; the workspace answers `502` for a few seconds (3.6–4.2 s, `docs/evidence/m1-tenant.md` §10) |
| the old code after the reset | dead; the new code is live |
| `POST /first-visit/recovery` with the new code | `200`, a one-time `?d=` link; the link opens the file with the backup password and shows a third code |
| the same code again | refused with the same text |

**What it changed.** The 15 November runbook said a wrong recovery code answers
`404` and that a reset changes the `/verify` sign-in row; the drill measures
`200` + text, and a row that reads `PASS` before and after. It also measured that
the reset signs out other browsers and restarts the gateway, that a `409` on the
reset form is the "never signed in" branch, and that at that head the bare
`/first-visit` was `404` (ODL-925 moved it to the login page, `200`). The live
half is why §4.4's `?c=` URL is gone: the old permanent URL returns the form,
never the file. See §6.3.

### 6.3 Every line the drills changed

| # | Drill | v1.x said | The drill measured | v1.2 says |
|---|---|---|---|---|
| 1 | 6.1 | a failed build had no path: §4.1 restarts a machine, §4.6 reads a boot log | the claim worker terminates the machine, removes the DNS record and the queued files, leaves the row `claimed`, and writes one alert; the retry orders one machine | §0, §5.0, C-6 |
| 2 | 6.1 | a failed provision leaves a machine to find | `create → poll → terminate`, `0` live, no DNS record, no kit or ready email | the worker cleans up; the fix is a retry |
| 3 | 6.2 | a wrong recovery code answers `404` (15 Nov, the permanent `?c=` URL) | `200` with `That recovery code does not match.` — on the live demo and on the scratch guest | `200` + text; never test the status alone |
| 4 | 6.2 | the `/verify` sign-in row changes after a reset | it reads `PASS` before and after — it proves a hash is present | the exit is a login `200` |
| 5 | 6.2 | (unstated) a reset keeps other sessions | the secret is replaced, the other browsers are signed out, the gateway restarts (`502` for seconds) | §4.3 |
| 6 | 6.2 | `/first-visit` answers `200` while the form waits | the bare path is `404`; the ready link's token is the whole address | test the ready link; `409` on reset = never set |
| 7 | 6.2 | `/first-visit/recovery?c=<code>` downloads the file | `?c=` returns the form; the code is `POST`ed and the file is behind a one-time `?d=` link | §2.2 and §4.4 |
| 8 | 6.4.1 | HQ can run the tenant's restore with `--repo` and the backup key | `NXDOMAIN`; HQ's key is not accepted by the backup box | the rebuilt machine restores |
| 9 | 6.4.1 | `/health` is `200` on a live tenant | `302` to `/login` | `/verify` is the liveness page |
| 10 | 6.4.2 | the seed refusal exits 2 | exit 1 | exit 1 |
| 11 | 6.4.2 | `odilectl rotate-token --apply` | `NotWired`; `seed.py token rotate` works | `seed.py token rotate` |
| 12 | 6.4.2 | an expired token blocks the install | cache-first: a machine that already fetched never re-exchanges | the cache is a way out |
| 13 | 6.4.2 | `/manifest` is the manifest URL | `404`; `/manifest.json` is `200` | `/manifest.json` |
| 14 | 6.4.2 | `status --json` proves a slug exists | exits 0, `ok: true`, `machines: []` | read `count` |
| 15 | 6.4.2 | a stopped heartbeat means the machine is down | the demo tenant's heartbeat is 2 d 7 h stale while `/verify` answers `200` with `PASS — five checks`; the poll plans `down` and `backup` alerts (15 Nov 10:51:05Z) | `/verify` first, then the heartbeat job |

### 6.4 The earlier drills (15 November 2026)

Kept because §4.4 and §4.5 cite them. Their route measurements predate ODL-579
(19 November), which replaced the recovery file's permanent URL with a `POST` and
a one-time `?d=` link — row 3 of §6.3 is the current measurement.

#### 6.4.1 A lost key

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
| a mistyped code | `404` `The recovery file is not available on this machine.` (the permanent `?c=` URL; ODL-579 replaced it) |
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
that an HQ restore proves the file opens the repository. v1.1 said `404` (the
page never said which part was wrong) and named the restore as the rebuilt
machine's job. See §6.3, rows 3, 8 and 9.

#### 6.4.2 A failed seed

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
command is `seed.py token rotate` (the `odilectl` wrapper still raises
`NotWired`), and the cache-first rule is the fastest way out. See §6.3,
rows 10–13.

### 6.5 A live measurement beside the drills

On 15 November 2026 the poll's own view of the demo tenant was recorded: its
last heartbeat was `2026-11-13 03:01:14Z` (`odilectl health --json`:
`heartbeat_age_seconds: 200,991` when the poll ran at `10:51:05Z`,
classification `down`, brief missed), while `/verify` answered `200` with
`measurement 9f01725a…` and `PASS — five checks`. The poll planned a `down`
alert ("heartbeat stopped 2d 7h ago; port 22 answers host key …") and a `backup`
alert ("last backup 2d 7h ago (threshold 1d 12h)"). The machine was up; the
heartbeat job was what stopped — on the day the demo was reinstalled
(13 November). The public `/status` page counted it: `Downtime 3,421 min`
against `In-service time 2.4 machine-days`. That is the discriminator §4.1 puts
second, and it is why "down" in this page means *the heartbeat stopped*.

The defect was ODL-561's, and it landed on 15 November (`371d5d1`): on
22 November the demo's heartbeat is live — the poll at `00:41:37Z` read a last
beat of `2026-11-22 00:40:46Z`, 51 seconds earlier — and `health` classifies it
`ok`. The same poll now plans one `brief_missing` alert —
"the 07:00 brief did not arrive; heartbeats are live" — which is the brief job,
not the machine, and is the live counterpart of §4.1's second command.

## 7. Limits

1. **The re-cut drills are not a live failed tenant.** Drill 1 runs the
   shipping claim spine against the fixtures the shipping suite uses — no vendor
   order was placed, so "billed" means the provider's own live set, and the
   terminate call is what empties it. Drill 2's live half is the bench and demo
   tenants' endpoints; its reset half is the shipped first-visit service on a scratch
   guest root, because the bench tenant's first visit was never completed after
   its 21 November image (`/first-visit/reset` `409`) and there is no sign-in
   there to lose. Neither drill powers off a tenant; the live stopped-VM check is
   ODL-332's.
2. **`waiting_for_owner` is legacy.** The state remains in the machine; the
   poll no longer parks a tenant there (`odilectl/health.py`, ODL-467). §4.2 is
   about the sign-in, not about a parked boot.
3. **The `spend` alert is dormant** until a heartbeat carries `facts.spend`;
   ODL-242 owns the per-user key and the zero-balance message. §4.8 says what
   to read instead. C-8 still quotes the `$12` allowance row, which
   `CLAIMS.md` §1a holds and alpha.0's envelope replaces with a ten-dollar cap
   (Savio, 20 November, D2); ODL-577 lands the copy.
4. **A grant is root, and the log is on the machine.** A root shell can edit
   `authorized_keys` and the log; the five post-revoke checks are how the user
   sees what it left (`CLAIMS.md` A10).
5. **The provider console is a door we do not control.** It shows the boot and
   can restart or power off; it is not a shell and it does not decrypt anything
   (`CLAIMS.md` §3, gap 2).
6. **The demo tenant is now the counter-example that closed.** Its heartbeat
   stopped on 13 November while the machine served `/verify` (§6.5); ODL-561
   (`371d5d1`) restored the timers and took the demo off `/status` on
   15 November. On 22 November its heartbeat is live — the `00:41:37Z` poll read
   a last beat of `2026-11-22 00:40:46Z`, 51 seconds earlier — and `health`
   classifies it `ok`. The live defect the same poll now plans is a
   `brief_missing` alert — "the 07:00 brief did not arrive; heartbeats are
   live" — and the alert's `first_command` is still `ssh -i ~/.ssh/odile …`,
   the key that does not exist (`odilectl/health.py`).
7. **The bench's account site was down for the first half of the A2 window.**
   `odile-bench.service` had restarted 17,060 times by 22 November 00:26Z
   because an orphaned `seed.py serve` from a deleted worktree (started
   21 November 11:39) held `127.0.0.1:8082`; `https://test.odile.fyi/` answered
   `404` while `/health` answered `200`. The orphan was killed and the unit
   restarted; at `00:41:37Z` `check_bench.sh` read 14/16, and both failures were
   the tenant's recorded measurement, not the machine — the 21 November image was
   serving `2caf4a5b…` while `docs/evidence/a1-bench-baseline.json` still
   recorded `674cc40d…`. ODL-936 (`t_921b50d0`, A2/infra/S) re-recorded the
   baseline at `2026-11-22T00:48:56Z` (`expected_measurement 2caf4a5b…`), and
   `check_bench.sh` passes 16/16 (re-run at `02:22:45Z`). ODL-936 owns the
   probe, the timer, the re-record and the orphan rule. The repair, if the
   orphan recurs: `ss -ltnp | grep ':8082'` names the holder, kill that pid,
   then `systemctl restart odile-bench.service`. Its `/verify` also carries a
   `LUKS FAIL` row — the unit's `ProtectSystem=strict` blocks `cryptsetup`'s
   lock on every machine; `t_d55b0e47` owns that, and the machine is serving.
