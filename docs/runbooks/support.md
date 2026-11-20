# Support — a diagnostics bundle by default, a grant by choice

Odile holds no standing key to a tenant machine. The default is no access: the
user runs `odile-diag`, which bundles versions, disk usage, services, recent
logs and the machine's own access report into one file. If that is not enough,
`odile-support grant --hours 24` installs Odile's support public key from the
signed seed behind a forced command, for a fixed number of hours. The user
starts it; `revoke` ends it; a timer ends it at the expiry even if nobody is
looking.

`support/odile_support.py` is both programs. `odile-diag` is the same file
invoked under its own name. **The installer places both** (ODL-474): the file
at `/usr/local/sbin/odile-support` (0755 root) and `odile-diag` linked beside
it at `/usr/local/bin/odile-diag`. ODL-217 shipped the program and proved it on
the tenant by placing it by hand; the embedding, the two names and the release
cut are ODL-474's.

**The one fact that is easy to get wrong: a reboot does not close a grant, but
the bytes in `authorized_keys` do not survive one by themselves.** The tenant
image runs the release installer on every boot (`odile-release-setup.service`),
and its `ensure_verify_surface` writes exactly one line — the measured boot key
— to root's `authorized_keys`. A grant line written before the reboot is
deleted by that rewrite. What makes a grant live is the state file
(`/var/lib/odile/support-grant.json`) and its expiry, and the forced command
reads that state on every connection. So the grant's own timer converges in
both directions: a live grant whose line a boot removed gets the identical line
put back (within 30 seconds), and a due grant loses its line and gets the
`expired_at` tombstone. Expiry and revoke are the closures; a reboot is not.
An open SSH session also survives both closures until it exits. This file states
that, the grant card states it, and §8's drill measures both halves.

---

## 1. The default: no grant

```sh
sudo odile-support status
```

```
no grant
```

The independent checks agree:

```sh
sudo grep -c odile-support /root/.ssh/authorized_keys   # 0
sudo w                                                   # no support session
curl -sS https://<slug>.odile.fyi/verify | grep "operator key"
```

`<slug>` is the machine's own name (`odl-7971a8` in the drill), and that page
is fed by `/var/lib/odile/access-report.txt`; its `operator key` row reads
`PASS none (authorized_keys: 1 line(s), the measured throwaway)`. The page at
`https://odile.fyi/verify` is not that page: HQ answers **302** to
`https://demo.odile.fyi/verify`, the demo tenant's own page, where the
sentence, the report and the access report are one machine. HQ's own report is
the JSON at `https://odile.fyi/attestation`.

`status` never creates the state, the log or the timer. As root it converges
first, so a grant whose timer never started still closes — or is restored — the
next time anyone looks.

---

## 2. `odile-diag` — the bundle the user emails

```sh
sudo odile-diag
```

```
wrote /var/lib/odile/diag-20261112T212718Z.txt (47651 bytes)
Send this file to help@odile.fyi. It holds no password, key, prompt history or file contents.
```

`--out PATH` writes elsewhere; `--out -` writes to stdout. The file is 0644 so
the user can attach it to an email from the workspace.

It collects, in six marked sections:

| Section | What is in it |
|---|---|
| `system` | `/etc/os-release`, `uname -a`, uptime, CPU count, memory, `python3`/`openssl`/`ssh` versions, `/etc/odile/production`, the seal id, the cached seed's **public** fields, the boot record's release/measurement/cmdline hash/installer hash, the placed installer's SHA-256, the signed manifest's SHA-256 and size |
| `disk` | `df -hT`, `lsblk`, `cryptsetup status root_crypt`, `du -sh /var/lib/odile /var/log` |
| `services` | `systemctl --failed`, `systemctl is-active` and `status --lines=0` for the Odile units and Caddy, the grant timer's next elapse |
| `logs` | the last 200 journal lines for the Odile units, and the tail of `/var/log/odile-support.log` |
| `audit` | `/var/lib/odile/access-report.txt` verbatim, and the support key's fingerprint |
| `support` | `odile-support status` and the log path |
| `redaction` | how many replacements the redaction pass made |

It never reads:

- `/etc/odile/seed.json` as a whole (only `slug`, `hermes_version` and
  `backup_repo_url` are printed; the NEAR key is not);
- `/etc/odile/key.env`, `/etc/odile/recovery.txt`, `/etc/odile/seed.sig`;
- any private key, any `.env`, any file under a home directory;
- the agent's sessions, memory or prompt history;
- any user file. There is no `cat` of a workspace file anywhere in the program.

Every collected line still passes through a redaction pass: a private-key block
becomes `[redacted private key]`; a `PASSWORD=`, `TOKEN=`, `API_KEY=`,
`SECRET=`, `AUTH…=` or `Authorization:` value becomes `[redacted]`; a JSON
`"password": "…"` value does too; `Bearer …`, `sk-…`, `ghp_…` and `scrypt$…`
values are replaced. A second pass catches a match the first pass exposed. The
bundle's `redaction` section states how many replacements ran. The measurement,
fingerprints and seal id are **not** redacted: they are public by design, and
they are the numbers support needs.

The test suite plants a NEAR key, the machine's `/etc/odile/key.env`, a backup
password, a sign-in hash, a prompt-history line, a workspace file, a bearer
token, a journal token, a log password and a private-key block into a throwaway
root, runs `diag`, and asserts that none of them survives
([`tests/test_support.py`](../../tests/test_support.py), `DiagTests`). A
stranger can rerun that proof with:

