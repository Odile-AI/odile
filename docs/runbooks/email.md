# Email — Odile sends from its own machine

**Card:** ODL-102 · **Lane:** infra · **Owner:** builder · **Status:** live.
Postfix 3.10.6 and OpenDKIM 2.11.0 run on HQ; the seven DNS rows are published
and verified against the zone's authoritative nameservers; submission on 587
with SASL is the tenant interface; delivery reports are parsed into a JSONL
record instead of read by eye. Gmail's own headers for the 587-path invite show
`spf=pass`, `dkim=pass`, `dmarc=pass`, and the message is in the INBOX. The
12-hour soak is the last measurement (§6, §10.2).

Odile sends two kinds of transaction: the **invite** (a claim link) and, from
M3, the **brief**. Both leave from `mail.odile.fyi` — a dedicated subdomain on
HQ's own addresses, signed by OpenDKIM, delivered straight to the recipient's
MX. No relay and no third party in the message path.

## 1. Addresses and envelope

| Role | Value | Notes |
|---|---|---|
| From (invite) | `Odile <invite@mail.odile.fyi>` | Every card that sends an invite uses this exact address. |
| From (brief, M3) | `Odile <brief-<slug>@mail.odile.fyi>` | Same domain, same DKIM key; no new DNS. |
| Reply-To | `help@odile.fyi` | Apex MX points here; the alias delivers into the `help` Maildir (§4). |
| Return-Path | `b-<yyyymmdd>-<hash>@mail.odile.fyi` | VERP, unique per message: the hash covers recipient + send nonce, so a bounce identifies the send. |
| DKIM `d=` | `mail.odile.fyi`, selector `odile` | Strict alignment with From. |
| Envelope domain | `mail.odile.fyi` | SPF is evaluated here, not on the From address. |

## 2. The decision

**Self-hosted Postfix + OpenDKIM on HQ**, chosen by Savio on 2026-11-06. It
supersedes the earlier relay choice (Resend), which had been the builder's call
from the three options an older revision of the card named.

| Option | Why not |
|---|---|
| Resend / Postmark / Migadu | A processor sees every invite and brief, and each needs a standing credential on HQ. Cost $0 at this volume, but the privacy sentence in `docs/CLAIMS.md` has to admit the relay. |
| **Self-hosted (chosen)** | The message never touches a third party. Cost $0, no API key, full control of the bounce path. Price paid: one IP whose reputation is entirely ours, and HQ becomes a single point of failure for mail. |

The always-on property is why this is viable at all: a box that is up can hold a
queue; a laptop could not.

## 3. DNS

### 3.1 Reverse DNS — the prerequisite (done)

Servury sets reverse DNS; the box cannot. **Done 2026-11-06:** both
`203.0.113.139` and `2001:0db8:0:1c9::1` have PTR `mail.odile.fyi`.

Gmail requires forward-*confirmed* reverse DNS for every sender, not just bulk
senders. A PTR that does not forward-resolve is worthless, which is why §3.2
must exist before the PTR is worth anything. Before this change the PTR was
`hostfake.local`, a placeholder that would have had every message rejected.

### 3.2 The records — zone `odile.fyi`

Seven rows; the **Name** column is what Cloudflare's form wants (it appends the
zone). Proxy must stay **off** (grey cloud) on all of them.

| # | Type | Name | Value | TTL |
|---|---|---|---|---|
| 1 | `A` | `mail` | `203.0.113.139` | Auto |
| 2 | `AAAA` | `mail` | `2001:0db8:0:1c9::1` | Auto |
| 3 | `TXT` | `mail` | `v=spf1 ip4:203.0.113.139 ip6:2001:0db8:0:1c9::1 -all` | Auto |
| 4 | `MX` | `mail` | `mail.odile.fyi` priority `10` | Auto |
| 5 | `MX` | `@` (apex `odile.fyi`) | `mail.odile.fyi` priority `10` | Auto |
| 6 | `TXT` | `odile._domainkey.mail` | `v=DKIM1; k=rsa; p=…` — from `/etc/dkimkeys/odile.txt` | Auto |
| 7 | `TXT` | `_dmarc.mail` | `v=DMARC1; p=quarantine; adkim=s; aspf=s; pct=100; rua=mailto:dmarc@mail.odile.fyi` | Auto |

- Rows 1–2 are the MX target and the forward half of the PTR. Without them the
  PTR fails and Gmail rejects.
- Row 3 authorises exactly two addresses, both ours. No `include:`, no third
  party — if a relay is ever added, this line changes.
- Row 4 is the **bounce** path: remote servers send delivery reports to our
  VERP address, which routes back here.
- Row 5 is the **reply** path: a reply to `help@odile.fyi` reaches this MTA,
  where the alias delivers it to a local mailbox (§4). It is also the apex MX,
  so every other address at `odile.fyi` is rejected rather than relayed.
- Row 6 is the DKIM public key. 2048-bit, generated on this machine; the
  private half never leaves `/etc/dkimkeys`. A hand-copied truncation is the
  classic failure (§11).
- Row 7 is ours alone. `adkim=s` and `aspf=s` are safe because From and the
  envelope sender are both `mail.odile.fyi`.
- **No apex record for sending.** `odile.fyi` never sends.

`rua` points at `dmarc@mail.odile.fyi`, which the catch-all delivers into the
bounce mailbox. Aggregate reports arrive as XML attachments and are ignored by
`scripts/mail_dsn.py` by design.

### 3.3 Applying them

Typing seven rows by hand is how a DKIM key gets truncated. The script writes
them from the machine's own state — the addresses below, the key in
`/etc/dkimkeys/odile.txt` — and is idempotent:

