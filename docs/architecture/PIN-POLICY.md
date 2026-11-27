# The measurement pin — policy

**Status:** Accepted · 8 November 2026 · Architect · refreshed 10 November 2026 at `main` `4280a1e`
**Answers:** ODL-393 — where a release's measurement comes from, who checks it,
how it ships, how it rotates, and what stops when it does not match.
**Related:** `docs/architecture/ADR-image-vs-installer.md` §4, §6 item 3 and the
review in §14; `docs/architecture/ADR-anchor.md` §4–§6; `docs/CLAIMS.md` §1
("Measured boot…"), §3 gap 1 and §4.4–§4.6; `docs/runbooks/verify.md`;
`docs/runbooks/install.md`; `docs/runbooks/manifest.md`; cards ODL-201,
ODL-202, ODL-208, ODL-214, ODL-319, ODL-332, ODL-395–ODL-404, ODL-420, ODL-421.
**Decision owner:** Architect. The pin schema, the rotation rule and the
fail-closed table are this seat's. A new provider, a new trust boundary or a
stronger public claim still goes to Savio.

---

## 1. The invariant at risk

The product's access claim rests on a boot the user checked, not on a boot we
say we checked. Four properties make that falsifiable:

- **P1 — the number is derived, not copied.** For every tenant boot, the
  48-byte launch measurement in the CPU-signed report equals the value
  recomputed from the published release artifacts, the command line the
  machine itself reports, its vCPU count and the measurement parameters. If it
  does not, the ceremony stops.
- **P2 — the recipe the user checks is the recipe the machine booted.** The
  release id in the report selects the release entry in the pin. A release
  that is not pinned is a stop, never a fallback to "latest".
- **P3 — no secret reaches the machine before P1 and P2 hold.** The setup
  token, the NEAR key and the mail relay secret are fetched only after the
  boot is checked.
- **P4 — a rotation is a reviewed, additive diff.** A new Servury release adds
  an entry; it does not replace the entry a running tenant still boots.

A pin that is one number someone copied from a page satisfies none of these.
The rest of this document is the mechanical answer to each.

---

## 2. What the number is, and what it is not

A SEV-SNP launch measurement is a SHA-384 digest over the initial guest state.
For Servury's measured release it covers:

| Input | Where it comes from |
|---|---|
| Firmware | `OVMF.amdsev.fd` in the release directory |
| Kernel | `vmlinuz` in the release directory |
| Initramfs | `initrd.img` in the release directory |
| Command line | the machine's own `/proc/cmdline`, with the trailing ` initrd=initrd` stripped — 1144 bytes on the 4-vCPU Blackbox measured for this policy |
| vCPUs | the plan the machine was ordered at (Plus: 4) |
| vCPU type, guest features, guest policy, VMM | `RELEASE.json` → `measurement_parameters`: `EPYC-v4`, `0x1`, `0x30000`, `QEMU` |

It does **not** cover the disk, the app layer, the host key, or the TCB. The
host key is bound separately in the report's `REPORT_DATA` (Servury's
`bb-attest` puts the SHA-256 of the boot's host key there). The TCB selects
which VCEK signs the report; a TCB change moves the certificate, not the
measurement.

Because the command line carries the tenant's IPv4, boot key, hostname and
instance id, **the number is per machine**. A number copied from another
machine is not a pin; it is a coincidence waiting to fail.

---

## 3. Where a release's measurement comes from

