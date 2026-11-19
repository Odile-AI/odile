# Verify — odile-verify and what the report proves

**Status:** we can prove the boot report; we cannot prove liveness or the app layer.
**Owner:** security seat · **Review:** before the 14 November submission and at V0.1.1.

`odile-verify` is the command a stranger runs after cloning the repository. It
fetches the machine's `/attestation`, reads the report out of the signed bytes,
verifies the AMD certificate chain, and compares the launch measurement to the
pin.

It uses Python 3 and the `openssl` binary; there is no pip install and no state
written. It fetches the machine's `/attestation` and AMD's Key Distribution
Service for the report's own VCEK, using the raw 64-byte chip id and the
reported TCB; the certificate must chain to the ARK pinned in this repository.
The VCEK is per chip and per TCB, so the repository vendors AMD's ASK and ARK,
not a machine's certificate. `--no-kds` refuses the second fetch.

The pin is a release pin: `releases[<id>]` carries the artifact hashes and the
measurement parameters, while the v1 top-level record is the demo machine's
number. A tenant selects its own release entry from the served document and
passes its own number with `--expect`; the demo keeps its v1 record, so the
command below still passes with no argument.

```sh
git clone <repo> odile && cd odile
./odile-verify https://odile.fyi
```

## What it checks

| Row | The check | A FAIL means |
|---|---|---|
| `vcek` | The report's own VCEK was fetched from AMD's KDS for the report's raw chip id and reported TCB (a `--amd-dir` that binds the report, such as the synthetic fixtures, skips the fetch) | The key server was unreachable, answered a non-certificate, or does not know the chip/TCB; the run fails closed rather than verifying with another chip's certificate |
| `chain` | `openssl verify` walks VCEK → ASK → ARK with the fetched VCEK and the pinned ASK/ARK (or a whole `--amd-dir` chain), then compares the ARK SHA-256 to the fingerprint pinned in `tools/snp_report.py` | The signature is not rooted in AMD's Milan root, or the pinned root was replaced |
| `cpu binding` | The VCEK's AMD HWID extension equals the report's `chip_id`, and its TCB extensions equal `reported_tcb` | The certificate is for another chip or another TCB; refresh the fixtures, or the KDS returned the wrong key |
| `signature` | `openssl dgst -sha384` verifies the ECDSA P-384 signature over bytes `[0, 0x2A0)` with the VCEK | The report changed after the CPU signed it |
| `policy` | The guest policy is exactly `0x30000` | `DEBUG` (bit 19) lets the hypervisor read memory; `MIGRATE_MA` (bit 18) permits a transport-key migration. Either one voids the confidentiality claim |
| `tcb` | The **committed** SNP SPL is at least `0x1B` (AMD-SB-3020, Milan) | The firmware is rolled back below a known advisory floor. The committed value is checked, not the hypervisor-settable `reported_tcb` |
| `report` | Version is 2, 3 or 5; size is 1184; VMPL is 0; the signature algorithm is ECDSA P-384 / SHA-384 | Wrong structure, a report from a lower-privilege layer, or an unexpected algorithm |
| `served` | The JSON `measurement`, `report_size` and `report_version` agree with the signed bytes | The summary is lying about the report; trust the signed bytes, not the JSON |
| `release` | The release id (`--release`, the served document's `release`, or neither) selects a `releases[<id>]` entry, and a reported vCPU count is one of its `vcpu_counts` | The release is not in the pin, the entry is malformed, `--release` and the served document disagree, or the machine's plan was never measured for that entry |
| `expected` | The signed measurement equals `--expect` | A different boot image, a different release, or a different command line |
| `pin` | The signed measurement equals the v1 top-level record (the demo, and the fallback when no release is selected) | A different boot image, a different release, or a stale pin |

The verifier extracts the measurement from the report, not from the JSON
summary. A host that edits the summary is caught by `served`; a host that edits
the report is caught by `signature`.

## The tenant document

A tenant serves the boot report, not a report fetched on page view. Servury's
`bb-seed` writes `/run/blackbox/attest.json` (0644) before switch-root; a
oneshot at boot transforms it into `/var/lib/odile/attestation.json` in the
shape this verifier reads: `available`, `guest`, `measurement` from 0x90:0xC0
of the signed bytes, `report_version`, `report_size`, `fetched_at`,
`report_b64`, `note`, `release` and the measured `cmdline`. `fetched_at` is
the boot report's write time, so a reboot refreshes it and the measurement
stays the same unless the image or the command line changed. There is no timer
and no listener on a tenant: Caddy serves the file.

On a first-visit machine the per-slug Caddy block names three paths, then
falls through to the Webapp:

```
handle /attestation {
    header Cache-Control "no-store"
    header Content-Type "application/json"
    rewrite * /attestation.json
    root * /var/lib/odile
    file_server
}

handle /verify {
    header Cache-Control "no-store"
    reverse_proxy 127.0.0.1:<first-visit port>
}

handle /first-visit* {
    header Cache-Control "no-store"
    reverse_proxy 127.0.0.1:<first-visit port>
}

handle {
    reverse_proxy 127.0.0.1:<webapp port> {
        header_up Host {host}
        flush_interval -1
    }
}
```

`/attestation` is the only route that serves a file from `/var/lib/odile`: it
rewrites every request to `/attestation.json`, so the request path never
reaches `file_server`. `/verify` and `/first-visit*` are not file routes — both
proxy to the same first-visit service on loopback: `/verify` renders the access
report (`/var/lib/odile/access-report.txt`) as HTML, and `/first-visit*` covers
the form, the recovery file and the reset form. Those two handlers are spliced
in only when that service is part of the machine
(`/etc/odile/first-visit.json`); a classic install has `/attestation` and the
catch-all alone, never a proxy to a dead port. Every other path falls through
to the Webapp, so no other file under `/var/lib/odile` is reachable over HTTP.
A missing boot report, or a status other than `ok`, serves `available: false`
with an `error`; the verifier fails closed on it.

This is the boot report, not a liveness proof. It does not measure the app
layer.

## The machine's own access report

`/verify` on a tenant shows five lines read from that machine — usable user
keys, the boot key, the seal, the sign-in hash, the operator key — under the
provenance line `read from your machine, <UTC time>`. The page's heading is
`The access report, from your machine` and the caption is that provenance line.
The 10 November bench block is the fallback for a machine with no report file
(the demo on HQ, a bench); a tenant with no file says the report has not been
written yet rather than showing the bench as its own state.

The report is a file:

    /var/lib/odile/access-report.txt   0644 root:root

written by a boot oneshot. The unit is `odile-access-report.service`; it runs
as root, before the agent unit, and again when a sign-in is set:

```sh
systemctl status odile-access-report.service     # Result=success after a run
journalctl -u odile-access-report.service -n 20  # the wrapper's two lines and the report
cat /var/lib/odile/access-report.txt             # exactly what /verify renders
```

The unit runs a wrapper, `/usr/local/lib/odile/access_report.sh`. It resolves
the pin, prints which file it used, and execs the strict bar:

```
verify_and_unlock.sh --mode access --root / --user odile --expect <pin> \
    --out /var/lib/odile/access-report.txt
```

The pin is the machine's own, in this order: `/etc/odile/tenant-image.pin`
(`uki_signed.measurement` — the signed UKI this image boots), then
`/etc/odile/boot-record.json` (the number the user passed to
`odile_install.py --expect`), then `/var/lib/odile/attestation.json` (the boot
report this machine produced), then `/etc/odile/odile-verify.pin`. No 96-hex
measurement in any of them exits 2 and writes nothing, so `/verify` says the
report has not been written rather than showing lines checked against nothing.

A FAIL is still a report: the five lines and their FAIL are written, and the
file is replaced in one rename so a reader never sees half of it. `--out` keeps
stdout the report itself — the provenance line exists only in the file — so
`tail -n +2 FILE` equals a hand run's output byte for byte.

The first visit sets the sign-in hash, and the report written at boot still
describes the placeholder `.env`. The POST therefore restarts the unit after it
writes the hash:

```
systemctl restart --no-block odile-access-report.service
```

`restart`, not `start`: the unit is `RemainAfterExit=yes` and already active
after boot, so a `start` would be a no-op and the page would keep the
placeholder line. After the restart the file's timestamp moves and the sign-in
line names the hash that is now on disk.

Reading it back by hand, on the machine:

```sh
head -n 1 /var/lib/odile/access-report.txt   # read from your machine, 2026-11-…
tail -n +2 /var/lib/odile/access-report.txt   # the five lines and the summary
```

### The fail-closed cases

Both are tampering a root shell could do, and both must reach the page. On a
running machine, change one thing, restart the unit, reload `/verify`:

```sh
# 1. A usable user key: one authorized_keys line that is not the measured throwaway.
#    The classic installer creates only /root/.ssh, so make the tenant's own
#    directory first: the report reads every account's authorized_keys, and a
#    key in the tenant's file opens the machine as well as one in root's.
install -d -o odile -g odile -m 0700 /home/odile/.ssh
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINotTheBootKeyOdileBench000000000000000000 odile' \
    >> /home/odile/.ssh/authorized_keys
systemctl restart odile-access-report.service
# the page: usable keys  FAIL  1 authorized key(s) other than the measured throwaway

# 2. No sign-in hash: remove the scrypt line the first visit wrote.
sed -i '/^HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=/d' /home/odile/.hermes/.env
systemctl restart odile-access-report.service
# the page: sign-in hash  FAIL  no scrypt hash in /home/odile/.hermes/.env
```

The unit's result is `Result=exit-code`: the reporter fails closed with status
1, so `systemctl status odile-access-report.service` says `failed` while
`/var/lib/odile/access-report.txt` holds the failing report. That is the report
talking, not a broken unit — a clean machine's run ends `Result=success`. The
bench proves the same two states on a booted image, not only in a unit test
(see the run log below).

A file that is present but is not what `--mode access --out` writes (wrong
line count, wrong tags, an unparseable timestamp, over 64 KiB) is never
silently replaced by the bench block: the page says the file is not in the
shape the reporter writes. Every value is escaped; the file is root-written,
but the page is public.

### The run log

Measured on the bench image built from this tree (QEMU TCG, the signed UKI,
the real seed service), 11 November 2026. The image's pin is `37ff12d6…` and
its seal_id is `odl454-bench-v1`. The cases ran on one installed disk,
chronologically: the installed boot at 12:53, the extra key at 13:06, the
deleted hash at 13:13, the first-visit boot at 13:35, the visit at 13:40. Each
boot read the disk the previous case left, so a FAIL in one transcript can be an
earlier case's state rather than the machine's own:

- The boot oneshot resolved the pin and printed the command it ran:
  `access-report: /etc/odile/tenant-image.pin pins 37ff…`, then
  `access-report: verify_and_unlock.sh --mode access --root / --user tenant
  --expect 37ff… --out /var/lib/odile/access-report.txt`. The file was 0644
  root:root. Its first line was `read from your machine, 2026-11-11T13:35:33Z`
  and its sign-in line was FAIL `no scrypt hash in /home/tenant/.hermes/.env` —
  the 13:13 case below had deleted that hash and this boot reused the same disk,
  so this is not an unvisited machine. The installed boot at 12:53 wrote
  `read from your machine, 2026-11-11T12:53:43Z` with `sign-in hash PASS scrypt
  hash present in /home/tenant/.hermes/.env`: an unvisited machine PASSes that
  line on the placeholder hash.
- The first visit's POST answered 200 (`Your sign-in is set`); the service
  logged `first-visit: sign-in set for 'tenant'` and `first-visit: the access
  report refresh requested`, and systemd started this unit again. The re-run
  wrote `read from your machine, 2026-11-11T13:40:20Z` with the sign-in line
  PASS. On a disk nobody has tampered with that refresh is the timestamp move
  alone — 13:35:33 to 13:40:20, the sign-in line already PASS on the
  placeholder — not a FAIL→PASS; on this disk it also replaced the hash the
  13:13 case had deleted. That restart is what makes the page describe the
  hash now on disk.
- One extra line in `/home/tenant/.ssh/authorized_keys` — one key that is not
  the measured throwaway — made the next run write
  `usable keys FAIL 1 authorized key(s) other than the measured throwaway in
  /home/tenant/.ssh/authorized_keys: SHA256:Sz5O5…`. The unit was `failed`
  (`Result=exit-code`) and the page still rendered the FAIL.
- Deleting the `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH` line made the next
  run write `sign-in hash FAIL no scrypt hash in /home/tenant/.hermes/.env`.
  That deletion is the state the 13:35 first-visit boot above inherited.

The scan reads every account's `authorized_keys` and every private half under
`/root`, `/home/*`, `/etc/odile` and `/var/lib/odile`, so its cost grows with
the tenant's home. On the bench (TCG, after the Hermes tree is installed) a
cold run took 2m58s–3m28s and the re-run after the first visit, with the page
cache warm, took 47 s. The unit is `Type=oneshot` with no start timeout and
runs `Before=` the agent unit, so `/verify` reads either the old file or the
new one, never half of one. A real machine is not emulated; ODL-332 records
the boot transcript and the page there.

## Which release is checked

`odile-verify` picks one `releases[<id>]` entry in `odile-verify.pin`, in this
order:

- `--release <id>` names it explicitly;
- otherwise the served document's `release` field names it;
- otherwise the v1 top-level record is used — that is the demo's number, and
  `./odile-verify https://odile.fyi` keeps working with no argument.

A release id that is not a key of `releases` is a FAIL, never a fallback to the
newest entry: the pin entry is what says which bytes and parameters the number
belongs to, and "latest" is not a release the user checked. `--release` that
disagrees with the served document is also a FAIL — the machine says which
release it booted.

The demo is the source named by the pin's top-level `source`
(`https://odile.fyi/attestation`). Any other source is a tenant machine, and
its expected number is `--expect`, the value the user recomputed with
`sev-snp-measure` or `blackbox-verify`. A tenant run without `--expect` is a
usage error (exit 2), not a PASS on the AMD chain alone: the chain proves a
genuine CPU signed a report, not that it is the report the user expected.

## A tenant run, from a laptop

The demo's report names the HQ chip and a tenant's names another, so every live
run fetches that report's own VCEK from AMD's KDS and checks it against the ARK
pinned in `tools/snp_report.py`; the run prints a `vcek` row for the fetch. The
tenant release (`2026.11.2`, from the served document) selects its entry in
`odile-verify.pin`, and `--expect` is the number in the ready email
(`templates/emails/ready.txt`). The commands are:

```sh
curl -fsSLO https://odile.fyi/odile-verify.tar.gz
echo 'bc304f8a22053f8f9a4c38922d53d17879fa81e719109c3de667af8c5912e13d  odile-verify.tar.gz' | sha256sum -c -
tar xzf odile-verify.tar.gz
cd odile-verify && ./odile-verify --expect <measurement from your ready email> https://<slug>.odile.fyi
```

On macOS, `shasum -a 256 -c -` reads the same hash line.

`sha256sum -c` prints `odile-verify.tar.gz: OK`; the tarball hash moves only
with the reviewed files inside it (the verifier, `tools/snp_report.py`, the pin
and AMD's ASK and ARK), never with the second the service built it. The number is
the one the machine's page and the ready email print; it is per machine,
because the measured command line carries the machine's own key, hostname and
address.

The verifier fetches two URLs on a live run: the machine's `/attestation`, and
the KDS object for the report's raw 64-byte chip id (128 hex characters) and
reported TCB. It does not fetch the pin, a log or the chain itself. If the KDS is
down the run is a FAIL with the reason on the `vcek` row — never a fallback to
another chip's certificate.

The chip id is the KDS's `hwid` path segment, not a digest of it: a
`sha256(chip_id)` URL is 64 hex characters and answers `400 Bad Request`.
`tests/test_verify.py` serves a saved tenant VCEK from a loopback KDS and
asserts the request path is the raw chip id and the reported TCB tuple.

### The tenant release pin

`releases["2026.11.2"]` is the release the live tenant boots; the HQ host that
served the demo booted it too.
It was copied from the release directory, not from prose:

```sh
REL=2026.11.2
curl -fsS "https://servury.com/downloads/blackbox/$REL/RELEASE.json" -o RELEASE.json
sha256sum RELEASE.json        # 0e5ca12f…e98f0 = the pin's release_json_sha256
sha256sum -c SHA256SUMS       # OVMF.amdsev.fd, vmlinuz, initrd.img, RELEASE.json: OK
```

The entry carries `vcpu_counts` `[4, 6]` — the plan sizes it was measured at.
The 6-vCPU HQ host that served the demo booted the loose artifacts above and
measured `e99728ef…3fbc`; the 4-vCPU tenant boots a signed UKI built from this
release (`odile-tenant.efi` `5cb31808…` + the same `OVMF.amdsev.fd`) and measures
`9f01725a…f137`. The entry's artifact hashes describe the loose release, so a
tenant's number is checked with `--expect`, not against those hashes;
§2026.11.2 carries both measurements. The 2026.11.1 entry stays — rotation is
additive, and a tenant still on it must keep verifying. `provision_release` is
the release a new order should boot (ODL-398); this verifier never reads it.

## What it does not prove

The PASS line says exactly what happened: the CPU signed this report and the
launch measurement matches the pin. Everything else is outside it.

1. **The boot report, not a liveness proof.** On a tenant the report is the
   one `bb-seed` wrote at boot, and a oneshot copies it to the served path
   (ODL-397); on HQ a five-minute timer refreshes a fetched report. It is a
   genuine report from this machine; replaying it later still passes. It does
   not prove the machine is up at the moment you read the page, and it is not a
   challenge to a live process. The command prints this sentence on every run
   except `--print-pin`, where stdout carries only the pin document.
2. **The app layer is not hardware-measured.** Measured boot covers firmware,
   kernel, initramfs and command line. Hermes, the installer and the skills are
   pinned by a signed manifest and a published hash, but the CPU does not
   measure them (CLAIMS.md gap 1).
3. **The console passes through the host.** The report does not cover the
   serial or VNC console. Never type the passphrase or a secret there
   (CLAIMS.md gap 2).
4. **Odile keeps three powers.** Stop/delete, DNS, and a different boot image
   from the panel. A different image is detectable here because the measurement
   changes, but only if you check (CLAIMS.md gap 3).
5. **The pin is provenance, not innocence.** `odile-verify.pin` and this
   verifier live in the same repository: they prove which image the machine
   ran, not that the image is honest. Closing that gap needs a record outside
   the repository — a signed release, or a network anchor. The product trusts
   GitHub alone until after V1.0 (`docs/architecture/ADR-anchor.md`).
6. **The TCB check is a floor, not an audit.** It checks the committed SNP SPL
   against AMD-SB-3020. It does not enumerate every CVE, and it does not floor
   the microcode level.
7. **The release label and the command line are machine-reported.** A root
   shell can edit the served `release` and `cmdline` fields. The CPU signature
   covers the measurement, and `--expect` is what catches a swapped image; the
   label selects a pin entry, it does not prove the label. The per-machine
   record is a description of a verified boot, not a second signature.

## Trust anchors

| Anchor | Value | How to check it out of band |
|---|---|---|
| AMD Milan ARK | SHA-256 `69:d0:63:b4:53:44:d2:6a:2e:94:e1:f4:21:0d:e4:9e:f5:55:30:82:87:d4:c1:74:44:5c:95:63:9a:54:0b:cd` | Compare with AMD's published ARK for Milan; the constant is in `tools/snp_report.py` |
| AMD ASK and ARK | `tests/fixtures/amd/ask.pem`, `ark.pem` | `openssl verify -CAfile ark.pem -untrusted ask.pem <the KDS VCEK>`; the VCEK is per chip and is fetched, not vendored |
| AMD KDS (every live run) | `https://kdsintf.amd.com/vcek/v1/Milan/<raw chip id>?blSPL=…&teeSPL=…&snpSPL=…&ucodeSPL=…` | The returned VCEK must chain to the pinned ARK and name the report's chip and TCB; a proxy on the TLS path can substitute only a certificate that still passes both |
| Synthetic fixture chain | `tests/fixtures/verify/synthetic-amd/` | Test-only: the fixture's `trust` block names the ARK SHA-256, and only a local `--report` file may use it. The shipped pin keeps AMD's ARK |
| Measurement | `odile-verify.pin` | The signed report itself, plus a rebuild of the measured release |
| Verifier code | `odile-verify`, `tools/snp_report.py` | Read both files: standard library and `openssl` only |

The report and the certificates are public; there is no secret in the verifier,
the fixtures or the output. The repository ships no VCEK and no chip id: the
test fixtures carry an `ODILE-SYNTHETIC-` marker, and the synthetic chain is a
test-only root. The HTTPS request only carries the bytes: the proof is the CPU
signature, not TLS. A proxy that rewrites the JSON is caught by `served` and
`signature`. The KDS fetch is over TLS, but the certificate it returns is not
trusted because of TLS: it must chain to the ARK pinned in the repository and
name the report's chip and TCB, and the report signature must verify with it. A
substituted KDS answer fails one of those three checks.

## The pin file

`odile-verify.pin` is a release pin plus the demo machine's record. Schema 2 is
additive: the v1 top-level fields are the demo's number, byte for byte, and the
new `releases` map is keyed by release id. A v1 verifier still reads the file.

```json
{
  "schema": 2,
  "provision_release": "2026.11.1",
  "measurement": "e997…3fbc",
  "report_version": 5,
  "policy": "0x30000",
  "fetched_at": "2026-11-13T01:55:04Z",
  "pinned_at": "2026-11-13",
  "source": "https://odile.fyi/attestation",
  "commit": "9678eb3ae28d736a975d24ac685efa29a1cce6ae",
  "releases": {
    "2026.11.1": {
      "release_url": "https://servury.com/downloads/blackbox/2026.11.1/",
      "release_json_sha256": "aa37…9a65",
      "artifacts": {
        "OVMF.amdsev.fd": "8731…958b",
        "vmlinuz": "9675…14f2",
        "initrd.img": "8c2a…eaf7"
      },
      "measurement_parameters": {
        "vcpu_type": "EPYC-v4", "guest_features": "0x1",
        "policy": "0x30000", "vmm": "QEMU"
      },
      "vcpu_counts": [4],
      "inputs": {
        "kernel_version": "6.12.115+deb13.1-amd64",
        "kernel_package_sha256": "6aa1…3b2a",
        "debian_snapshot": "20261103T000000Z",
        "edk2": "edk2 edk2-stable202505 unknown"
      },
      "pinned_at": "2026-11-08",
      "evidence": "docs/runbooks/verify.md §Evidence at pin time"
    },
    "2026.11.2": {
      "release_url": "https://servury.com/downloads/blackbox/2026.11.2/",
      "release_json_sha256": "0e5c…e98f0",
      "artifacts": {
        "OVMF.amdsev.fd": "8731…958b",
        "vmlinuz": "9675…14f2",
        "initrd.img": "f045…5092"
      },
      "measurement_parameters": {
        "vcpu_type": "EPYC-v4", "guest_features": "0x1",
        "policy": "0x30000", "vmm": "QEMU"
      },
      "vcpu_counts": [4, 6],
      "inputs": {
        "kernel_version": "6.12.115+deb13.1-amd64",
        "kernel_package_sha256": "6aa1…3b2a",
        "debian_snapshot": "20261103T000000Z",
        "edk2": "edk2 edk2-stable202505 unknown"
      },
      "pinned_at": "2026-11-13",
      "evidence": "docs/runbooks/verify.md §The tenant release pin"
    }
  }
}
```

The release entry says *which bytes and which parameters*; it deliberately
carries no measurement, because the number is a function of the machine's
command line. `vcpu_counts` lists only the plan sizes that were actually
measured: 2026.11.1 carries `[4]`; 2026.11.2 carries `[4, 6]` for the tenant's
4-vCPU Plus and the 6-vCPU plan the HQ host ran. `provision_release` is the
release a new order should boot (ODL-398 reads it); the verifier never uses it.

`commit` is the `main` HEAD the demo record was generated from; the pin lands
in the next commit, because a commit cannot contain its own hash. The card's
requirement — the repo commit that pinned it — is satisfied by that parent
commit plus the commit that carries this file.

On HQ, the `odile-attest-refresh` timer advances `fetched_at` every five
minutes while the measurement and the signature stay the same. On a tenant,
`fetched_at` is the boot report's write time and moves on a reboot. Do not pin
`fetched_at`; pin the measurement.

## The boot record

On a PASS with a pinned release selected, the command prints the per-machine
record from PIN-POLICY §4.3: `schema` `odile.boot-record.v1`, `release`, the
pin entry's `artifacts` and `measurement_parameters`, `vcpus` from the served
document (or from a single-element `vcpu_counts` when the document does not
report it), `cmdline_sha256` over the served command line with ` initrd=initrd`
stripped, `measurement`, `checked_at` and `verifier`.

```sh
./odile-verify --expect <measurement> --record boot-record.json https://<slug>.odile.fyi
./odile-verify --expect <measurement> --previous boot-record.json https://<slug>.odile.fyi
```

`--record` and `--previous` need the served document to carry `cmdline`: a
document without it fails closed (`served` FAIL) rather than writing a record
with an unknown command line. `attest.py` (on HQ) and the tenant's boot-report
transformer (ODL-397) are what put the measured command line into the document.

`--record` writes the record; `--previous` compares this boot with an earlier
one and names which of `release`, `cmdline_sha256` or `measurement` moved, or
says `same`. That is the sentence a bare FAIL cannot give:

| What moved | What it means |
|---|---|
| `release` | the machine booted a different pinned release — a release move |
| `measurement`, with `release` and `cmdline_sha256` unchanged | the pin entry's bytes or parameters changed under the same id — a changed pin, not a changed machine |
| `cmdline_sha256`, with `release` unchanged | the machine was rebuilt or re-addressed; the command line is per machine |
| nothing (`same`) | same image, same machine |

The record is the user's file, never committed. The installer writes its own
copy to `/etc/odile/boot-record.json` (ODL-396), so the app layer carries the
boot it was installed on.

## Refreshing the pin

Only after the machine's boot image has changed or a release has been promoted.
`--print-pin` writes only the pin document to stdout; the check transcript goes
to stderr. It **merges**: the fresh demo record replaces `measurement`,
`report_version`, `policy`, `fetched_at`, `pinned_at`, `source` and `commit`,
and `schema`, `provision_release` and every `releases` entry are kept. The
redirect below would otherwise delete the release map.

```sh
./odile-verify --print-pin https://odile.fyi > odile-verify.pin
./odile-verify --print-pin https://odile.fyi > /tmp/pin.again
diff odile-verify.pin /tmp/pin.again      # only fetched_at/pinned_at/commit may move
git diff -- odile-verify.pin
./odile-verify https://odile.fyi          # PASS against the new pin
make check
```

The change is a security review item: the diff must show exactly one new
measurement and a new `fetched_at`/`commit`, and no change to the `releases`
map. A release entry is generated from the release directory after the
measurement has been run twice and `blackbox-verify` agrees — `sha256sum -c
SHA256SUMS`, then the `RELEASE.json` hashes copied from the file, never retyped
from prose.

## Refreshing the AMD certificates

The machine is a Milan guest. The VCEK is per chip and per TCB, so a firmware
update, a host move or a rebuild on another chip needs no fixture refresh: the
verifier fetches the report's own VCEK from the KDS on every run. The repository
vendors AMD's Milan ASK and ARK, which change only when AMD rotates its roots.
The test fixtures are synthetic and are regenerated by
`tools/make_synthetic_report.py`, not fetched from AMD.

The chip id is one 64-byte value, and the KDS takes it in hex, not hashed:

| Where the chip id appears | Form | Length |
|---|---|---|
| the report, bytes `[0x1A0, 0x1E0)` | raw bytes | 64 bytes |
| AMD's KDS `hwid` path segment | those bytes in hex | 128 hex characters |
| the VCEK's HWID extension, OID `1.3.6.1.4.1.3704.1.4` | the same 64 bytes | 64 bytes |

`sha256(chip_id)` is 64 hex characters; it is neither a KDS input nor what the
extension carries, and a URL built from it answers `400 Bad Request`. The
ceremony wrapper passes the raw chip id for the same reason (`fetch_amd_certs`
in `scripts/verify_and_unlock.sh`).

```sh
# 1. Read the chip id — the KDS path segment — and the reported TCB.
curl -fsS https://odile.fyi/attestation -o /tmp/attestation.json
python3 - <<'PY'
import base64, json
r = base64.b64decode(json.load(open("/tmp/attestation.json"))["report_b64"])
chip, tcb = r[0x1A0:0x1E0], r[0x180:0x188]
assert len(chip) == 64, "the chip id is 64 bytes at 0x1A0"
print("chip_id", chip.hex())
print("tcb", "blSPL=%d teeSPL=%d snpSPL=%d ucodeSPL=%d" % (tcb[0], tcb[1], tcb[6], tcb[7]))
PY

# 2. Fetch AMD's Milan chain (ASK + ARK) and split it.
curl -fsS https://kdsintf.amd.com/vcek/v1/Milan/cert_chain -o /tmp/milan.pem
python3 - <<'PY'
import re
text = open("/tmp/milan.pem", encoding="utf-8").read()
certs = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n", text, re.S)
assert len(certs) == 2, "expected ASK then ARK"
open("tests/fixtures/amd/ask.pem", "w", encoding="utf-8").write(certs[0])
open("tests/fixtures/amd/ark.pem", "w", encoding="utf-8").write(certs[1])
PY

# 3. Fetch the VCEK for that chip id and TCB (both printed in step 1), to
#    inspect it. It is not vendored: the verifier fetches it per run.
curl -fsS "https://kdsintf.amd.com/vcek/v1/Milan/<chip_id>?blSPL=<a>&teeSPL=<b>&snpSPL=<c>&ucodeSPL=<d>" \
  -o /tmp/vcek.der

# 4. Verify the chain, then run the gate. The chain alone does not name a chip:
#    a VCEK from another chip verifies against ASK and ARK. `make check` runs
#    `cpu binding`, which is what catches the mismatch.
openssl x509 -inform DER -in /tmp/vcek.der -out /tmp/vcek.pem
openssl verify -CAfile tests/fixtures/amd/ark.pem \
  -untrusted tests/fixtures/amd/ask.pem /tmp/vcek.pem
make check

# 5. The synthetic test fixtures are generated, not fetched. A re-run rotates
#    their P-384 chain and rewrites the four documents; no AMD access needed.
/usr/bin/python3 tools/make_synthetic_report.py
```

Three pitfalls, all observed on this box on 7 and 10 November 2026:

- The key server rate-limits and sometimes answers `400 Bad Request` instead of
  `429`; retry the same URL after a second. A TCB value the KDS cannot encode
  (`blSPL=200`, `ucodeSPL=999`) returns `400` persistently — re-read
  `reported_tcb` rather than guessing. The KDS does not check the tuple against
  the chip: it signs the values you ask for (`blSPL=99` returns a certificate
  whose extension says 99), so a wrong but representable tuple returns `200`
  and step 4's `cpu binding` is what rejects it.
- The `hwid` segment is the chip id itself: 128 hex characters, never 64. A
  64-hex digest answers `400`; a well-formed chip id AMD does not know answers
  `404`. Both mean the segment is wrong, not that the key server is busy.
- `openssl x509 -text` prints the custom AMD extensions as escaped text. Use
  `openssl asn1parse -in vcek.pem -i` when checking the HWID and TCB by hand;
  the OIDs are `1.3.6.1.4.1.3704.1.4` (HWID) and `1.3.6.1.4.1.3704.1.3.{1,2,3,8}`
  (blSPL, teeSPL, snpSPL, ucodeSPL).

## Evidence at pin time

### 2026.11.1 (7–9 November 2026)

- `fetched_at` `2026-11-06T13:33:07Z`, from `https://odile.fyi/attestation`.
- Measurement `519c7c9aae54bf139cacd783e1b69bd98b01e79b9e8b52feb41aadb7a483f481227a8a3b64ea2b885234ca56e09c9ee8`.
  Independently recomputed on 7 November 2026 with `sev-snp-measure` from the
  published 2026.11.1 artifacts — `OVMF.amdsev.fd` `8731846c…958b`, `vmlinuz`
  `96758c40…14f2`, `initrd.img` `8c2aff57…eaf7`, all matching `RELEASE.json` —
  and this machine's `/proc/cmdline` (with the trailing `initrd=initrd`
  removed), 4 vCPU, `EPYC-v4`, guest features `0x1`. The stripped command line
  is 1144 bytes.
- Release directory re-fetched on 9 November 2026 from
  `https://servury.com/downloads/blackbox/2026.11.1/`: `sha256sum -c
  SHA256SUMS` → `OVMF.amdsev.fd: OK`, `vmlinuz: OK`, `initrd.img: OK`,
  `RELEASE.json: OK` (4/4). `RELEASE.json` itself hashes to
  `aa37e4ad…9a65`, which is the pin entry's `release_json_sha256`. The
  `SHA256SUMS` file is unsigned (`5ba79632…72dd`), so TLS to `servury.com`
  plus the 2026.09 rebuild remain the release's trust anchor; the pin records
  the hashes so a later swap is visible.
- Policy `0x30000`; committed TCB SNP SPL `29`, above AMD-SB-3020's `27`.
- ARK fingerprint `69:d0:63:b4:…:9a:54:0b:cd`.
- The launch measurement is the digest of firmware, kernel, initramfs and the
  command line; the command line is part of the measured image, so the SSH key
  in `/proc/cmdline` is covered by the same proof.

### 2026.11.2 (13 November 2026)

- The demo record was refreshed at `fetched_at` `2026-11-13T01:55:04Z` from
  `https://odile.fyi/attestation`; measurement
  `e99728efbbf23ae517bde68b7667448aa98ed8905e48cddae037ff1742cd10f3116f98b3ece36aad2e4c8b9e72643fbc`.
  It was recomputed with `sev-snp-measure` from the published 2026.11.2
  artifacts — `OVMF.amdsev.fd` `8731846c…958b`, `vmlinuz` `96758c40…14f2`,
  `initrd.img` `f045406e…5092`, all matching `SHA256SUMS` and `RELEASE.json` —
  and the live command line (1144 bytes after the ` initrd=initrd` strip),
  6 vCPU, `EPYC-v4`, guest features `0x1`, VMM `QEMU`: the recompute is
  `e99728ef…3fbc`, the signed measurement.
- The release directory was re-fetched on 13 November 2026 from
  `https://servury.com/downloads/blackbox/2026.11.2/`: `sha256sum -c
  SHA256SUMS` → `OVMF.amdsev.fd: OK`, `vmlinuz: OK`, `initrd.img: OK`,
  `RELEASE.json: OK` (4/4). `RELEASE.json` itself hashes to
  `0e5ca12f…e98f0`, the pin entry's `release_json_sha256`.
- `vcpu_counts` `[4, 6]`: the 4-vCPU tenant's signed UKI
  (`odile-tenant.efi` `5cb31808…` + `OVMF.amdsev.fd` `8731846c…`, 4 vCPU,
  `EPYC-v4`, guest features `0x1`) measures `9f01725a…f137`; the 6-vCPU HQ
  host's loose release measured `e99728ef…3fbc`. Both were measured against
  these artifacts.
- Policy `0x30000`; committed TCB SNP SPL `29` on the demo and `28` on the
  tenant, both above AMD-SB-3020's `27`; the tenant's reported (hypervisor-set)
  SNP SPL is `23`, which is why the floor is checked on `committed_tcb`.
- ARK fingerprint `69:d0:63:b4:…:9a:54:0b:cd`; the tenant's VCEK was fetched
  from AMD's KDS for chip `3fa18b…` and TCB `bl=4 tee=0 snp=23 ucode=213`.

### The audit behind the pin (13 November 2026)

Run by the security seat between 03:30Z and 03:56Z on the machine whose report
the pin records, from the release directory fetched to
`/var/tmp/odile-502-release/2026.11.2`. Transcripts:
`docs/ops/evidence/ODL-502/pin-time.txt` and `surfaces.txt`.

- **Two tools, one number.** `blackbox-verify` — fetched from the unversioned
  path, sha256 `8c937f02…0f97`, the copy `docs/CLAIMS.md` §4.4 audits — run in
  stdin mode on `/run/blackbox/attest.json` with `--release ./2026.11.2
  --vcpus 6` prints `PASS: this VM is a genuine SEV-SNP guest running the
  measured release` and binds `REPORT_DATA` to host key `SHA256:chXx…`. The
  independent recompute and the vendor verifier agree, so the pin is not one
  tool checking itself.
- **The shipped initramfs is the published source.** Every program that reads
  the passphrase or serves the report — `bb-shell`, `unlock.sh`,
  `fde-encrypt.sh`, `servury-handoff`, `servury-boot-follow` and the `bb-*` /
  `dropbear` init-premount and init-bottom scripts — is byte-identical (`cmp`)
  to `blackbox-images-2026.11.2.tar.gz` (`21bd5c8b…c2742`). `snpreport` is
  compiled from `src/snpreport.c` and was not rebuilt: this is provenance for
  the scripts, not reproducibility for the binary.
- **Every input moves the number.** One bit flipped in `OVMF.amdsev.fd`,
  `vmlinuz`, `initrd.img` or the command line yields four different digests
  (`382d0840…`, `75255288…`, `ede1ae19…`, `716df384…`); the control is
  `e99728ef…`. The pin would be decorative if any of them left it unchanged.
- **Only the initramfs moved.** `OVMF.amdsev.fd` `8731846c…` and `vmlinuz`
  `96758c40…` are byte-identical to the pinned 2026.11.1 entry; `initrd.img`
  moved `8c2aff57…` → `f045406e…`. The rotation is "same firmware and kernel,
  new initramfs", and the boot record names it.
- **The manifest is still unsigned.** `.asc` and `.sig` for `SHA256SUMS` and
  `RELEASE.json` answer `404` with an HTML body (60,081 bytes), so the release
  root remains TLS to `servury.com` plus the 2026.09 rebuild (PIN-POLICY §3,
  §8). The pin records the hashes so a later swap is visible; it cannot prove
  they came from Servury.
- **What this audit does not prove.** The initrd's compiled `/bin/snpreport`,
  the source tree's own reproducibility (the tarball is Servury's, not our
  rebuild), and the app layer (CLAIMS gap 1). It proves the published bytes are
  the bytes this machine booted, not that they are honest.

