# Alerting — the heartbeat, the poll and the down alert

**Status:** v0.1, fixture-proven; the live stopped-VM check is ODL-332's evidence.
**Card:** ODL-216, re-cut ODL-467. **Code:** `scripts/heartbeat.py`, `odilectl/health.py`.
**Tests:** `tests/test_heartbeat.py` (48 tests, hermetic).

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
and alert. A systemd timer every five minutes:

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

## 6. Reading and testing the ledger

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

## 7. What to do with an alert

| Alert | Operator action |
|---|---|
| `down` | `python3 -m odilectl status <slug> --json`, then the provider panel. No operator key opens it; a support grant needs the user. |
| `brief_missing` | The machine is up; the 07:00 job did not run. `ssh -i ~/.ssh/odile … 'hermes cron list'` and the gateway journal. |
| `disk` | `df -h /` on the guest; the usual cause is an agent loop writing files. Delete with the owner's approval; a full disk stops the brief. |
| `spend` | `journalctl -u odile-agent -n 50` on the guest; the alert carries the `spent_usd` and `cap_usd` the heartbeat reported. There is no local meter or monthly cap to read; this alert is the warning. |
| `backup` | `cat /var/lib/odile/backup.json`; restic runs at 03:00 local. A stale backup is the one alert with no in-guest recovery; ODL-215 owns the drill. |

Alerts clear on their own when the condition is gone; the next poll sets
`resolved_at`. `down` resolves when a heartbeat newer than the stopping beat
arrives; a legacy open waiting_for_owner row is resolved by the same poll and
never re-raised.

## 8. Limits

1. **The heartbeat proves liveness, not honesty.** It is signed by the key the
   guest generated at install; the first heartbeat is trust-on-first-use. TLS
   to HQ protects that first post; a hostile first post could register a
   different key. V0.1 accepts this, and the tenant's own host key / `/verify`
   is the independent check.
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

## 9. Rollback

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