```sh
/usr/bin/python3 -m unittest tests.test_support -v
```

The live proof is in §8.4: two bundles from the tenant, one of them over a
support log into which a password, a token, a bearer value and a private-key
block had been planted, and 14 absence/marker checks that pass.

---

## 3. `odile-support` — status, grant, revoke

| Command | What it does | Exit |
|---|---|---|
| `sudo odile-support status` | prints `no grant`, `no grant (expired …)`, `no grant (revoked …)` or the live grant with its reason, expiry, key fingerprint and last session | 0 |
| `sudo odile-support status --json` | the same as one JSON object (`grant`, `reason`, `restored_at`, `expired_at`, `revoked_at`, `authorized_keys`) | 0 |
| `sudo odile-support grant --hours 24` | installs the support key for 24 hours (default 24; fractional hours work; maximum 720) | 0 |
| `sudo odile-support grant --hours 24 --reason "…"` | the same, recording why; the reason is in the state, the log and the card | 0 |
| `sudo odile-support grant --hours 24 --dry-run` | prints the line, state and timer it would write; writes nothing | 0 |
| `sudo odile-support revoke` | removes the key line now and writes the revoked tombstone | 0 |
| `sudo odile-support revoke --dry-run` | prints how many lines it would remove | 0 |
| `sudo odile-support expire` | a due grant: removes the key; a live grant: puts back a line a boot removed (the timer runs this; `--quiet` for cron) | 0 |
| `odile-support session` | the forced command sshd runs; never typed by a person | the shell's exit, or 1 when refused |

`grant` refuses to write anything it cannot make safe:

| Exit | Meaning |
|---|---|
| 2 | `--hours` is not between 0 and 720 (exclusive of 0) |
| 3 | no usable support public key at `/etc/odile/support-key.pub`, or `/usr/local/sbin/odile-support` is missing/not executable |
| 4 | not root |
| 5 | systemd is running but the timer could not be written or enabled; the grant is rolled back |
| 1 | any other runtime refusal |

Every mutation is idempotent. A second `grant` replaces the one line and
refreshes the expiry (`grant live (refreshed)`); a second `revoke` prints
`no grant`; `expire` with nothing due prints nothing and exits 0.

`status` runs as the tenant user too, but a non-root run cannot read root's
`authorized_keys` and says `key line: unreadable without root` instead of
claiming the key is missing.

---

## 4. What a grant actually writes

`grant` reads **one** key from `/etc/odile/support-key.pub`. That file is
written by the installer from the signed seed (`ensure_pin_files`), mode 0644,
and holds the public half only. The parser accepts one bare key line, with the
seed's comment (parentheses and spaces are fine), and refuses a private key, a
second line, a `command="…"` prefix, or a blob whose wire type does not match.

It appends exactly one line to root's `authorized_keys`, mode 0600, preserving
every existing line byte-for-byte (the measured throwaway boot key):

```
command="/usr/local/sbin/odile-support session",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ssh-ed25519 AAAA… odile-support-grant-<12 hex>
```

- `command=` pins the key to the session wrapper. The key cannot open a
  tunnel, forward an agent, run an X11 channel or a user rc, and it cannot run
  an arbitrary command.
- `no-pty` is deliberately **absent**: the grant is an interactive shell.
- The comment `odile-support-grant-<id>` is the marker `revoke` and `expire`
  remove. A hand-written line that merely mentions the name is left alone.
- The wrapper the forced command runs is found by absolute path; on a tenant
  the command is exactly `/usr/local/sbin/odile-support session`.

The state file `/var/lib/odile/support-grant.json` (0644, no secret) holds the
grant id, slug, reason, created/expires timestamps, hours, public key blob,
fingerprint and comment, and later the `expired_at` or `revoked_at` tombstone.

When systemd is running, `grant` writes and enables two units:

```
/etc/systemd/system/odile-support-expire.service   ExecStart=/usr/local/sbin/odile-support expire --quiet
/etc/systemd/system/odile-support-expire.timer     OnActiveSec=10s, OnBootSec=20s, OnUnitActiveSec=30s
```

The timer is what makes both halves of the DoD true without anyone typing:
within 30 seconds of `expires_at` the key is removed, and within 30 seconds of
a boot the release installer's rewrite is undone by putting the granted line
back. Without systemd, `grant` says so and both halves are enforced by the next
`status`, `grant` or `expire` run.

Every grant, session start, session end, expiry, revoke and restore appends one
line to `/var/log/odile-support.log` (0644). From §8.3's Run B:

```
2026-11-12T21:23:43Z event=grant grant=508e99caaaa9 expires=2026-11-13T21:23:43Z fingerprint=SHA256:0GGWIgIPGD2lSbBBHGZW7CftORVh25oSRfZYu1QtPKw hours=24 reason='ODL-217 drill: reboot, observe the rewrite, restore, use'
2026-11-12T21:24:49Z event=reassert grant=508e99caaaa9 restored=1
2026-11-12T21:24:49Z event=start grant=508e99caaaa9 command=- key=SHA256:0GGWIgIPGD2lSbBBHGZW7CftORVh25oSRfZYu1QtPKw remote=203.0.113.139:58004
2026-11-12T21:24:49Z event=end grant=508e99caaaa9 rc=0 seconds=0
2026-11-12T21:24:49Z event=revoke grant=508e99caaaa9 removed=1
```

`reassert` means the line was missing and was rebuilt from the state — on this
image, that is the boot rewrite being undone. The log is written on the guest.
A root shell can edit it; that is stated on the grant card and in the deck's A10
answer.