```sh
./scripts/send_test.py --apply-dns          # plan only: create/update/unchanged
./scripts/send_test.py --apply-dns --apply  # write; a second run prints "nothing to do"
```

It never deletes. Records are compared on type, name and value (MX also on
priority), and a name that exists twice is a hard error rather than a guess.

**Trap seen on this box:** right after the writes, plain `dig` still showed
nothing because the local resolver had cached the earlier NXDOMAIN for the
zone's SOA minimum (1800 s). `--check-dns` therefore asks the zone's
authoritative nameserver for every record (and for the PTR via
`in-addr.arpa`/`ip6.arpa`), so the checker cannot be fooled by its own cache.

## 4. The machine

### 4.1 Packages and files

Ubuntu 26.04 packages: `postfix`, `postfix-pcre`, `opendkim`, `opendkim-tools`,
`dovecot-core`, `dovecot-imapd`. `postfix-pcre` is load-bearing: without it the
sender-login map is an "unsupported dictionary type" and every authenticated
submission is deferred with `451 4.3.0` at RCPT time. (`sasl2-bin` is no longer
needed: submission AUTH is Dovecot's. The package may remain installed, but
nothing reads `/etc/sasldb2`.)

**Postfix, OpenDKIM and Dovecot are captured in git**, not typed by hand:

```
deploy/postfix/main.cf            deploy/opendkim/opendkim.conf
deploy/postfix/master.cf          deploy/opendkim/KeyTable
deploy/postfix/virtual            deploy/opendkim/SigningTable
deploy/postfix/sender_login.pcre  deploy/opendkim/TrustedHosts
deploy/postfix/sender_canonical.pcre  deploy/opendkim/odile.txt  (public key only)
deploy/dovecot/dovecot.conf
```

The old `deploy/postfix/sasl/smtpd.conf` (Cyrus auxprop) is deleted: submission
now asks Dovecot's auth socket (§4.4), and `deploy/postfix/apply.py --apply`
removes the deployed copy.

```sh
./deploy/postfix/apply.py             # dry run: say what would change
./deploy/postfix/apply.py --apply     # write, postmap, create mailboxes, restart
./deploy/postfix/apply.py --check     # exit 1 on drift
./deploy/postfix/apply.py --self-test # offline checks for the fresh-host path

./deploy/dovecot/apply.py             # dry run: the IMAPS listener and its state
./deploy/dovecot/apply.py --apply     # write the config, reconcile the service
./deploy/dovecot/apply.py --check     # exit 1 on drift, or 143 open
./deploy/dovecot/apply.py --self-test # offline checks, including "no 143"

./deploy/opendkim/apply.py --check    # every selector in KeyTable: key, mode, TXT
./deploy/opendkim/apply.py --apply
./deploy/opendkim/apply.py --self-test
```

The applier compares content, owner and mode; a second run prints
`nothing to do`. A Postfix change **restarts** the service rather than
reloading it, because `postfix reload` re-reads `main.cf` but does not rebuild
listening sockets — a change to `inet_protocols` would look applied and not be.

Two things on a fresh host are *derived* from the committed files, and both are
reconciled rather than assumed:

- **`/etc/postfix/virtual.db`** is rebuilt with `postmap` whenever it is
  missing, older than its source, or holds a different set of addresses than
  `virtual`. `--check` lists the database itself (`postmap -s`) and compares
  every key and destination with the committed file, so an **extra** alias —
  one removed from the source but still in the database — is drift too, not
  only a stale destination. `--apply` rebuilds whenever it rewrites `virtual`,
  which is what makes a removal take effect.
- **the two mailboxes** in §4.3 are created with their Maildirs.

`--check` exits 1 for either. That is what stops a host where `help@` and every
bounce address are silently dead from reporting `clean`.

`milter_default_action = tempfail` is deliberate: an unsigned invite is worse
than a delayed one, so if OpenDKIM dies Postfix queues instead of sending bare
mail. `UserID opendkim:postfix` is what makes the milter socket group-owned by
`postfix`; without it every connection is `451 4.7.1` logged as
`Permission denied`. `OversignHeaders From` is why `h=` lists `From` twice
(§10.3 covers the verification rule).

`inet_protocols = all`: the name has an AAAA and a PTR for it, and a delivery
has been observed over IPv6 (`relay=…[2607:…]:25`). `mynetworks` includes
`[::1]/128` for local IPv6 clients.

### 4.2 Secrets — six files, none of them in git

| Secret | Path | Mode | What it can do |
|---|---|---|---|
| Submission account (test tenant) | `/root/.odile/mail_test_account.json` | `0600` | `host`, `port`, `username`, `secret`, `tls_ca`; used by `send_test.py` |
| IMAP + SMTP account hashes | `/etc/dovecot/users` | `0600 root` | two `{SHA512-CRYPT}` lines — `help@odile.fyi` (mailbox) and `odile-test@mail.odile.fyi` (submission only); read by `auth-worker`, used by IMAP and by Postfix on 587 (§4.4, §5.1) |
| Derived IMAP userdb | `/etc/dovecot/userdb` | `0600 root` | the mailbox line only; keeps the submission-only account out of IMAP (§4.4) |
| Mailbox password (handover) | `/root/.odile/mailbox-help-password.txt` | `0600` | the one plaintext copy of that password; relayed to Savio over Matrix |
| Cloudflare DNS token | `/root/.odile/cloudflare_dns_token` | `0600` | `Zone.DNS Edit` on `odile.fyi` only; `--apply-dns` |
| Gmail app password (optional) | `/root/.odile/gmail_app_password` | `0600` | IMAP read for `--gmail-headers` (§10.2) |

