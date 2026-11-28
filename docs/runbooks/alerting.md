# Alerting — the heartbeat, the poll and the down alert

**Status:** v0.1, fixture-proven; the live stopped-VM check is ODL-332's evidence.
**Card:** ODL-216, re-cut ODL-467; the demo's two timers are ODL-561. **Code:**
`scripts/heartbeat.py`, `odilectl/health.py`, `deploy/demo-host/check_demo_host.sh`.
**Tests:** `tests/test_heartbeat.py` (48 tests, hermetic), `tests/test_status.py`.

A tenant that goes quiet must be understood before its owner notices, and a
stopped heartbeat must be named as exactly that — downtime, whatever port 22
answers. This runbook covers the two halves:

* the guest's **signed heartbeat** (`collect` → `sign` → `post`), received by
  `scripts/heartbeat.py serve`;
* HQ's **five-minute poll** (`odilectl health --apply`), which reads the
  heartbeat, polls each tenant's `/health`, reads port 22's host key, classifies
  a stopped heartbeat as `down`, and sends the alerts.

The two sentences that cover a stop are from `docs/CLAIMS.md` (A5, A11):

> After a reboot, it starts itself. The seal is the key.
>
> A repeating job — the morning brief, a watchdog — runs when the machine comes back up if it was due while it was down.

## 1. The loop

```
guest  scripts/heartbeat.py collect   versions, disk, uptime,
       scripts/heartbeat.py sign      last backup, last brief
       scripts/heartbeat.py post  ──▶  POST https://odile.fyi/heartbeat
                                          │ Ed25519 over the exact body
                                          ▼
HQ     scripts/heartbeat.py serve        heartbeat_keys + heartbeats
                                          │
       odilectl health --apply  ◀─────────┘   every 5 minutes
         ├─ GET https://<slug>.odile.fyi/health
         ├─ port 22: read the host key (no session, no login)
         ├─ heartbeat age ≥ 600 s → down (the port-22 answer is the symptom)
         └─ Matrix + email + outbox
```

The receiver holds no credential: the signature is the authentication. The
public half rides the first signed heartbeat and is registered then (trust on
first use). A later heartbeat that offers a different key is a `409`, never a
quiet re-registration. `at` must move forward, so a captured heartbeat cannot be
replayed.

## 2. Thresholds and what each alert means

| Alert | Fires when | Symptom the alert carries | First command |
|---|---|---|---|
| `down` | heartbeat age ≥ 600 s | "heartbeat stopped 10m 0s ago; port 22 answers host key SHA256:… / port 22 did not answer (…); brief missed: yes\|no" | `python3 -m odilectl status <slug> --json` |
| `brief_missing` | the 07:00 brief is overdue while heartbeats are live | "the 07:00 brief did not arrive; heartbeats are live" | `ssh -i ~/.ssh/odile … 'hermes cron list'` |
| `disk` | latest heartbeat reports disk > 80% | "disk 91.0% used (threshold 80%)" | `ssh -i ~/.ssh/odile … 'df -h /'` |
| `spend` | a heartbeat reports `facts.spend` above 80% of the `cap_usd` it names | "spend $95.00 of $100.00 cap (95.0%)" | `ssh -i ~/.ssh/odile … 'journalctl -u odile-agent -n 50 --no-pager'` |
| `backup` | last successful backup > 36 h ago | "last backup 1d 16h ago (threshold 1d 12h)" | `ssh -i ~/.ssh/odile … 'cat /var/lib/odile/backup.json'` |
| `backup_usage` | the box's `stats.size_data` + `stats.size_snapshots` ≥ 800 GiB (858,993,459,200 bytes) | "the backup box 709870 odile-backups uses 800.0 GiB (78.1% of 1.0 TiB); 2 of 100 sub-accounts; readback 0m 0s ago; 800.0 GiB warn line reached" | `python3 -m odilectl backup-usage --apply --json` |
| `backup_usage_critical` | the same total ≥ 950 GiB (1,020,054,732,800 bytes) | "the backup box 709870 odile-backups uses 950.0 GiB (92.8% of 1.0 TiB); …; 950.0 GiB critical line reached" | `python3 -m odilectl backup-usage --apply --json` |