## Failure transcripts worth keeping

```sh
# Wrong pin (96 zero hex characters)
./odile-verify --expect "$(printf '00%.0s' $(seq 48))" https://odile.fyi   # FAIL, exit 1

# A report changed after the CPU signed it
python3 - <<'PY'
import base64, json
d = json.load(open("tests/fixtures/verify/synthetic-report.json"))
raw = bytearray(base64.b64decode(d["report_b64"])); raw[0x90] ^= 1
d["report_b64"] = base64.b64encode(bytes(raw)).decode()
# Next to the fixture so its `trust` block still resolves.
json.dump(d, open("tests/fixtures/verify/synthetic-tampered.json", "w"))
PY
./odile-verify --report tests/fixtures/verify/synthetic-tampered.json
# signature FAIL, exit 1
rm tests/fixtures/verify/synthetic-tampered.json

# A document from a release the pin does not carry
./odile-verify --report /tmp/unknown-release.json --expect <measurement>
#   release     FAIL  release not pinned: 2026.12.9
# FAIL, exit 1 — never a fallback to the newest pinned release

# A tenant run with no --expect: the chain alone is not a boot check
./odile-verify --report tests/fixtures/verify/synthetic-tenant.json
# odile-verify: error: a tenant run needs --expect <measurement>; ...
# exit 2

# A tenant report changed after the CPU signed it (the KDS-fetched VCEK)
python3 - <<'PY'
import base64, json
d = json.load(open("tests/fixtures/verify/synthetic-tenant.json"))
raw = bytearray(base64.b64decode(d["report_b64"])); raw[0x90] ^= 1
d["report_b64"] = base64.b64encode(bytes(raw)).decode()
json.dump(d, open("tests/fixtures/verify/synthetic-tenant-tampered.json", "w"))
PY
./odile-verify --report tests/fixtures/verify/synthetic-tenant-tampered.json \
  --expect 9f01725a…f137                          # signature FAIL, exit 1
rm tests/fixtures/verify/synthetic-tenant-tampered.json

# A KDS that does not answer: fail closed on the vcek row, no fallback
./odile-verify --report tests/fixtures/verify/synthetic-tenant.json \
  --expect 9f01725a…f137 --kds-base http://127.0.0.1:9
# vcek        FAIL  AMD KDS is unreachable at http://127.0.0.1:9/...
# exit 1
```