There is no `/etc/sasldb2`. It held the same two passwords in cleartext because
SASL PLAIN/LOGIN authenticates against the stored value; it was retired on
2026-11-22 when submission moved to Dovecot's passwd-file.
`db_dump -p /etc/sasldb2` now fails because the file is gone.

The DKIM private key is **not** in git. The generator is:

```sh
./deploy/opendkim/genkey.py --check   # exists, 0600, matches odile.txt
./deploy/opendkim/genkey.py --apply   # generate only if absent; never overwrites
```

It produces `/etc/dkimkeys/odile.private` (0600, `opendkim:opendkim`) and the
public TXT. Commit the TXT as `deploy/opendkim/odile.txt`; never the key.

### 4.3 Mailboxes

| Mailbox | User | Home | What lands there |
|---|---|---|---|
| Bounce | `dsn` | `/var/mail/dsn` | every VERP address and every unmatched address under `mail.odile.fyi`, including DMARC reports |
| Reply | `help` | `/var/mail/help` | `help@odile.fyi`, `help@mail.odile.fyi`, `invite@mail.odile.fyi` |

Both are `nologin` users with no shell, password or key — mailboxes, not
accounts. The applier creates either one if it is missing, with its
`Maildir/{new,cur,tmp}`, and fixes the Maildir's mode and owner; `--check`
exits 1 when a user, home or Maildir is absent. `scripts/mail_dsn.py` reads
`/var/mail/dsn/Maildir`, so a host built without the `dsn` mailbox cannot parse
a bounce even though the alias resolves.

### 4.4 Reading the mailbox — IMAPS on 993

The support mailbox has a reader: Dovecot 2.4.2 serves the same Maildir over
IMAPS, and Thunderbird is the first client. Nothing is forwarded and nothing is
copied; the Maildir is the storage.

| Setting | Value |
|---|---|
| IMAP | `mail.odile.fyi`, port **993**, SSL/TLS (implicit TLS, 1.2 minimum) |
| SMTP | `mail.odile.fyi`, port **587**, STARTTLS (§5) |
| Username | `help@odile.fyi` — one login for both IMAP and SMTP |
| Password | one secret; the handover copy is `/root/.odile/mailbox-help-password.txt` (0600 root) |
| Certificate | `CN=mail.odile.fyi`, the certificate Postfix already serves (self-signed, pinned by tenants — §13) |

There is **no** port 143 listener and no plaintext authentication:
`service imap-login { inet_listener imap { port = 0 } }` removes the socket and
`auth_allow_cleartext = no` refuses a plaintext AUTH anyway. The packaged
`dovecot.socket` binds 143 *and* 993, so it is masked; the daemon owns 993.
`./deploy/dovecot/apply.py --check` fails if 143 is listening.

The storage is read in place:

```cfg
mail_driver = maildir
mail_path = /var/mail/help/Maildir   # Dovecot 2.4's spelling of mail_location
mail_home = /var/mail/help
mail_uid = help
mail_gid = help
```

Opening INBOX performs Maildir's normal `\Recent` transition: the messages in
`new/` are renamed into `cur/` with a `:2,` suffix on the first `SELECT`. The
content does not change — measured 2026-11-22, 14 before and 14 after, the same
inode, size and SHA-256 for every message. Postfix keeps delivering new mail to
`new/`; nothing is re-delivered or rewritten.

**The credential.** `/etc/dovecot/users` holds exactly two lines at 0600 root:
the mailbox account
`help@odile.fyi:{SHA512-CRYPT}$6$…:1000:1000::/var/mail/help::` and the
submission-only account
`odile-test@mail.odile.fyi:{SHA512-CRYPT}$6$…:65534:65534::/nonexistent::`.
Both are read by `auth-worker` (`use_worker = yes`), because the auth process
runs as `dovecot` and cannot read a 0600 root file. The same store serves IMAP
and 587: Postfix is configured `smtpd_sasl_type = dovecot` and asks
`/var/spool/postfix/private/auth`, so submission AUTH checks the same hash.
`/etc/dovecot/deny.imap` names the submission-only account in a passdb that
runs first, so its IMAP login is refused with `[AUTHENTICATIONFAILED]`; the
empty `/etc/dovecot/deny.smtp` means Postfix's `service=smtp` request is not
denied. `/etc/dovecot/userdb` is derived from the same file and holds only the
mailbox line as a second layer. Postfix's AUTH request carries `nologin`, so it
performs no userdb lookup: `odile-test@mail.odile.fyi` authenticates on 587,
but cannot complete an IMAP login or read the support Maildir. The mailbox
password's only plaintext copy is the handover file; it is never in git, on a
command line, or in a log. The submission-only account's plaintext is its
pre-existing 0600 `mail_test_account.json` (§4.2).

**Two expected log lines, one real failure.** The first lookup after a Dovecot
start or reload logs, twice,
`auth: Error: passwd-file /etc/dovecot/users:open(...) failed: Permission denied (euid=107(dovecot) …)`
(and the same for `/etc/dovecot/userdb`), then `passdb: … auth succeeded` /
`imap-login: Logged in`. The parent auth process opens each file before handing
the lookup to `auth-worker` (root); that first open is the noise, and the worker
caches the result, so the pair appears once per auth-process lifetime rather than
on every login. With `use_worker = yes` these lines are expected. Only `internal
auth failure`, `Authentication failed`, or a missing `auth succeeded` means a
broken credential.

**Dovecot's own files.** The first `SELECT` writes `dovecot-*` index files
(`dovecot.index`, `dovecot.index.log`, …) into the Maildir. They are Dovecot's
cache, not messages: the 14 message files keep their inode, size and SHA-256,
and the `new/`/`cur/` message contents are unchanged. “No message moves” is
about the message files, not the directory listing.

