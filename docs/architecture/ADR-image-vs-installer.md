# ADR-001 — Tenant boot route: signed installer on Servury's measured release

**Status:** Accepted · 7 November 2026 · Architect
**Answers:** ODL-333 — "is a prebuilt image our route to tenant machines?"
**Short answer:** not in V0.1. V0.1 boots Servury's measured release and the user
runs one signed installer. The prebuilt **UKI** is the route to the V0.1.1
keyless boot; the prebuilt **disk image** waits for V0.2, when the app layer has
a reason to be measured.
**Related:** `docs/MVP-V0.1.md` §3–§7, §12 (D6, D16, D17, D23, D25);
`docs/CLAIMS.md` §3 gap 1; `docs/product/DECISION-agent-access.md` §3a;
`docs/architecture/DESIGN-claim-ceremony.md`; cards ODL-201, ODL-202, ODL-203,
ODL-310, ODL-319, ODL-330, ODL-332, ODL-380, ODL-393, ODL-395–ODL-404.
**Decision owner:** Architect. Adopting option B later needs Savio: it puts
Odile's build pipeline and an artefact host inside the tenant's boot chain — a
new trust boundary, and for the disk image a new network path.

**Annex — 8 November 2026.** §14 records the measurement-pin policy
(`docs/architecture/PIN-POLICY.md`) and the installer trust-boundary review
against this ADR. It corrects one sentence in §6 item 3 of this ADR, and §4 now
points at that correction: `odile-verify` compares a report to the pin; the
recompute is `sev-snp-measure` and `blackbox-verify`. The decision in §5 does
not change.

**Refresh — 10 November 2026.** §14.3 is re-read at `main` `4280a1e`. At that
head `installer/odile_install.py` is sha256 `d8535f2b…` (400,283 bytes):
`--verify-signature` → `signature: OK`, exit 0; `--self-test` → PASS, 151 `ok`;
`--manifest` prints `installer_sha256 d8535f2b…` and `hermes_install_sha256
abf33860…`. The live `https://hermes-agent.nousresearch.com/install.sh` is
66,864 bytes and hashes to that pin. Rows whose verdict moved carry the merged
card and the line numbers at this head; the 8 November review is kept where it
explains what moved.

**Correction — 12 November 2026.** §15 records two corrections made after this
ADR was accepted. The boot image **does** have an API verb
(`GET|POST ?action=boot-image`); only the disk image does not. And the M1
tenant runs the signed custom UKI **plus a pre-sealed disk image** written by
the panel — the shape §5 placed in V0.2. §5's decision text is kept as
accepted, not as current; §15.2 says what runs, which card shipped it, when the
seal made it safe, and that no ADR recorded the switch. The title is kept as
accepted.

---

## 1. The invariant at risk

The product's defining property is *the user is the only keyholder, and the boot
they verify is the boot that runs* (`docs/MVP-V0.1.md` §2, D14–D17). The route
we choose touches three invariants:

- **Invariant 2 — no operator credential.** A prebuilt **pre-encrypted** disk
  image needs a volume key at build time. Whoever builds the image holds that
  key, so Odile would hold the disk key again. The route is only safe if the
  volume key is generated and sealed *on the tenant machine* (V0.1.1 work).
- **The measured-boot claim.** "Measured boot against a published release"
  (`docs/MVP-V0.1.md` §7; the public copy is `docs/CLAIMS.md` §1) names *whose*
  release. Today it is Servury's, rebuilt byte-for-byte by us. With BYO image it
  becomes ours, and the sentence, the evidence command and gap 1 all change.
- **"Odile is a provisioner, not a root holder"** (`docs/MVP-V0.1.md` §6.1,
  Invariant 3). If a boot image is wrong, the machine never reaches an SSH
  prompt; recovery is an operator action in the panel. The installer route fails
  *after* boot, where the user can retry it themselves.

## 2. What the evidence says — the two options as Servury actually ships them

**Option A — signed installer on Servury's measured release (today's ODL-203).**
`POST ?action=create` with `os=debian13`, `snp=true`, `fde=true`,
`measured=true`, `ssh_key=<user's>`; the machine boots Servury's measured
initramfs; the user verifies, encrypts, unlocks; one Ed25519-signed installer
runs on the booted guest and installs Hermes, the Webapp, the meter and Caddy
(`docs/MVP-V0.1.md` §6.2–§6.4). The API is explicit that measured boot "needs
`snp`, `fde` and `ssh_key`" and the order form adds "Measured boot takes exactly
one" key — the per-tenant measurement and the one-key invariant come from the
same field.

**Option B — a prebuilt image built and signed by Odile.** Servury documents two
shapes, both **panel-only** (Manage → Boot image / Disk image), each a URL plus
a SHA-256:

- **B1, UKI only.** One PE file carrying kernel, initramfs and command line,
  built with `ukify`; the node downloads it, refuses a hash mismatch, and
  re-hashes it at every start before QEMU sees the path. "Your image has to
  bring everything the release initramfs used to do: configure the network …
  fetch and expose an attestation report … and decide what to do with the disk,
  which is untouched." The suggested verification line is
  `sev-snp-measure … --kernel blackbox.efi --append ''`. Limits: kernel file
  ≤ 1 GiB and ≥ 128 KiB, command line ≤ 4096 bytes, http(s) only.
- **B2, UKI + disk image.** Raw or qcow2 written over `/dev/sda` by the node:
  hash checked, machine stopped, disk snapshotted and discarded, image written
  with `qemu-img convert`, machine started; on failure the snapshot is rolled
  back. A reinstall returns to Servury's template.

Two facts bound the comparison before the axes:

1. **The API cannot do B.** `?action=os` lists three OS images
   (`debian13`, `rocky9`, `freebsd15`); `?action=create` and `?action=reinstall`
   take `os`, `snp`, `fde`, `measured`, `ssh_key`/`password` — no boot-image or disk-image parameter *(corrected 12 November 2026: **disk** image only; `?action=boot-image` measured 200 — details below)*.
   Every BYO machine is a **human panel step per tenant**, outside `odilectl`,
   and therefore outside the M2 "machine factory" audit.
   *(12 November 2026 — corrected: the **boot** image does have a separate API
   verb, `GET|POST ?action=boot-image&id=…`, measured 200 on the live API;
   only the **disk** image is panel-only (`?action=disk-image` → 404). The
   `create` and `reinstall` parameters still carry no image. So B1 is
   scriptable and B2 is a human step. §15.1 has the two commands and the
   reason the API's own verb list missed it.)*
2. **Neither route measures the app layer.** The launch measurement covers
   firmware, kernel, initramfs and command line (Servury, *Measured boot* §2).
   A disk image changes *what is on the disk*, not *what the CPU measured at
   launch*. B2 gets the app layer closer to measurable (dm-verity later); it
   does not measure it in V0.1.

Sources: <https://servury.com/docs/guides/measured-boot/#byoi> and `#byod`;
<https://servury.com/servers/blackbox/> item 04;
<https://servury.com/docs/?api=blackbox> (`?action=os`, `?action=create`,
`?action=reinstall`).