## Run log, 7 November 2026

```
$ ./odile-verify https://odile.fyi
  source           https://odile.fyi/attestation
  fetched_at       2026-11-06T13:33:07Z
  measurement      519c7c9aae54bf139cacd783e1b69bd98b01e79b9e8b52feb41aadb7a483f481227a8a3b64ea2b885234ca56e09c9ee8
  chain       PASS  VCEK -> ASK -> ARK verifies; ARK SHA-256 69:d0:63:b4:…:9a:54:0b:cd
  cpu binding PASS  the VCEK names this chip and TCB (bl=4 tee=0 snp=23 ucode=213)
  signature   PASS  ECDSA P-384 over the 672-byte report verifies with the VCEK
  policy      PASS  policy 0x30000: SMT allowed, DEBUG clear, migration agent not permitted
  tcb         PASS  committed bl=4 tee=0 snp=29 ucode=213; SNP SPL 29 >= 27 (AMD-SB-3020)
  report      PASS  version 5, 1184 bytes, VMPL 0, ECDSA P-384 / SHA-384
  served      PASS  the JSON summary agrees with the signed bytes
  pin         PASS  matches odile-verify.pin (fetched_at 2026-11-06T13:33:07Z, commit 92af143…)
PASS — the CPU signed this report and the launch measurement matches the pin.
$ echo $?
0
```