`backup_usage` and `backup_usage_critical` are box-level, not a tenant's: the
ledger slug is `backup-box-<id>` (`backup-box-709870` for the BX11), no tenant
is named, and `status/status.py`'s `INCIDENT_KINDS` is `('down',)`, so neither
reaches the public status page. Both are inclusive — `>=` 800 GiB, `>=` 950
GiB — while disk, spend and backup stay strict `>`: a ceiling is a place the
bytes reach, an age is a duration that has passed. Their input is the record
`odilectl backup-usage --apply` writes, run daily; the poll reads whatever the
last run left and the symptom carries the readback's age. A promotion is a new
row and a new delivery: the critical kind opens while the warn row is resolved,
so an operator who saw the warn also gets the critical.

`waiting_for_owner` and `unlock_email` are retired (ODL-467). Under the sealed
boot a reboot comes back by itself, so a stopped heartbeat is `down`; the
replacement for both rows is the `down` alert.

Every alert names the **tenant**, the **slug**, the **symptom** and the **first
command**. Disk, spend and backup thresholds are strict `>`: exactly 80% disk,
exactly 80% of the `cap_usd` a heartbeat names and exactly 36 h since the last
backup do not alert. The heartbeat threshold is inclusive: a heartbeat age of
exactly 600 s raises `down`, because "within 10 minutes" means 600 s is already
ten minutes.

The poll classifies on heartbeat age alone; port 22 is a symptom. A missed 07:00
brief is folded into the `down` symptom when the heartbeat is stopped, and it is
its own `brief_missing` alert only while heartbeats are live.

## 3. The unlock email is retired

No owner email is sent at all. The replacement for ODL-216's `unlock_email` row
is the single `down` alert: the operator gets it at the configured
`alert_email` and Matrix room, and the outbox always keeps a copy.

ODL-216's email named `ssh -i ~/.ssh/odile -o IdentitiesOnly=yes root@<ip> unlock`;
that command no longer exists under the sealed boot.

## 4. Configuration (HQ only, mode 0600)

`/root/.odile/config.yaml` is the operator's file. Nothing here belongs on a
tenant machine, and no secret is in git.

| Key | Meaning |
|---|---|
| `heartbeat_db` | heartbeat + alert ledger; defaults to the same file as `state_db` (`/var/lib/odile/waitlist.db`) |
| `alert_outbox` | local copy of every alert; default `/var/lib/odile/outbox` |
| `alert_email` | the operator's alert address (not the owner) |
| `alert_transports` | comma list: `smtp`, `matrix` (the outbox is always written) |
| `smtp_host`, `smtp_port`, `smtp_from`, `smtp_username`, `smtp_password_file`, `smtp_ca_file`, `smtp_starttls` | the Odile mail relay; `smtp_password_file` and `smtp_ca_file` are read at run time |
| `matrix_homeserver`, `matrix_room_id`, `matrix_token_file` | Savio's Matrix room; the token file is read at run time |
| `tenant_domain` | default `odile.fyi`; the `/health` URL is `https://{slug}.<tenant_domain>/health` |
| `tenant_health_url` | override the whole `/health` URL template |

The outbox is a transport, not a debugging leftover: it is what the fixture
evidence reads, and it keeps the message when SMTP or Matrix is down. A
delivery failure is written into the alert's `deliveries` array and retried on
the next poll; `odilectl health --json` prints it under `delivery_errors`.

Example (test relay; the real secret is read from `/root/.odile/` at run time):

```
alert_email: invite@mail.odile.fyi
alert_transports: smtp
smtp_host: mail.odile.fyi
smtp_port: 587
smtp_from: Odile <invite@mail.odile.fyi>
smtp_username: odile-test
smtp_password_file: /root/.odile/mail-test-secret
smtp_ca_file: /etc/ssl/certs/mail.odile.fyi.crt
```

## 5. Running it

### The receiver

```sh
# one-time, on HQ
/usr/bin/python3 scripts/heartbeat.py serve --db /var/lib/odile/waitlist.db --port 8099
```