---

## 5. The grant card

`grant` prints this, and the operator reads it to the user:

```
grant live
  tenant:     odl-7971a8
  reason:     disk full after an update
  expires:    2026-11-13T17:00:00Z (24h 0m left)
  key:        SHA256:… (odile-support-grant-<id>)
  revoke:     sudo odile-support revoke
  log:        /var/log/odile-support.log

  This is a handover, not a peek.
  On this unlocked machine the key can read the mounted disk, every file, the
  agent's memory and past prompts, the sign-in hash, and the backup password if
  the recovery file is still on the machine. It can add a LUKS keyslot or copy
  the volume key, so it can keep disk access after the grant.
  Expiry and revoke block new logins; an open session keeps running until it
  exits. To end one: run `w` to find its pts/N, then `pkill -t pts/N`.
  A reboot does not close the grant: this image's release installer rewrites
  root's authorized_keys at boot, and this grant's timer puts its line back
  within 30 seconds. The closures are the expiry above and `sudo odile-support
  revoke`.
  After revoke, check for what a root shell could leave behind:
    sudo cat /root/.ssh/authorized_keys
    DEV=$(sudo cryptsetup status root_crypt | sed -n 's/^ *device: *//p')
    sudo cryptsetup luksDump "$DEV"
    if command -v crontab >/dev/null; then sudo crontab -l; else echo 'crontab not installed'; fi
    sudo systemctl list-timers --all
  The log is written on this machine; a root shell can edit it.
```

**The handover.** A grant is root on a running, unlocked machine. It can read
the mounted disk, every file, the agent's memory and past prompts, the sign-in
hash, and the backup password if the recovery file is still there. It can add a
LUKS keyslot or copy the volume key, so it can keep disk access after the
grant. It cannot read a passphrase the user never set, and it cannot reach the
initramfs: the disk is unlocked before the OS (and any key in
`authorized_keys`) exists.

**Post-revoke checks.** The user runs, or asks the agent to run:

```sh
sudo cat /root/.ssh/authorized_keys
DEV=$(sudo cryptsetup status root_crypt | sed -n 's/^ *device: *//p')
sudo cryptsetup luksDump "$DEV"
if command -v crontab >/dev/null; then sudo crontab -l; else echo 'crontab not installed'; fi
sudo systemctl list-timers --all
sudo systemctl list-units --type=service --state=running | grep -i odile
```

The LUKS device is derived from the mapper this image uses (`root_crypt` →
`device: /dev/sda3`), not from a guessed `/dev/vda2`;
`cryptsetup luksDump /dev/mapper/root_crypt` is refused because the mapper is
plaintext. Debian 13 on this image has no `cron` package, so the cron check
says `crontab not installed`; systemd timers are the scheduler, and
`list-timers` is the cron-shaped check. Measured on the tenant, immediately
after §8.2's revoke:

```
ssh-ed25519 AAAA… odile-bench throwaway boot key    # the only authorized_keys line
device: /dev/sda3
LUKS header information
Version:       2
Epoch:         5
keyslots: 1
crontab not installed
NEXT … odile-support-expire.timer … odile-backup-tenant.timer … apt-daily.timer …
```

The grant's own `odile-support-expire.timer` is in the list and is harmless
once the state is revoked: each tick calls `expire`, finds nothing due and
exits 0. §9 disables it when the program is removed. A root shell could have
added a second key, a service, a cron job or a keyslot; the checks are how the
user sees it. The grant card says so before the grant.

---

## 6. Expiry, revoke and what a reboot does

`revoke` and a passed `expires_at` both remove the key from root's
`authorized_keys`, so **new** logins fail with `Permission denied (publickey)`.
Neither closes a session that is already open.

```sh
sudo odile-support revoke
```

```
grant revoked
  removed: 1 key line(s) from /root/.ssh/authorized_keys
  key:     SHA256:… (odile-support-grant-<id>)
  An open session keeps running until it exits. Run `w` to find its pts/N,
  then `pkill -t pts/N`.
  After revoke, check for what a root shell could leave behind:
    sudo cat /root/.ssh/authorized_keys
    DEV=$(sudo cryptsetup status root_crypt | sed -n 's/^ *device: *//p')
    sudo cryptsetup luksDump "$DEV"
    if command -v crontab >/dev/null; then sudo crontab -l; else echo 'crontab not installed'; fi
    sudo systemctl list-timers --all
```

For an open session:

```sh
sudo w
#  17:05:02 up 3 days,  1 user,  load average: 0.00, 0.01, 0.00
# USER     TTY      FROM             LOGIN@   IDLE   JCPU   PCPU WHAT
# root     pts/0    203.0.113.9      17:01    0.00s  0.03s  0.00s -bash
sudo pkill -t pts/0
```

`w` finds the `pts/N`; `pkill -t pts/N` ends it. The session's own shell exits,
sshd closes the PTY, and the operator's side sees `Connection to … closed.` The
wrapper catches the signal and writes the `event=end … rc=143 signal=SIGTERM`
line, so a killed session leaves an audit trail rather than a dangling `start`.

`expire` is the same removal on the clock. With the timer running it happens
within 30 seconds of `expires_at`; the state keeps an `expired_at` tombstone so
`status` says `no grant (expired <expires_at>)` afterwards.