## 3. The eight axes

| # | Axis | A — installer on Servury's release | B — prebuilt image (B1 UKI / B2 disk) |
|---|---|---|---|
| 1 | **Boot and time-to-ready** | Provision ≤ 5 min (Servury: "usually within five minutes"). Then user-paced: verify, `encrypt` in place (writes the whole 100 GB once; unmeasured), `unlock`, installer (4.7 GB Hermes toolchain, MVP §8; unmeasured). The clock is the ceremony plus the install tail. | B1 removes nothing from the ceremony and saves no time by itself. B2 removes the install tail only: a pre-encrypted disk would need a build-time key (breaks Invariant 2), so V0.1 would still run `encrypt`; B2 adds a per-tenant panel step and a multi-GB download + `qemu-img convert`. The saving is the smaller half of the clock. |
| 2 | **Reproducibility of the build** | The boot chain is Servury's and **already rebuilt byte-for-byte** by our audit: `OVMF.amdsev.fd` identical (`8731846c…`, edk2 tag `edk2-stable202505`, resolved to commit `6951dfe7…` in our 2026-11-01 rebuild), `initrd.img` identical, `vmlinuz` byte-identical to Debian's signed package. Our artefact is a shell script and a manifest — trivially reproducible. The rootfs is Servury's stock cloud image, not reproducible by us, but it is encrypted before it runs. | We must make our own UKI and disk image reproducible bit-for-bit (mmdebstrap + snapshot.debian.org pin + `SOURCE_DATE_EPOCH` + fixed package set) — a pipeline we have never run, on the boot path. The firmware stays Servury's and stays reproducible. ODL-380 is the bench that proves this locally. |
| 3 | **Signing and provenance** | Installer and manifest are Ed25519-signed; the key is in the recovery kit and at `/odile-signing-key.pub`. The CPU does **not** measure the installer — provenance, not measurement (CLAIMS gap 1). Note: Servury's own `SHA256SUMS` is unsigned (audit finding 1), so the release's root of trust is TLS to `servury.com`, not a signature. | Boot-chain integrity = panel-declared SHA-256 checked by the node at every start + the CPU measurement. Disk image = panel-declared SHA-256 checked **at write time**, not at boot. Authorship improves: the code that asks for the passphrase is ours and measured. Provenance needs the same Odile signing key over a manifest that now carries image hashes and the expected measurement, plus an artefact host. |
| 4 | **Measurement pin publication and rotation** | The pin is a **recipe** (release id + published artifact hashes) plus a **per-machine number** recomputed from the cmdline the machine reports — every tenant's cmdline differs (IP, key, hostname, instance id), so every tenant's measurement differs. Rotation is a vendor event: a new release, moved on request with the disk kept and the host key changing once. We republish a recipe we did not compute. | The pin is ours to compute: (OVMF sha256, our image sha256, panel cmdline, vCPUs) → measurement, shipped **with** the image in the same signed manifest. Rotation is ours — but a third party's firmware is an input (axis 8). |
| 5 | **First-boot secret injection** | Passphrase at Servury's initramfs (`encrypt`/`unlock`); single-use setup token at install over TLS; Webapp password at install. A measured VM gets no seed ISO; the initramfs writes the cloud-init seed from the measured cmdline. | Same secrets, but the initramfs is ours: the token fetch and the passphrase prompt become measured code, and **this is the only place a sealed key can live** (ODL-310). B2 pre-encrypted is the trap: the volume key exists at build time unless the UKI seals it on first boot, which is V0.1.1. |
| 6 | **Update path and rollback** | One mechanism. The installer is idempotent; re-running it repairs or updates. Kernel and initramfs are Servury's — `apt upgrade` in the guest does not change what boots; a release move keeps the disk and changes the host key once. Rollback = re-run the previous installer, or rebuild + restore. | Adds a second mechanism without removing the first. UKI swap is non-destructive and reversible ("Back to the Servury release"); the disk image write is **destructive** (snapshot rollback only if the write itself fails) and is an install, not an update. Userspace still needs the signed installer/updater. |
| 7 | **Disk footprint** | Stock template plus packages fetched at first boot; the measured reference box sits at 13 GB used with Hermes and this repo (MVP §8). Peak package download ≈ 4.7 GB. | The image is downloaded into the machine's storage and then converted — room for the artefact plus the written disk; a compressed qcow2 of a ~13 GB rootfs is multi-GB **per provision**, re-downloaded on every re-provision. Plus an artefact host and egress we do not run today. |
| 8 | **Servury firmware change** | The firmware is part of the release, and the release id is in the pin, so a firmware change arrives as a **release rotation** we already handle. Observed: `OVMF.amdsev.fd` is `8731846c…` in 2026.09, 2026.11 and 2026.11.1 — zero change across three releases, promised nowhere. | The firmware is an **input to our number**. A new OVMF re-measures every unchanged UKI: every published pin is invalidated although our bytes did not change, and every sealed key (V0.1.1) breaks. We must carry the OVMF hash in the manifest and keep the binary to recompute. The coupling becomes an Odile event instead of a vendor one. |

## 4. The measurement and the pin — one paragraph, and the command that produces it

A tenant's measurement is a function of the firmware, the kernel and initramfs
bytes, the exact command line, the vCPU count, the guest features and the guest
policy; the app layer is not in it. Because the command line carries the
tenant's IP, SSH key, hostname and instance id, **the pin is a recipe plus a
per-machine recomputation, not a number copied from a release page**: publish
the release id and artifact hashes in the ready email, then have `odile-verify`
recompute the expected number from the artifacts and the command line the
machine itself reports, assert the user's key fingerprint and IP inside that
command line, and write `known_hosts` only on PASS. *(8 November 2026 — the
tool split is corrected in §14.2: the recompute, the key-and-IP assertion and
the `known_hosts` write belong to `sev-snp-measure` / `blackbox-verify` and
the ODL-202 wrapper; `odile-verify` compares the signed report's measurement
to the pin or `--expect` and writes no state.)* The same paragraph holds for
B, except the kernel input is our UKI and the OVMF hash becomes a published
field of the manifest rather than an implementation detail of the vendor
release. The exact command, run twice on this 4-vCPU Blackbox at release
2026.11.1, produced the pinned number both times:

```sh
# artifacts from https://servury.com/downloads/blackbox/2026.11.1/ ; sha256sum -c SHA256SUMS: 4/4 OK
# OVMF.amdsev.fd 8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b
# vmlinuz        96758c40859c94163e5cf81399a107d679e984e251cb4e13aba4e7d92f5d14f2
# initrd.img     8c2aff57573c4af11289553fa185c8f13613acca688a52c2e579d4ea9c2beaf7
CMD="$(sed 's/ initrd=initrd$//' /proc/cmdline)"      # 1144 bytes on this machine
sev-snp-measure --mode snp --vcpus 4 --vcpu-type EPYC-v4 --guest-features 0x1 \
  --ovmf OVMF.amdsev.fd --kernel vmlinuz --initrd initrd.img --append "$CMD"
# 519c7c9aae54bf139cacd783e1b69bd98b01e79b9e8b52feb41aadb7a483f481227a8a3b64ea2b885234ca56e09c9ee8
# (second run, same bytes: identical; equals odile-verify.pin and the measurement in /run/blackbox/attest.json's report)
```

For B the command changes shape only — the UKI is one input and the panel
command line is either empty (the `.cmdline` section applies) or the per-tenant
string:

```sh
sev-snp-measure --mode snp --vcpus 4 --vcpu-type EPYC-v4 --guest-features 0x1 \
  --ovmf OVMF.amdsev.fd --kernel blackbox.efi --append ''
```

**Pin consequence.** `odile-verify.pin` v1 is one number for one machine (today,
Odile's own page). For tenants it becomes a **tuple** — `schema`, `release`,
`ovmf_sha256`, `kernel_sha256` (vmlinuz or the UKI), `initrd_sha256`,
`cmdline_sha256` (with `initrd=initrd` stripped), `vcpus`, `guest_features`,
`policy`, `measurement` — and the verifier prints *which field* differs. A
firmware change under B and a changed key under A both move the measurement;
without the tuple the user cannot tell them apart, and the tuple is what lets a
tenant see "same image, new firmware" instead of a bare FAIL. The change is
additive: a v1 pin still verifies, and ODL-319 owns the schema.

## 5. Decision

**V0.1 ships option A.** Servury's measured release, the user-run signed
installer, no image build in the tenant path. `odilectl provision` stays the
only provisioner, and the installer — already specified in ODL-203 and grown
from `deploy/demo-host/` — is the only net-new mechanism on the machine.

**The prebuilt image is adopted in two steps, not one.** B1 (UKI only) is the
V0.1.1 carrier for sealed auto-unlock: one UKI for every tenant, a per-tenant
measurement from the measured command line, the LUKS volume key derived and
sealed on the guest (ODL-310). B2 (prebuilt disk) is V0.2, when dm-verity or a
measured root gives the disk a reason to be measured — otherwise it is a faster
install of an unmeasured rootfs and a destructive write, which is the worst of
both.

*(12 November 2026 — **this paragraph no longer describes what runs.** M1
shipped the signed custom UKI **and a pre-sealed disk image written by the
panel** (ODL-332, merged `8c2ad99`; the seal landed 2026-11-11 in `0d62fb4`;
eight boots on 2026-11-12). The V0.1.1/V0.2 split above is kept as accepted,
not as current. §15.2 records the route, what answered Invariant 2, and that no
ADR recorded the switch; the scope reconciliation is with the Product Lead.)*

## 6. The three things that must be true

1. **The installer finishes on a fresh Plus in ≤ 15 minutes, unattended after the
   passphrase, idempotently, with the seed service answering — and needs no
   operator shell.** Measured on the rented machine (ODL-203, ODL-332). Fifteen
   minutes is the budget this decision is made against, not a measured number.
2. **The ceremony runs on the stock release initramfs with no Servury-side
   change, and every failure is retryable by the user.** Verify → `encrypt` →
   passphrase → `unlock` → boot, timed on the same machine (ODL-202, ODL-332).
   If a step needs a vendor change or an operator, A's failure model is gone.
3. **The pin is per machine and checked before the passphrase.** `odile-verify`
   recomputes the measurement from the published artifacts and the reported
   command line, asserts the user's key fingerprint and IP inside it, and writes
   `known_hosts` only on PASS; the ready email carries the release id and the
   recipe (ODL-201, ODL-319). A pin checked after the passphrase is decoration.

   *(8 November 2026 — the tool split is corrected in §14.2. The recompute is
   `sev-snp-measure` / `blackbox-verify`; `odile-verify` checks the AMD chain
   and the pinned number. The check still happens before the passphrase.)*

## 7. The single fact that would reverse this

**If the measured install tail on a fresh Plus exceeds 15 minutes, or fails on
one of the first five machines, the installer — not the ceremony — is the
bottleneck, and B2 becomes the cheaper fix.** Then the image route takes the
V0.1 slot and this ADR is superseded. The test is one machine, one stopwatch,
five runs; ODL-332 is where it lands.

## 8. What only a real Blackbox can prove

The local bench (ODL-380) has no `/dev/kvm`: it can prove the build, the
measurement arithmetic and a TCG boot, and nothing about SEV-SNP or Servury's
panel. These sentences are testable only on the rented machine (ODL-330), and
each is a bench item for Monday:

1. `odilectl provision` with `snp=true&fde=true&measured=true&ssh_key=<user>`
   returns a machine whose `/proc/cmdline` carries exactly that one key and whose
   report `measurement` equals the `sev-snp-measure` output over the published
   release artifacts and that command line at the plan's vCPU count.
2. `encrypt` completes on the 100 GB Plus disk and, after `unlock`, the volume
   shows exactly one LUKS2 keyslot, `argon2id`, `aes-xts-plain64`, and the root
   filesystem mounts.
3. The installer completes in ≤ 15 minutes from a cold machine, and the gate
   answers its **measured** codes at `https://<slug>.odile.fyi`: an anonymous
   `/` is **302** to `/login`, an anonymous `/api/*` is **401**, `/login` is
   **200**, and a valid session gets **200** (101 for `/api/ws`). Hermes
   0.23.0's gate is a form provider with a cookie, not HTTP Basic, so a "401 at
   `/`" would fail the check (`docs/runbooks/demo-host.md` §"The gate, measured
   — 302, not 401"; `deploy/demo-host/deploy_demo_site.py:gate_ok` asserts
   302/401/200). Re-measured on the live demo host while writing this ADR.
4. A reboot parks the machine in the initramfs with the **same** host key
   (2026.11.1: generated on first boot, sealed to an SNP-derived key and kept
   for the life of the machine; 2026.09 and earlier made a new key each boot);
   `unlock` returns it to service; HQ marks "waiting for owner" from the
   heartbeat stop.
5. A release move keeps the disk and changes the host key once — the rotation
   path the pin depends on.
6. The panel accepts a UKI by URL + SHA-256, refuses a mismatched hash, boots
   the image, and "Back to the Servury release" restores boot — the B1
   prerequisites, proven on the same rented machine if it stays a week.
7. A guest can derive the SNP-derived key for the LUKS volume and seal/unseal a
   volume key across a reboot; a changed cmdline, release or TCB breaks the seal
   and the machine fails closed (ODL-310).

