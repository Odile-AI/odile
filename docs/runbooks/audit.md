# The handoff audit — prove the access claim on the machine

ODL-208. `scripts/audit.py` is the program a tenant — or a sceptic they invite —
runs to check the access claim from inside the machine: one boot key, no
password, one keyslot, no operator path. The installer places it at
`/usr/local/bin/odile-audit` (`root:root 0755`) and the signed manifest records
its `audit_sha256`, so the file a stranger runs is the file Odile shipped.

The audit is **the machine's own testimony**. A root shell can change every row;
the boot measurement says which software was supposed to be running, and `/verify`
tags the rows accordingly. The Odile sign-in is not an SSH key and is not part of
this audit: this audit covers the machine.

## 1. What it checks

Ten rows, in this order; every row prints the command that produced it.

| Row | The claim | The command |
|---|---|---|
| `boot key` | the measured command line carries exactly one `bb.key=`, it decodes to one public key, and no private half of it is under `/root`, `/home`, `/etc/odile` or `/var/lib/odile` | `cat /proc/cmdline` |
| `authorized keys` | every line in every account's `authorized_keys` **and** `authorized_keys2` — both of sshd's default files — is that same key; zero usable user keys | `cat /root/.ssh/authorized_keys /root/.ssh/authorized_keys2 /home/*/.ssh/authorized_keys /home/*/.ssh/authorized_keys2` |
| `root password` | `/etc/shadow` holds no password hash for root | `passwd -S root` |
| `password auth` | the effective sshd configuration says `PasswordAuthentication no` (an `Include` drop-in can override the main file, and does here) | `sshd -T \| grep -i '^passwordauthentication'` |
| `operator key` | no `authorized_keys` or `authorized_keys2` line has options (a forced command) or a comment naming an operator, support or deploy key | `grep -nE '^(command=\|.*odile\|.*operator\|.*support)' /root/.ssh/authorized_keys /root/.ssh/authorized_keys2 /home/*/.ssh/authorized_keys /home/*/.ssh/authorized_keys2` |
| `operator account` | uid 0 is held by `root` alone, and no login account looks like an operator or support account | `getent passwd` |
| `keyslots` | the root filesystem is one LUKS2 container with one keyslot and no token | `cryptsetup luksDump /dev/sda3` |
| `agent socket` | no process carries `SSH_AUTH_SOCK` and no agent socket holds a key (a login-session socket with no key loaded is not a credential) | `grep -l SSH_AUTH_SOCK /proc/*/environ; ls /tmp/ssh-*/agent.* /run/user/*/openssh_agent` |
| `support grant` | no `/etc/odile/support-grant.json` with an expiry still in the future, and no `odile-support` key in `authorized_keys` or `authorized_keys2` | `cat /etc/odile/support-grant.json; grep -n odile-support /root/.ssh/authorized_keys /root/.ssh/authorized_keys2 /home/*/.ssh/authorized_keys /home/*/.ssh/authorized_keys2` |
| `login history` | every successful publickey login in sshd's log used the measured boot key's fingerprint; the list is printed, one line per login | `journalctl -u ssh --no-pager -o cat \| grep 'Accepted '` |

The passphrase is never read and never printed. The audit opens nothing: it
reads `cryptsetup luksDump`, which describes the header, and the sshd journal.
Nothing is written except the optional `--save` report, which is written
atomically at mode 0644.

## 2. Run it

On the machine, as the tenant user with the installer's NOPASSWD sudo:

```sh
sudo odile-audit
```

Copy-pasteable evidence for `/verify`, and a file that outlives the terminal:

```sh
sudo odile-audit --save /var/lib/odile/handoff-audit.txt
```

The same facts as JSON (one document, stable row names):

```sh
sudo odile-audit --json
```

| Exit | Meaning |
|---|---|
| `0` | every row passed |
| `1` | at least one row failed; the rows and their detail are still printed, and `--save` still writes the file |
| `2` | bad input or a missing prerequisite (a `--root` that is not a directory, `--json` with `--save`) |

A mounted machine (a bench, a disk opened read-only) can be audited without
booting it; the fixtures let a reviewer point every read at a file:

```sh
sudo odile-audit --root /mnt/guest \
    --cmdline /mnt/guest/proc/cmdline --luks-device /dev/sda3 \
    --auth-log /mnt/guest/var/log/auth.log
```

## 3. The report

One provenance line, one line per check with `PASS`/`FAIL` and the fact, then
the command that produced it; the summary names every failed row. The shape is
the same one `installer/access_report.sh` writes, so `/verify` can quote a row
without re-formatting it:

```
read from your machine, 2026-11-12T21:44:02Z
odile-audit — the handoff audit

  boot key        PASS  ssh-ed25519 SHA256:…; one bb.key in the command line,
                        public key only, no private half of it on the machine
      cat /proc/cmdline
  authorized keys PASS  1 line(s) over 1 file(s), every one the measured boot key; 0 usable user keys
      …
  login history   PASS  1 successful publickey login(s), every one the measured boot key
      journalctl -u ssh --no-pager -o cat | grep 'Accepted '
    login root 203.0.113.9 SHA256:…

  PASS — 10 checks: …
  A root shell can change these rows; the boot measurement says which software
  was supposed to be running.
```

`read from your machine, <UTC>` is the line `/verify` tags as machine-reported;
the boot measurement is the attacker-resistant half.

## 4. Prove the audit can fail: plant a second key

