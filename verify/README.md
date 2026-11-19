# `verify/` — checking a machine against its pin

Two programs carry the name:

| file | what it is |
|---|---|
| [`../odile-verify`](../odile-verify) (repo root, executable) | the standalone verifier a stranger runs on a laptop: it fetches one URL, reads the report out of the signed bytes, walks the AMD certificate chain, checks the ECDSA signature and compares the launch measurement to [`../odile-verify.pin`](../odile-verify.pin). Python 3 and the `openssl` binary; no pip, and no state written unless you ask for `--record`. |
| [`verify.py`](verify.py) | the machine's own claim table (ODL-236): every verification claim with where it was checked (`checked from your laptop` or `read from the machine`), rendered from the machine's records. It renders and is tested; **it is not yet what a guest serves at `/verify`** — that page is `installer/first_visit.py` rendering [`web/verify.html`](../web/verify.html) around the access report (`installer/README.md`). |

The long version — every row, every FAIL meaning, and the seven things the
report does **not** prove — is [`docs/runbooks/verify.md`](../docs/runbooks/verify.md).
What may be said publicly is [`docs/CLAIMS.md`](../docs/CLAIMS.md).

## 1. From your laptop, before you trust a machine

```sh
git clone https://github.com/Odile-AI/odile && cd odile
./odile-verify --expect <96-hex measurement> https://<slug>.odile.fyi
```

`<96-hex measurement>` is the number in the ready email and on the machine's
`/verify` page. A tenant run **requires** `--expect`: without it the command
exits 2, because the AMD chain alone proves a genuine CPU signed *a* report,
not that it is the report you expected. The demo is the one exception — it is
the `source` the pin names, so `./odile-verify https://odile.fyi` passes with
no argument.

Exit codes: `0` PASS, `1` FAIL, `2` usage or I/O error.

| row | the check | a FAIL means |
|---|---|---|
| `vcek` | the report's own VCEK was fetched from AMD's KDS for its raw chip id and reported TCB (a tenant; the demo binds the vendored VCEK) | the key server was unreachable or answered for another chip/TCB; the run fails closed |
| `chain` | `openssl verify` walks VCEK → ASK → ARK, and the ARK SHA-256 equals the one pinned in `tools/snp_report.py` | the signature is not rooted in AMD's Milan root |
| `cpu binding` | the VCEK's HWID and TCB extensions equal the report's `chip_id` and `reported_tcb` | the certificate is for another chip or TCB |
| `signature` | ECDSA P-384 over report bytes `[0, 0x2A0)` verifies with the VCEK | the report changed after the CPU signed it |
| `policy` | the guest policy is exactly `0x30000` | `DEBUG` or migration-agent bits are set, which voids the confidentiality claim |
| `tcb` | the committed SNP SPL is at least the AMD-SB-3020 floor | the firmware is rolled back below a known advisory floor |
| `report` | version 2/3/5, 1184 bytes, VMPL 0, ECDSA P-384 / SHA-384 | wrong structure or a lower-privilege report |
| `served` | the JSON summary agrees with the signed bytes | the summary is lying; trust the bytes |
| `release` | the selected release id is in the pin and the vCPU count was measured for it | the release is not pinned, or never measured at this size |
| `expected` | the signed measurement equals `--expect` | a different boot image, release or command line |
| `pin` | the signed measurement equals the pin's own record | a different image, or a stale pin |

## 2. Keep a boot record, and compare it after every reboot

```sh
./odile-verify --expect <measurement> --record boot-record.json https://<slug>.odile.fyi   # once
./odile-verify --expect <measurement> --previous boot-record.json https://<slug>.odile.fyi # every reboot
```

`--previous` prints `same`, or names exactly what moved:

| what moved | what it means |
|---|---|
| `release` | the machine booted a different pinned release |
| `measurement` only | the pin entry's bytes or parameters changed under the same id — a changed pin, not a changed machine |
| `cmdline_sha256` only | the machine was rebuilt or re-addressed; the command line is per machine |
| nothing (`same`) | same image, same machine |

The record is the user's file (`odile.boot-record.v1`); the installer keeps its
own copy at `/etc/odile/boot-record.json`.

## 3. The pin

`odile-verify.pin`, schema 2, has two halves:

- the top-level record is the **demo** machine's report — `measurement`,
  `report_version`, `policy`, `fetched_at`, `source`, `commit`;
- `releases[<id>]` is a **recipe**, not a number: the artifact hashes
  (`OVMF.amdsev.fd`, `vmlinuz`, `initrd.img`), the measurement parameters
  (`vcpu_type`, `guest_features`, `policy`, `vmm`), the `vcpu_counts` that were
  actually measured, and the build inputs (`kernel_version`,
  `kernel_package_sha256`, `debian_snapshot`, `edk2`).

The number is a function of the machine's command line, which is why a tenant
passes its own with `--expect`. `--print-pin` **merges** a fresh demo record
into the file and keeps the release map; only a release move touches it, and the
diff is a security review item (runbook §Refreshing the pin).

Other flags: `--release <id>` picks an entry explicitly, `--pin <file>`,
`--report <file>` (a saved attestation), `--amd-dir <dir>`, `--kds-base <url>`,
`--no-kds` (never fetch from AMD; vendored chain only).

## 4. The machine's own claim table (`verify.py`)

```sh
sudo /usr/bin/python3 verify/verify.py                    # the page on stdout (no flag)
sudo /usr/bin/python3 verify/verify.py --json             # the same rows, machine-readable
sudo /usr/bin/python3 verify/verify.py --out /var/lib/odile/verify.html
sudo /usr/bin/python3 verify/verify.py --serve --port 8099 # Caddy's upstream (binds 127.0.0.1)
```

It runs as root because the handoff audit reads `/etc/shadow` and sshd's
journal; it binds loopback (Caddy is the public listener) and writes nothing
except `--out` (atomic, 0644). On a bench, point it at a mounted root:

```sh
sudo /usr/bin/python3 verify/verify.py --root /mnt/guest --luks-dump /tmp/luks.txt \
    --now 2026-11-13T12:00:00Z
```

Every row carries its source. A root shell can change the machine rows; the
CPU's signature over the report is the half it cannot fake — which is why the
laptop run in §1 is the one to keep.

`verify.py` is the fuller page; the `/verify` a guest serves today is
`installer/first_visit.py` rendering `web/verify.html` with the access report
from `/var/lib/odile/access-report.txt` (see [`installer/README.md`](../installer/README.md)).
The two read the same records; neither is hardware-measured.

## The tests

`tests/test_verify.py` runs the real command against saved reports and the
vendored AMD chain with no network: a good report passes, a wrong pin fails, a
byte changed after signing fails the signature, a release absent from the pin
fails, `--expect` is required on a tenant, and `--print-pin` merges rather than
replaces. The `odile.boot-record.v1` record and its `--previous` comparison are
covered there too (`test_record_writes_the_section_43_record`,
`test_previous_says_same_or_names_the_field_that_moved`).
`tests/test_bootrecord.py` is a different thing: HQ's measured-boot record row
(ODL-398).