What the bench *can* settle and should: identical UKI measurements from two
builds, a bootable image under QEMU TCG, the smoke set in the guest, image
sizes and wall-clock, and the exact command line that Monday's machine will
reproduce.

## 9. Consequences

- **One new mechanism in V0.1, not three.** The installer replaces nothing that
  exists; B would add a UKI build, an artefact host and a destructive disk
  writer on the boot path at the same time.
- **Failure stays user-serviceable.** A failed installer leaves a running
  machine the user can repair by re-running it; a failed boot image leaves a
  machine that needs the operator panel. For ten hand-held tenants with no
  operator shell, that asymmetry is worth more than the install minutes.
- **V0.1.1 is not delayed.** Sealed unlock needs a UKI either way; ODL-380's
  bench feeds it directly, and the UKI is the one place a keyless boot can live.
- **A claim does not move.** "Measured boot against a published release" stays
  true and Servury's release stays the subject. If B is adopted, three sentences
  move (the release becomes ours, the installer paragraph, gap 1) and that is
  Savio's call, not this ADR's.
- **The pin schema is the durable part.** Whichever route ships, the tuple in
  §4 is what makes a rotation explainable; ship it with ODL-319.

## 10. Alternatives rejected

- **B2 for V0.1 (prebuilt disk).** Rejected: a pre-encrypted image puts the
  volume key back in Odile's hands (Invariant 2); an unencrypted one keeps the
  `encrypt` ceremony anyway; the write is destructive; the panel step is outside
  `odilectl`; and the app layer is still unmeasured. It buys the install tail
  and costs a second mechanism.
  *(12 November 2026 — the pre-encryption objection is answered by the
  first-boot seal in §15.2: the build-time key opens only the factory disk and
  is killed before the user arrives. The other objections — a destructive
  write, a panel step outside `odilectl`, an unmeasured app layer — stand; the
  route shipped anyway.)*
- **A hybrid — prebuilt disk plus the installer for updates.** Rejected: it
  keeps both mechanisms and adds the destructive path for no gain over A.
- **Deferring the image work entirely.** Rejected: the bench is cheap, it
  de-risks V0.1.1, and it is the checklist for the one rented machine.

## 11. Reversibility

**Two-way until the first tenant machine is provisioned; one-way after.** Before
tenant one, switching to B is a commit, a build pipeline and a panel setting.
After tenant one, switching routes means a rebuild and restore per tenant (the
disk holds their data under their passphrase), plus a new measurement and a new
pin for every machine. The pin tuple is additive and reversible; a prebuilt disk
image that has been written over a tenant's disk is not.

## 12. Evidence

- Servury, *Measured boot* — §7 Bring your own image (`#byoi`), §8 Bring your
  own disk (`#byod`), §2 what is measured, §9 what it does not protect:
  <https://servury.com/docs/guides/measured-boot/>
- Servury, *Blackbox* item 04 — custom UKI, disk image or ISO:
  <https://servury.com/servers/blackbox/>
- Servury, *Blackbox API* — `?action=os`, `?action=create`, `?action=reinstall`
  (no image parameter): <https://servury.com/docs/?api=blackbox>
- Release 2026.11.1 artifacts and hashes:
  <https://servury.com/downloads/blackbox/2026.11.1/SHA256SUMS>
- Our independent rebuild of the 2026.09 release (identical OVMF, initrd and
  Debian kernel): `/root/servury-blackbox-verification.md` §"2026-11-01 rebuild
  run"; the 2026.11.1 in-place verification in §"2026-11-05".
- The measurement run in §4 of this ADR: performed on this host (a 4-vCPU
  Blackbox at 2026.11.1), artifacts hash-checked, two identical results,
  equal to `odile-verify.pin` and to the measurement in the report that
  `/run/blackbox/attest.json` serves (`report_b64`, parsed with
  `tools/snp_report.py`: 1184 bytes, measurement 519c7c9a…9ee8).
- The gate in §8 item 3: re-measured against the live demo host on 7 November
  2026 — `GET /` → **302**, `GET /login` → **200**, `GET /api/sessions` →
  **401**; the same table is asserted by `deploy/demo-host/deploy_demo_site.py:gate_ok`
  and recorded in `docs/runbooks/demo-host.md` §"The gate, measured".
- The host key in §8 item 4: on this host, the fingerprint in
  `/run/blackbox/attest.json` (`SHA256:2XWkeR4wwCkW9+iwfhTV/vFufe88fUTxvsXrcRBrCyA`)
  equals `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`; the release guide
  states the key is kept for the life of the machine and served across reboots
  (2026.09 and earlier regenerated it each boot).
- Bench plan: ODL-380 (`docs/backlog.yaml`), `docs/runbooks/image-bench.md`
  when it lands. The card body's original "ODL-332 bench notes" was a stale
  number: ODL-332 is the first-tenant end-to-end card; the bench was renumbered
  ODL-380 in the M1/M2 re-cut and had not run when this ADR was written. ODL-332
  is where the install tail is timed, which is why §6 and §7 cite it.

## 13. Follow-ups this does not do

- **Pin v2 tuple** — additive schema and verifier output (ODL-319).
- **ODL-380** — the local bench; its runbook must end with §8's seven
  sentences, each mapped to a command.
- **ODL-310** — the seal spike; on green, B1 is the V0.1.1 card, not a rebuild
  of this decision.

## 14. Annex — the pin policy and the trust-boundary review (8 November 2026)

**Reviewed:** `installer/odile_install.py` at the head of the reviewed branch
(sha256 `e55ef700…d90c4a`; signature **BAD** and `--self-test` 2 FAILED at that
hash — see row 2 and ODL-403), the signing key
`installer/odile-signing-key.pub` (file sha256 `13e8c2ae…36e2`, equal to the
key published at `odile.fyi`), `seed/seed.py`, `scripts/manifest.py`,
`odile-verify`, `attest.py`, `odilectl/provision.py`, and the release
`2026.11.1` artifacts fetched from `servury.com` on the day. **Scope:**
ADR-001's invariants, not a fresh opinion about the route. **Policy:**
`docs/architecture/PIN-POLICY.md`.

### 14.1 The decision this annex records

The pin is a **release recipe**, and a tenant's number is **derived per
machine**. `odile-verify.pin` gains an additive `releases` map (schema 2) that
carries the release id, the three artifact hashes, the measurement parameters
and the verified vCPU counts; the v1 top-level fields stay as the demo
machine's record. The expected number for a tenant is computed from the release
entry plus the command line the machine reports, and printed as a per-machine
**record**, never committed. A rotation adds an entry; it does not replace the
entry a running tenant still boots. PIN-POLICY §4–§7 carry the schema, the
commands and the fail-closed table.

### 14.2 The sentence this annex corrects

§6 item 3 of this ADR says "`odile-verify` recomputes the measurement from the
published artifacts and the reported command line". It does not, and making it
do so would put `sev-snp-measure` (pip) or a second implementation of the AMD
digest into the one command a stranger runs with the standard library. The
corrected split:

