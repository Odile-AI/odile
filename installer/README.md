# `installer/` — the signed installer, the first visit, and the machine's own evidence

| file | what it is |
|---|---|
| [`odile_install.py`](odile_install.py) | the installer: one printable, signed file, run as root (or as the tenant user on the image) |
| [`odile_install.py.sig`](odile_install.py.sig) | the detached signature over the exact bytes of that file |
| [`odile-signing-key.pub`](odile-signing-key.pub) | the published Ed25519 public key; the recovery kit carries the same bytes |
| [`odile-signing-key.prev.pub`](odile-signing-key.prev.pub) | the previous key, kept so an old kit still explains itself |
| [`first_visit.py`](first_visit.py) | the first-visit service: sets the Odile sign-in on the machine, shows the recovery code once, serves the recovery file behind it, and resets the sign-in with the code |
| [`access_report.sh`](access_report.sh) | the boot wrapper that resolves the pin and writes `/var/lib/odile/access-report.txt` — the file `/verify` renders |
| `embed_*.py` | the only supported way to refresh the copies the installer carries; each one rewrites its block and its SHA-256 pin in `odile_install.py` |
| [`sim_tenant.py`](sim_tenant.py) | a simulated tenant for the installer's self-test |

The long versions: [`docs/runbooks/install.md`](../docs/runbooks/install.md),
[`docs/runbooks/audit.md`](../docs/runbooks/audit.md),
[`docs/runbooks/manifest.md`](../docs/runbooks/manifest.md).

## Check the installer before you run it

```sh
./installer/odile_install.py --verify-signature \
    --pubkey installer/odile-signing-key.pub --signature installer/odile_install.py.sig
./installer/odile_install.py --self-test        # the pure functions and every embedded pin
./installer/odile_install.py --print-source     # the exact program, for reading
./installer/odile_install.py --manifest         # the static manifest, as JSON
```

`--verify-signature` prints the key's SHA-256 and fingerprint, verifies the file
against the detached signature, and exits without changing anything. `--self-test`
checks each embedded copy against its source and its pinned SHA-256, so a stale
pair cannot ship green. Run as root it needs `--expect` (or a boot record) — the
96-hex measurement `odile-verify` printed — and asserts the boot report's
release with `--release`.

## What it puts on the machine

**The installer places** (a tenant run):

- the Webapp, the Hermes checkout and the skills bundle, and the Caddy site;
- `/usr/local/bin/odile-install.sh` — the copy that ran;
- `/usr/local/lib/odile/backup.py`;
- `/etc/odile/`: `key.env` (the only inference credential, 0600 root),
  `support-key.pub`, `backup-repo.txt`, `recovery.txt`, and `boot-record.json`
  when it was given `--expect`;
- the units `odile-agent.service`, `odile-attest-boot.service`,
  `odile-backup-tenant.service` and its timer.