It binds loopback. Caddy terminates TLS and proxies the one path; merge this
into the existing `odile.fyi` block and `systemctl restart caddy` (the service
runs with `admin off`, so there is no hot reload):

```
@odile_heartbeat path /heartbeat
handle @odile_heartbeat {
    reverse_proxy 127.0.0.1:8099
}
@odile_heartbeat_health path /heartbeat/health
handle @odile_heartbeat_health {
    rewrite * /health
    reverse_proxy 127.0.0.1:8099
}
```

`GET https://odile.fyi/heartbeat/health` is the receiver's own liveness line.
A systemd unit (`/etc/systemd/system/odile-heartbeat.service`):

```ini
[Unit]
Description=Odile signed-heartbeat receiver
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/odile
ExecStart=/usr/bin/python3 /opt/odile/scripts/heartbeat.py serve --db /var/lib/odile/waitlist.db --port 8099
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### The guest job

At install, generate the pair once; the private half never leaves the machine:

```sh
scripts/heartbeat.py keygen --out /etc/odile/heartbeat
```

Then the five-minute job (the 07:00 run is the daily one; `--brief-status` is
written by the brief sender):

```sh
/usr/bin/python3 /opt/odile/scripts/heartbeat.py collect \
    --slug "$(hostname)" \
    --backup-status /var/lib/odile/backup.json \
    --brief-status /var/lib/odile/brief.json \
    --public-key /etc/odile/heartbeat/heartbeat.pub \
  | /usr/bin/python3 /opt/odile/scripts/heartbeat.py post \
    --url https://odile.fyi/heartbeat \
    --key /etc/odile/heartbeat/heartbeat.key
```

`collect` reads disk from `shutil.disk_usage`, uptime from `/proc/uptime`, and
the last backup / brief timestamps from the two status files. It writes no
secret and sends no prompt text. It sends no spend fact: there is no local meter
or monthly cap, and the poll's `spend` alert reads `facts.spend` only from a
heartbeat that carries one.

### The poll

```sh
/usr/bin/python3 -m odilectl health --apply --json
```

Without `--apply` it is a dry run: it prints what it would raise and send, and
writes nothing. `--json` is the machine view; the human lines name each tenant
and alert. `--backup-usage PATH` names the box readback the poll reads
(default `/var/lib/odile/backup/usage.json`); its units are bytes, and its two
ceilings are 800 GiB = 858,993,459,200 bytes and 950 GiB = 1,020,054,732,800
bytes, inclusive. A record that is missing or malformed is printed by name and
reason (`backup usage: no readback at <path>`) and plans no box alert — never a
green box. The readback is a separate daily command:
`python3 -m odilectl backup-usage --apply --json`. The poll's systemd timer
every five minutes:

```ini
# /etc/systemd/system/odile-health.service
[Unit]
Description=Odile tenant health poll

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/odile
ExecStart=/usr/bin/python3 -m odilectl health --apply --json

# /etc/systemd/system/odile-health.timer
[Unit]
Description=Poll tenant health every five minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

## 6. The demo machine — what a reinstall must put back