- **The recompute** is Servury's `blackbox-verify` (which also binds the host
  key in `REPORT_DATA` and walks the AMD chain) and, independently,
  `sev-snp-measure` at pin time — the exact command in §4, run twice.
- **The comparison** is `odile-verify`: it reads the measurement out of the
  signed report bytes and compares it to the pin or `--expect`. It stays
  standard library plus `openssl`, and it fetches the machine under test plus
  the VCEK AMD publishes for the report's chip (ADR-002 §6a; the demo chip's
  vendored VCEK was deleted for its silicon id).
- **The pre-passphrase check** therefore uses the vendor verifier or the
  ODL-202 wrapper; the initramfs serves `attest` over SSH, not HTTPS, so
  `odile-verify <ip>` alone does not work there.

Two implementations that disagree are a signal; one reimplementation is not.

### 14.3 The review

| # | ADR-001 invariant | Verdict | Evidence, or the gap and its card |
|---|---|---|---|
| 1 | **No operator key** (`docs/MVP-V0.1.md` §6.1, Invariant 2) | **Met** in the installer; **met elsewhere** for the proof | No `authorized_keys` write anywhere in `odile_install.py`; root locked (`passwd -l root`), `PasswordAuthentication no`, `PermitRootLogin prohibit-password`; `odile` has NOPASSWD sudo by D12, not an operator key. The proof is ODL-208's handoff audit, parked with ODL-332. Caveat: the installer does not install the audit, so "audited at handoff" is a runbook step, not an install step. |
| 2 | **Signature verified before anything executes** (`ADR-001` §5; `docs/MVP-V0.1.md` §6.4 step 1) | **Met** — ODL-403; gate ODL-404 | At `main` `4280a1e` the installer is sha256 `d8535f2b…` (400,283 bytes): `--verify-signature` → `signature: OK (sha256 d8535f2b…)`, exit 0, and `--self-test` → PASS, 151 `ok`. `verify_or_die` (`odile_install.py:8167`) runs at `:8208`, before `check_boot` (`:8211`), the seed fetch (`:8216`) and any install action; a bad signature exits 3. The 8 November head `e55ef700…` was the stale pair: ODL-391 changed the installer and meter without re-embedding or re-signing, ODL-403 (merged `60f91c2`) re-embedded and re-signed, and ODL-404 (merged `6d789d9`) put `--self-test` in `make check`. The anchor: ODL-400 (merged `f9e46bc`) prints the key's file sha256 and `SHA256:` fingerprint, refuses a kit that names another key, and `--self-test` pins the shipped key to `PUBLISHED_KEY_SHA256` / `PUBLISHED_KEY_FINGERPRINT`; that is kit-vs-key plus the user's check against the URL, not a trust anchor inside the program. Strictly, Python executes the file before any in-file check; the guarantee is "before the first state change and before third-party code", not "before the interpreter starts". |
| 3 | **Single-use seed token** (`docs/MVP-V0.1.md` §6.4 step 2) | **Met** | `seed/seed.py:444` `redeem`: checks run in order, only the final conditional `UPDATE` consumes the token, so two exchanges yield one seed and one 410; used/expired are 410, a wrong instance is 403. The response is signed over the exact bytes and verified before parsing (`odile_install.py:5629`–`:5630`). The installer's cache-first path (`:5561`) is the idempotency requirement, not a second redemption. The 24-hour default TTL is a ceremony-window question for ODL-221/ODL-232, not a pin invariant. |
| 4 | **Fail-closed on a pin mismatch** (`docs/MVP-V0.1.md` §6.3 step 1; `ADR-001` §6 item 3) | **Met** — ODL-396 | `odile-verify` exits 1 on a wrong pin, a tampered report or a bad chain and writes no state. The pre-passphrase gate is `scripts/verify_and_unlock.sh` (ODL-202, merged `653cf6d`), which runs `blackbox-verify`, then `odile-verify --report`, and writes `known_hosts` only on PASS. At `main` `4280a1e`, `tenant_main` calls `verify_or_die` (`:8208`), then `check_boot` (`:8211`), then `load_or_fetch_seed` (`:8216`); `check_boot` (`:5455`) reads `/run/blackbox/attest.json`, asserts `status`, the report version, `--expect`, the one `bb.key=` and one `bb.ip4=`, the kit fingerprint and `--release`, exits 5 on a mismatch with no token spent, and writes `/etc/odile/boot-record.json`. `ensure_pin_files` is at `:6596`, not `:2670`. |
| 5 | **The pin is per machine** (`ADR-001` §4 and §6 item 3) | **Met** — ODL-395 | `odile-verify.pin` is schema 2 with `provision_release` and a `releases` map. `odile-verify --release 2026.11.1 --expect <measurement>` selects the entry from `releases`, fails `release not pinned: <id>` for an unknown one (`odile-verify:254`), and prints the `odile.boot-record.v1` record; `--record` writes it and `--previous` names the field that moved (`:321`). The 2026.11.1 entry carries the three artifact hashes, the measurement parameters and `vcpu_counts [4]` (PIN-POLICY §4.2). |
| 6 | **Assert the user's key fingerprint and IP inside the cmdline** (`ADR-001` §6 item 3) | **Met** — ODL-202 | Servury's `bb-attest` puts the user's base64 key (`bb.key=`) and the address (`bb.ip4=`) in the measured command line; `blackbox-verify` binds the host key through `REPORT_DATA` and keyscans it. `scripts/verify_and_unlock.sh` (merged `653cf6d`) asserts exactly one `bb.key=` and one `bb.ip4=`, compares the key's SHA256 fingerprint to `--boot-key` and the address to `--host` (`:439`–`:463`), and stops on a mismatch. |
| 7 | **Write `known_hosts` only on PASS** (`ADR-001` §6 item 3) | **Met** — ODL-202 | `odile-verify` writes no state at all. `scripts/verify_and_unlock.sh` is the only writer: `write_known_hosts` (`:991`) is called after all five checks (`:1084`), and `--self-test` pins "not written on a `bb.key` mismatch" and "not written on a wire-key mismatch". |
| 8 | **App-layer provenance: a signed manifest and a published hash** (`docs/CLAIMS.md` §3 gap 1; ODL-214) | **Met** for (a) — ODL-399; **gap** ODL-332 for (b) and (c) | The manifest builds, signs, verifies and serves on HQ (ODL-214, done). (a) **closed by ODL-399** (merged `3b9b155`): `HERMES_INSTALL_SHA256` is `abf33860…`; `ensure_install` fetches the bytes, hashes them, and refuses before the temporary file is written or `bash` starts (`verify_install_script` `:6210`, fetch `:6294`, check `:6296`, `bash` `:6304`); a one-byte edit is refused, exit 1, with nothing written. The hash is the gate on the one third-party program that runs before the commit pin; the commit pin and its Webapp-bundle check are unchanged. `--manifest` prints `hermes_install_sha256`, and `scripts/manifest.py build` records it at `hermes.install_script_sha256`, refusing a root whose installer predates the pin (`:478`, `:543`; `docs/runbooks/manifest.md:49,135`; `docs/runbooks/install.md` §4b). At the merge the installer was `ee4bbd41…` (257,577 bytes); at `main` `4280a1e` it is `d8535f2b…` (400,283 bytes), and the install-script pin has not moved — the live script is 66,864 bytes and hashes to it. (b) the tenant copy of the signed manifest and `/manifest` is ODL-332; (c) `manifest.installer_sha256` names `/usr/local/bin/odile-install.sh`, which the installer still does not create — ODL-332. |
| 9 | **The ready email carries the release id and the recipe** (`docs/MVP-V0.1.md` §6.2 step 4; `ADR-001` §6 item 3) | **Met in part** — ODL-398 records the release; the ready email still does not print it | ODL-398 (merged `1a90f0c`) adds `tenant_state.release` (schema v2), `odilectl provision` records the pin's `provision_release` on the state row and refuses to order when the pin has no `provision_release` (`odilectl/provision.py:242`), and `odilectl health` reports `release_drift`. The ready email (`odilectl/claim.py`, `templates/emails/ready.txt`) prints the expected measurement and the first-visit link but not the release id; the recorded row is the source when a surface adds it. |
| 10 | **A rotation is explainable** (`ADR-001` §4, "Pin consequence") | **Met** — ODL-395 | The tuple is `odile-verify.pin` schema 2; a tenant run without `--expect` is a usage error, not a chain-only PASS; `--previous` compares `release`, `cmdline_sha256` and `measurement` and prints "<field> moved: X -> Y" (`odile-verify:321`). A rotation is still a reviewed, additive diff (PIN-POLICY §6), not an in-place replace. |