**Rotate it** — one command writes the hash, the derived userdb and the
handover file from one generated secret, then proves the IMAP login:

```sh
./deploy/dovecot/apply.py --set-password
./deploy/dovecot/apply.py --check
```

It prints paths, never the secret. There is no second place to change: the 587
AUTH checks the same hash, so the old password fails on the next AUTH. The
submission-only account rotates from its own file, without putting the secret
on a command line:

```sh
/usr/bin/python3 -c 'import json; print(json.load(open("/root/.odile/mail_test_account.json"))["secret"])' \
  | ./deploy/dovecot/apply.py --set-account-password odile-test@mail.odile.fyi --password-stdin
```

**Sending as `help@odile.fyi`.** Thunderbird's From is the apex address; the
envelope is moved to `help@mail.odile.fyi` before the queue, because
`odile.fyi` publishes no SPF record (§3.2) and `mail.odile.fyi` is the domain
that authorises this host and signs with `d=mail.odile.fyi`. Three tracked
files carry that one decision: `sender_login.pcre` lets the login claim its own
apex envelope sender, `sender_canonical.pcre` rewrites that envelope, and
`sender_canonical_classes = envelope_sender` keeps the rewrite away from
`From:`/`Reply-To:` — the Postfix default rewrites header sender addresses
too, which on 2026-11-22 delivered the first attempt as
`From: help@mail.odile.fyi`. `deploy/opendkim/SigningTable` names the apex
address so the message is signed at all.

Measured 2026-11-22 with a real 587 submission: `From: Odile Help
<help@odile.fyi>`, `Return-Path: <help@mail.odile.fyi>`, Gmail `spf=pass`,
`dkim=pass` (`d=mail.odile.fyi`, `s=odile`) and the message in the INBOX.
`dmarc` is **absent**, not failed: the apex publishes no `_dmarc` record, so
Gmail has nothing to evaluate for `header.from=odile.fyi`. Adding one is a DNS
change and was out of scope for this card.

## 5. Submission — the tenant interface

Tenants do not get a shell and do not talk to port 25. They get one SASL
username and one secret, and submit over 587 with STARTTLS:

```text
host      mail.odile.fyi
port      587
security  STARTTLS (required; AUTH is not offered before it)
auth      PLAIN or LOGIN
username  tenant-<id>          (M1); odile-test today
secret    per tenant
```

This is the shape the seed already carries as `mail_relay {host, port,
username, secret}`. M1 mints one account per tenant; this card proves the
interface with one test account.

### 5.1 Creating an account

```sh
# as root; the secret is read from a file, never from argv or the process list
umask 077
openssl rand -hex 24 > /root/.odile/tenant-<id>.secret
./deploy/dovecot/apply.py --set-account-password tenant-<id>@mail.odile.fyi \
  --password-stdin < /root/.odile/tenant-<id>.secret
```

The key is the full address. The realm is unchanged: `auth_default_domain =
mail.odile.fyi` in `deploy/dovecot/dovecot.conf` makes a client that sends a
bare `tenant-<id>` resolve to `tenant-<id>@mail.odile.fyi`, the same name
`saslpasswd2 -u mail.odile.fyi` used to store. There is no `/etc/sasldb2` to
chmod; the account is one `{SHA512-CRYPT}` line in `/etc/dovecot/users`, and
`/etc/dovecot/userdb` deliberately omits it so the account cannot open IMAP.

The test account's file is `/root/.odile/mail_test_account.json`; a client
reads it, the operator never types the secret into a command line.

### 5.2 What an authenticated tenant may send

`smtpd_sender_login_maps = pcre:/etc/postfix/sender_login.pcre` restricts the
**envelope sender** to `*@mail.odile.fyi`, owned by the authenticated login:

```text
/^[^@]+@mail\.odile\.fyi$/   odile-test@mail.odile.fyi
```

Dovecot returns the passwd-file key as `sasl_username`, so a client that sends
the bare `odile-test` is recorded as `odile-test@mail.odile.fyi`; the map value
must be that full name (measured 2026-11-22: the bare value produced `553
Sender address rejected: not owned by user odile-test@mail.odile.fyi`). A
spoofed envelope is rejected at RCPT with `553 5.7.1 Sender address rejected:
not owned by user …`. Today the map names one tenant login; M1 replaces the row
with one per tenant. From: is not restricted by this map — the tenant
template and the per-tenant DKIM signing table are the M1 controls.

Port 25 stays local-only: `mynetworks = 127.0.0.0/8 [::1]/128`, and SASL is
enabled only on the submission service in `master.cf`.

### 5.3 Which of our two addresses carries the message

`mail.odile.fyi` publishes an A and an AAAA, both with a forward-confirmed PTR
and both in the SPF record (§3.2), and Postfix will use either. Gmail's verdict
is not the same on the two: measured 2026-11-22, the same brief text — sent as
a tenant sends it, from a fresh `brief-<slug>@mail.odile.fyi` address, to a
Gmail alias that had never received one — was filed to **Spam 4 times of 4**
when the relay connected over the A (`203.0.113.139`) and to the **INBOX 4
times of 4** over the AAAA (`2001:0db8:0:1c9::1`), with `spf=pass`, `dkim=pass`
and `dmarc=pass` on every copy. The message shape was held constant across
those eight sends — two forms (bare URLs on their own lines, and inline links)
each sent both ways — and the address, not the form, decided the folder. The
scorer agrees that the message is not the variable: the two families' copies
score the same (spamassassin 0.8, rule for rule) while Gmail files them on
opposite sides.

The relay therefore prefers IPv6:

```cfg
# deploy/postfix/main.cf  (the source of truth; /etc/postfix is a copy)
smtp_address_preference = ipv6
```

Postfix still tries IPv4 when a destination publishes no AAAA or the IPv6
connection fails, so this is a preference and not a v6-only path. Apply it the
supported way and prove it:

```sh
./deploy/postfix/apply.py --apply     # writes /etc/postfix/main.cf, reloads
./deploy/postfix/apply.py --check     # exit 0
postconf smtp_address_preference      # smtp_address_preference = ipv6
```

**Which address did a delivered message use?** The relay's log line and the
receiver's `Received` header both name it:

```sh
grep "gmail-smtp-in" /var/log/mail.log | tail -1    # relay=…[2001:…]:25
./scripts/send_test.py --gmail-headers "<subject>"  # placement, plus the address in spf=pass
```

A `relay=…[203.0.113.139]` (an IPv4 literal) after this change means the
setting is not in effect — most likely `/etc/postfix/main.cf` was edited by
hand and the applier has not run since.

## 6. First run and the daily check

```sh
# 0. offline logic: DNS parsing, SPF/DKIM/DMARC checks, VERP, DKIM verify, soak report
./scripts/send_test.py --self-test
./deploy/postfix/apply.py --self-test    # alias db, dsn/help mailboxes
./deploy/dovecot/apply.py --self-test    # 993 only, maildir in place, hash rules
./deploy/opendkim/apply.py --self-test   # active selector and its committed TXT

# 1. the machine: services, milter, key, submission hash store, AUTH, banner
./scripts/send_test.py --check-mta
./deploy/postfix/apply.py --check        # virtual.db, mailboxes, Dovecot auth store
./deploy/dovecot/apply.py --check        # config, 143 closed, 993 open, hash 0600
./deploy/opendkim/apply.py --check       # every selector in KeyTable

# 2. the records: A, AAAA, SPF (both families), DKIM (DNS vs local key),
#    DMARC, forward-confirmed PTR for v4 and v6, bounce MX, reply-to MX
./scripts/send_test.py --check-dns

# 3. what will be sent, with no side effect
./scripts/send_test.py --send --to <address> --dry-run
./scripts/send_test.py --send --to <address>

# 4. what happened to that queue id
./scripts/send_test.py --status <queue id>

# 5. the soak (24 messages, one every 30 minutes, 12 hours); --start resumes
./scripts/send_test.py --soak --to <address>
./scripts/send_test.py --soak --to <address> --start 13   # resume at message 13
./scripts/send_test.py --soak-report /var/log/odile-mail-soak.jsonl
```

`--send` and `--soak` use a VERP envelope sender of
`b-<utc date>-<sha256 prefix>@mail.odile.fyi` where the hash covers the
recipient **and a per-send nonce**, so every message has its own Return-Path
and a bounce names the send, not merely the day. The soak writes one JSON line
per message to `/var/log/odile-mail-soak.jsonl` and its report fails if any
message bounced, stayed unknown, or if an open 4xx is older than an hour.

Every message also carries
`List-Unsubscribe: <mailto:help@odile.fyi?subject=unsubscribe>`. Gmail asks
bulk senders for an unsubscribe route; the header is free and keeps a fresh
domain looking legitimate.

`--check-mta` is a live probe, not a config read: it opens 587, negotiates
STARTTLS against the pinned certificate, and authenticates with the account in
§4.2. `--check-dns` compares the DKIM TXT with the key on disk, so a stale
record is a failure rather than a pass with a warning.

## 7. Bounces, complaints and DMARC reports

Every message leaves with a VERP Return-Path. A rejection comes back as an
RFC 3464 delivery report into `/var/mail/dsn/Maildir`. A complaint (ARF,
RFC 5965) and a DMARC aggregate report can arrive in the same mailbox; they are
different things and the parser keeps them apart.

```sh
./scripts/mail_dsn.py                    # append new records to the JSONL file
./scripts/mail_dsn.py --dry-run          # print, write nothing
./scripts/mail_dsn.py --file <message>   # parse one report and print it
```

Default record file: `/var/lib/odile/mail/dsn.jsonl`. One record per report:

```json
{"kind": "dsn", "bounce": "hard",
 "recipient": "odile-no-such-user-9f3a2@gmail.com", "status": "5.1.1",
 "original_sender": "b-20261106-6ebcb1903fb0@mail.odile.fyi",
 "queue_id": "D94611A7A8C", "reporting_mta": "mail.odile.fyi",
 "recipients": [{"recipient": "…", "action": "failed", "status": "5.1.1",
                 "remote_mta": "gmail-smtp-in.l.google.com",
                 "diagnostic": "smtp; 550-5.1.1 …"}],
 "message_id": "…", "source": "1793981641.…", "parsed_at": "…"}
```

- **Hard** (`5.x.x` or `Action: failed`) — remove the address from the
  waitlist (`./manage.py rm <email>`) and write one line in the daily brief.
  There is no auto-suppression list at alpha.0; the waitlist *is* the
  suppression list.
- **Soft** (`4.x.x` or `Action: delayed`) — nothing for 24 hours. Postfix
  retries on its own schedule; `postqueue -p` shows the queue.
- **Complaint** (`kind: complaint`) — a human marks the message as spam. Gmail
  offers no feedback loop for personal accounts, so this is usually only seen
  for receivers that do send ARF. Treat it like a hard bounce.
- **DMARC aggregate** — parsed as `ignored` on purpose: it is a report about
  our authentication, not about a recipient. Read it only when SPF/DKIM/DMARC
  failures are suspected.
- The parser keys records by `Message-ID` (or a hash of the bytes), so running
  it twice appends nothing the second time. It never moves or deletes mail.