Odile's own demo (the judge path, `demo.odile.fyi` → `odl-fake01`) runs the
same signed heartbeat and nightly backup as a tenant, but it is not a tenant:
its password is published, the judge can stop or break it, and it is **excluded
from `/status`** (`status/status.py`'s `DEMO_SLUGS`).  The page counts tenant
machines only; a silent demo must never become Odile's published downtime.

A reinstall puts the image back, and the image does not carry the two timers.
Measured 2026-11-15: the demo's last heartbeat was `2026-11-13T03:01:14Z`, the
day it was reinstalled, `/var/lib/odile/backup.json` was gone, and
`odilectl health --json` planned a `down` and a `backup` alert for a machine
that was up (`/verify` answered 200).  The machine's own check is the seed, so
the next rebuild cannot lose them:

```sh
# from the repo root on HQ; the judge password is read at run time
DEMO_PASSWORD_FILE=/root/.odile/judge-pass.txt \
    ./deploy/demo-host/check_demo_host.sh --tenant
```

The `--tenant` drive uploads `scripts/backup.py` with the check, then runs
`--local` on the machine.  That half is idempotent and prints `same` on a
converged machine:

| Step | What it writes |
|---|---|
| the program | `/usr/local/lib/odile/backup.py` (0755 root, the checkout's `scripts/backup.py`) |
| the repository | `/etc/odile/backup-repo.txt` — the seed's URL when its host resolves, otherwise `sftp://odile-backup@odile.fyi:2222/tenants/odl-fake01` (the storage box today is HQ's own sftp service, `docs/runbooks/backup.md` §3) |
| the SFTP key | `/etc/odile/backup-ssh/id_ed25519` (0600) and `known_hosts` (0600). On this demo machine the check generates the key with `ssh-keygen` and records the host key with `ssh-keyscan -H`; on a tenant the installer writes both from the seed's pin and the machine generates no key (ODL-924/933) |
| the heartbeat key | `/etc/odile/heartbeat/heartbeat.key` (0600) and `.pub` |
| the backup timer | `odile-backup-tenant.service` + `.timer` from the program's own templates (`OnCalendar=*-*-* <HH:MM>:00` at the slug's own `02:00 + int(sha256(slug)[:2], 16) % 240`, `RandomizedDelaySec=0`, `Persistent=true`) |
| the heartbeat timer | `odile-heartbeat.service` + `.timer` (`OnCalendar=*:0/5`, `Persistent=true`), running `backup.py heartbeat` |

The HQ half then finishes what only HQ can:

1. **The SFTP key.**  A rebuilt machine makes a new key; the backup box refuses
   it until its public half is in `/srv/odile-backup/.ssh/authorized_keys`.
   The check appends it (idempotent; `ODILE_BACKUP_AUTHORIZED_KEYS` overrides
   the path).
2. **The heartbeat key.**  The receiver is trust-on-first-use and refuses a
   different key with **409** (`odilectl/health.py` `record`).  A rebuild
   therefore leaves a stale registration: the check compares the machine's
   `heartbeat.pub` with `heartbeat_keys.public_key` for the slug and, when they
   differ, deletes the row so the first signed beat registers the new key.  This
   is the runbook's §10 revoke, done automatically and only on a mismatch.
3. **One backup.**  When the last success is older than twelve hours the check
   starts `odile-backup-tenant.service` (through a transient unit, so the
   Webapp's 30-second command ceiling cannot cut it) and waits for a fresh
   `last_success_at` and snapshot in `/var/lib/odile/backup.json`.  The wait is
   `BACKUP_WAIT_SECONDS` (default 900 s) because a first backup of a full home
   is a minutes-scale job; the demo's first successful snapshot (2026-11-15,
   47 MB / 635 files) took 1.5 s of restic time, but only after the repository
   answered.  If the budget ends with the unit still running, the check prints a
   `note` — the timer finishes it and the next run reads the record — but if
   the unit is `failed` or `inactive`, that is a `FAIL` carrying the unit's
   state and the last journal lines.

   The old 300 s wait reported a timeout for the 2026-11-15 seed, which had
   actually failed in under a second: the journal shows `Starting
   odile-backup-tenant.service` and `Fatal: wrong password or no key found` in
   the same second, and the successful run after the repository was repaired
   came 18m42s after the first start.  The unit's state and journal are now
   what the check reports, so the cause can never again be a timeout.
4. **The beat.**  It starts `odile-heartbeat.service` and reads `MAX(at)` for
   the slug out of HQ's own `heartbeats` table; a beat newer than ten minutes
   is the line `HQ holds a signed heartbeat for odl-fake01 at <epoch>`.