### 14.4 What this annex does not change

The route decision in §5 stands: Servury's measured release, the user-run
signed installer, no image build in the tenant path. No operator key, no
escrow, no new provider, no new standing credential. The installer is not
changed by this card. At `main` `4280a1e` the installer and pin follow-ups are
merged — ODL-202, ODL-395, ODL-396, ODL-397, ODL-398, ODL-399, ODL-400,
ODL-403 and ODL-404. Three cards §14.3 names remain open, and none is a pin
gap: ODL-332 carries the tenant manifest copy and the first tenant run, ODL-208
(the handoff audit) is parked with it for the PASS and the planted-key FAIL,
and ODL-232 (the ceremony-window question) is held, superseded by ODL-459
(merged `4e182a1`). The pin schema is two-way: a v1 verifier keeps working,
and reverting to v1 means ignoring the `releases` map. It stops being cheap at
the first tenant whose ready email names a release — the same moment §11 names.

*(12 November 2026 — the first sentence no longer describes the tenant path:
M1 runs the custom UKI plus a pre-sealed disk, not Servury's release plus the
installer (§15.2). The installer is still signed and still installs the
release; the boot chain is what moved.)*

## 15. Annex — 12 November 2026: the record corrected

A measurement taken after this ADR was accepted, and a route change in M1,
mean parts of the sections above no longer describe the system. This annex is
the dated correction; the sections above are kept as written.

### 15.1 The boot image has an API verb; the disk image does not

Measured on the live API with the production account's reseller key,
2026-11-12T17:52Z, reproduced at 19:07Z while writing this annex:

```sh
KEY=$(cat /root/.odile/servury_reseller_key); B=https://servury.com/api/reseller.php
curl -s -H "X-Reseller-Key: $KEY" "$B?action=boot-image&id=0badc0de"
# 200 {"success":true,"data":{"boot_image":{"next_start":"custom",
#      "release":"2026.11.2","image":{"url":"https://odile.fyi/releases/m1/odile-tenant.efi",
#      "sha256":"5cb318083d2ce0555132a008b0d5dfb7613e6bd0a3fb19d0ae29adac8b1b6a5e",
#      "size":120481240,"type":"pe","cmdline":""},
#      "download":{"state":"ready","finished":"2026-11-12T14:42:00Z", …},
#      "limits":{"max_bytes":1073741824,"max_cmdline":4096,"min_kernel_bytes":131072},
#      "network":{…,"suggested_cmdline":"console=ttyS0,115200 ip=203.0.113.87::…"}}, …}}
curl -s -H "X-Reseller-Key: $KEY" "$B?action=disk-image&id=0badc0de"
# 404 {"success":false,"error":"Unknown action. Available: plans, locations, os,
#      account, status, servers, server, graphs, serial, rdns, console, invoice,
#      invoices, create, renew, upgrade, ip, deposit, power, reinstall, hostname,
#      label, terminate"}
```

The GET returned the tenant's boot state: `next_start: custom`, the image URL
and SHA-256, `download.state: ready`, the size and command-line limits, and
`network.suggested_cmdline`. The write path is `POST ?action=boot-image&id=…`
with `{"url", "sha256"}`; a POST with an empty body answers `400 "url and
sha256 are required … To go back to the Servury release, send {"release":
true}"` — measured the same day, and exercised for real at 20:08:03Z (below).
`?action=disk-image` is **404**. The disk image is still panel-only.

**Re-measured 2026-11-12T19:41Z** while reviewing this annex: the same GET
still answered **200** with `next_start: custom` and `download.state: ready`,
but the image was then `f7bc0ceba60442b0…` (`added` 19:19:08Z) — the
`odlukitest` test UKI from `t_c1e0f336` (*Automate the UKI test cycle*,
`odl/uki-cycle` @ `007eeef`, built at
`/var/tmp/odile-t_c1e0f336-build/odile-tenant.efi`; its boot transcript
carries `bb.host=odlukitest`, `bb.ip4=203.0.113.117`), not this tenant's
round-8 pair. The body quoted above is the 17:52Z/19:07Z state, kept as
measured.

**The write path, exercised 2026-11-12T20:08:03Z by this card's probe.**
`POST ?action=boot-image&id=0badc0de` with the round-8 pair (`{"url":
"https://odile.fyi/releases/m1/odile-tenant.efi", "sha256": "5cb31808…"}`)
answered `success: true`, `"Download started. Poll GET … until download.state
is ready"`, and the GET showed `download.state: ready` at 20:08:06Z with the
pair in place (`added: 2026-11-12T20:08:04Z`). The call re-set the pair the
field already carried at 20:04:49Z, so it re-fetched the same bytes and left
the tenant's state unchanged.