```
$ ./odile-verify --expect <96 zeros> https://odile.fyi
  expected    FAIL  is 519c7c9a…9ee8, --expect says 0000…0000
FAIL — the report did not verify against the chain and the pin.
$ echo $?
1
```

```
$ ./odile-verify --report /tmp/tampered.json      # one bit flipped at 0x90
  signature   FAIL  the report signature does not verify with the VCEK
  served      FAIL  the JSON measurement is not the signed measurement
FAIL — the report did not verify against the chain and the pin.
$ echo $?
1
```

## Run log, 9 November 2026 (v2)

A saved document whose summary carries `release` and `cmdline` — the shape
`attest.py` writes and ODL-397 serves — selects the release entry from the
document, and `--expect` is the user's recomputed number:

```
$ ./odile-verify --report /tmp/v2-attestation.json --expect 519c…9ee8 --record /tmp/boot-record.json
  served      PASS  the JSON summary agrees with the signed bytes
  release     PASS  2026.11.1 is pinned (3 artifacts, vcpu_counts [4])
  expected    PASS  matches --expect 519c7c9a…9ee8
  record           written to /tmp/boot-record.json
PASS — the CPU signed this report and the launch measurement matches the pin.
$ echo $?
0

$ ./odile-verify --report /tmp/v2-attestation.json --expect 519c…9ee8 --previous /tmp/boot-record.json
  previous         same
```