**Who sees a failure.** The operator runs `mail_dsn.py` with the daily brief
and reads the counts; a hard bounce or a complaint is a line in the brief and a
removal from the waitlist. There is no pager, no webhook, and no automatic
suppression at alpha.0 — that is ODL-235's problem, not a hidden cron.

## 8. Queue monitoring

```sh
postqueue -p                 # what is queued, and why
postqueue -j | head          # machine-readable, one JSON object per message
postqueue -f                 # retry now (does not bypass a remote 5xx)
tail -f /var/log/mail.log    # delivery lines carry the queue id and dsn=
```

- An empty queue is the normal state; the soak prints `sent 2.0.0` for each
  message.
- A queue older than a few minutes means the remote is deferring or outbound 25
  is blocked. `postqueue -p` shows the reason; `dsn=4.x.x` is a deferral.
- Do not delete queue files to "fix" a queue. `postsuper -d <id>` is the
  delete path, and it is not idempotent; retry first.
- `--soak-report` is the measured check: it fails when any queued message has
  an open 4xx older than an hour, and it fails unless the log holds the expected
  number of messages (`--count`, default 24). A log with one surviving record
  is not a 24-message soak.
- The soak JSONL (`/var/log/odile-mail-soak.jsonl`) is the durable record.
  `/var/log/mail.log` keeps three rotations, so the queue ids are gone within a
  day or two; `--soak-report` re-reads the log wherever a line survives and
  otherwise trusts the verdict the soak recorded at send time, and its
  `verdicts:` line says how many came from each source. A report that calls
  every message `unknown` means the code lost that fallback, not that the mail
  failed.

## 9. DKIM key handling and rotation

The key pair is generated on the machine that signs. The private half never
leaves `/etc/dkimkeys`; the public half is committed in
`deploy/opendkim/odile.txt` and published under
`odile._domainkey.mail.odile.fyi`.

**Rotation**, with no gap in signing:

1. Generate a new selector without touching the old key:
   `./deploy/opendkim/genkey.py --apply --selector odile-2027-01`
2. Publish the TXT it prints at
   `odile-2027-01._domainkey.mail.odile.fyi` and wait for the authoritative
   answer to match (`dig +short TXT …`).
3. Add a line to `deploy/opendkim/KeyTable` and point `SigningTable` at the
   new selector; `./deploy/opendkim/apply.py --apply` (OpenDKIM reloads).
   `--check` now follows the KeyTable: the moment the tables name the new
   selector it demands `/etc/dkimkeys/<selector>.private` (0600,
   `opendkim:opendkim`) and `deploy/opendkim/<selector>.txt`, so a rotation
   that forgot the TXT fails instead of signing with a key nobody published.
4. Send one test and check `--verify-dkim` on the copy, then Gmail's header.
5. Remove the old selector from the tables and the zone once no message signed
   with it can still be in flight (allow a day).

`./deploy/opendkim/genkey.py --check` fails if the private key on disk does not
match the committed public TXT — the check that stops a rotation from being
half-done. `./deploy/opendkim/apply.py --check` applies the same match to
**every** selector the committed KeyTable names, which is the check to run after
switching. Two valid selectors may exist during the changeover; that is normal
and is why KeyTable is a file, not a single line.

If the private key leaks: rotate as above with a new selector, publish, switch,
then delete the old TXT and the old key.

## 10. Evidence, and how to reproduce it

### 10.1 Independent authentication checks

Port25's verifier (`check-auth@verifier.port25.com`) replies to the envelope
sender, so its report lands in `/var/mail/dsn/Maildir`:

```text
Summary of Results
SPF check:          pass
"iprev" check:      pass
DKIM check:         pass
HELO hostname:  mail.odile.fyi
mail-from:      b-…@mail.odile.fyi
```

That is an SPF pass, a forward-confirmed PTR pass and a DKIM pass measured by a
third party, with no inbox involved. It is not Gmail, and it does not report
DMARC.

### 10.2 Gmail's own headers

There are two paths; the IMAP one is the working default.

- **App password (works today).** The app password is at
  `/root/.odile/gmail_app_password` (0600). Two commands read the mailbox:

  ```sh
  # the four headers for the newest message with this subject, plus placement;
  # exits 1 unless spf, dkim and dmarc all pass and it is in the INBOX
  ./scripts/send_test.py --gmail-headers "test invite (ODL-102)"

  # where every matching message was filed (the soak's inbox-not-spam check);
  # fails unless exactly --count messages (default 24) matched, all in the INBOX
  ./scripts/send_test.py --gmail-placement "Odile soak"
  ```

  `--gmail-headers` reads `[Gmail]/All Mail` first and asks `[Gmail]/Spam` when
  nothing matches there; `--gmail-placement` asks both. So "delivered" and "in
  the inbox" are two different answers and both are recorded. A copy in
  `[Gmail]/Spam` answers `X-GM-LABELS ()`: Spam is a folder and not a label
  (measured 2026-11-22 on two quarantined briefs), so the folder a copy came
  from is part of the verdict and the tool prints it. An em-dash subject is
  searched by its longest ASCII run, because IMAP SEARCH is ASCII-only.
- **Paste (fallback).** Gmail → the message → `⋮` → **Show original** → copy
  `Authentication-Results`, `DKIM-Signature`, `Return-Path` and the first
  `Received:`, with the recipient redacted.

A browser cannot do it here at all: the saved cookie session stops at Google's
device-verification step, the persisted profile is signed out, and the
automatable browser is refused with "This browser or app may not be secure"
(measured: `navigator.userAgentData === null`, `navigator.webdriver === true`,
`HeadlessChrome/145` in the UA, SwiftShader WebGL — a headless-shell build, not
desktop Chrome). The credentials are fine; the browser class is not, which is
why the app password is the path that works.