**The same power moved this tenant once already, and nothing logged it.** At
19:41Z the field on `0badc0de` carried `f7bc0ceb…` (`added` 19:19:08Z) —
the `odlukitest` test UKI from `t_c1e0f336` (*Automate the UKI test cycle*,
`odl/uki-cycle` @ `007eeef`), whose command line differs
(`bb.host=odlukitest`, `bb.ip4=203.0.113.117`). Its measurement therefore
differs from the round-8 `9f01725a…` the disk is sealed to, and a start in
that window would have failed to unseal. The field was back on the round-8
pair at 20:04:49Z, and no restart of `0badc0de` is recorded in
19:19:08Z–20:04:49Z — the last documented boot is the 15:05:12Z restart
(`docs/evidence/m1-tenant.md` §10.2). No record names who set it: the fenced
panel route did not — `/root/.odile/servury-panel.log` holds no line for
`0badc0de` — and the API path appends nothing to any Odile log, so whether the
call was the reseller API or the vendor's own web panel is not recoverable
from the evidence. That is the operator power in one paragraph — one call, one
tenant, next start, no log — and the same call puts it back. `?action=disk-image`
is still **404**.

**Why §2 fact 1 was wrong.** The API's own `404 Unknown action` text
enumerates the available verbs and **omits `boot-image` even though the handler
answers 200**. The enumeration was quoted instead of probed. The precise
statement: `?action=create` and `?action=reinstall` take no image parameter;
the **disk** image has no verb; the **boot** image is `GET|POST
?action=boot-image`. The per-tenant panel step in `docs/evidence/m1-tenant.md`
§4 survives on the disk field alone (the command line stays empty for this
per-tenant UKI, because it lives inside the UKI).

**What this does not change.** The operator power is still real: whoever holds
the reseller key for the owning account chooses the boot image the node starts.
It is a scriptable call now instead of a human paste into the panel — a smaller
operational cost and a larger blast radius, not a weaker power. The key is per
account: the M1 automation account `<servury-account:dev>` has no reseller key on disk, so
its machines still need the fenced panel tool (`docs/ops/PANEL-ACCESS.md`).
The node still checks the declared SHA-256 at every start and the CPU still
measures what boots, so the user's check — measurement and host key before the
sign-in — is unchanged. The residual-power sentence ("we can set a different
boot image from the panel") keeps its meaning: the power is identical, only the
mechanism is broader. Whether the deck's wording should name the API too is the
Product Lead's (card `t_bf99b807`); it is not edited here.

### 15.2 The route that shipped: the UKI plus a pre-sealed disk

§5 says V0.1 ships option A; B1 (UKI only) is V0.1.1; B2 (prebuilt disk) is
V0.2. What runs, from M1, is the combination §5 split: the tenant boots the
signed custom UKI **and a pre-sealed disk image written by the panel**.

- **The card:** ODL-332, *First tenant on the custom measured image*, merged
  `8c2ad99` on 2026-11-12T17:16Z. The UKI and disk build is ODL-454's
  (`deploy/image/sealed-image.sh`, `deploy/image/uki.sh`); the production
  build record is `docs/evidence/m1-image.md`; the machine is `odl-fake01` /
  `0badc0de`. `docs/runbooks/image.md` §1 already opens with the custom
  measured UKI at this head and carries the same dated correction in its §9
  panel step; §9 is the part corrected for the API verb.
- **The date the seal landed:** `deploy/image/bb-sealed-provision`
  landed 2026-11-11T20:45Z in `0d62fb4`, run by
  `odile-seal-provision.service` on first boot. The first boot opens the
  factory disk with the build-time bootstrap key, generates a fresh 64-byte
  volume key **on the guest**, adds it as a LUKS2 keyslot, seals it with
  `SNP_GET_DERIVED_KEY` (policy and measurement), proves it opens the volume,
  then kills the bootstrap keyslot and shreds the plaintext volume key and the
  bootstrap key file. It was proven on the tenant at 2026-11-12T01:39Z; eight
  first boots by 14:43Z show `opened-bootstrap` → seal re-provisioned,
  bootstrap key shredded, and the ninth boot (the restart) opens with the
  SNP-derived key and prints `same` (`docs/evidence/m1-tenant.md` row 4,
  §9–§9.8, §10.2).
  *(Corrected 12 November 2026 — what the seal re-keys and what it does not.
  "Volume key" is the script's own word for `V'`, but strictly `V'` is a fresh
  **keyslot passphrase**: `luksAddKey` adds it as a new keyslot and
  `luksKillSlot` kills the bootstrap one, and neither operation changes the
  LUKS **master (volume) key**. The master key is still the build-time key;
  the published image plus its published `/odile/luks-bootstrap.key` recovers
  it (`luksDump --dump-master-key` → SHA-256 `74f2552b…`, identical before and
  after the keyslot operations), and that master key opens the header copy
  after slot 0 is killed — security verdict ODL-466 §0.1, reproduced
  2026-11-12T21:17Z. The seal made the **bootstrap-passphrase path** safe on
  that date; it added a keyslot, it did not replace the build-time door.
  `t_28dda3b7` owns the first-boot re-key.)*
- **Invariant 2 is answered at handoff.** A build-time key exists, but it
  opens only the factory disk, which holds no user data; the first boot
  replaces it with a key generated on the tenant's chip and destroys it before
  the user arrives. That is the difference between this route and the B2 the
  ADR rejected.
  *(Corrected 12 November 2026: **half** answered, not fully. The bootstrap
  **keyslot** is dead at handoff — killed on first boot, the bootstrap key file
  shredded — so the published bootstrap passphrase no longer opens the volume.
  The **master key** is not dead: it is the build-time key and is recoverable
  from the published image, so Odile, and anyone holding the image, can still
  open the tenant's disk until the first-boot re-key lands (`t_28dda3b7`;
  ODL-466 §0.1, A3/A4/A8). "The user is the only keyholder" is therefore not
  true of the live tenant yet; the claims rows are the Product Lead's
  (`t_bf99b807`).)*
- **Was the switch decided in writing?** Partly, and not in this ADR. The
  strict bar (Savio, 2026-11-10) and `docs/MVP-V0.1.md` D27 record the custom
  measured image as the live V0.1 branch; §12's v5 note says D25 is superseded
  by D26/D27; ODL-454's context says §5's "UKI is V0.1.1" is superseded by the
  strict bar. **No ADR amendment, and no D-row, recorded that the tenant's disk
  would be prebuilt and panel-written in V0.1**, and §5's B1/B2 split and
  `docs/MVP-V0.1.md` §3's "a prebuilt disk image is later — V0.2" row were
  left standing. The board took that step inside ODL-454/ODL-332 without the
  decision being written down. This annex records what shipped; it does not
  back-date a decision that was never made.
- **§11 has moved.** "Two-way until the first tenant machine is provisioned;
  one-way after" — the M1 test tenant is provisioned and boots the shipped
  pair, so the cheap window is closed. A route change is now rebuild and
  restore per tenant.