The record `/tmp/boot-record.json` carries `schema` `odile.boot-record.v1`,
`release` `2026.11.1`, the entry's three artifact hashes and
`measurement_parameters`, `vcpus` `4`, `cmdline_sha256`, the measurement,
`checked_at` and `verifier`. A previous boot on the same machine prints
`same`; a moved `release`, `cmdline_sha256` or `measurement` prints the field
that moved.

`env -i PATH=/usr/bin:/bin ./odile-verify https://odile.fyi` exits 0, so the
command needs no environment beyond Python 3 and `openssl`. `make check` on
this branch: 590 tree tests and 20 meter tests, shellcheck, claims lint and
brand lint all green.

## Run log, 13 November 2026 (the demo and the tenant)

The demo still passes with no argument, against the refreshed demo record:

```
$ ./odile-verify https://odile.fyi
  source           https://odile.fyi/attestation
  fetched_at       2026-11-13T01:55:04Z
  measurement      e99728efbbf23ae517bde68b7667448aa98ed8905e48cddae037ff1742cd10f3116f98b3ece36aad2e4c8b9e72643fbc
  chain       PASS  VCEK -> ASK -> ARK verifies; ARK SHA-256 69:d0:63:b4:…:9a:54:0b:cd
  cpu binding PASS  the VCEK names this chip and TCB (bl=4 tee=0 snp=23 ucode=213)
  signature   PASS  ECDSA P-384 over the 672-byte report verifies with the VCEK
  policy      PASS  policy 0x30000: SMT allowed, DEBUG clear, migration agent not permitted
  tcb         PASS  committed bl=4 tee=0 snp=29 ucode=213; SNP SPL 29 >= 27 (AMD-SB-3020)
  report      PASS  version 5, 1184 bytes, VMPL 0, ECDSA P-384 / SHA-384
  served      PASS  the JSON summary agrees with the signed bytes
  release     PASS  2026.11.2 is pinned (3 artifacts, vcpu_counts [4, 6])
  pin         PASS  matches odile-verify.pin (fetched_at 2026-11-13T01:55:04Z, pinned from 9678eb3ae28d736a975d24ac685efa29a1cce6ae)
PASS — the CPU signed this report and the launch measurement matches the pin.
$ echo $?
0
```