A PASS is only worth what the FAIL is worth. On a machine you own, add one
second key to sshd's **second** default file, the tenant user's
`authorized_keys2` (sshd's default `AuthorizedKeysFile` is
`.ssh/authorized_keys .ssh/authorized_keys2`), and run the audit again:

```sh
install -d -m 0700 -o "$USER" -g "$USER" "$HOME/.ssh"
printf '%s\n' 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU5Pru+2th/4Bdp7sk96CHqbhvX7uJ4D2WbNzGnL+Cj planted key' \
    >> "$HOME/.ssh/authorized_keys2"
sudo odile-audit; echo "exit $?"
```

The `authorized keys` row must fail — one line that is not the measured boot
key — the summary must name it, and the exit must be `1`. Remove the file and
the audit passes again:

```sh
rm -f "$HOME/.ssh/authorized_keys2"
sudo odile-audit --save /var/lib/odile/handoff-audit.txt
```

The same plant in `authorized_keys` (root's or the tenant's) fails the same
row. The script prints the offending path and line number, so the evidence is
specific: `…/home/<user>/.ssh/authorized_keys2:1 ssh-ed25519 SHA256:…`.

For the other direction — the boot key's **private** half — the strict bar
names `/root/boot-key` as a sin. Copy it there (0600, no `.pub` sibling) and
the `boot key` row must fail:

```sh
sudo install -m 0600 -o root -g root /path/to/boot-key /root/boot-key
sudo odile-audit; echo "exit $?"
rm -f /root/boot-key
```

The audit scans `/root`, `/home`, `/etc/odile` and `/var/lib/odile` for a file
that is a private key and names the boot key's public half (`ssh-keygen -y`, or
a `.pub` sibling); the row prints the path it found.

## 5. The script is in the installer's published manifest

The installer embeds `scripts/audit.py` byte for byte
(`installer/embed_verify.py` regenerates the block and the pin) and places it
like the `/verify` surface. Two records name it:

```sh
# the installer's own manifest record
$ ./installer/odile_install.py --manifest | grep audit_sha256
  "audit_sha256": "fd66f284b1c9f4606a2ff0fadcbe60d29a642df170083f6aa7a13394a3ee9127",
$ sha256sum scripts/audit.py
fd66f284b1c9f4606a2ff0fadcbe60d29a642df170083f6aa7a13394a3ee9127  scripts/audit.py
```

On the machine, the signed manifest at `/etc/odile/manifest.json` (served at
`/manifest`) carries the same row, and `scripts/manifest.py build --audit`
reproduces it from an installed root. The release cut records the audit script in `release/manifest.json` (the
follow-up cut is `2026.11.13-6`), so the document a stranger verifies is the
one that names the program they run. `./installer/odile_install.py --self-test`
fails on a one-byte edit of the embedded script or of the checkout.

## 6. Self-test and tests

No machine is needed for the logic: `--self-test` builds a clean machine and
six planted ones in a temporary directory and asserts each direction.

```sh
$ /usr/bin/python3 scripts/audit.py --self-test
audit self-test: PASS (11 ok, 0 failed)
$ /usr/bin/python3 -m unittest tests.test_audit -v
Ran 53 tests in 1.699s

OK
```

The fixtures cover a second key in the tenant's home as well as root's, the
same second key in `authorized_keys2` in both homes, a forced-command line, an
operator-shaped comment, an operator account, a second uid 0, a private half
beside a matching public key, the boot private key at `/root/boot-key` with no
`.pub` sibling, an unrelated private key and a TLS key that are not the boot
key's half, two `bb.key=` tokens, a missing or non-base64 key, a root password
hash, a `PasswordAuthentication yes` drop-in that beats the main file, a second
keyslot, a LUKS token, a plain ext4 root, an active grant, an expired grant, a
grant with no expiry, an agent socket, a foreign login fingerprint and a
password login.

## 7. Boundaries

- **A support grant makes the audit fail, on purpose.** During a grant the
  machine legitimately has a second key and a second keyslot; the `support
  grant` row says so and the summary names it. After the grant is revoked the
  grant login stays in sshd's journal and the `login history` row reports the
  operator's fingerprint — the machine's history, not a current credential.
  The page renders the 2-key/2-keyslot state separately (ODL-236).
- **A root shell can change every row.** Only the boot measurement is
  attacker-resistant; `/verify` tags the rows machine-reported for that reason.
- **The audit does not read the passphrase.** It never opens the LUKS container;
  `luksDump` reads the header. A keyslot count of one means one volume key, not
  that the passphrase is strong.
- **The Odile sign-in is not an SSH key.** The Webapp password is a scrypt hash
  in the tenant's `.env`; the `access_report.sh` row covers it, not this audit.

## 8. Evidence

The PASS run and the planted-key FAIL runs are the card's evidence; the exact
commands and their output are in the handoff for ODL-208, in the review that
requested this follow-up, and in the live replay transcript committed at
`docs/evidence/odl-208-audit.md` — including why the extra `/root/boot-key`
path is part of the replay. On the machine itself the same run is saved at
`/var/lib/odile/handoff-audit.txt` by the `--save` command in §2. The manifest
hash is
`539e205f7d26e1c0757a4cdabbcae444c98df10f6e68eff92b4c147604cf4e4e` for the
audit script and `881bc481045b0ac8d184c30986889c82dc04a48e8693781a97f90716e9c848d0`
for the release's signed `manifest.json` (`2026.11.13-6`).