Two different things are called "the measurement" and they must not be
confused: the **expected** number (computed from published bytes and the
machine's command line) and the **reported** number (signed by the CPU). The
check is the equality of the two.

| Source | What it gives | Whose it is | How it is checked |
|---|---|---|---|
| Servury release directory, e.g. `https://servury.com/downloads/blackbox/2026.11.1/` | the three artifacts, `SHA256SUMS`, `RELEASE.json` (release id, artifact hashes, kernel package, snapshot, edk2 tag, measurement parameters) | Servury's | `sha256sum -c SHA256SUMS`; the hashes are recorded in the pin |
| The machine's boot report | firmware, kernel, initramfs and command line as the CPU saw them; `report_b64`, `cmdline`, `release`, `hostkey`, `status` | the CPU's, via Servury's initramfs | written to `/run/blackbox/attest.json` at boot (mode 0644); the `attest` forced command serves it at the initramfs |
| The recompute | the expected digest | ours | `sev-snp-measure`, the exact command in §5; Servury's `blackbox-verify` does the same arithmetic and also binds the host key |
| `odile-verify` | the AMD chain, the report signature, the policy, the TCB floor, and the equality of the reported number to the pin (demo) or to `--expect` | ours | `./odile-verify`, standard library plus `openssl`, two URLs — the machine under test, and AMD's KDS for that report's own VCEK — and no fetch of the pin, a log or the chain above the VCEK (ADR-002 §6 and its 19 November 2026 addendum, §6a) |

The rule: **the expected number is computed from the release artifacts and
the machine's own command line; it is never copied from a release page and
never taken from the machine's JSON summary.** `odile-verify` reads the
measurement out of the signed report bytes precisely because the summary can
lie.

Servury's `SHA256SUMS` is **unsigned** (audit finding, 5 November 2026). The
release's root of trust is therefore TLS to `servury.com`, plus independent
evidence that the published bytes are the bytes that boot: our 2026.09 rebuild
(OVMF, initrd and the Debian kernel identical) and the 2026.11.1 in-place
verification. `RELEASE.json` records the edk2 tag but not its commit
(`edk2-stable202505 unknown`); the pin records the field as it is, `unknown`
included, so nobody later mistakes it for a proved input. `docs/MVP-V0.1.md`
§7 (the claims ladder) carries the caveat in its allowed public wording:
"Measured boot against a published release. The release manifest is not yet
signed — pin the hashes or rebuild it." `docs/CLAIMS.md` §4.6 is a different
document — the **app** manifest, which Odile signs; it says nothing about
Servury's release manifest.

### 3.1 The trust root of the pin

**The pin is trusted as a signed file in the public repository.** The trust root
is the repository: `odile-verify.pin` is committed, so the bytes a reader checks
are the bytes at a commit they chose, and every change to it is a dated, diffable
line of public history. Two things that sentence must not be read to promise,
because neither is true today:

- **No detached signature covers the pin.** There is no `odile-verify.pin.sig`.
  Odile's Ed25519 key (`installer/odile-signing-key.pub`) signs the app manifest
  and the release index, not this file, and `odile-verify` reads the pin as JSON
  and verifies no signature over it.
- **No third party timestamps it.** Nothing proves the pin existed before a given
  boot except GitHub's own history. That is exactly the property the NEAR testnet
  anchor bought for one machine, and the reason it is removed by ODL-421: it cost
  more than it was worth (`docs/architecture/ADR-anchor.md`).

The pin is where the recipe is *written down*, not where it is *certified*. What
a reader can always do is repeat the derivation of §2 and §5.1 from Servury's
published artifacts and the machine's own command line — and that is the check
this policy is built on. ADR-002 §5 records why V0.1 does not add a signature
over the pin (a signature without a timestamp does not give the property a chain
would, and a signature nothing verifies goes stale silently), and §6 of that ADR
records the rule this section protects: `odile-verify` fetches no URL but the
machine under test and the VCEK AMD publishes for its chip — never the pin,
never the chain above the VCEK, never a log. §6a of that ADR records the 19
November 2026 change behind the second URL: the demo chip's vendored VCEK was
deleted because the certificate names the chip, so the verifier now asks AMD's
KDS for the report's own VCEK and checks it against the pinned ARK.

---

## 4. The repo artefact — `odile-verify.pin`

### 4.1 What exists today (v1)

`odile-verify.pin` is one JSON document holding one machine's number — the
`odile.fyi` demo host — plus where it came from:

```json
{
  "measurement": "519c7c9a…9ee8",
  "report_version": 5,
  "policy": "0x30000",
  "fetched_at": "2026-11-06T13:33:07Z",
  "pinned_at": "2026-11-07",
  "source": "https://odile.fyi/attestation",
  "commit": "92af143560a1e3b55a9699a6f572c02b09fadb9b"
}
```

It is enough for the demo and insufficient for a tenant: a tenant's number
varies with its command line, and a v1 pin cannot say **which release** the
number belongs to. `odile-verify` can only compare it to a literal.

### 4.2 What ships next (v2, additive)

The pin becomes a **release recipe plus the demo record**. The v1 top-level
fields stay, byte for byte, so a v1 verifier keeps working and the existing
demo PASS does not move. The new `releases` map is what a tenant reads:

```json
{
  "schema": 2,
  "provision_release": "2026.11.1",
  "measurement": "519c7c9a…9ee8",
  "report_version": 5,
  "policy": "0x30000",
  "fetched_at": "2026-11-06T13:33:07Z",
  "pinned_at": "2026-11-07",
  "source": "https://odile.fyi/attestation",
  "commit": "92af143560a1e3b55a9699a6f572c02b09fadb9b",
  "releases": {
    "2026.11.1": {
      "release_url": "https://servury.com/downloads/blackbox/2026.11.1/",
      "release_json_sha256": "aa37e4ade903aa933eb702f9d9d2191fc4861a1c723ae6e7e0e986e1d4eb9a65",
      "artifacts": {
        "OVMF.amdsev.fd": "8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b",
        "vmlinuz": "96758c40859c94163e5cf81399a107d679e984e251cb4e13aba4e7d92f5d14f2",
        "initrd.img": "8c2aff57573c4af11289553fa185c8f13613acca688a52c2e579d4ea9c2beaf7"
      },
      "measurement_parameters": {
        "vcpu_type": "EPYC-v4",
        "guest_features": "0x1",
        "policy": "0x30000",
        "vmm": "QEMU"
      },
      "vcpu_counts": [4],
      "inputs": {
        "kernel_version": "6.12.115+deb13.1-amd64",
        "kernel_package_sha256": "6aa130b0864e58c7102b53ab9c2be4071fae62394f57c80bf4e1912f4c9b3b2a",
        "debian_snapshot": "20261103T000000Z",
        "edk2": "edk2 edk2-stable202505 unknown"
      },
      "pinned_at": "2026-11-08",
      "evidence": "docs/runbooks/verify.md §Evidence at pin time"
    }
  }
}
```

The top-level fields are the **demo record** (`source` is
`https://odile.fyi/attestation`); they are frozen for compatibility. A tenant
reads `releases[<id>]`. The release entry says *which bytes and which
parameters*; it deliberately does **not** carry a tenant's measurement,
because that number is a function of the tenant's command line.

`provision_release` is the release a new order is expected to boot.
`odilectl provision` records it with the tenant (ODL-398); the ready email does
not print it. The tenant row is the source wherever the release is shown. The
verifier never uses it: a machine on any pinned release verifies against its
own entry. Moving `provision_release` is a deliberate act after step 4 of §6,
not a side effect of adding an entry.

`vcpu_counts` names the plan sizes for which the entry has been measured.
2026.11.1 carries `[4]` because the verification was on a 4-vCPU Plus; a
2-vCPU Core entry is added only after a measurement on a 2-vCPU machine.

`--print-pin` must **merge**, not replace: it updates the top-level demo record
and keeps `schema`, `provision_release` and every `releases` entry. The
refresh line in `docs/runbooks/verify.md` redirects stdout into the file, so a
`--print-pin` that emitted only the v1 fields would quietly delete the release
map. That is the one idempotency bug this schema can have; ODL-395 tests it.

### 4.3 The per-machine record (not in the repo)

The number the user checks is a **record**, printed on PASS and kept by the
user, never committed:

```json
{
  "schema": "odile.boot-record.v1",
  "release": "2026.11.1",
  "artifacts": { "OVMF.amdsev.fd": "…", "vmlinuz": "…", "initrd.img": "…" },
  "measurement_parameters": { "vcpu_type": "EPYC-v4", "guest_features": "0x1",
                              "policy": "0x30000", "vmm": "QEMU" },
  "vcpus": 4,
  "cmdline_sha256": "<sha256 of the command line with ' initrd=initrd' stripped>",
  "measurement": "519c7c9a…9ee8",
  "checked_at": "2026-11-08T21:10:02Z",
  "verifier": "blackbox-verify (recompute) + odile-verify (chain and expected number)"
}
```

The record is what makes a rotation explainable: if the OVMF hash moved and
the kernel and initramfs did not, the firmware changed; if `vmlinuz` and
`initrd.img` moved, the kernel did. A bare FAIL cannot tell those apart. The
installer writes its own copy of the record to `/etc/odile/boot-record.json`
so the app layer carries the boot it was installed on (ODL-396).

### 4.4 Rejected alternatives

- **One number, rotated in place on every release.** Rejected: a release move
  is per machine (the disk is kept; machines move on different days), so
  replacing the number breaks every tenant still on the old release. It also
  makes "same image, new firmware" indistinguishable from "different image".
- **One file per release under `pins/`.** Rejected: `odile-verify --pin` is a
  single path, `README.md`, `CLAIMS.md` and the tests name
  `odile-verify.pin`, and a directory adds a discovery rule and a "which
  file?" failure mode. One diffable file is the smaller interface.
- **Put a tenant's number in the repo at provision time.** Rejected: it
  requires an HQ write per tenant and turns the repo into a per-machine
  database; the derivation already reproduces the number from public inputs.

---

## 5. The command — who checks what, and when

Five moments. Each names one owner and one exit condition.

| Moment | Owner | Command | Stop condition |
|---|---|---|---|
| **Pin time** — before the first tenant boots a release | security runs it; architect pins it; verifier reproduces the diff | `sha256sum -c SHA256SUMS`, then the `sev-snp-measure` command below, then `blackbox-verify` on one machine at that release | any hash mismatch, any measurement disagreement, or a release already used by a tenant |
| **Before the passphrase** — the ceremony | the user, on their laptop | `blackbox-verify --host <ipv4> --release ./2026.11.1 --vcpus 4` (or the ODL-202 wrapper doing the same and feeding `odile-verify --report`) | any FAIL; the user does not type the passphrase and does not add the host key |
| **After boot** — a running tenant | the user | `./odile-verify --expect <measurement> https://<slug>.odile.fyi` (ODL-397) and `./odile-verify https://odile.fyi` for the demo | any FAIL; the report is the boot report, not a liveness proof |
| **At install** | the installer | `--expect <measurement>` checked against `/run/blackbox/attest.json` before the seed exchange (ODL-396) | mismatch: exit non-zero before the token is spent |
| **At provision** | `odilectl` | records `provision_release` with the tenant and refuses a pin without it; reconcile flags a heartbeat that reports a different release (ODL-398) | the order is stopped, or the tenant is marked `release_drift` rather than `active` |

### 5.1 Pin time

```sh
# 1. The bytes.
cd 2026.11.1/
sha256sum -c SHA256SUMS                 # 4/4 OK
python3 -m json.tool RELEASE.json       # release, artifacts, inputs, measurement_parameters

# 2. The expected number, on a machine at that release, from its own command line.
CMD="$(sed 's/ initrd=initrd$//' /proc/cmdline)"           # 1144 bytes here
sev-snp-measure --mode snp --vcpus 4 --vcpu-type EPYC-v4 \
  --guest-features 0x1 --ovmf OVMF.amdsev.fd --kernel vmlinuz \
  --initrd initrd.img --append "$CMD"

# 3. The whole-machine check the user will run: recompute + AMD chain + host-key binding.
blackbox-verify --host <ipv4> --release . --vcpus 4

# 4. Record the entry, then prove the demo still verifies and the tree is green.
git diff -- odile-verify.pin                 # v2 first commit: schema, provision_release, one release block
./odile-verify https://odile.fyi
make check
```

Run step 2 twice. Two identical answers are the minimum evidence; the
2026.11.1 run for this policy produced `519c7c9a…9ee8` twice, and the same
value is in `odile-verify.pin` and in the live report. Step 3 needs
`python3`, `cryptography`, `sev-snp-measure` and `ssh-keyscan`; that is
Servury's dependency set, not Odile's. `odile-verify` stays standard library
plus `openssl`.

### 5.2 Before the passphrase

The machine is at Servury's measured initramfs. It serves no HTTPS; the report
is fetched over SSH by the `attest` forced command, which accepts only the key
in the measured command line.

```sh
# On the laptop, once: the release directory the pin names.
curl -fsSLO https://servury.com/downloads/blackbox/2026.11.1/{OVMF.amdsev.fd,vmlinuz,initrd.img,SHA256SUMS}
sha256sum -c SHA256SUMS
# Compare the three hashes with releases["2026.11.1"].artifacts in odile-verify.pin.
# A match is what makes the local recompute a check of the pin, not a check of
# whatever servury.com served this morning.

# The vendor verifier: one file, not one per release. Fetch it once and record
# its hash; the 5 November audit's copy is 8c937f02…0f97.
curl -fsSLO https://servury.com/downloads/blackbox/blackbox-verify
sha256sum blackbox-verify

ssh -i ~/.ssh/odile -o IdentitiesOnly=yes root@<ipv4> attest > /tmp/attest.json
python3 blackbox-verify --release ./2026.11.1 --vcpus 4 < /tmp/attest.json
```

The unversioned path is the working one: `…/blackbox/<release>/blackbox-verify`
404s for 2026.11.1. `docs/CLAIMS.md` §4.4 prints the unversioned path and the
audited sha256 (ODL-402). The dependency set is the one in §5.1 step 3.

`blackbox-verify --host <ipv4>` does the SSH fetch and the recompute, and also
keyscans the host key, checking that the key on the wire is the one in the
report. On PASS it prints the `known_hosts` line; only then does the ceremony
continue. The ODL-202 wrapper owns the copy-paste form of this, including the
assertion that `bb.key` is the user's boot key and `bb.ip4` is the expected
address.

`odile-verify <ip>` alone does **not** work here: it expects the HTTPS
`/attestation` shape, and the initramfs does not serve it. That sentence is a
correction to ODL-202's original card text; the wrapper is the owner.

### 5.3 After boot

A running tenant serves `/attestation` (ODL-397) in the shape `attest.py`
writes; the demo serves the same shape at `https://odile.fyi/attestation`.
The expected number for a tenant is `--expect` — the value the recompute
produced. **ODL-395 landed:** the verifier selects the release entry from the
document's `release` field, or from `--release`, and fails if the release is not
in the pin; a tenant run without `--expect` is a usage error, not a silent PASS
on the chain alone. The demo keeps its v1 record, so `./odile-verify
https://odile.fyi` still passes without an argument; it is the source named by
the pin's top-level `source`. `--record` writes the per-machine record and
`--previous` compares an earlier one, naming `release`, `cmdline_sha256` or
`measurement` when a field moved — the sentence that makes a rotation
explainable.

---

## 6. Rotation — what a new Servury release changes

Servury ships a release by changing the bytes in a new directory
(`https://servury.com/downloads/blackbox/<release>/`). A machine keeps booting
the release it was moved to; existing tenants do not move by themselves.

| What changed in the release | Artifact hash that moves | Measurement | What the user sees | What Odile does |
|---|---|---|---|---|
| Kernel or `initrd.img` (new kernel, initramfs scripts) | `vmlinuz`, `initrd.img` | every machine's number changes | a new release id; the ceremony repeats | add the release entry; never edit the old one |
| Firmware (`OVMF.amdsev.fd`) | `OVMF.amdsev.fd` | every machine's number changes even with the same kernel | "same image, new firmware" in the record | add the entry; refresh the vendor-verifier run |
| Measurement parameters (`vcpu_type`, `guest_features`, `policy`, `vmm`) | none of the three hashes necessarily | every number changes; a changed policy can void the confidentiality claim | a review item, not a routine rotation | architect review; if `DEBUG` or migration bits appear, stop and escalate |
| Debian snapshot or kernel package (`inputs`) | none, if the rebuild is bit-identical | unchanged when the three hashes are unchanged | the provenance changed, the number did not | record the new `inputs` in the new release entry |
| TCB / microcode | none | unchanged | `odile-verify`'s `cpu binding` uses the KDS VCEK fetched for the report's reported TCB | nothing; the fetch moves with the report, and the pin does not move |
| Host key | none (not a measurement input) | unchanged | a changed host key on the first connect after a move | the ceremony's stop sign; the key is re-sealed once |

The procedure, in order:

1. **Fetch and hash-check** the release directory; read `RELEASE.json`.
2. **Compare the parameters** to the entry for the previous release. A change
   in `measurement_parameters` is an architect review, not a hash bump.
3. **Measure one machine** at the new release (step 2 of §5.1), twice.
4. **Run the vendor verifier** against that machine (`blackbox-verify`), so the
   recompute and the repository check agree before any tenant sees it.
5. **Add** the release block to `odile-verify.pin`. Do not edit or remove the
   block for a release any tenant still boots. Remove a block only when
   `odilectl` reports no tenant on that release. Move `provision_release` only
   when new orders should boot the new release; a machine on the old entry
   keeps verifying against it.
6. **Prove it**: `./odile-verify https://odile.fyi` still PASSes; `make check`
   is green; the diff is one added release block plus a new `pinned_at`.
7. **Tell the next machine**: `provision_release` names the entry a new order
   boots, and `odilectl provision` records it with the tenant (ODL-232,
   ODL-398); the ready email does not print it. The verifier selects the entry
   the machine's own report names, never "latest". A machine on a release that
   is not in the pin fails, which is the point.

One more consequence, stated because it is the one users notice: a release
move keeps the disk and **changes the host key once**. The passphrase does not
change. The ceremony says: re-run the verifier, accept the new key only on a
PASS, then unlock.

---

## 7. Fail-closed

The rule is one sentence: **a FAIL stops the ceremony before a secret is
typed, fetched or written.** Concretely:

| Failure | `odile-verify` | Installer (ODL-396) | Provision (ODL-398) |
|---|---|---|---|
| Measurement differs from `--expect` (or, for the demo, from the v1 record) | `expected`/`pin` FAIL, exit 1, no state written | exit non-zero before the seed exchange; no boot record written | — |
| Release not in the pin | FAIL, "release not pinned", exit 1 | `--release` absent from the pin → stop; without `--release`, print and record the reported release | refuses to record the tenant as provisioned |
| AMD chain or report signature fails | `chain`/`signature` FAIL, exit 1 | `status != "ok"` → stop | — |
| Policy is not `0x30000` | `policy` FAIL, exit 1 | stop | — |
| JSON summary disagrees with the signed bytes | `served` FAIL, exit 1 | — | — |
| Pin file missing or malformed | `pin` FAIL, exit 1 | missing `--expect` → stop in tenant mode | pin unreadable → stop |
| Re-run after a release move | — | recorded measurement differs → stop until a new `--expect` is supplied | — |

**ODL-396 landed:** after `--verify-signature` and before the seed exchange the
installer reads the boot report `bb-seed` copies at switch-root
(`/run/blackbox/attest.json`), asserts `status`, the report version, the
measurement at `0x90:0xC0` against `--expect`, the one `bb.key=` and one
`bb.ip4=` in the command line, the kit's `boot_key_fingerprint` when it carries
one, and `--release` when given; it prints the release, the measurement and the
boot-key fingerprint, and writes `/etc/odile/boot-record.json`. A mismatch exits
5 with no token spent and no secret written. The installer's own signature is
checked first (exit 3); the seed is checked against the kit key (ODL-204). The
real `bb-seed` transcript is ODL-332. The review table in ADR-001 §14 carries the
state at `main` `4280a1e`: the 8 November rows are kept where they explain what
moved, and every row that is now met says so.

`odile-verify` exits 0 on PASS, 1 on FAIL, 2 on usage or I/O. It writes no
state, so "fail-closed" there means: no `known_hosts` line, no PASS, no
reason to unlock. The wrapper (ODL-202) is what turns a FAIL into a stop sign
in the ceremony.

---

## 8. What this policy does not prove

- **The Servury manifest is unsigned.** The pin records hashes; TLS and our
  rebuild are the evidence that the hashes are the bytes that boot. A provider
  that swapped `initrd.img` and regenerated `SHA256SUMS` would be caught by our
  rebuild or by the pinned value, not by a vendor signature.
- **The pin and `odile-verify` live in the same repository.** They prove which
  image, not that the image is honest, and nothing outside the repository
  timestamps them: the repository is the whole trust root (§3.1). For a tenant,
  the derivation from Servury's published artifacts is the second source. The
  NEAR testnet anchor that used to be named here is removed by ODL-421, because
  it covered one machine and put a second service in the reader's path
  (`docs/architecture/ADR-anchor.md` §2 and §6).
- **The report is a boot report, not a liveness proof.** It proves what the
  CPU signed, not that the machine is up now, and it does not measure the app
  layer (CLAIMS.md gap 1).
- **The TCB check is a floor, not an audit.** It checks the committed SNP SPL
  against AMD-SB-3020; it does not enumerate every advisory.
- **A root shell can edit machine-reported files.** `/etc/odile/boot-record.json`
  and `/verify` rows read from the machine are tagged as such; only the CPU
  signature and the laptop recompute are attacker-resistant.

---

## 9. Gaps and their state

State at `main` `4280a1e`, 10 November 2026. A row is closed only when the
merged code carries the property; the card id stays so the history is readable.

| Gap | Card | State |
|---|---|---|
| A running tenant has no `/attestation`, so the post-boot check in CLAIMS §4.5 is not reachable | ODL-397 | **Closed** — merged at `9b3a7ee`; the installer writes the boot-report transformer and the `odile-attest-boot.service` oneshot that renders `/run/blackbox/attest.json` for the public route (`odile_install.py:6233`, `:6626`). |
| The release is not recorded with the tenant; `odilectl provision` cannot refuse an unpinned release; the ready email has no source for the release id | ODL-398 | **Closed** — merged at `1a90f0c`; `tenant_state.release` (schema v2) is written by `odilectl provision` from the pin's `provision_release`, which it refuses to order without (`odilectl/provision.py:242`); `odilectl health` reports `release_drift`. The ready email prints the machine's own boot-record measurement but not the release id (the ODL-459 re-cut dropped the command list); the recorded row is the source for whichever surface prints it next. |
| The Hermes `install.sh` is fetched over HTTPS and executed without a hash pin, before the pinned commit applies | ODL-399 | **Closed** — merged at `3b9b155`. `HERMES_INSTALL_SHA256 = abf33860…`; `ensure_install` fetches the bytes, hashes them, and refuses before the temporary file is written or `bash` starts (`verify_install_script` `:6210`, fetch `:6294`, check `:6296`, `bash` `:6304`). `--manifest` prints `hermes_install_sha256` (`:8137`); `scripts/manifest.py build` records it at `hermes.install_script_sha256` and refuses a root whose installer predates the pin (`scripts/manifest.py:478`, `:543`; `docs/runbooks/manifest.md:49,135`; `docs/runbooks/install.md` §4b). At the merge the installer was `ee4bbd41…` (257,577 bytes); at `main` `4280a1e` it is `d8535f2b…` (400,283 bytes), and the install-script pin has not moved. The live script on 10 November 2026 is 66,864 bytes and hashes to the pin. |
| The installer's trust anchor is the kit's key or the sibling key; no fingerprint is compared with the published key | ODL-400 | **Closed** — merged at `f9e46bc`; the installer prints the key's file sha256 and `SHA256:` fingerprint, refuses a kit whose `signing_key_sha256` names another key (`signing_key_problem` `:5098`), and `--self-test` pins the shipped pair to `PUBLISHED_KEY_SHA256` / `PUBLISHED_KEY_FINGERPRINT`. It is kit-vs-key plus the user's check against `odile.fyi`, not a trust anchor inside the program. |
| The ceremony wrapper was parked; its card text said `odile-verify <ip>`, which the initramfs cannot serve | ODL-202 | **Closed** — merged at `653cf6d`; `scripts/verify_and_unlock.sh` checks the pin's release artifacts, runs `blackbox-verify`, asserts exactly one `bb.key=` and one `bb.ip4=`, binds the host key, and calls `odile-verify --report --expect --release`; it writes `known_hosts` only after all five checks (`:1084`). |
| The tenant copy of the signed manifest and `/manifest`, and the `/usr/local/bin/odile-install.sh` that `manifest.installer_sha256` names, are not installed | ODL-332 | **Open** — `scripts/manifest.py` finds neither installer candidate on a tenant root and requires `--installer`; the tenant copy and the Caddy route are the first-tenant card. |

---

## 10. Evidence, 8 November 2026

The commands and outputs below were run for this card on the live 4-vCPU
Blackbox at release 2026.11.1. They are the pattern every pin rotation
repeats.

```
# excerpt: long hashes are shortened with …
$ sha256sum -c SHA256SUMS
OVMF.amdsev.fd: OK
vmlinuz: OK
initrd.img: OK
RELEASE.json: OK

$ python3 -m json.tool RELEASE.json
{"release": "2026.11.1", "built": "2026-11-03T13:04:57Z",
 "artifacts": {"OVMF.amdsev.fd": "8731846c…958b", "vmlinuz": "96758c40…14f2",
               "initrd.img": "8c2aff57…eaf7"},
 "inputs": {"debian_snapshot": "20261103T000000Z",
            "edk2": "edk2 edk2-stable202505 unknown",
            "kernel_package": "linux-image-6.12.115+deb13.1-amd64_6.12.115-2_amd64.deb",
            "kernel_package_sha256": "6aa130b0…3b2a",
            "kernel_version": "6.12.115+deb13.1-amd64"},
 "measurement_parameters": {"vcpu_type": "EPYC-v4", "guest_features": "0x1",
                            "policy": "0x30000", "vmm": "QEMU"}}

$ CMD="$(sed 's/ initrd=initrd$//' /proc/cmdline)"; printf '%s' "$CMD" | wc -c
1144
$ sev-snp-measure --mode snp --vcpus 4 --vcpu-type EPYC-v4 \
    --guest-features 0x1 --ovmf OVMF.amdsev.fd --kernel vmlinuz \
    --initrd initrd.img --append "$CMD"
519c7c9aae54bf139cacd783e1b69bd98b01e79b9e8b52feb41aadb7a483f481227a8a3b64ea2b885234ca56e09c9ee8
# second run: identical

$ ./odile-verify https://odile.fyi
  source           https://odile.fyi/attestation
  fetched_at       2026-11-08T21:10:02Z
  measurement      519c7c9aae54bf139cacd783e1b69bd98b01e79b9e8b52feb41aadb7a483f481227a8a3b64ea2b885234ca56e09c9ee8
  chain       PASS  VCEK -> ASK -> ARK verifies; ARK SHA-256 69:d0:63:b4:…:9a:54:0b:cd
  cpu binding PASS  the VCEK names this chip and TCB (bl=4 tee=0 snp=23 ucode=213)
  signature   PASS  ECDSA P-384 over the 672-byte report verifies with the VCEK
  policy      PASS  policy 0x30000: SMT allowed, DEBUG clear, migration agent not permitted
  tcb         PASS  committed bl=4 tee=0 snp=29 ucode=213; SNP SPL 29 >= 27 (AMD-SB-3020)
  report      PASS  version 5, 1184 bytes, VMPL 0, ECDSA P-384 / SHA-384
  served      PASS  the JSON summary agrees with the signed bytes
  pin         PASS  matches odile-verify.pin (fetched_at 2026-11-06T13:33:07Z, pinned from 92af143560a1e3b55a9699a6f572c02b09fadb9b)
```

The boot report this host serves at `/run/blackbox/attest.json` has
`release: "2026.11.1"`, `status: "ok"`, a 1184-byte `report_b64`, the same
measurement, and the command line quoted above. The host key in it is
`SHA256:2XWkeR4wwCkW9+iwfhTV/vFufe88fUTxvsXrcRBrCyA`; it is bound in
`REPORT_DATA`, not in the measurement.