The live tenant is another chip, so the run fetches its own VCEK. This is the
`odl-7971a8` report the server has served since its last boot; the verifier
fetched the VCEK for chip `3fa18b…` and TCB `bl=4 tee=0 snp=23 ucode=213`
from `https://kdsintf.amd.com`, and the certificate chained to the same pinned
ARK:

```
$ ./odile-verify --expect 9f01725afd71968f74e6dec5f7768b9dfaf52a46cbcfe8a49e106b5cff94028080a47c3d7f56dd7c5c49e6781517f137 \
    https://odl-7971a8.odile.fyi
  source           https://odl-7971a8.odile.fyi/attestation
  fetched_at       2026-11-13T01:13:51Z
  measurement      9f01725afd71968f74e6dec5f7768b9dfaf52a46cbcfe8a49e106b5cff94028080a47c3d7f56dd7c5c49e6781517f137
  kds              https://kdsintf.amd.com/vcek/v1/Milan/<chip-id>?blSPL=4&teeSPL=0&snpSPL=23&ucodeSPL=213
  vcek        PASS  fetched the report's own VCEK from AMD KDS for chip <chip-id> TCB bl=4 tee=0 snp=23 ucode=213 (sha256 <per-fetch digest>); the chain is checked against the pinned ARK
  chain       PASS  VCEK -> ASK -> ARK verifies; ARK SHA-256 69:d0:63:b4:…:9a:54:0b:cd
  cpu binding PASS  the VCEK names this chip and TCB (bl=4 tee=0 snp=23 ucode=213)
  signature   PASS  ECDSA P-384 over the 672-byte report verifies with the VCEK
  policy      PASS  policy 0x30000: SMT allowed, DEBUG clear, migration agent not permitted
  tcb         PASS  committed bl=4 tee=0 snp=28 ucode=213; SNP SPL 28 >= 27 (AMD-SB-3020)
  report      PASS  version 5, 1184 bytes, VMPL 0, ECDSA P-384 / SHA-384
  served      PASS  the JSON summary agrees with the signed bytes
  release     PASS  2026.11.2 is pinned (3 artifacts, vcpu_counts [4, 6])
  expected    PASS  matches --expect 9f01725a…f137
PASS — the CPU signed this report and the launch measurement matches the pin.
$ echo $?
0
```