**Scope question, handed to the Product Lead.** §5, D25's text and
`docs/MVP-V0.1.md` §3's table say the prebuilt disk is V0.2; M1 shipped it in
V0.1. Whether that is V0.1 scope (the strict bar made the sealed disk
necessary) or an early V0.2 step is a scope and product-record question, not
the Architect's, and this card edits no claim text. Card `t_bf99b807` asks the
Product Lead to reconcile the rows; if it is a formal V0.1 scope change, the
authority matrix sends it to Savio.

**Product Lead ruling, 15 November 2026 (card `t_bf99b807`).** Recorded as
**V0.1**, and not a formal scope change: D27 made the custom measured image the
live V0.1 branch, R5 requires the sealed keyless boot (M1's exit criterion is
that the seal boots), and ODL-454 — an M1 card — built the disk
(`deploy/image/sealed-image.sh`) beside the UKI. What stays V0.2 is the row's
other half: an appliance image that removes the installer step, because the
installer still runs on the first boot. The dated corrections sit beside
`docs/MVP-V0.1.md` §5's *Prebuilt appliance image* row (the row is §5, not §3)
and in its decision log at D25 and D6; the deck's gap-3 sentence is unchanged
(the v3.10c note records why). The keyholder property still waits on
`t_51ebf972` publishing the re-keyed image — `t_28dda3b7` is the code, and the
served image still carries the old keyslot seal.

### 15.3 What this annex does not change

- **No access-model change.** Still user-only: no operator key, no root
  password, no passphrase escrow, no standing credential on a tenant. The
  build-time bootstrap key is dead after the first boot; the tenant's volume key
  is generated on the chip and sealed to it.
  *(Corrected 12 November 2026: the bootstrap **keyslot** is dead; the LUKS
  **master key** is still the build-time key, recoverable from the published
  image, until `t_28dda3b7`'s first-boot re-key. The sealed key is a
  guest-generated **keyslot passphrase**, not a new volume key — ODL-466
  §0.1.)*
- **No new provider or network path.** The artefact host (`odile.fyi/releases`)
  was already in the boot chain for the UKI; Servury's node fetches the disk
  from the same host. The build pipeline was already in the boot chain when the
  strict bar adopted the custom image.
- **No claim edit and no pin change.** `docs/CLAIMS.md` is untouched; the
  residual-power sentence keeps its meaning; the pin tuple and verifier are
  unchanged. One claim *does* move under the shipped route: `docs/CLAIMS.md`
  line 85 ("firmware, kernel, initramfs and command line are hashed into a
  value the CPU signs") stays true, but the measured initramfs is the hooked
  one, so the MVP §7 candidate that says the measurement matches **Servury's
  published release** is no longer the right sentence. That, the V0.2 rows and
  the §4.4 verifier command are the Product Lead's handoff, not this ADR's.
- **No re-decision.** §5's reasoning is kept; §15.2 records what runs and when
  it became safe.
- **One thing it opens.** The API write leaves no record at Odile: the fenced
  panel route appends every write to `/root/.odile/servury-panel.log`, the API
  route appends nothing, and the log holds no line for `0badc0de`. A boot-image
  change is unattributable today. Card `t_57196155` wraps the call in one
  fenced helper that logs the pair it replaced; until it lands, record a
  `--set` by hand the way §15.1 does.

### 15.4 Evidence

- The two `curl` calls and their 200/404 outputs in §15.1, reproduced
  2026-11-12T19:07Z against `0badc0de` with the production reseller key;
  re-checked at 20:37:54Z and again at 20:53:42Z — **200**/**404** both times,
  the round-8 pair in place (`added` 20:08:04Z). The GET was re-measured at
  19:41Z (the image had moved at 19:19:08Z to the `odlukitest` UKI from
  `t_c1e0f336`; no record names the writer), and the write path exercised at
  20:08:03Z (POST → `success: true`,
  `download.state: ready` at 20:08:06Z). The same measurement is in
  `docs/ops/PANEL-ACCESS.md` §"Update, 2026-11-12 17:55Z".
- The audit gap: `grep -c 0badc0de /root/.odile/servury-panel.log` → **0**,
  against `BOOT_IMAGE`/`DISK_WRITE` lines for `<server:ukitest>` in the same file. The
  fenced panel route logs every write; the API route logs nothing.
- **The seal's limit (security verdict, 12 November 2026):**
  `bb-sealed-provision` adds a keyslot key and kills keyslot 0; the LUKS master
  key is unchanged. Reproduced 21:17Z in
  `docs/security/CLAIMS-VERDICT-strict.md` §0.1 (branch `odl/ODL-466`,
  `0b3c8f1`). The re-key is `t_28dda3b7`; the claims rows it blocks are the
  Product Lead's (`t_bf99b807`).
- The route: `8c2ad99` (ODL-332 merge), `0d62fb4` (the seal script),
  `deploy/image/bb-sealed-provision`, `deploy/image/odile-seal-provision.service`,
  `docs/evidence/m1-image.md`, `docs/evidence/m1-tenant.md` §0 row 4, §9–§9.8,
  §10.2. The GET in §15.1 names the round-8 UKI (`5cb31808…`); the disk half
  of the pair is `c1278242…` in `docs/evidence/m1-image.md`.
- The archived 11 November board snapshot under
  `docs/ops/evidence/2026-11-11-shift2/` quotes the old card body; the dated
  correction is `docs/ops/evidence/2026-11-11-shift2/CORRECTION-2026-11-12.md`,
  and the snapshot itself is left as written.
- **The same false claim in three more places.** `docs/ops/UKI-CYCLE.md` item 1
  (it asks Servury for a verb that already exists) carries this card's dated
  note. The two tool headers were corrected by `898d493` (`t_c1e0f336`'s card,
  merged 12 November 2026 23:25Z): `tools/servury_cycle.py`'s docstring now
  states the boot-image verb, and `tools/servury_panel.py` adds
  `--set-boot-image` and calls the panel the route for an account with no
  reseller key. An earlier draft of this bullet said "corrected here and
  nowhere else"; that stopped being true when `898d493` merged, so it is
  corrected here. Each is a dated note or a corrected sentence, no behaviour
  change. They are outside this card's four named files because each is read at
  the moment of action, and a stale one puts a human in the panel for a step
  that is one call.
  `tests/test_record_corrections.py` keeps the four named records and the two
  archived board snapshots honest from here on.

- **Provenance of `f7bc0ceb…` (corrected 12 November 2026):** the image is
  `t_c1e0f336`'s (*Automate the UKI test cycle*, `odl/uki-cycle` @ `007eeef`,
  built at `/var/tmp/odile-t_c1e0f336-build/odile-tenant.efi`). An earlier
  draft of §15.1 and of the shift-2 correction named ODL-456, which has no
  record of the hash; `tests/test_record_corrections.py` now holds the hash and
  its owner together. The writer who set it on `0badc0de` at 19:19:08Z is not
  named by any record.