**Only in first-visit mode** (`--first-visit`, the image's path) it also writes
the evidence surface and the first-visit service:
`/usr/local/lib/odile/verify.html`, `access_report.sh`, `verify_and_unlock.sh`,
`first_visit.py`, `first-visit.html` and its icons, `/usr/local/bin/odile-audit`,
`/etc/odile/first-visit.json` (the token hash — written once), and the
`odile-first-visit.service` + `odile-access-report.service` units. A classic
install without that flag has none of them.

**The image's first boot places** (`deploy/image/`, before the installer runs):

- `/usr/local/lib/odile/manifest.py` and `odile-manifest.py`, with
  `/etc/odile/manifest.json` + `.sig` and `odile-manifest.service`
  (`odile-firstboot.py`);
- `/etc/odile/odile-signing-key.pub` (and `.prev.pub`) — `sealed-image.sh`,
  `odile-release-setup.sh`;
- `/etc/odile/tenant-image.pin` — `place-pin.sh`, `uki.sh`;
- `/etc/odile/boot-key.pub` and `/etc/odile/seal-id` — `sealed-image.sh`.

The installer **reads** those image files (the pin, the seal id, the manifest
pair) but does not write them; the manifest pair is what `/manifest` serves.

## The access report (`/verify`)

```sh
cat /var/lib/odile/access-report.txt          # 0644 root — exactly what /verify renders
head -n 1 /var/lib/odile/access-report.txt    # the provenance line
systemctl status odile-access-report.service
```

`access_report.sh` resolves the pin, prints which file it used, and execs the
strict bar:

```
verify_and_unlock.sh --mode access --root / --user <tenant> --expect <pin> \
    --out /var/lib/odile/access-report.txt
```

The five lines — zero usable user keys, the throwaway boot key with no private
half, the seal, the sign-in hash, no operator key — are written as a PASS or a
FAIL; a FAIL is still a report, and the file is replaced in one rename. To check
the page is not lying, re-run the wrapper into a throwaway file and diff:

```sh
sudo -n env ODILE_ACCESS_USER=odile ODILE_ACCESS_OUT=/tmp/report.txt \
     /usr/local/lib/odile/access_report.sh
tail -n +2 /var/lib/odile/access-report.txt > /tmp/a.txt
tail -n +2 /tmp/report.txt                  > /tmp/b.txt
diff /tmp/a.txt /tmp/b.txt                  # no output = identical
```

The image drop-in sets `ODILE_ACCESS_USER=tenant`; outside systemd pass
`ODILE_ACCESS_USER` for the machine's own account, or the reporter looks for the
wrong home.

## The seal, checked by hand

```sh
lsblk -o NAME,SIZE,TYPE,FSTYPE                 # the crypto_LUKS partition
cat /etc/odile/tenant-image.pin                 # uki_signed.measurement — the image's own number
tr ' ' '\n' < /proc/cmdline | grep seal_id      # the per-tenant seal id
sudo cryptsetup luksDump /dev/<luks-device> | sed -n '/^Keyslots:/,/^Digests:/p'
```

What a clean machine shows: LUKS2, **one** keyslot (`aes-xts-plain64`,
argon2id) and an **empty** `Tokens:` section — one volume key, no second way in.
The `seal` line of the access report checks the policy (`0x30000`), the
`seal_id` from the measured command line, and that the measurement equals the
pin. Change the image and the disk stays locked; that is the sentence, and these
four commands are how a sceptic checks it.

## The first visit and the recovery file

`first_visit.py` is the only page that sets the sign-in. The installer writes a
single-use token's SHA-256 and the unit; the user opens
`https://<slug>.odile.fyi/first-visit?t=<token>`, chooses the password that opens
the Webapp, and is shown a recovery code once. Routes: `GET/POST /first-visit`,
`GET /first-visit/recovery?c=<code>`, `GET/POST /first-visit/reset`,
`GET /health`.

```sh
sudo cat /etc/odile/recovery.txt    # 0600 root — the agent holds sudo, the tenant user does not
```

The file holds the backup password and the certificate SHA-256, and it is the
only copy: the page serves the same bytes for download, behind the recovery
code. While the machine is running the agent can show it again; if the machine is
gone, the copy the user kept is the one that opens the backup. `backup.py verify`
proves that password opens the repository (see [`scripts/README.md`](../scripts/README.md)).

## Embedded copies, and the signature

Everything the installer ships inside its own file is pinned by SHA-256 and
regenerated only by its `embed_*.py` tool:

| tool | carries |
|---|---|
| [`embed_verify.py`](embed_verify.py) | `web/verify.html`, `access_report.sh`, `verify_and_unlock.sh`, `scripts/audit.py` |
| [`embed_first_visit.py`](embed_first_visit.py) | `first_visit.py` and its page |
| [`embed_tenant.py`](embed_tenant.py) | `templates/tenant-agents.md` and every `skills/<name>/SKILL.md` |
| [`embed_backup.py`](embed_backup.py) | `scripts/backup.py` |
| [`embed_support.py`](embed_support.py) | the support-grant program |
| [`embed_export.py`](embed_export.py) | the export program |

After any change to an embedded source: run the tool, then
`./installer/odile_install.py --self-test`, then re-sign — the private key is
`/root/.odile/installer-signing.pem`, mode 0600, never in git:

```sh
openssl dgst -sign /root/.odile/installer-signing.pem \
    -out installer/odile_install.py.sig installer/odile_install.py
```

The manifest signs with the same identity, so one published key explains both.
The release pair is cut by `tools/recut_release.py` (see [`release/`](../release)).