The KDS's VCEK is re-signed per request (the serial and validity move, the
key does not), so the `vcek` sha256 is a per-fetch identifier, not a pin. The
stable values are the chip id, the TCB, and the measurement.

A byte changed at `0x90` after the CPU signed it fails the signature even with
the tenant's own VCEK; the run exits 1:

```
$ ./odile-verify --report /tmp/tenant-tampered.json --expect 9f01725a…f137
  measurement      9e01725afd71968f74e6dec5f7768b9dfaf52a46cbcfe8a49e106b5cff94028080a47c3d7f56dd7c5c49e6781517f137
  vcek        PASS  fetched the report's own VCEK from AMD KDS …
  signature   FAIL  the report signature does not verify with the VCEK
  served      FAIL  the JSON measurement is not the signed measurement
  expected    FAIL  is 9e01725a…f137, --expect says 9f01725a…f137
FAIL — the report did not verify against the chain and the pin.
$ echo $?
1
```

The independent reimplementation agrees without `odile-verify`'s report
parser: `sev-snp-measure` over the published `odile-tenant.efi` (`5cb31808…`)
and `OVMF.amdsev.fd` (`8731846c…`) at 4 vCPU, `EPYC-v4`, guest features `0x1`
prints `9f01725a…f137`; the same `openssl` chain walk over the KDS VCEK
prints `Verify OK`. Both commands are in the card's evidence.