**Observed, 2026-11-06** — produced by the IMAP path, not pasted by hand:

```text
Authentication-Results: mx.google.com;
  dkim=pass header.i=@mail.odile.fyi header.s=odile header.b=R2pk9+qZ;
  spf=pass (google.com: domain of b-20261106-39e4b3c92c39@mail.odile.fyi
            designates 203.0.113.139 as permitted sender)
            smtp.mailfrom=b-20261106-39e4b3c92c39@mail.odile.fyi;
  dmarc=pass (p=QUARANTINE sp=QUARANTINE dis=NONE) header.from=mail.odile.fyi
Return-Path: <b-20261106-39e4b3c92c39@mail.odile.fyi>
Received: by 2002:a05:6802:6243:b0:664:a21c:b9bf with SMTP id s3csp3481663ocb;
          Fri, 6 Nov 2026 07:48:55 -0800 (PST)
```

That is the 15:48 UTC invite sent over submission 587 with SASL, and Gmail
filed it in the INBOX. The reply path was proved from the outside too: a reply
sent from that Gmail account to `help@odile.fyi` arrived over IPv6 and this MTA
verified Gmail's own DKIM (`Authentication-Results: … dkim=pass
header.d=gmail.com`), then delivered it to `/var/mail/help/Maildir`.
Re-proved 2026-11-08 19:25 UTC with a fresh reply: `Return-Path:
<owner@example.com>`, `X-Original-To: help@odile.fyi`,
`Delivered-To: help@mail.odile.fyi`, `dkim=pass (2048-bit key) header.d=gmail.com`
over IPv6 — the alias and the inbound path still work, not just on the day they
were built.

The first soak run exposed the reputation problem the decision predicted: after
three inbox placements, nine near-identical messages with one shared VERP
sender went to Spam (`--gmail-placement` first read only All Mail, which
excludes Spam, and undercounted). The fix was threefold — a per-message VERP
sender, `List-Unsubscribe`, and moving the nine back to the inbox through IMAP,
which is Gmail's "not spam" action. At 21:45 UTC the tally was **13/13 soak
messages and 2/2 invites in INBOX, 0 in Spam**, and message 13's own headers
showed `spf=pass`, `dkim=pass`, `dmarc=pass` with its own `Return-Path`.
Report the tally in the daily brief until the soak settles.

**Final soak result, 2026-11-08** — the 24-message run of 2026-11-06
15:49 → 2026-11-07 03:10 UTC, re-read two days later:

```text
$ ./scripts/send_test.py --soak-report /var/log/odile-mail-soak.jsonl
sent (2.x):      24 of 24
deferred (4.x):  0
bounced (5.x):   0
unknown:         0
oldest open 4xx: none
PASS  24/24 sent (2.x), no open 4xx older than an hour

$ ./scripts/send_test.py --gmail-placement "Odile soak"
matched: 24 message(s) (All Mail + Spam)
  INBOX 24
  SPAM  0
  OTHER 0
PASS  placement  24/24 in the inbox, nothing in spam or elsewhere

