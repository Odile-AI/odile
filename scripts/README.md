# `scripts/` — the operator scripts

One file per job. A tenant gets them two ways: most are embedded in the signed
installer ([`installer/embed_*.py`](../installer)) and written to
`/usr/local/lib/odile/` (or `/usr/local/bin/odile-audit`), where `--self-test`
fails if an embedded copy differs; `manifest.py` is the exception — the image's
first boot copies it, and the manifest pair it checks, out of the release (see
[`installer/README.md`](../installer/README.md)).

| file | what it does | on a machine |
|---|---|---|
| [`audit.py`](audit.py) | the handoff audit: ten rows that prove the access claim | `/usr/local/bin/odile-audit` |
| [`backup.py`](backup.py) | restic backup, retention, drill, restore, password check | `/usr/local/lib/odile/backup.py` |
| [`heartbeat.py`](heartbeat.py) | the guest's signed heartbeat and the receiver that records it | — |
| [`mail_dsn.py`](mail_dsn.py) | turns delivery reports into records, so a bounce is read by a program | — |
| [`manifest.py`](manifest.py) | the app-layer manifest: build, sign, verify, diff, serve | `/usr/local/lib/odile/manifest.py` (image first boot, from the release) |
| [`provision_one.py`](provision_one.py) | provision exactly one real tenant on a Servury Blackbox | — |
| [`recovery_kit.py`](recovery_kit.py) | render the operator's recovery kit (install-time, not the user's file) | — |
| [`send_test.py`](send_test.py) | exercise the self-hosted mail path and read Gmail's verdict | — |
| [`verify_and_unlock.sh`](verify_and_unlock.sh) | the first-boot ceremony, and the access report | `/usr/local/lib/odile/verify_and_unlock.sh` |
| [`deploy.sh`](deploy.sh) | deploy this tree to the runtime and restart the service | — |

Each program is the authority on itself: `python3 scripts/<file> --help`.

## The ones you run for trust

### `manifest.py` — is this the app layer Odile signed?

```sh
# on the machine: the machine's own copy, its own key, no network
sudo /usr/local/lib/odile/manifest.py verify \
    --manifest /etc/odile/manifest.json \
    --signature /etc/odile/manifest.json.sig \
    --pubkey /etc/odile/odile-signing-key.pub

# or from a laptop: verify the bytes the machine serves against the repo's key
curl -fsS https://<slug>.odile.fyi/manifest -o m.json
curl -fsS https://<slug>.odile.fyi/manifest.sig -o m.json.sig
python3 scripts/manifest.py verify --manifest m.json --signature m.json.sig
```

`verify` prints `signature: OK` and one line naming the schema, the skill count,
the Hermes commit and the model; a changed byte breaks it. The document records
the installer's and the audit's sha256, the Hermes commit and lockfiles, every
Python and npm package version, the toolchain pins, the OS package versions,
each skill's content hash, and the model pin. `/manifest` and `/manifest.sig`
are served per tenant with `cache-control: no-store`.

**This is an operator signature, not a hardware measurement.** It says which
bytes Odile installed, not that they are safe — the CPU does not cover the app
layer (`docs/CLAIMS.md` gap 1). Build, sign, diff, serve: `manifest.py
build|sign|diff|serve`; the runbook is
[`docs/runbooks/manifest.md`](../docs/runbooks/manifest.md).

### `verify_and_unlock.sh --mode access` — the access report

```sh
sudo scripts/verify_and_unlock.sh --mode access --root / --user odile \
    --expect <96-hex pin> --out /tmp/report.txt
```

Five lines, each PASS or FAIL: zero usable user keys, the throwaway boot key
with no private half, the seal (policy `0x30000`, `seal_id`, measurement), the
sign-in hash, and no operator key. It scans every account's `authorized_keys`
and the private halves under the usual roots, and re-runs the seal check against
`--expect` — a number that does not match fails the seal line, which is the
point.

On a machine the boot unit runs the wrapper [`installer/access_report.sh`](../installer/access_report.sh),
which resolves the pin itself (`/etc/odile/tenant-image.pin` →
`boot-record.json` → `attestation.json` → `odile-verify.pin`) and writes
`/var/lib/odile/access-report.txt`; `/verify` renders that file. `--out` keeps
stdout the report itself — the provenance line exists only in the file — so
`tail -n +2 FILE` equals a hand run byte for byte. `--self-test` checks the
report shape and the FAIL cases with no machine.

Modes: `verify` (check only), `encrypt` / `unlock` (the first-boot ceremony),
`attest` / `report` (print the raw report), `access` (above). Runbook:
[`docs/runbooks/audit.md`](../docs/runbooks/audit.md).

### `audit.py` (`odile-audit`) — the handoff audit

```sh
sudo odile-audit                                   # the machine's own report
sudo odile-audit --save /var/lib/odile/handoff-audit.txt
sudo odile-audit --json                            # the same facts, machine-readable
python3 scripts/audit.py --self-test               # the fixtures, no machine needed
```

Ten rows, in order: `boot key`, `authorized keys` (both of sshd's default
files), `root password`, `password auth`, `operator key`, `operator account`,
`keyslots`, `agent socket`, `support grant`, `login history` — each printed with
the command that produced it. Exit `0` every row passed, `1` a row failed (the
report is still written), `2` bad input or a missing prerequisite. A mounted
machine can be audited without booting it (`--root`, `--cmdline`, `--luks-dump`,
`--auth-log`).

The audit is the machine's own testimony: a root shell can change every row, so
it is read beside the boot measurement, never instead of it. The signed
installer places it at `/usr/local/bin/odile-audit`; on a machine installed
before that step landed, run the same file from a checkout —
`sudo python3 scripts/audit.py` — or check `/usr/local/bin` first.

### `backup.py` — does the recovery password open the backup?

```sh
sudo /usr/local/lib/odile/backup.py verify          # the recovery password opens the repo
sudo /usr/local/lib/odile/backup.py drill           # create, back up, delete, restore, compare
sudo /usr/local/lib/odile/backup.py status
sudo /usr/local/lib/odile/backup.py restore latest --target /var/tmp/restore
```

`verify` is the one to run after a first visit: it reads the backup password
from the recovery file (`--recovery`, default `/etc/odile/recovery.txt`) and
proves the repository opens — a wrong password is refused. `drill` proves the
other half: a canary file is backed up, deleted, restored and compared. `init`,
`run`, `heartbeat` and `install` are the operator/timer side; the runbook is
[`docs/runbooks/backup.md`](../docs/runbooks/backup.md).

### `recovery_kit.py` — the operator's kit

```sh
python3 scripts/recovery_kit.py render --slug <slug> --ipv4 <address> \
    --instance-id <id> --token <64-hex> --measurement <96-hex> \
    --signing-key <file.pub> --out kit.txt
```

Renders [`templates/recovery-kit.md`](../templates) — the install-time file
that carries the setup token, the measurement to check and the signing key. It
is **not** the user's recovery file: that one is written on the machine at first
visit (`/etc/odile/recovery.txt`, see [`installer/README.md`](../installer/README.md)).