## The NEAR testnet anchor is removed

ODL-323 wrote the demo machine's measurement to a disposable NEAR testnet
contract and linked the transaction from `/verify`. ODL-421 removed it on
9 November 2026: the contract and its CLI and wasm, the public record,
`tests/test_anchor.py`, `docs/runbooks/near-anchor.md`, the `/verify` section
and its explorer link are gone, and no network record replaced them. The reason
is in `docs/architecture/ADR-anchor.md`: the pin in the repository is the trust
root until after V1.0.

The disposable key at `/root/.odile/near-anchor-testnet.pem` was deleted after
the page stopped linking the account. It was outside git and worth nothing, so
deleting the file is the whole cleanup; the abandoned testnet account holds no
value and needs no transaction.

## Residual risks

| Risk | Owner | Review |
|---|---|---|
| A genuine report can be replayed; `odile-verify` does not challenge the machine with a nonce | security | V0.1.1 sealed auto-unlock |
| The pin lives in the same repository as the verifier; a compromised repo can pin a different image | Savio | after V1.0, if a customer asks (`docs/architecture/ADR-anchor.md`) |
| The vendored roots are AMD's Milan ASK/ARK; a root rotation needs a reviewed refresh | security | on every release or AMD root change |
| A live run needs AMD's KDS and TLS to `kdsintf.amd.com`; the KDS can be slow or down, and the VCEK is re-signed per request | security | every release; `--no-kds` is the offline fallback for a directory that carries a VCEK (the synthetic fixtures) |
| The synthetic test fixtures carry an `ODILE-SYNTHETIC-` chip id and a test-only root; the scanner skips that marker and reports any other chip id | security | when `tools/make_synthetic_report.py` changes |
| The KDS answer is trusted only after the pinned-ARK chain, the VCEK's chip/TCB binding and the report signature all pass | security | every release |
| The TCB floor covers SNP SPL only; microcode is not floored | security | when AMD publishes a new advisory |
| `openssl` and the Python interpreter are trusted binaries | security | base image review |
| The served `release` and `cmdline` are machine-reported; a root shell can edit them | security | every release |