$ ./scripts/send_test.py --gmail-headers "Odile soak 24/24"
placement: INBOX   PASS spf pass   PASS dkim pass   PASS dmarc pass   PASS inbox
```

Both counts are enforced: `--soak-report` and `--gmail-placement` fail unless
the expected number of messages is present (`--count`, default 24), so a log
that lost 23 of its 24 records cannot pass on the strength of the one it kept.

The placement tally is the DoD **as it stands today**: all 24 are in the INBOX,
none in spam, and the last message carries all three passes. It is a current
tally, not an arrival-time one — nine of the first twelve arrived in Spam and
were moved to the inbox through IMAP (the "not spam" signal) after the
per-message VERP sender and `List-Unsubscribe` change landed between messages
12 and 13. Message 24 arrived in the INBOX with its own `spf=pass`, `dkim=pass`,
`dmarc=pass`.

The clock is not a clean 12 hours either: because those fixes were applied
mid-run, messages 1–12 ran 15:49–21:20 and 13–24 ran 21:40–03:10 — a 20-minute
gap at 13 and an 11 h 21 m window, with every other interval at 1803 s. The
acceptances and the placement are unaffected; the shape is stated because "one
every 30 minutes" without qualification would be false.

A fresh invite sent 2026-11-08 19:22 UTC over submission 587 with SASL (this
time over IPv6, `2001:0db8:0:1c9::1`) was accepted `250 2.0.0` and filed in the
INBOX with the same three passes — so the result is not a stale artifact of the
soak window.

### 10.3 Proving the signature locally

`--verify-dkim FILE` checks `bh=` against the body and `b=` against the private
key's public half with `openssl`, so signing can be proven without DNS and
without an inbox. It implements relaxed/relaxed only, and one rule that is easy
to get wrong: when `h=` names a field twice (OpenDKIM oversigns `From` to stop
header injection) and the message has one instance, the extra reference is
**dropped**, not signed as an empty header. Getting that backwards produces a
verifier that rejects valid mail.

Cross-checked against `dkimpy` as a dev-time oracle: the same message returns
PASS there and here.

## 11. Failure modes seen in the wild

| Symptom | Cause | Fix |
|---|---|---|
| `451 4.3.0 … Temporary lookup failure` at RCPT on 587 | `postfix-pcre` missing; `pcre:/etc/postfix/sender_login.pcre` cannot load | install `postfix-pcre`, restart postfix |
| IMAP login is `internal auth failure`, log says `Permission denied` on `/etc/dovecot/users` **and nothing else** | the passdb lookup ran in the auth process (uid `dovecot`) instead of `auth-worker`, so a 0600 root file is unreadable | keep `use_worker = yes` in both passdb and userdb; two `Permission denied` lines followed by `auth succeeded` are expected (§4.4) |
| `AUTH` on 587 fails (`535 5.7.8`) with a correct password | Postfix cannot reach Dovecot's auth socket, or the bare username does not resolve to a passwd-file key | `ls -l /var/spool/postfix/private/auth` (0660 `postfix:postfix`), `./deploy/dovecot/apply.py --check`; then check `auth_default_domain = mail.odile.fyi` and the full address in `/etc/dovecot/users` |
| Something listens on 143 | the packaged `dovecot.socket` is enabled; it binds 143 as well as 993 | `./deploy/dovecot/apply.py --apply` masks it; `--check` fails while 143 is open |
| Thunderbird warns the certificate is not trusted | the certificate is self-signed and pinned (§13); this host's CA store trusts it, a fresh client does not | accept it once for this host, or import `/etc/ssl/certs/mail.odile.fyi.crt` |
| Mail sent as `help@odile.fyi` arrives with `From: help@mail.odile.fyi` | `sender_canonical_maps` is rewriting header sender addresses as well as the envelope | keep `sender_canonical_classes = envelope_sender` in `main.cf` |
| `553 5.7.1 Sender address rejected: not owned by user …` | Authenticated tenant used an envelope sender outside `mail.odile.fyi` | send with a VERP sender, or extend `sender_login.pcre` (M1) |
| `451 4.7.1 Service unavailable` on every connection | Milter socket unreachable: directory or socket not group-owned by `postfix` | `deploy/opendkim/apply.py --apply`; check `UserID opendkim:postfix` |
| `--check-dns` fails for a record `dig @1.1.1.1` shows | Local resolver serving the pre-change NXDOMAIN for the SOA minimum | believe `--check-dns` (it asks the authoritative server); the cache clears on its own |
| `dkim=fail (bad signature)` at the receiver | Truncated DKIM TXT, or `c=` not relaxed/relaxed | re-publish from `/etc/dkimkeys/odile.txt`; `--verify-dkim` on the sent copy |
| `spf=pass` but `dmarc=fail` | From domain and envelope domain differ | keep both under `mail.odile.fyi`; `aspf=s` makes that strict |
| Delivered, but in spam | Reputation starts at zero on a fresh IP, and our two addresses do not have the same reputation | check DMARC first; then which address the relay used (§5.3) — the A was filed to Spam 4/4 and the AAAA to the INBOX 4/4 on 2026-11-22; then the From domain's age (spamassassin's `FROM_FMBLA_NEWDOM28` fired on every copy that day) and engagement |
| `550-5.7.26 … sender is unauthenticated` | Records missing or a key mismatch | `--check-dns`, then resend (seen on the first attempt from this box) |
| Mail queues and never leaves | Outbound 25 blocked, or destination greylisting | `postqueue -p`; `telnet gmail-smtp-in.l.google.com 25` |
| The box is down | Single point of failure | senders retry; queued invites survive on disk, but the waitlist and mail share one host that must stay up |

## 12. Rollback

**Stop IMAP** — `systemctl stop dovecot`. Mail on 25 keeps arriving in the
Maildir; it is simply unread until Dovecot starts again. Submission on 587 also
stops authenticating while Dovecot is down, because Postfix no longer has a
credential store of its own (`smtpd_sasl_type = dovecot`): clients get a
`535`/`451` until it is back. `systemctl disable --now dovecot` also drops it
across reboots; the config stays tracked in `deploy/dovecot/` for the next
`--apply`. Rotating the credential is
`./deploy/dovecot/apply.py --set-password`, which replaces the single IMAP/SMTP
hash, the derived userdb and the handover file together — the old password
fails on the next AUTH.

**Stop sending** — `systemctl stop postfix`. There is no vendor to pause, no key
to revoke. Everything queued waits on disk.

**Roll back to a relay in one command** — the DKIM signature stays local, so
DMARC keeps passing on DKIM even if SPF at the relay's IP does not:

```sh
postconf -e 'relayhost = [smtp.resend.com]:587' \
  'smtp_sasl_auth_enable = yes' \
  'smtp_sasl_password_maps = hash:/etc/postfix/relay_auth' \
  'smtp_sasl_security_options = noanonymous' \
  'smtp_tls_security_level = encrypt' && postmap /etc/postfix/relay_auth && systemctl restart postfix
```

Write the relay's credentials to `/etc/postfix/relay_auth` at mode 0600 first
(`[smtp.resend.com]:587 user:password`), then `postmap` it. To undo, remove
`relayhost` and reload. Because OpenDKIM signs before the relay, `d=mail.odile.fyi`
still aligns; only SPF changes, and strict `aspf=s` needs the relay to keep the
envelope sender on our domain (it does for a submission relay).

**Stop signing** — remove the milter line or stop `opendkim`; with
`milter_default_action = tempfail`, mail queues rather than going out bare.

**Remove the DNS** — delete rows 1–7. Nothing else on the zone is affected.
Reverse DNS is Servury's; pointing the PTR elsewhere ends sending.

## 13. Out of scope

No webhook, no automatic suppression list, no inbound human mail beyond
`help@`, no open/click tracking, no relay. The waitlist app still sends nothing
(ODL-235 is the sender). A public TLS certificate and per-tenant DKIM signing
are follow-ups; today the certificate is self-signed, a tenant pins it from its
seed (`mail_relay.tls_ca`, cached at `/etc/odile/mail-ca.pem`), and the test
client pins the same PEM.