If the backup fails with `wrong password or no key found`, the machine's
`/etc/odile/recovery.txt` and the repository disagree: the file's password does
not open `tenants/<slug>`. Two live causes — a password rotated on the
repository (`restic key add` / `key remove`, `docs/runbooks/backup.md` §7)
without the machine's file re-rendered, and a machine installed as a **fresh**
install onto a slug whose repository already exists (its seed names no
`previous_instance`, so the installer mints a new password into the file). A
rebuild's paste is the third: the file is written before restic reads it, so a
well-formed file whose password does not open this repository leaves the
password in place, `wrong_password` in the restore state and the backup timer
stopped (`docs/runbooks/backup.md` §2.2) — the timer is enabled, so the next
boot runs the unit against the same wrong file. Measured on the demo,
2026-11-15, on the sealed rebuild path ODL-937 retired (the rebuilt machine
minted its own password): the machine's recovery file
(`2026-11-13T10:29:12Z`) did not open the repository created
`2026-11-12T19:01Z`. The check reports the failure and
the remedy — the `FAIL` line carries the error from `backup.json`, or, when the
unit died before writing one, `systemctl show`'s state and the journal tail —
and doing it keeps the bytes:

```sh
# HQ, root: keep the unreadable repository, then let the machine make a new one
mv /srv/odile-backup/tenants/odl-fake01 \
   /srv/odile-backup/tenants/odl-fake01.unreadable-$(date -u +%Y-%m-%d)
install -d -m 0700 -o odile-backup -g odile-backup \
    /srv/odile-backup/tenants/odl-fake01

# on the demo machine, as root (serial console or the tenant console)
/usr/bin/python3 /usr/local/lib/odile/backup.py init \
    --repo 'sftp://odile-backup@odile.fyi:2222/tenants/odl-fake01' \
    --recovery /etc/odile/recovery.txt \
    --key /etc/odile/backup-ssh/id_ed25519 \
    --known-hosts /etc/odile/backup-ssh/known_hosts
```

Then re-run the check's HQ half (a second `--tenant` run) and the fresh
repository is backed up against the recovery file the machine actually holds.
The root cause is a file and a repository that were not written together; a
rebuild keeps them together with the first-visit paste
(`docs/runbooks/backup.md` §2.2), which replaced ODL-215's sealed path, and
this check only reports the mismatch.

A machine that is already converged prints `same` for both keys and skips the
backup inside twelve hours.  `--local` (serial console, root on the machine)
does the machine half only and prints the two public keys; without
`--tenant` the two HQ lines above are the operator's.

After a seed, the two checks the card asks for are:

```sh
/usr/bin/python3 -m odilectl health --json | grep -A2 '"slug": "odl-fake01"'
/usr/bin/python3 status/status.py --db /var/lib/odile/waitlist.db --json
```

`health --json` plans no `down` and no `backup` alert for the demo, and
`status/status.py` reports `"excluded_tenants": 1` with no demo downtime.

## 7. Reading and testing the ledger

The alert ledger lives beside the state machine. One row per open condition;
`resolved_at` is set when the condition clears, never deleted:

```sh
sqlite3 -header -column /var/lib/odile/waitlist.db \
  "SELECT slug, kind, datetime(first_seen_at,'unixepoch') AS first_seen,
          datetime(resolved_at,'unixepoch') AS resolved, tenant
     FROM health_alerts ORDER BY first_seen_at, slug, kind"
```

The local copy of every message is `/var/lib/odile/outbox/alerts.jsonl` (one
JSON line per delivery, with `at`, `kind`, `slug`, `channel`, `recipient`,
`subject` and `body`) plus one `.eml` per email.

The hermetic test module reproduces the card's evidence without a tenant machine:

```sh
/usr/bin/python3 -m unittest tests.test_heartbeat -v
```

* the fixture heartbeat stop at exactly 600 s raises `down`, never
  `waiting_for_owner`, and writes no unlock email; a 90 s reboot between beats
  raises nothing;
* port 22's answer — the recorded key, a different key, or no answer — is a
  symptom of `down`, never a second classification;
* a missed 07:00 brief folded into `down` when the heartbeat is stopped, and its
  own alert while live;
* disk, spend and backup thresholds, and the exactly-at-threshold no-alert;
* the poll's idempotency, the returning heartbeat's resolve, and the dry run
  that writes nothing;
* the real receiver over a socket, the SMTP transport against a fake server,
  the Matrix transport against a fake homeserver, and the SSH probe against a
  fake SSH server.