**A reboot is not a closure.** The image's release installer rewrites root's
`authorized_keys` to the single measured boot key at every boot, so the granted
line is gone when the machine comes up. The state is still live, and
`odile-support-expire.timer` puts the identical line back within 30 seconds
(`event=reassert`). An SSH login attempted inside that window fails; the grant
itself is not closed. If the timer was never installed (`--no-systemd`), the
line stays gone until the next root `status`, `grant` or `expire` run restores
it. §8.3 measures both halves. The closures are the expiry and `revoke`.

---

## 7. `/verify` during a live grant

The access report is the machine's own audit. Its `usable keys` and
`operator key` rows count every line in `authorized_keys`, so a live grant
turns both rows **FAIL** on purpose: there is an operator key, and its expiry is
the mitigation. This is the honest reading of the deck's A10 — "a support grant
is a time-boxed door" — and the rows return to PASS after `revoke` or expiry.
Measured on a bench root on 2026-11-19 (the gate's own fixture: the synthetic
SEV-SNP report, the measured throwaway boot key and the tenant `.env`, with the
grant line and its state added — the same bench `tests/test_verify.py` drives;
the tenant drill's version of these rows is §8.5):

```
usable keys    FAIL  1 authorized key(s) other than the measured throwaway in /root/.ssh/authorized_keys: SHA256:zaZC3CG0yGBGyTbJ8NR8JnL8pzL2Xao1vR2cgYQFkC0; the support grant 39602e79e81a explains one of them (expires 2026-11-20T15:50:44Z)
boot key       PASS  ssh-ed25519 SHA256:LLwDfYyE3VMc9T9o+XvydfNzrXxyzaQ2lysSxCSSEWA; public key only, no private half of it on the machine
seal           PASS  policy 0x30000; seal_id strict-bench-v1; measurement 519c7c9aae54bf139cacd783e1b69bd98b01e79b9e8b52feb41aadb7a483f481227a8a3b64ea2b885234ca56e09c9ee8
sign-in hash   PASS  scrypt hash present in /home/odile/.hermes/.env; no plaintext password
operator key   FAIL  authorized_keys carries command="/usr/local/sbin/odile-support session",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdWyPF1Bcwd6sFiL4KiUtmYTq2FLe95TmfBQS0fEnu2 odile-support-grant-39602e79e81a; one is the support grant 39602e79e81a (expires 2026-11-20T15:50:44Z)
support grant  OPEN  grant 39602e79e81a; expires 2026-11-20T15:50:44Z; key SHA256:zaZC3CG0yGBGyTbJ8NR8JnL8pzL2Xao1vR2cgYQFkC0; line present in /root/.ssh/authorized_keys

OPEN — a support grant is live until 2026-11-20T15:50:44Z (grant 39602e79e81a): the two key rows fail because that granted key is in /root/.ssh/authorized_keys.
```

and on the same bench with no grant state, the report is unchanged from
ODL-473 — five rows and this verdict (after `revoke` on the tenant, §8.5, the
same lines with the tenant's own fingerprint):

```
usable keys    PASS  0 usable user keys; every authorized_keys line is the measured boot key (SHA256:LLwDfYyE3VMc9T9o+XvydfNzrXxyzaQ2lysSxCSSEWA)
operator key   PASS  none (authorized_keys: 1 line(s), the measured throwaway)
PASS — five checks: zero usable user keys, the throwaway boot key, the seal, the sign-in hash, no operator key.
```

The row's witness is `/var/lib/odile/support-grant.json`, not the line's
shape: the state must be inside its window and carry no `expired_at` or
`revoked_at` tombstone, and it may account only for the exact forced-command
line `odile-support grant` wrote — the marker `odile-support-grant-<id>`, the
state's own key fingerprint, a `command="…/odile-support … session"` prefix,
and root's own `authorized_keys`. A hand-written grant-shaped line, a
tombstoned state, or a state edited to name another key still fails both rows,
so the row cannot excuse a key a root shell planted. The fingerprint in the
FAIL rows is the key in `/etc/odile/support-key.pub` at grant time:
`SHA256:clN4wFkP…` in the drill, because the drill had replaced the seed key
with a throwaway (§8); on a tenant it is the seed key, `SHA256:HEPfoGHs…`.

After a reboot the installer's `authorized_keys` rewrite deletes the granted
line and the timer restores it within 30 s (§3), so a report written inside
that window has five PASS rows, the same `support grant  OPEN` row with
`line missing from /root/.ssh/authorized_keys (the timer restores it within
30 s)`, and this verdict — still OPEN, because the state is what makes the
grant live:

```
OPEN — a support grant is live until 2026-11-20T15:50:44Z (grant 39602e79e81a); its line is missing from /root/.ssh/authorized_keys (the timer restores it within 30 s).
```

The reporter unit exits non-zero whenever a row fails — the report's verdict is
also the unit's exit status — so `systemctl restart odile-access-report` shows
`Result: exit-code` while the granted line is in place (exit 1 above, exit 0 on
the no-grant bench and in the reboot window): the unit's status is the report's
verdict, not a broken unit.

A grant explains the two key rows and nothing else. The verdict is built from
the rows that failed, so a compound state names the grant **and** the other
failing labels instead of blaming only the door. Same bench, the drill's grant
id, with the attestation policy moved to `0x30001` (the seal row fails):

```
seal           FAIL  policy 0x30001; seal_id strict-bench-v1; measurement 519c7c9a… (policy is 0x30001, not 0x30000)
support grant  OPEN  grant 39602e79e81a; expires 2026-11-20T15:50:44Z; key SHA256:zaZC3CG0…; line present in /root/.ssh/authorized_keys

OPEN — a support grant is live until 2026-11-20T15:50:44Z (grant 39602e79e81a); 1 other check FAIL (seal): the report did not pass.
```

and with a planted key and its private half beside the grant (`usable keys`
fails for more than the grant):

```
usable keys    FAIL  2 authorized key(s) other than the measured throwaway in /root/.ssh/authorized_keys: SHA256:zaZC3CG0…, SHA256:+miR6jIgSAfkl/YB…; the support grant 39602e79e81a explains one of them (expires 2026-11-20T15:50:44Z); 1 private half on the machine: /home/odile/.ssh/id_ed25519

OPEN — a support grant is live until 2026-11-20T15:50:44Z (grant 39602e79e81a); 1 other check FAIL (usable keys): the report did not pass.
```

The exit status follows the rows: 1 wherever a row fails (the pure grant above
and both compound states), 0 in the reboot window and with no grant. A grant
accounts for `usable keys` only when the single foreign key is the state's own
fingerprint and for `operator key` only when the one grant-shaped line is the
exact line the state owns, so a second planted key keeps its own FAIL label.

HQ audits a grant through the log the tenant carries
(`/var/log/odile-support.log`), `odile-support status --json`, and the
`audit`/`logs` sections of the bundle the user emails.

A tenant's `/verify` is `verify/verify.py` + `templates/verify.html`
(`odile-verify.service` on 127.0.0.1:8098 behind Caddy's one no-store handle),
and it names a live grant in its own `support access` row, read from the state
file by the program that writes it (`odile-support status --json`). Rendered on
2026-11-20 with the shipped program over a bench root
(`verify/verify.py --root <bench> --luks-dump <dump> --now <t> --out <page>`),
the grant state reads:

```
support access  NOTE  grant live: 544895ed3819, expires 2026-11-21T00:36:31Z, reason ODL-217 drill: grant, use, expire · 2 keys in authorized_keys (1 grant) · 2 keyslots · support key SHA256:9wJnb+0o…
LUKS            NOTE  2 keyslots (0, 1) — the second is the support grant's handover; after revoke the header must be one keyslot again
```

and with the state removed the same two rows read `support access  PASS  no
grant` and `LUKS  PASS  one keyslot (slot 0), no token: one volume key, no
second way in`. The page's tenant block carries the published scoped row
(`No one at Odile can log in unless you grant it.`, `docs/CLAIMS.md` §1
*Access*), so the sentence above the rows and the row that proves it agree.

The report's sixth row is this card's too. The reporter
(`scripts/verify_and_unlock.sh`, `332eccdf…`) ships inside the signed installer
and `2026.11.20-1`: with no grant it prints the five rows and `PASS — five
checks …` (exit 0); with a live grant it prints the same five plus
`support grant  OPEN  grant <id>; expires …; key …; line present …` and an
`OPEN — …` verdict (exit 1). The parsers in `app.py` and
`installer/first_visit.py` accept the sixth row only together with the OPEN
verdict. The tenant's page no longer reads the report file — it reads the state
directly — so the report is the support bundle's copy and HQ's fallback page's
copy of the machine's own audit (`docs/runbooks/install.md` §12.6).

---

## 8. The drill (2026-11-12, re-run 2026-11-13)

**Machine:** the M1 tenant `odl-7971a8` (`203.0.113.87`, Debian 13, SEV-SNP,
the ODL-332 image), driven through its own Webapp terminal as the `tenant` user
with NOPASSWD sudo, from this workstation. The tenant's `/verify` still reports
the ODL-332 pin: seal id `53ac131dda13e47a`, measurement `9f01725a…f137`.

**Method.** The program was uploaded, checked byte-for-byte against the
checkout, placed at `/usr/local/sbin/odile-support` and symlinked as
`/usr/local/bin/odile-diag` — the installer now places both (ODL-474), so the
drill's hand placement is the pre-landing state. The support key in
`/etc/odile/support-key.pub` was replaced for the drill with
a throwaway `ssh-ed25519` key whose private half stayed on this workstation;
the seed key was put back afterwards (§8.5, §9). Every command below is a line
from the round-2 transcripts attached to the card
(`odile-217-r2-grant-use-expire.txt`, `odile-217-r2-revoke.txt`,
`odile-217-r2-verify-and-diag.txt`, `odile-217-r2-diag-planted-transcript.txt`,
`odile-217-r2-diag-verdict.txt`, `odile-217-r2-planted-values.txt`,
`odile-217-r2-restore-final.txt`) and the two bundles
(`odile-217-r2-diag.txt`, `odile-217-r2-diag-planted-bundle.txt`). §8.3 quotes
the round-1 reboot transcripts (`reboot.txt`, `reboot-observed.txt`). The
harness that drove the terminal and recorded each command is attached as
`odile-217-drill-scripts-r2.tar.gz` (`d2.py`, `d2_phase*.py`, `d2_check.py`,
`drill_lib.py`, the drill's public key); it reads the tenant's sign-in password
from a path and contains no secret.

**Program lineage.** The program's bytes moved three times, and each pair was
checked byte-for-byte between this checkout and `/usr/local/sbin/odile-support`
on the tenant:

| sha256 | measured on | what it carries |
|---|---|---|
| `eb06772a…` | the first full drill, 2026-11-12T21:06–21:30, including §8.3's two reboot runs | before the `--reason` one-line normalization |
| `3f710593…` | the round-1 reviewer's live smoke, after that drill | the post-drill `--reason` one-line normalization |
| `acfbbfed…` | §8.1, §8.2, §8.4, §8.5 of this round | the post-revoke command block (M2) |

The full values are
`eb06772a8b9ec39084e274e15e59d3df19c91f22441d5d9b4f4b73b6f6a2a448` (the
first drill's pair, recorded on the checkout and on
`/usr/local/sbin/odile-support` at 21:09:48Z in `grant-use-expire.txt`),
`3f71059379350e8acd089b9e4f44f92df6b7b1cfea5cdf3dc51b07891640d3d2` (the
reviewer's smoke) and
`acfbbfedbd1cff429c9f348a759f2be1c319c28503b3af24386c20b0b871ffe1` (this
round); `odile-217-r2-grant-use-expire.txt` records the same SHA-256 on the
checkout and on `/usr/local/sbin/odile-support`.

The program changes between `eb06772a…` (the pair the first drill's reboots ran
with) and `acfbbfed…` are exactly two: the `--reason` one-line normalization
that produced `3f710593…`, and this round's two print sites (`git diff e69dd7f
HEAD -- support/odile_support.py` shows the grant card and `revoke`, nothing
else). The timer, the state, the forced command and the redaction pass are
untouched, so §8.3's reboot measurement at `eb06772a…` still describes the
same code path. A reader comparing the two drills' bundles
sees the round-1 hashes (`bd1c7dd6…`, `75afc3cb…`) and this round's
(`cf73959f…`, `3ef5b8a7…`); the difference is the timestamps and the support
section's grant id, not the format.

One harness fact a rerun needs: the Webapp terminal **masks secret-shaped
output** (a `sk-…` token becomes `sk-…` with an ellipsis, a bearer value
becomes `***`, a PEM block becomes `[REDACTED PRIVATE KEY]`). Files fetched as
base64 are exact; a verdict that depends on a secret's absence must be
computed from the bytes on disk, not from the terminal echo. A second trap
measured this round: a secret-shaped literal in a *tool-written file* can be
rewritten on write, so `d2_phase6.py` assembles its planted values and marker
expectations from pieces and compares the guest copy's byte count and SHA-256
before trusting the plant. §8.4 does both.

### 8.1 Grant, use, expire

```
$ sudo odile-support status
no grant
$ sudo odile-support grant --hours 0.02 --reason 'ODL-217 drill: grant, use, expire'
systemctl daemon-reload: ok
systemctl enable --now odile-support-expire.timer: ok
grant live
  tenant:     odl-7971a8
  reason:     ODL-217 drill: grant, use, expire
  expires:    2026-11-13T00:07:19Z (1m left)
  key:        SHA256:clN4wFkPkmjrpKbjkg6Z7N7zSB3L7w/AcHV5ysbfWAQ (odile-support-grant-9947606342d1)
```

`authorized_keys` then held two lines — the boot key and the grant — and the
state held public material only (0644); the log held one `event=grant` line.
The SSH session through the forced command:

```
$ ssh -tt -i <drill key> root@203.0.113.87 <<< 'id -un; hostname; echo SSH_CONNECTION=$SSH_CONNECTION; sudo -n odile-support status; exit'
root
odile-bench
SSH_CONNECTION=203.0.113.139 45472 203.0.113.87 22
grant live … (odile-support-grant-9947606342d1)
  key line:   present
  last session: 2026-11-13T00:06:08Z event=start grant=9947606342d1 …
```

The log, from the same run:

```
2026-11-13T00:06:08Z event=start grant=9947606342d1 command=- key=SHA256:clN4… remote=203.0.113.139:45472
2026-11-13T00:06:08Z event=end grant=9947606342d1 rc=0 seconds=0
```

The expiry, polled every 10 seconds (the timer's tick is 30 s):

```
2026-11-13T00:07:38Z 1
2026-11-13T00:07:48Z event=expire grant=9947606342d1 expires=2026-11-13T00:07:19Z removed=1
2026-11-13T00:07:58Z 0
```

`status` then said `no grant (expired 2026-11-13T00:07:19Z)`, the state carried
`"expired_at": "2026-11-13T00:07:48Z"`, `authorized_keys` was back to the one
boot line, and a new SSH login failed:

```
root@203.0.113.87: Permission denied (publickey).   (exit 255)
```

### 8.2 Revoke, with an open session

```
$ sudo odile-support grant --hours 24 --reason 'ODL-217 drill: revoke with an open session'
grant live … (odile-support-grant-7e5a1f48454b)
$ sudo w
USER     TTY      FROM             LOGIN@   IDLE   JCPU   PCPU WHAT
root     pts/1    203.0.113.139    00:08    3.00s  0.06s   ?    sleep 600
$ sudo odile-support revoke
grant revoked
  removed: 1 key line(s) from /root/.ssh/authorized_keys
  key:     SHA256:clN4wFkP… (odile-support-grant-7e5a1f48454b)
  An open session keeps running until it exits. Run `w` to find its pts/N,
  then `pkill -t pts/N`.
  After revoke, check for what a root shell could leave behind:
    sudo cat /root/.ssh/authorized_keys
    DEV=$(sudo cryptsetup status root_crypt | sed -n 's/^ *device: *//p')
    sudo cryptsetup luksDump "$DEV"
    if command -v crontab >/dev/null; then sudo crontab -l; else echo 'crontab not installed'; fi
    sudo systemctl list-timers --all
```

A new login was refused immediately (`Permission denied (publickey)`, 255),
while `w` still showed `pts/1`. `sudo pkill -t pts/1` ended it: the operator's
side printed `Terminated`, the ssh exit was 143, and the log got

```
2026-11-13T00:08:14Z event=end grant=7e5a1f48454b rc=143 seconds=9 signal=SIGTERM
```

The grant went from one line to a `revoked_at` tombstone, `status` said
`no grant (revoked 2026-11-13T00:08:08Z)`, and the post-revoke checks in §5
then ran on the tenant (their output is there).

### 8.3 A reboot with the key still installed

Two runs, because the interesting half is a race. In both, the grant was made,
the machine rebooted, and the key was still in `authorized_keys` when the
reboot started. Both were measured at `sha256 eb06772a…`, the pair the first
drill placed at 21:09:48Z (`grant-use-expire.txt`); Run A's grant was made at
21:14:35Z and Run B's at 21:23:46Z, so both reboots are inside that same drill
window. The convergence path they exercise (`expire` → `reassert`) is untouched
by the later `--reason` and print-text changes, which is why the round-1
transcript still describes the same code path (§8 Method). The round-2 reviewer
reproduced it independently at `acfbbfed…`: the boot rewrite left
`authorized_keys` at 01:14:04Z, the timer logged `event=reassert … restored=1`,
`status` showed `restored:`, and `revoke` closed it
(`odile-217-verifier-r2-evidence.txt.gz` on the card).

**Run A — the tenant's normal path, timer never stopped.** After the reboot the
machine was up at 21:15:40Z, and the log already said:

```
2026-11-12T21:15:02Z event=reassert grant=b65055154c47 restored=1
```

`status` showed `grant live … key line: present … restored: 2026-11-12T21:15:02Z
(a boot rewrote authorized_keys; the timer put the line back)`, and an SSH
session through the same key worked (`root`, `odile-bench`, exit 0). `revoke`
then removed the line and the next login was refused (255). The `reassert`
line is the proof the line had been missing: it is only written when the state
is live and `authorized_keys` holds no grant line.

**Run B — the timer stopped for the measurement, so both halves are visible.**
The grant was `508e99caaaa9` (expires `2026-11-13T21:23:43Z`) and the timer was
disabled before the reboot. After the boot:

```
$ sudo stat -c '%y %a %n' /root/.ssh/authorized_keys
2026-11-12 21:24:03.704000000 +0000 600 /root/.ssh/authorized_keys
$ sudo grep -c odile-support /root/.ssh/authorized_keys
0
$ sudo odile-support status --no-expire
grant live
  expires:    2026-11-13T21:23:43Z (23h 59m left)
  key:        SHA256:0GGW… (odile-support-grant-508e99caaaa9)
  key line:   MISSING from /root/.ssh/authorized_keys
$ sudo ssh-keygen -lf /etc/odile/support-key.pub
256 SHA256:HEPfoGHskI8eGF74cp4ea/ZS4ZxSK7FoRWy0l0ClaV0 support@odile.fyi (ODL-217 pending) (ED25519)
```

So: the release installer rewrote the file at 21:24:03 (the file's mtime), the
state was still live, and the key file now held the **seed's** key, not the
granted one — the drill's throwaway key had been replaced by the installer's
`ensure_pin_files`.

Starting the timer again (`sudo systemctl start odile-support-expire.timer`)
restored the line:

```
sample 00: 2026-11-12T21:24:38Z | 0 | key line: MISSING from /root/.ssh/authorized_keys
sample 01: 2026-11-12T21:24:44Z | 0 | key line: MISSING from /root/.ssh/authorized_keys
sample 02: 2026-11-12T21:24:49Z | 1 | key line: present | restored: 2026-11-12T21:24:49Z (a boot rewrote authorized_keys; the timer put the line back)
```

```
2026-11-12T21:24:49Z event=reassert grant=508e99caaaa9 restored=1
2026-11-12T21:24:49+00:00 odile-bench systemd[1]: Starting odile-support-expire.service - Odile support grant — expire a due grant, restore a live one...
```

The restored line carries the **granted** key, not the one now in the key file:

```
ssh-ed25519 AAAA… odile-bench throwaway boot key
command="/usr/local/sbin/odile-support session",no-agent-forwarding,… ssh-ed25519 AAAA… odile-support-grant-508e99caaaa9
```

and SSH with the drill private half worked (exit 0). `revoke` removed the line,
`status` said `no grant (revoked 2026-11-12T21:24:49Z)`, and the next login was
refused (255).

**What this settles.** A reboot neither keeps the bytes nor closes the grant.
The boot rewrite deletes the line; the state and expiry make the grant live; the
timer restores the identical line within 30 seconds; only expiry and `revoke`
are closures. The window between the rewrite and the restore is up to 30
seconds, and a login attempted inside it fails.

### 8.4 The diag bundle

```
$ sudo odile-diag --out /var/tmp/odile-diag-final.txt
wrote /var/tmp/odile-diag-final.txt (45841 bytes)
$ sudo sha256sum /var/tmp/odile-diag-final.txt
cf73959f9e869429b3c3e91de386652ed579de437b3d54fa5bd31e449db05408
```

The second bundle is the redaction proof: a password, a token, a bearer value
and a private-key block were appended to `/var/log/odile-support.log`, the file
the bundle's `logs` section carries, and the bundle was generated over it after
the plant was checked against the local file (231 bytes, sha256 `8aa394c1…` on
both sides):

```
$ sudo odile-diag --out /var/tmp/odile-diag-planted.txt
wrote /var/tmp/odile-diag-planted.txt (42773 bytes)
3ef5b8a70de4a255a5d796408750e066d4eebaaa4f1d820fb8f739ca3864e7ba
```

The verdict, computed from the fetched bytes (not the terminal's masked echo),
16 checks:

```
planted password absent: PASS
planted token absent: PASS
planted bearer absent: PASS
planted PEM body absent: PASS
planted password marker: PASS
planted token marker: PASS
planted auth marker: PASS
planted PEM marker: PASS
live meter key absent: PASS
live sign-in hash absent: PASS
live backup password absent: PASS
live workspace line 'odile-smoke-ok' absent: PASS
live bundle has the support log: PASS
live bundle redaction footer: PASS
live bundle says what it excludes: PASS
live bundle carries no PEM block: PASS
verdict: PASS
```

The live values were the tenant's own: a 35-character meter key (`sk-` + 32
hex), an 86-character scrypt sign-in hash, a 32-character backup password, a
35-character NEAR key, and a two-line workspace file. The workspace's lines are
`odile-smoke-ok` and `tenant`; the second is also the bundle header's own word
(`tenant: odl-7971a8`), so the check names the distinctive `odile-smoke-ok`
line — the generic word is not evidence either way. None of these values is in
the bundle; the planted ones are replaced by `PASSWORD=[redacted]`,
`NEAR_API_KEY=[redacted]`, `Authorization: [redacted]` and
`[redacted private key]` (7 replacements on the planted bundle, 0 on the clean
one).

**The two FAIL blocks in the round-1 attachments, explained.** The
`diag-verdict.txt` block stamped 21:38:23 is the first run of the verdict
checker: the checker's final text (attached in the first tarball, mtime 21:38)
carries exactly the two fixes its failures name — it no longer treats the bare
workspace word `tenant` as a distinctive file line (it is also the bundle
header's word), and it compares the exclusion sentence with the bundle's actual
wording. The bytes it read are the same bundle the 21:38:50 run passes over:
the local copy was written by the fetch recorded at 21:27:22, and nothing in
the harness rewrote it before 21:38; the difference between the two blocks is
the checker, not the bundle. The 21:38:50 block is the definitive run over the
21:27 bundle (`sha256 bd1c7dd6…`); it passes. The round-1
`diag-planted-transcript.txt` also carries `bearer marker: FAIL`: that harness
expected `Bearer [redacted]`, while the program's assignment pass replaces the
whole value, `Authorization: [redacted]`. The definitive checker tests the
marker the program actually emits and passes; this round's `d2_phase6.py` was
corrected to the same expectation and passes too. Neither FAIL block is a
bundle defect.

### 8.5 `/verify`, and the restore

During the live grant, a regenerated report gave `usable keys FAIL`,
`operator key FAIL` and the page's `FAIL — the access report did not pass`
(the drill's rows; §7 quotes the shape after the sixth row landed with
`t_e23f3288`). After `revoke`, a regenerated report gave
`usable keys PASS`, `operator key PASS`, `PASS — five checks …`.

After the drill the machine was put back: `revoke`, the seed key restored in
`/etc/odile/support-key.pub` from the copy taken before the swap, the units
disabled and removed, and the state, log, program, symlink and temp files
deleted. The end state at 2026-11-13T00:12:07Z:

```
1 /root/.ssh/authorized_keys
0                      # lines mentioning odile-support
ssh-ed25519 AAAA… odile-bench throwaway boot key
256 SHA256:HEPfoGHskI8eGF74cp4ea/ZS4ZxSK7FoRWy0l0ClaV0 support@odile.fyi (ODL-217 pending)
0 timers listed
sudo: odile-support: command not found
```

The seed key's bytes are `sha256 0a3e229e…` — the same file the drill started
from.

---

## 9. Restoring a machine after a drill

The grant installs one key and one timer, and leaves a state file and a log.
After a drill on a machine that is not the user's:

```sh
sudo odile-support revoke
sudo install -m 0644 /path/to/original/support-key.pub /etc/odile/support-key.pub
sudo rm -f /var/lib/odile/support-grant.json /var/log/odile-support.log
sudo systemctl disable --now odile-support-expire.timer
sudo rm -f /etc/systemd/system/odile-support-expire.{service,timer}
sudo systemctl daemon-reload
```

`revoke` is the important one; the rest returns the machine to the state the
installer shipped. The seed support key is public material — keep a copy before
replacing it. Run the block from a shell that is **not** the grant session: the
session's closing `event=end` writes the log again after the `rm` (a 65-byte
file). If you clean up from inside the grant session, repeat the log `rm` after
it exits and check `sudo test ! -e /var/log/odile-support.log`. A drill that
placed the program by hand removes it too:

```sh
sudo rm -f /usr/local/sbin/odile-support /usr/local/bin/odile-diag
```

---

## 10. Where the pieces live

| Piece | Path | Mode |
|---|---|---|
| the program (both names) | `/usr/local/sbin/odile-support`, `odile-diag` | 0755, root |
| the support public key (signed seed) | `/etc/odile/support-key.pub` | 0644, root |
| the grant's one line | `/root/.ssh/authorized_keys` | 0600, root |
| the grant state / tombstone | `/var/lib/odile/support-grant.json` | 0644, root |
| the audit log | `/var/log/odile-support.log` | 0644, root |
| the expire/restore timer | `/etc/systemd/system/odile-support-expire.{service,timer}` | 0644, root |
| the diagnostics bundle | `/var/lib/odile/diag-<UTC>.txt` | 0644, root |

There is no standing key: `authorized_keys` holds the measured throwaway boot
key until a user grants, and holds it again after the grant closes. A live
grant adds exactly one line; a reboot rewrites the file to the boot key and the
grant's timer restores its line within 30 seconds.