A manual fixture replay (the shape used for the card's evidence) is:

```sh
# 1. one tenant row at ready, a recorded initramfs key, and a beat at T
# 2. start the receiver, post the beat, then run the poll at T+600:
/usr/bin/python3 -m odilectl health --apply --json \
  --db /tmp/odile-fixture.db --heartbeat-db /tmp/odile-fixture.db \
  --outbox /tmp/odile-outbox \
  --recorded-host-keys /tmp/odile-recorded-keys.txt \
  --no-http --now 1794039000
```

`--recorded-host-keys` takes `ssh-keyscan`-shaped lines (`host keytype base64`)
and the line `host unreachable`; it is how a stopped machine is replayed
without a stopped VM.

## 8. What to do with an alert

| Alert | Operator action |
|---|---|
| `down` | `python3 -m odilectl status <slug> --json`, then the provider panel. No operator key opens it; a support grant needs the user. |
| `brief_missing` | The machine is up; the 07:00 job did not run. `ssh -i ~/.ssh/odile … 'hermes cron list'` and the gateway journal. |
| `disk` | `df -h /` on the guest; the usual cause is an agent loop writing files. Delete with the owner's approval; a full disk stops the brief. |
| `spend` | `journalctl -u odile-agent -n 50` on the guest; the alert carries the `spent_usd` and `cap_usd` the heartbeat reported. There is no local meter or monthly cap to read; this alert is the warning. |
| `backup` | `cat /var/lib/odile/backup.json`; `systemctl list-timers odile-backup-tenant.timer` names the next elapse — once a night in the 02:00–06:00 UTC window, at the slug's own minute. A stale backup is the one alert with no in-guest recovery; ODL-215 owns the drill. |

Alerts clear on their own when the condition is gone; the next poll sets
`resolved_at`. `down` resolves when a heartbeat newer than the stopping beat
arrives; a legacy open waiting_for_owner row is resolved by the same poll and
never re-raised.

## 9. Limits

1. **The heartbeat proves liveness, not honesty.** It is signed by the key the
   guest generated at install; the first heartbeat is trust-on-first-use. TLS
   to HQ protects that first post; a hostile first post could register a
   different key. alpha.0 accepts this, and the tenant's own host key /
   `/verify` is the independent check.
2. **The host-key probe reads identity, not a signature.** It returns the key
   the machine presents; the recorded initramfs fingerprint is printed beside it
   by `odilectl status` for comparison. It proves "this is the key the machine
   presents", not "this machine is honest"; since ODL-467 it is a symptom line,
   not the classification.
3. **`/health` is an HTTP liveness check**, not attestation. It is one input to
   the alert; the heartbeat age decides the classification and the host key is
   part of the symptom.
4. **The live stopped-VM check is ODL-332's evidence.** This card's stopped
   tenant is a fixture; the port-22 probe itself is exercised live against
   Odile's own SSH host (`<server:hq>`) and against a fake SSH server.
5. **No operator shell.** The probe is a bare key exchange: no session, no
   authentication, no `ssh-keyscan` process. The only shell in the card is the
   first command text in the operator's alert.
6. **Matrix and email are best effort.** A failed delivery is recorded and
   retried every five minutes; the outbox always has the message. The mail
   relay's certificate is pinned by `smtp_ca_file` when it is self-signed.

## 10. Rollback

* Stop and disable `odile-health.timer` and `odile-heartbeat.service`; alerts
  stop and no state changes.
* `odilectl health` without `--apply` is read-only; run it in dry-run mode to
  inspect what the poll would do.
* The heartbeat and alert tables are additive (`CREATE TABLE IF NOT EXISTS`);
  dropping `heartbeats`, `heartbeat_keys` and `health_alerts` loses only the
  heartbeat history and alert ledger, never the tenant spine or the waitlist.
* Revoking one tenant's heartbeat key is `DELETE FROM heartbeat_keys WHERE
  slug = ?` followed by that tenant's next heartbeat, which registers the new
  key (the old beats stay for audit).
