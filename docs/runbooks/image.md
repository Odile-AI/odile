# The measured tenant image — build, sign, measure, and the one panel step

This is the runbook for the custom measured UKI that boots a tenant with the
sealed-unlock hook: Servury's published kernel and initramfs, one extra
init-premount script that the real `mkinitramfs` places in `ORDER`, a random
64-byte LUKS passphrase sealed to the measured command line, and a signed UKI
with a pinned measurement. Card: ODL-454, following ODL-310's spike
(`docs/product/SPIKE-sealed-unlock.md`) and ODL-380's bench
(`docs/runbooks/image-bench.md`).

The stock Servury 2026.11.2 initramfs is a NO-GO for a no-passphrase tenant:
it has no hook, so every boot ends at `Please unlock disk root_crypt:`.
Nothing here changes Servury's release. The image is uploaded through the
panel's **Manage → Boot image** field, one step per tenant (§9).
*(12 November 2026: that boot field is also `GET|POST ?action=boot-image`,
measured 200; for the shipped UKI-plus-disk route the panel step that remains is
the **disk** write. §9 has the correction.)*

## 1. What is built, and what proves it

| Artefact | Built by | What it is |
|---|---|---|
| `out-sealed/boot/initrd.img` | `deploy/image/sealed-initrd.sh` | Servury's 2026.11.1 initramfs rebuilt with `bb-sealed-unlock` in the generated ORDER |
| `out-sealed/odile-tenant-sealed.raw` | `deploy/image/sealed-image.sh` | a GPT disk: BIOS-boot placeholder, 512 MiB ext4 boot partition with the sealed blob, LUKS2 root with the tenant stack |
| `out-sealed/odile-tenant.efi` (+ `.unsigned`) | `deploy/image/uki.sh` | the UKI: Servury's kernel, the hooked initramfs, the measured command line; signed with the image key |
| `deploy/image/tenant-image.pin` | `uki.sh --pin` | the JSON pin: hashes, ORDER, seal id, both measurements, signature |

The bench host has no `/dev/kvm` and no AMD PSP, so no real
`SNP_GET_DERIVED_KEY` can run here. The bench substitutes one small binary,
`deploy/image/bench-snpreport`, with Servury's snpreport interface and its
BBHK1 file format. It derives its key from **the measured command line**
(HMAC-SHA-256 over `/proc/cmdline` with a public bench root key) instead of
the chip's VCEK. What that proves is the boot chain: the hook is in `ORDER`,
it runs before `cryptroot`, the blob it reads opens the LUKS container, no
passphrase is typed, and a changed command line makes it fail closed. The
cryptographic derivation itself was proven on a real Blackbox in ODL-310 §2/§5
and was not re-proven here. §10 draws the line.

## 2. Prerequisites

On the bench host (measured 2026-11-10, Ubuntu 26.04, kernel 6.12.115, no
`vmx`/`svm`):

```sh
# packages
apt-get install -y debootstrap qemu-system-x86 ovmf cryptsetup-bin gcc cpio \
    sbsign systemd-boot-efi shellcheck openssl
# sev-snp-measure 0.0.13 and ukify are already needed by the ODL-380 bench
/usr/bin/python3 /usr/lib/systemd/ukify --version
sev-snp-measure --version
```

Build inputs that are not in the repo:

| Input | Where | Provenance |
|---|---|---|
| Servury 2026.11.1 `vmlinuz` and `initrd.img` | `out-sealed/release/` (fetched by `sealed-initrd.sh` when absent) | `https://servury.com/downloads/blackbox/2026.11.1/`; hashes `96758c40…` / `8c2aff57…` |
| `blackbox-images-2026.11.1` source tree | `/tmp/blackbox-images` (or `--source`); the build reads it, `--dry-run` does not | `blackbox-images-2026.11.1.tar.gz`, sha256 `ef888e88…` |
| initramfs-tools chroot | `/var/tmp/bb/chroot` (or `--chroot`) | Servury's `build/mkchroot.sh`; Debian 13, `initramfs-tools` 0.148.4 |
| image signing key | `/root/.odile/image-signing.key` | RSA-3072, mode 0600, never in git; the certificate is committed at `deploy/image/odile-image-signing.crt` (sha256 `aea9052c…`, fingerprint `5A:21:B4:7F:…:75:C0`) |

The chroot needs the release kernel's module tree. `sealed-initrd.sh` copies
it out of the published initrd (`unmkinitramfs`) and runs `depmod`; a chroot
that has never built an initramfs also needs the packages Servury's
`mkchroot.sh` installs. The script checks for `mkinitramfs` and stops with a
named error if it is missing.

`--dry-run` needs no source tree: it prints the plan — with a `missing` line
naming the source when it is absent — and exits 0 on a host that has never had
`blackbox-images`. The **build** needs it: without the tree the script exits 2
and prints `sealed-initrd: no blackbox-images source at … (set --source)`
before it fetches or writes anything. The default path is `/tmp`, which is
tmpfs, so a machine that reboots between builds sees the dry run succeed and
the build stop with that line.

## 3. The four commands

```sh
./deploy/image/sealed-initrd.sh --dry-run
sudo ./deploy/image/sealed-initrd.sh --out deploy/image/out-sealed

# First build: a fresh throwaway key (its private half is shredded), and the
# measured command line this disk is sealed to.
./deploy/image/sealed-image.sh --dry-run
sudo ./deploy/image/sealed-image.sh --out deploy/image/out-sealed

# Rebuild the disk to the line the first build just generated, exercising the
# per-tenant path: same bb.key, same seal_id, same bytes (no new keygen). The
# UKI below embeds exactly these bytes, so the pin and the disk agree.
sudo ./deploy/image/sealed-image.sh --out deploy/image/out-sealed \
    --force --cmdline-file deploy/image/out-sealed/boot/cmdline

# Build, sign, measure twice and pin the UKI from that same line.
./deploy/image/uki.sh --in deploy/image/out-sealed --sign \
    --pin deploy/image/tenant-image.pin --variant odl454-other

./deploy/image/smoke.sh sealed --disk deploy/image/out-sealed/odile-tenant-sealed.raw \
    --uki deploy/image/out-sealed/odile-tenant.efi --expect opened \
    --out /var/tmp/odl454/boot-opened
./deploy/image/smoke.sh sealed --disk deploy/image/out-sealed/odile-tenant-sealed.raw \
    --uki deploy/image/out-sealed/variant/odile-tenant-odl454-other.efi --expect prompt \
    --out /var/tmp/odl454/boot-prompt

make check
```

Every state-changing step refuses to overwrite an existing artefact without
`--force` (the initrd and the disk) and has a `--dry-run`; the disk build is
destructive only inside `--out` and its outside-the-repo chroot. `uki.sh` runs
twice unsigned and twice per measurement form, and refuses to write the pin
unless each pair agrees.

## 4. The initrd: the hook at the generated ORDER

`sealed-initrd.sh` rebuilds Servury's initramfs through their source tree and
an initramfs-tools chroot. The hook declares `PREREQ="udev bb-wait"`;
`mkinitramfs` runs `cache_run_scripts()` and writes
`/scripts/init-premount/ORDER` by `tsort` over every script's `prereqs()`.
The script then **reads the ORDER back out of the built initrd** and fails if
the hook is missing, misplaced or byte-different:

```
order:
  /scripts/init-premount/bb-attest "$@"
  /scripts/init-premount/dropbear "$@"
  /scripts/init-premount/bb-wait "$@"
  /scripts/init-premount/bb-sealed-unlock "$@"
```

A copy into the directory without that line never runs: `run_scripts()`
sources `ORDER` and never looks at the directory. The earlier `PREREQ="udev"`
sorted the hook *before* `bb-attest` and `bb-wait`, and on a disk whose
`/dev/sda3` had not appeared it exited 0 without unsealing — an unbootable
machine under the strict bar. Both builds are in ODL-310's evidence 16; this
script reproduces the fixed one and asserts the position.

Two bench substitutions are made and printed:

- `/usr/bin/snpreport` is `deploy/image/bench-snpreport`, the TCG stand-in
  described in §1. Its source is `deploy/image/bench-snpreport.c`, compiled
  `-O2 -static -s` exactly like Servury's own binary.
- the build chroot gets one `/etc/crypttab` line,
  `root_crypt /dev/sda3 none luks,initramfs`, so cryptroot's hook generates
  `/cryptroot/crypttab`. Without it a failed unseal would end at a bare
  `Unable to find root device`; with it, the documented fail-closed state is
  the passphrase prompt. The release initrd ships an empty crypttab.

The build is deterministic: two runs with the same inputs produced the same
initrd (`ee164001…` twice on 2026-11-10, after the 2026-11-10 stand-in fix).

### 4.1 Unmounting the chroot (the build's own host-damage bugs)

The chroot mounts `/proc`, `/dev` and `/sys` inside the target root; `/dev`
and `/sys` are `--rbind` copies, so they carry their own children
(`/dev/pts`, `/dev/shm`, `/sys/fs/cgroup`). Two defects were measured on
2026-11-10 and both are fixed in `sealed-image.sh`:

1. **Mount propagation, and the wrong first fix.** The host's `/` is a
   *shared* mount (the systemd default). A mount made under a shared parent
   propagates into every other mount namespace on the machine; unmounting it
   in the build's namespace removes only that copy, and the peer namespaces
   keep the dm device open. The symptom was `cryptsetup close` answering
   `Device … is still in use` for the full 60-second retry, `dmsetup info`
   reporting `Open 1` with an empty holder list and no process holding a
   descriptor, while the build's own mount table was already empty. The
   evidence was in other processes' `/proc/<pid>/mountinfo`: the same dm
   device, still mounted at the build directory.
   The first fix was `mount --make-rprivate /` — and it was wrong, because run
   in the host namespace it makes the **host** tree private. Measured
   2026-11-10: after a build, `findmnt -o TARGET,PROPAGATION /home/odiledemo`
   read `private` and the live demo reconcile failed twice
   (`/home/odiledemo is mounted from rw, not /var/lib/odile-demo/home.img`)
   until `mount --make-shared /home/odiledemo`; a second build bind-mounted
   the host devtmpfs and `rm -rf`'d it. The build now re-execs itself under
   `unshare -m --propagation private` before its first mount and prints
   `ns private mount namespace`. Every loop partition, mapper, chroot bind
   mount and `rm -rf` lives in that namespace; the host tree is never
   touched. `--dry-run` still runs in the caller's namespace and mounts
   nothing.

2. **`rm -rf` through a live bind.** If an unmount falls back to lazy (or
   fails), `rm -rf "$MNT"` recurses through a still-live `/dev` bind and
   deletes the *host's* device nodes — measured: `/dev/urandom`, `/dev/tty`
   and `/dev/loop*` gone, after which `losetup` and `dmsetup` could not
   operate at all. The cleanup enumerates mount points from
   `/proc/self/mounts` (not `findmnt -R`, which reports nothing once the top
   mount is gone), unmounts deepest-first, and refuses to remove the tree
   while any mount point under it remains. It also checks that `/dev/null`,
   `/dev/tty`, `/dev/urandom` and `/dev/loop-control` are **character
   devices**, not merely files: a shell redirect recreates a deleted
   `/dev/null` as an ordinary file, which would pass a `-e` test. A residual
   mount, a leaked mapper or a missing node exits 3.

## 5. The disk: a throwaway key, a random volume key, a seal

`sealed-image.sh` builds one disk with three partitions, numbered so the
hook's defaults are the paths the boot uses: `sda2` is the ext4 boot
partition and `sda3` the LUKS container, exactly as on a Blackbox.

1. **The boot key is throwaway.** An Ed25519 keypair is generated; the
   public half is base64-encoded into `bb.key=` on the measured command line
   (Servury's initramfs base64-decodes it straight into
   `authorized_keys`); the private half is shredded before the disk is
   touched. The image therefore has **zero usable user keys**: one public key
   whose private half does not exist. `sealed/seal.txt` records
   `private_half=shredded` and the public fingerprint.
2. **V is random and is the only keyslot.** 64 bytes from `/dev/urandom`,
   `cryptsetup luksFormat --key-file V` on the LUKS partition, `mkfs.ext4` on
   the mapper, then the tenant stack through `install-stack.sh` inside the
   chroot, twice (the second run prints `same` — the same idempotency evidence
   as the plain image). **V is a keyslot passphrase, not the volume key**:
   `luksFormat` generates a separate random master (volume) key and wraps it in
   keyslot 0. Everything in this section is about V; §5.1 is about the volume
   key, which is the one the published image would otherwise share with every
   tenant.
3. **V is sealed to the exact command line**, and the plaintext is shredded.
   The blob is written to `sda2:/odile/luks-vol.key.sealed`, with its keyslot
   sidecar beside it.
4. **The round trip is proven before the build is called done**:
   `snpreport --unseal` with the command line must reproduce V byte for byte,
   and `cryptsetup luksOpen --test-passphrase` must accept it. A build that
   cannot open its own disk fails there.

### 5.1 The first boot re-keys the volume (ODL-454 follow-up)

A disk written from the published image boots with the **build-time volume
key**. A keyslot dance (`luksAddKey` + `luksKillSlot`) does not change it:
LUKS keeps the master key across both calls. So the published image — which
anyone can download, and which carries the bootstrap keyslot — opens a
tenant's disk. The first boot must therefore replace the volume key, not just
add a keyslot.

On the first boot, `odile-seal-provision.service` runs
`bb-sealed-provision` (Before `odile-firstboot.service`, so the page comes up
after the seal is final):

1. a fresh 64-byte V from the guest's `/dev/urandom`;
2. V added as a new keyslot, so the re-key has a proved way in before it
   starts;
3. V sealed with `SNP_GET_DERIVED_KEY(policy|measurement)`, the blob written
   to `sda2:/odile/luks-vol.key.sealed`, and the round trip proved
   (`snpreport --unseal` reproduces V, LUKS accepts it);
4. `cryptsetup reencrypt` **without `--keep-key`**: a new volume (master) key
   is generated on the guest, the data is re-encrypted in place — online, with
   the root mounted, so the boot continues — and every other keyslot is
   removed when it commits. The build keyslot dies there;
5. the keyslot sidecar is rewritten to the keyslot that survived (not the one
   that was asked for), and the volume key's fingerprint is compared with the
   one read through the build key before the re-key;
6. `/odile/luks-bootstrap.key` shredded and `seal.json` written.

The script refuses to commit unless it can prove the change on the guest: it
fingerprints the volume key through the build key before the re-key and
through V after (`cryptsetup luksDump --dump-master-key`; the SHA-256 of the
64 key bytes, the form ODL-466 published, is the only thing logged), requires
the two to differ, requires the build key to be refused afterwards, and
requires V to open the volume again. The whole thing is idempotent: a blob
that unseals, whose key opens the volume, on a volume the build key no longer
opens, is `already provisioned` and the run only finishes what is still owed
(the bootstrap file shredded, `seal.json` written).

An interrupted re-key is finished on the next boot, and the key that opens an
interrupted volume is **V, not the build key**. `cryptsetup reencrypt` puts the
new volume key in V's keyslot as its first act, so a volume left in
`Requirements: online-reencrypt-*` refuses the build key (`No key available
with this passphrase`) and the blob's V opens it (measured, ODL-454 follow-up
round 2; §7 of `docs/evidence/m1-image.md`). The next run unseals V, reads its
keyslot from the sidecar, runs `cryptsetup repair` — the recovery a hard kill
demands when the segment it was rewriting is still flagged `in-reencryption`;
on a crash between segments repair only re-verifies the re-encryption digest
and writes nothing — and then `cryptsetup reencrypt --resume-only --key-file V
--key-slot <sidecar>`, which finishes the pass and finalizes the header. Only
then does it decide: re-key if the build key still works, finalize if not.

What a reboot can and cannot recover is worth stating. The data pass rewrites
the volume a segment at a time, so a crash *during* the pass leaves the data a
mix of the build and the guest volume keys: the part already rewritten (the
ext4 superblock and the start of the volume) opens with V, which is what lets
the seal unit run at all, but the rest is still under the build key, so
mounting the whole root read-only there is not trustworthy. A crash at or after
the data pass (the finalize, the metadata commit) leaves every sector under V
and is recovered by the repair and resume above with no initramfs work. A crash
*inside* the data pass needs the pass finished first — `cryptsetup repair` and
`reencrypt --resume-only` with V, from an initramfs or rescue shell — before
the root will mount; only then does a normal boot follow.

`--rotate` is the seal-break path and the keyslot dance is its fallback: on a
volume that is already the guest's, a new V in a new keyslot, the old V
keyslot killed, the volume key unchanged.

Two costs are stated rather than hidden. The re-key re-encrypts the whole root
partition (7.3 GiB on the 8 GiB image), so the unit needs
`TimeoutStartSec=infinity` — the 90 s default would SIGTERM it mid-re-key on
every boot — and the first boot is minutes, not seconds, on a 2-vCPU guest.
Every later boot unseals in the usual time.

**The image must carry `udev`.** debootstrap's `minbase` does not install
systemd-udevd, and an online LUKS2 re-encryption is a device-mapper operation:
`cryptsetup reencrypt` waits for the udev event that follows the dm table swap.
Without udevd that wait never returns. Measured on the bench 2026-11-13: the
unit printed `re-keying /dev/sda3 …` at 192 s, then the guest went idle for
70 minutes with the header left at `Requirements: online-reencrypt-v2` — the
data was re-encrypted, the finalize never happened, so no state was written and
the bootstrap file stayed. The same absence kept `/dev/ttyS0` from becoming a
device unit (`dev-ttyS0.device: Job timed out`), so the console had no getty
and therefore no drill shell. `udev` is in the image's package list on this
branch, and `tests/test_sealed_image.py` reads the list.

A run killed after the re-key commits and before the shred is the other shape
the bench produced: the blob opens, the build key does not, the bootstrap file
is still on the boot partition and `seal.json` is absent. The next boot re-runs
the unit (its `ConditionPathExists=!/var/lib/odile/seal.json` still holds) and
the `already provisioned` path finishes the job — shred the file, write the
state — instead of exiting early. An interrupted re-encryption is finished
first: V (from the blob) opens the volume the build key no longer opens,
`cryptsetup repair` clears the recovery state a hard kill leaves, and
`reencrypt --resume-only --key-file V --key-slot <sidecar>` finishes the pass
before the `already provisioned` path finalizes. A stop mid-re-key is therefore
recoverable on the next boot; the cost is the remaining data pass, not the
machine.

**The bench rehearsal.** A build host has no AMD PSP, so the disk cannot be
sealed to a chip at build time; the production image ships the bootstrap key
and `--production`. `--first-boot-seal` (or `ODILE_FIRSTBOOT_SEAL=1`) is the
bench's version of the same state: the bootstrap key, **no blob**, and the
bench stand-in installed as the seal unit's `snpreport`. The hook then opens
with `opened-bootstrap`, and the first boot runs the real re-key code against
the stand-in "chip". It is not a tenant image and refuses to combine with
`--production`.

The command line is the per-tenant input. It carries `seal_id=<token>`,
`bb.host`, `bb.iid`, the throwaway `bb.key`, the network parameters and
`root=/dev/mapper/root_crypt`. `uki.sh` (§6) embeds the same bytes in the
UKI's `.cmdline`, so the firmware measures them and the hook derives from
them. The bench's `seal_id` is fixed (`odl454-bench-v1`) so the pin is
stable; a real tenant gets a fresh random token (§9).

**Rebuilding to an existing line.** The first build generates the throwaway
key, so its command line differs from run to run. Once a UKI exists, the disk
must be built from *that* line, or the blob is sealed to bytes the UKI does
not measure. `--cmdline-file FILE` takes the line verbatim: it decodes the
`bb.key=` public key into `boot/thrownaway-key.pub`, seals V to the same
bytes, and writes the same `boot/cmdline`. The rebuild is not byte-identical
(the LUKS header and the volume key are random), but the *measurement* is:
`uki.sh` rebuilds the unsigned UKI byte-for-byte from the pinned kernel,
initrd and command line. `--seal-id` and `--cmdline-file` are mutually
exclusive; a line without `root=/dev/mapper/root_crypt`, `bb.key=` or
`seal_id=` is rejected before anything is touched.

## 6. The UKI: build twice, sign, measure twice, pin

`uki.sh` builds the unsigned UKI **twice** and fails unless the two files are
byte-identical. Signing happens once, afterwards, because `sbsign` stamps a
PKCS#7 signing time: the signed file is not byte-reproducible, so the script
pins it by sha256 and measurement and pins the unsigned build separately for
a rebuild comparison.

```sh
./deploy/image/uki.sh --in deploy/image/out-sealed --sign \
    --pin deploy/image/tenant-image.pin --variant odl454-other
```

- `--sign` uses `/root/.odile/image-signing.key` and the committed
  `deploy/image/odile-image-signing.crt`, and verifies with `sbverify --cert`
  before measuring. The `.sig` is embedded in the PE; no detached file is
  needed.
- the measurement runs twice for the signed UKI, twice for the unsigned one,
  and twice for the loose kernel+initrd+cmdline form; each pair must agree.
  `sev-snp-measure --mode snp` is run against Servury's published
  `OVMF.amdsev.fd` (`8731846c…`, hash-checked), 2 vCPUs, `EPYC-v4`,
  guest-features `0x1`.
- `--variant T` builds a second UKI whose only change is `seal_id=T`. It is
  the changed-cmdline fail-closed test in §7 and the per-tenant separation
  proof: the same disk, the same image, a different measured line, a different
  derived key.

The pin is a JSON file in the repo (`deploy/image/tenant-image.pin`). It
records the OVMF, stub, kernel, initrd, hook and ORDER, the command line, the
seal id, the derived-key id, the throwaway-key fingerprint, the signed and
unsigned UKI hashes and measurements, the loose measurement, both runs of each,
the signing certificate's sha256 and fingerprint, and the raw disk's sha256.
`tests/test_sealed_image.py` parses it and asserts the shape, the agreement of
the two runs, the hook's position in ORDER, and that the derived-key id
matches the command line.

## 7. The bench boots

Both boots use QEMU TCG (no KVM) with the `ovmf` package's firmware,
`-kernel <uki>`, and the disk on **virtio-scsi** so the guest sees
`/dev/sda2` and `/dev/sda3` exactly as the hook and Servury's initramfs
expect. The UKI is loaded through firmware `fw_cfg`, the same way a Blackbox
launches the image; the firmware here is generic OVMF, not Servury's amdsev
firmware, because TCG has no SEV to exercise.

```sh
./deploy/image/smoke.sh sealed --disk .../odile-tenant-sealed.raw \
    --uki .../odile-tenant.efi --expect opened --out /var/tmp/odl454/boot-opened
```

The pass needs three things, and prints each: the hook's
`bb-sealed-unlock: root_crypt opened with the SNP-derived key` line in the
boot transcript, the guest's own `sealed-unlock PASS` line (from
`/run/bb/sealed-unlock.state` plus the kernel's answer for `/`), the same five
image-bench checks as the plain image (302/401/200/101, shell, restart
persistence, cron), and no passphrase typed anywhere. No SSH session is opened
in any step.

The guest-side check reads `/proc/mounts` for the device `/` was mounted from
and resolves it through `/sys/class/block/<dev>/dm/name`:

```
ODILE-BENCH sealed-unlock PASS state opened; / is /dev/mapper/root_crypt (dm name root_crypt)
```

That is the kernel's own name for the mapper, so a renamed symlink cannot
pass, and no userspace tool has to exist in the guest. The first version of
this check ran `cryptsetup status root_crypt`; the guest image does install
`cryptsetup-bin`, but `/dev/mapper/root_crypt` is created in the initramfs and
the root's udev did not recreate the symlink, so a boot that had unsealed
correctly still reported "inactive" (measured 2026-11-10). The bench's verdict
therefore does not depend on `cryptsetup` at all. `check-sealed-boot.sh` is
shipped at `/usr/local/sbin/check-sealed-boot.sh`; it was proven against the
hook in the spike's chroot harness (ODL-310), and its `cryptsetup status`
branch is **not** proven on this image — use the bench line above as the
authoritative guest check.

The fail-closed boot uses the variant UKI:

```sh
./deploy/image/smoke.sh sealed --disk .../odile-tenant-sealed.raw \
    --uki .../variant/odile-tenant-odl454-other.efi --expect prompt \
    --out /var/tmp/odl454/boot-prompt
```

The blob was sealed to `seal_id=odl454-bench-v1`; the variant's line says
`seal_id=odl454-other`, so `snpreport --unseal` returns 6, the hook prints
`bb-sealed-unlock: the blob is not sealed to this boot (unseal rc 6); leaving
cryptroot's prompt`, exits 0, and `cryptroot` reaches `Please unlock disk
root_crypt:`. The harness requires **both** lines: a bare prompt is also what
the stock no-hook image prints, so the hook's own failure line is what proves
the prompt is the documented fail-closed state rather than a missing hook.
That prompt is the documented fail-closed state — on a strict-bar disk there is
no passphrase to type, so a seal break is **rebuild + restore** (recovery code
+ backup), not a support session.

## 8. What the pin means, and how a stranger checks it

The pin is a record, not a reproducibility claim for the signed file:

- **Unsigned build**: two `ukify build` runs were byte-identical; the pin's
  `uki_unsigned.sha256` can be rebuilt from the pinned kernel, initrd and
  command line.
- **Signed build**: `sbsign` writes a signing time, so a re-sign produces a
  different file and a different measurement. `signature.verified: true` plus
  the certificate's sha256 and fingerprint say which key signed it; the
  measurement is the number Servury's firmware would report for *this* file.
- **Every input** is named by sha256: OVMF, stub, kernel, initrd, hook, raw
  disk. The initrd's ORDER is in the pin as strings, and the hook's sha256
  must equal `sha256sum spike/sealed-unlock/initramfs/bb-sealed-unlock`.
- **The disk hash is as-built.** `raw_image_sha256` is taken by the build
  before anything boots it; a boot writes to the disk (journal, machine-id,
  the smoke marker), so a disk that has been booted no longer matches. Hash it
  again before the first boot if you need to compare.

Check it with:

```sh
/usr/bin/python3 - <<'PY'
import json; p=json.load(open("deploy/image/tenant-image.pin"))
print(p["uki_signed"]["sha256"], p["uki_signed"]["measurement"])
print(p["signature"]["cert_fingerprint_sha256"], p["signature"]["verified"])
PY
sha256sum deploy/image/out-sealed/odile-tenant.efi
```

## 9. The first real tenant: one panel step

The production image is the same recipe with the real snpreport: rebuild
Servury's release initramfs with the hook (their `build/build-initramfs.sh`,
`KVER` from the published release) and do **not** substitute the bench
stand-in. Publish the UKI at an http(s) URL with its SHA-256, then at
provision time:

1. **Boot image — one API call; the panel step that remains is the disk.**
   `POST ?action=boot-image&id=…` with `{"url", "sha256"}`: the node downloads
   the file, checks the hash, and re-hashes it at every start before QEMU sees
   it. The pre-12-November step read "**Manage → Boot image** in the Blackbox
   panel: the URL and the SHA-256 … This is the one panel step per tenant; the
   reseller API has no boot-image verb" *(corrected 12 November 2026: **disk** image only; `?action=boot-image` measured 200 — details below.)*
   *(12 November 2026 — corrected: the **boot** image does have an API verb,
   `GET|POST ?action=boot-image&id=…` with `{"url", "sha256"}`; measured 200
   on the live API. The missing verb is the **disk** image's
   (`?action=disk-image` → 404). §1's route therefore splits two ways: the
   boot pair is one call, and the panel step that remains is the **disk**
   write — §1's `odile-tenant-sealed.raw` with its URL and SHA-256, in the
   panel's Disk image field. The command line stays empty for this
   per-tenant UKI. `docs/architecture/ADR-image-vs-installer.md` §15.1 has
   both commands; `docs/evidence/m1-tenant.md` §4 is the round-8 run.)*
2. **Command line**: the panel's command line is where the per-tenant line is
   entered for a per-release UKI (leave the UKI's `.cmdline` empty). It must
   carry `bb.host`, `bb.iid`, `bb.ip4`, `bb.gw4`, `bb.ip6`, `bb.gw6`,
   `bb.dns`, a **throwaway** `bb.key` (public half only; shred the private
   half) and a fresh `seal_id=<random>`. `bb.ip4` is not optional: the
   release installer's boot check reads the boot report's command line and
   requires exactly one `bb.ip4=` (it writes the address into
   `/etc/odile/boot-record.json`), and a line without it installs the seed
   and then exits 5 with no Caddy — measured on ODL-332 round 4.
   `sealed-image.sh --cmdline-file` now refuses such a line before the build.
   The disk is sealed to those exact bytes; on the
   bench the same file is replayed with
   `sealed-image.sh --cmdline-file FILE`. If the panel ignores a command line
   for a UKI, build a per-tenant UKI instead; either way the first boot must
   `attest` and confirm the measurement covers the per-tenant line before it is
   trusted (ODL-310 §7.1). The `seal_id` is not decorative: a different token
   derives a different key, which is what §7's fail-closed boot proves.
3. **First boot**: the image must seal its own disk, not ask for a
   passphrase. On a disk that is already LUKS, `bb-sealed-unlock` opens it with
   the bootstrap key (`opened-bootstrap`), and the booted system's
   `bb-sealed-provision` re-keys the volume and seals the guest key (§5.1).
   The serial must carry, in this order, `the blob opens … but the build
   keyslot still does; re-keying`, `re-keyed: volume key <before> -> <after>,
   keyslot N, only the guest key remains`, `sealed: … (keyslot N)`,
   `shredded the first-boot bootstrap key`; then the first-boot line. A second
   boot must print `bb-sealed-unlock: root_crypt opened with the SNP-derived
   key` and `odile-firstboot: same`. An **absent** state file means the hook
   never ran (ORDER), not that it failed silently.
4. **The drill, on the machine**: read the volume key's fingerprint through
   the live key and through the published image's bootstrap key, and require
   the first to differ from the published image's and the second to be refused:

   ```sh
   cryptsetup luksDump --dump-master-key --key-file live.key /dev/sda3   # sha256 of MK
   cryptsetup luksOpen --test-passphrase --key-file luks-bootstrap.key /dev/sda3
   # "No key available with this passphrase." after the first boot
   ```

   `docs/evidence/m1-image.md` §6 has the bench run of both.
5. **The measurement**: compare the first boot's attest record and
   `sev-snp-measure` against the pin. A new image changes the measurement, so
   Servury's own host-key blob breaks once and the initramfs makes a new host
   key (`hostkey.state=new`). That is one-time, not a defect.
6. **No operator key, no recovery credential.** The boot key is throwaway;
   the user's way back is the recovery code and the backup password, with
   rebuild + restore for a seal break. Root inside the guest can derive the
   key (the ioctl is guest-available); the seal protects against the host and
   an offline disk, which is the V0.1 threat model.

## 10. What the bench proves, and what only a Blackbox can prove

**The bench proves**: the hook is placed by the real `mkinitramfs` at the
generated ORDER; the release initramfs runs it before `cryptroot`; the sealed
blob opens a LUKS2 root with no passphrase and no SSH; **the first boot
re-keys the volume and the published image's key no longer opens the disk**
(§5.1: the re-key is the real `cryptsetup reencrypt` on the real script, only
the seal's chip is the stand-in); the full tenant stack boots and answers all
five bench checks; a changed `seal_id` in the measured command line changes
the derived key and fails closed at the prompt; the throwaway key's private
half is shredded; the UKI is signed and its measurement is stable across runs.

**Only a Blackbox can prove**: `SNP_GET_DERIVED_KEY` against a real VCEK and
PSP (the bench stand-in derives from the command line, not the chip); the
measurement the firmware actually extends on a real launch; the panel upload,
download and re-hash; the first boot on a disk whose volume was re-keyed by a
chip-derived seal; and a second tenant on the same chip.

## 11. Recorded evidence

Bench host, 2026-11-10: `Linux 6.12.115+deb13.1-amd64`, no `/dev/kvm`,
QEMU 10.2.1 (`-accel tcg,thread=multi`), QEMU's `ovmf` package
(`/usr/share/ovmf/OVMF.fd`, sha256 `7f4fffc5…`), `sev-snp-measure 0.0.13`,
`ukify 259`. The UKI is measured against Servury's published
`OVMF.amdsev.fd` (`8731846c…`), because that is the firmware a Blackbox
loads. The full serial transcripts and run logs stay on the bench host under
`/var/tmp/odl454/` and are attached to the ODL-454 card; the commands below
are the ones that produced them.

### 11.1 The artefacts

```sh
$ sha256sum deploy/image/out-sealed/odile-tenant.efi \
            deploy/image/out-sealed/odile-tenant.efi.unsigned
11917960da60cc213866f9322bcea1f47a6b808a3122274d6b6a1b2521e41e0a  .../odile-tenant.efi
861ce1a6e1fd338eaa9c46f32a7fa0753080fb9752b037e8f96f447fc7d59074  .../odile-tenant.efi.unsigned
```

| Input | sha256 |
|---|---|
| kernel `vmlinuz` | `96758c40859c94163e5cf81399a107d679e984e251cb4e13aba4e7d92f5d14f2` |
| initrd `initrd.img` | `ee1640011edbbbc3d6b1aef4fde35bb9ffbb51b209e08cd1d0399ad7acfa8e07` |
| hook `bb-sealed-unlock` | `0022021cfbcda3a3943ae1353a57ee4ce6e755e4d4c7bfe3df048ef884802f4f` |
| Servury `OVMF.amdsev.fd` | `8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b` |
| `linuxx64.efi.stub` | `e3b63f011902bc1b31d6a00bdb5b0dac72526aae882a4807e85a45d49f6e2306` |
| signing certificate | `aea9052cf8d2d0f404c5320848eb0f5402cea090248f05a51bbb5cfdbd2b1a06` |
| raw disk (as built, before any boot) | `d2b45fb04f0df8e4e78dda9a249785e97d7a1d8c257e9b50a1ab255e805a7a68` |
| sealed blob on `/dev/sda2` | `533b2447af7fb344007460344eca26e5a8b287aed242ac30e01731ce10cbd660` |
| derived-key id (`seal_id=odl454-bench-v1`) | `key_sha256=f800ca6fa3072d3b3b274886c677716514665c4acacb1b46f3d503cde6eb32b6` |

The measured command line and every one of these values are in
`deploy/image/tenant-image.pin`.

### 11.2 The hook is in the ORDER the real `mkinitramfs` generated

```sh
$ unmkinitramfs deploy/image/out-sealed/boot/initrd.img /var/tmp/odl454/evidence/initrd
$ cat /var/tmp/odl454/evidence/initrd/scripts/init-premount/ORDER
/scripts/init-premount/bb-attest "$@"
[ -e /conf/param.conf ] && . /conf/param.conf
/scripts/init-premount/dropbear "$@"
[ -e /conf/param.conf ] && . /conf/param.conf
/scripts/init-premount/bb-wait "$@"
[ -e /conf/param.conf ] && . /conf/param.conf
/scripts/init-premount/bb-sealed-unlock "$@"
[ -e /conf/param.conf ] && . /conf/param.conf
$ cmp /var/tmp/odl454/evidence/initrd/scripts/init-premount/bb-sealed-unlock \
      spike/sealed-unlock/initramfs/bb-sealed-unlock && echo same
same
```

### 11.3 The measurement, reproduced twice and pinned

Two fresh `sev-snp-measure` runs per form, after the pin was written; all six
agree with `deploy/image/tenant-image.pin` (`runs.*[0] == runs.*[1]`):

```sh
$ sev-snp-measure --mode snp --ovmf deploy/image/out-sealed/ovmf/OVMF.amdsev.fd \
    --kernel deploy/image/out-sealed/odile-tenant.efi \
    --vcpus 2 --vcpu-type EPYC-v4 --guest-features 0x1
9704da108f963a79e119c0ddbe8ffecea0e72f7c3575316490404b5b2ccfeb99906de8981a40c087a920988bc431024e
9704da108f963a79e119c0ddbe8ffecea0e72f7c3575316490404b5b2ccfeb99906de8981a40c087a920988bc431024e
$ sev-snp-measure ... --kernel deploy/image/out-sealed/odile-tenant.efi.unsigned ...
a75718c87f4df0eff00ba1c40a612e383fd852de0c74eda4f58f8a16ed884ac1f889e83f71d770b02b6846c05e735f6e
a75718c87f4df0eff00ba1c40a612e383fd852de0c74eda4f58f8a16ed884ac1f889e83f71d770b02b6846c05e735f6e
$ sev-snp-measure ... --kernel .../boot/vmlinuz --initrd .../boot/initrd.img \
    --append "$(cat .../boot/cmdline)" ...
ff6fd78255834670bb1ecfba0c053d7523ac4faa5ce41ed95e9902049d6d21e6e78868fba8fbd0a045a4acc4a53a678f
ff6fd78255834670bb1ecfba0c053d7523ac4faa5ce41ed95e9902049d6d21e6e78868fba8fbd0a045a4acc4a53a678f
$ /usr/bin/python3 -c "import json; p=json.load(open('deploy/image/tenant-image.pin')); \
    print([p['runs'][k][0] == p['runs'][k][1] for k in ('uki_signed','uki_unsigned','loose')])"
[True, True, True]
```

### 11.4 The signature

```sh
$ sbverify --cert deploy/image/odile-image-signing.crt \
      deploy/image/out-sealed/odile-tenant.efi
Signature verification OK
$ openssl x509 -in deploy/image/odile-image-signing.crt -noout -subject -fingerprint -sha256
subject=CN=Odile image signing (bench) 2026
sha256 Fingerprint=5A:21:B4:7F:D5:26:B6:E0:21:15:B1:E6:23:78:16:B1:D1:E5:63:67:CC:B5:B2:E8:22:F2:70:A7:7F:47:75:C0
```

### 11.5 The boot that unseals with no passphrase

```sh
$ ./deploy/image/smoke.sh sealed \
    --disk deploy/image/out-sealed/odile-tenant-sealed.raw \
    --uki  deploy/image/out-sealed/odile-tenant.efi \
    --expect opened --out /var/tmp/odl454/boot-opened2
ODILE-BENCH sealed boot odile-tenant.efi + odile-tenant-sealed.raw under QEMU TCG/OVMF, serial /var/tmp/odl454/boot-opened2/sealed-boot.log
ODILE-BENCH sealed-unlock PASS bb-sealed-unlock opened root_crypt before cryptroot ran
ODILE-BENCH guest gate answered after 151s wall
ODILE-BENCH gate-anonymous PASS GET / -> 302 /login?next=%2F; GET /api/sessions -> 401; GET /login -> 200; wrong password -> 401
ODILE-BENCH gate-session PASS POST /auth/password-login -> 200 (cookie: yes); GET / with cookie -> 200 (Webapp shell: yes); POST /api/auth/ws-ticket -> 200; GET /api/ws -> 101 (anonymous -> 403)
ODILE-BENCH shell PASS runuser -u tenant -- sh -lc ... -> Hermes Agent v0.23.0 (2026.9.24) · stable · local 2bd378ed1b8d
ODILE-BENCH persistence PASS marker survived systemctl restart odile-tenant; unit active; MainPID owner tenant
ODILE-BENCH cron PASS bench-heartbeat fired at 2026-11-10T13:41:44Z after 0s
ODILE-BENCH sealed-unlock PASS state opened; / is /dev/mapper/root_crypt (dm name root_crypt)
ODILE-BENCH sealed boot=151s total=326s serial=/var/tmp/odl454/boot-opened2/sealed-boot.log
ODILE-BENCH final PASS image-bench: PASS (5/5)
image-bench: PASS (5/5)
sealed-bench: PASS (unlocked without a passphrase; 5/5 image checks)
```

The serial transcript's own lines are the hook, not the harness:

```sh
$ grep -a bb-sealed-unlock /var/tmp/odl454/boot-opened2/sealed-boot.log
bb-sealed-unlock: root_crypt opened with the SNP-derived key
$ grep -ac 'Please unlock disk' /var/tmp/odl454/boot-opened2/sealed-boot.log
0
```

No passphrase was typed, no SSH session was opened, and the guest's own check
read `state opened` and the kernel's dm name for `/`.

### 11.6 The changed command line fails closed

The variant UKI is the same build with one token changed (`seal_id=odl454-other`;
it is unsigned, because this OVMF runs without Secure Boot and the seal depends
on the command line, not the signature):

```sh
$ ./deploy/image/smoke.sh sealed \
    --disk deploy/image/out-sealed/odile-tenant-sealed.raw \
    --uki  deploy/image/out-sealed/variant/odile-tenant-odl454-other.efi \
    --expect prompt --out /var/tmp/odl454/boot-prompt
ODILE-BENCH sealed boot odile-tenant-odl454-other.efi + odile-tenant-sealed.raw under QEMU TCG/OVMF, serial /var/tmp/odl454/boot-prompt/sealed-boot.log
ODILE-BENCH sealed-unlock PASS the changed cmdline failed the seal, the hook left the prompt
sealed-bench: PASS (fail-closed: a changed cmdline did not unseal)
```

The serial transcript carries both required lines — the hook's failure and
cryptroot's prompt:

```sh
$ grep -a -E 'bb-sealed-unlock|Please unlock disk root_crypt' \
      /var/tmp/odl454/boot-prompt/sealed-boot.log
bb-sealed-unlock: the blob is not sealed to this boot (unseal rc 6); leaving cryptroot's prompt
Please unlock disk root_crypt:
```

The derived key changes with the token, which is why the blob no longer opens:

```sh
$ deploy/image/out-sealed/bench-snpreport --key-id --cmdline-file deploy/image/out-sealed/boot/cmdline
key_sha256=f800ca6fa3072d3b3b274886c677716514665c4acacb1b46f3d503cde6eb32b6
$ deploy/image/out-sealed/bench-snpreport --key-id --cmdline-file deploy/image/out-sealed/variant/cmdline-odl454-other
key_sha256=b7ff864c36b4b5b69d883c112c5a9d70a75ff27a78de3c52a7d149bfa2c8c302
```

### 11.7 Zero usable user keys

```sh
$ ls -l deploy/image/out-sealed/boot/thrownaway-key.pub
-rw-r--r-- 1 root root 112 ... deploy/image/out-sealed/boot/thrownaway-key.pub
$ ls deploy/image/out-sealed/boot/thrownaway-key
ls: cannot access '.../thrownaway-key': No such file or directory
$ grep -E '^(boot_key|private_half)=' deploy/image/out-sealed/sealed/seal.txt
boot_key=SHA256:TXoEdGDopuhzALWE2MUfnKUIL+JmPTC2vmAL6tkQaA4
private_half=shredded
```

Read-only inspection of the sealed disk (unseal the blob, `cryptsetup open`,
mount `-o ro`) finds no `authorized_keys` anywhere and no `/home/tenant/.ssh`;
`/etc/odile/` holds only the public key and the seal id. The one key on the
image is the public half above; its private half does not exist.

### 11.8 `make check`

```sh
$ make check
/usr/bin/python3 -m unittest discover -s tests -v
Ran 802 tests in 62.474s
OK
Ran 20 tests in 15.521s
OK
claims lint: OK (16 file(s), 14 forbidden phrases)
brand lint: OK (12 surface(s), 21 allowed colours)
make check: OK
```

`tests/test_sealed_image.py` is part of the 802: 33 tests covering the
stand-in's key derivation, seal/unseal, the spike's reference blob in both
directions, the boot-verdict parser (`opened`, `prompt`, and the two
false-pass cases), the pin's shape, the scripts' dry runs, and the mount
namespace re-exec (its arguments are replayed, and the host's propagation is
unchanged).

### 11.9 The build leaves the host mount tree alone

The first version ran `mount --make-rprivate /` in the host namespace and
made the host tree private (see §4.1). The re-exec fix was exercised with an
existing disk, which stops the child right after the namespace is entered. The
transcript below is the 2026-11-10 measurement, when `/home/odiledemo` was
still the HQ demo's loop mount (`/var/lib/odile-demo/home.img`); `/home`
itself is not a mount, so re-running the `echo` today prints an empty second
field and the machine's own shared-mount count:

```sh
$ ./deploy/image/sealed-image.sh --out deploy/image/out-sealed
ns           private mount namespace (unshare -m --propagation private)
sealed-image: deploy/image/out-sealed/odile-tenant-sealed.raw exists; pass --force to rebuild it
$ echo "before: $(findmnt -no TARGET,PROPAGATION /) | /home/odiledemo $(findmnt -no TARGET,PROPAGATION /home/odiledemo) | shared mounts $(grep -c 'shared:' /proc/self/mountinfo)"
before: / shared | /home/odiledemo /home/odiledemo shared | shared mounts 2
# (same three values after the run)
```

The real build path below is unchanged: the namespace only decides where the
loop partitions, mapper and chroot binds are mounted, not what is built.

## 12. Cleanup and rollback

```sh
# the build artefacts are gitignored; remove them when the disk is needed back
rm -rf deploy/image/out-sealed
# the chroot keeps the release module tree and vmlinuz; remove them if the
# chroot is shared with another build
rm -rf /var/tmp/bb/chroot/lib/modules/6.12.115+deb13.1-amd64 \
       /var/tmp/bb/chroot/boot/vmlinuz-6.12.115+deb13.1-amd64
```

The build scripts close their mapper and loop on exit and assert the host's
cgroup2 mount and `/dev` nodes survived (`LEAK`/`HOST DAMAGE` exit 3). The
signing key stays in `/root/.odile/image-signing.key`; rotating it means a
new certificate, a new pin and a re-sign, not a rebuild of the disk.

## 13. The published image ships no usable sign-in (ODL-521)

### 13.1 The rule

A published image carries no password a stranger can use. `/home/tenant/.hermes/.env`
holds a scrypt hash of a password generated in the build and thrown away, with
the marker that says so; the first boot leaves it alone because it already
rejects every password; the release installer rewrites it on **every** run until
the first-visit page has set the owner's sign-in; and the page is the only
writer of a hash a person can use. The session-signing secret
(`HERMES_DASHBOARD_BASIC_AUTH_SECRET`) is not this rule: it is byte-identical
before and after the first boot, and ODL-522 makes it per-machine.

Before this card the image carried the scrypt hash of the bench fixture
`odile-bench-not-a-secret`. `install-stack.sh --seed` kept it on the first
boot and `ensure_env(first_visit=True)` kept any existing hash, so a workspace
was open with a public password from the page's first 200 until the owner's
first visit (ODL-466 §0.2, finding 1; on the live tenant the window was
14:44:45Z–14:57:46Z and the URL is in Certificate Transparency logs).

### 13.2 The mechanism

Three writers, in order, and one state key.

1. **The build.** `sealed-image.sh --production` writes `/etc/odile/production`
   before `install-stack.sh` runs. The production branch writes
   `# odile-signin-placeholder: 1`, a scrypt hash of a password generated and
   discarded, and a fresh session secret; the file gets mode 0600 and three
   keys. The plaintext password file (`/var/lib/odile-bench/password`) is a
   bench artefact and is not written at all. A bench build (no
   `/etc/odile/production`) keeps the documented fixture password its QEMU
   smoke signs in with, under `# bench-password-sha256:`.
2. **The first boot.** `odile-firstboot.py` runs `install-stack.sh --seed`; the
   production branch sees a scrypt hash that rejects the fixture and prints
   `same … (a discarded sign-in, no usable password)`. The file, and the
   session secret in it, do not move. A file with no scrypt hash at all (the
   vendor bootstrap leaves its own `.env.example` copy) or one that *does*
   accept the fixture is replaced.
3. **Every `--first-visit` run of the release installer.** `ensure_env` reads
   `/etc/odile/first-visit.json`; while `signin_set_at` is absent it rewrites
   the hash with another discarded one, even when the allowlist looks valid,
   and keeps the session secret. After `signin_set_at` it prints
   `same … (mode 0600, 3 allowlisted keys, allowlist ok)` and keeps the owner's
   hash.
4. **The page.** `POST /first-visit` writes the owner's scrypt hash and
   `signin_set_at`. Nothing else writes a usable hash.

### 13.3 The build: a discarded hash and the marker

The production build, at head `cf05e31f0309b6d118b6618b1ce0dfeafc9a662c`, with the fixture seed the boot will
use (`prepare` prints the one-time token once and stores it 0600):

```sh
$ OUT=/var/tmp/odile-odl521-prod
$ FX=/var/tmp/odile-odl521-boot
$ ./deploy/image/firstboot-fixture.py prepare --dir "$FX" \
      --instance 45445445-0000-4000-8000-000000000454
tenant     odl-bench
instance   45445445-0000-4000-8000-000000000454
expires    2026-11-15T15:28:20Z
token      15c13082a9fbb0e7536d4cc183c3af1ab9f068a7d869c7235c582f817cb313f9 (minted)
$ ./deploy/image/sealed-initrd.sh --out "$OUT" --force
$ ODILE_GUEST_NAME=odl-bench \
  ODILE_IP_TOKEN="ip=dhcp bb.ip4=10.0.2.15/24 bb.gw4=10.0.2.2 bb.dns=10.0.2.3" \
  ODILE_SEED_TOKEN="$(cat "$FX/token")" \
  ODILE_SEED_URL="http://10.0.2.2:18081/seed" \
  ODILE_SEALED_ROOTFS=/var/tmp/odile-odl521-prod-rootfs \
      ./deploy/image/sealed-image.sh --out "$OUT" --production --force
$ ./deploy/image/uki.sh --in "$OUT" --sign --pin "$OUT/tenant-image.pin" --vcpus 2
$ ./deploy/image/place-pin.sh --disk "$OUT/odile-tenant-sealed.raw" \
      --pin "$OUT/tenant-image.pin"
write
pin raw_image_sha256 -> 05177404cd75 (as built, pin inside)
release      installer 10a38b68681f…, manifest daa8340f566c…, listener manifest.py, unit odile-manifest.service
write        /home/tenant/.hermes/.env (mode 0600, tenant; a discarded sign-in, no usable password)
same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
=== production build rc=0 at 2026-11-14T15:36:46Z
$ sha256sum "$OUT/odile-tenant-sealed.raw"
05177404cd7596fd590a54ef605b2a0239d9326f8aa32ba78a1d7bb856e03817  /var/tmp/odile-odl521-prod/odile-tenant-sealed.raw
```

The build ran at `git rev-parse HEAD` = `cf05e31f0309b6d118b6618b1ce0dfeafc9a662c`. The commit that adds
this section changes only `docs/runbooks/image.md`, so the installer, the
release documents and the image bytes measured below are the ones that head
built.

The build also places the image's own pin inside the sealed root
(`place-pin.sh`, after `uki.sh`): `/verify`'s access report and the release
setup read the measurement from `/etc/odile/tenant-image.pin`, and without it
the first boot says `record skipped` and `/verify` has no 96-hex value to
print. One line of this head is a main-regression fix rather than the
sign-in rule: `deploy/image/odile-manifest.service` names `User=odile-manifest-svc`
beside `DynamicUser=yes`, because main's meter removal renamed its
`SupplementaryGroups` to `odile-manifest` — the same name systemd allocates
for the unit — and the first boot of the merged image stopped at
`Failed to update dynamic user credentials: User or group with specified name
already exists`. Without that line no boot reaches the sign-in evidence below.

The image embeds the release pair this tree cuts: installer `10a38b68681ff62f67c799626f52154a801df53e0a4d54cec8055c8d56f1e6df`
(617829 bytes), manifest `daa8340f566ca645bf8e2845e6a29ffcee6d5a0c6074592ed71b699dc075978e`, skills
`09be3cd1bd0556a9a8e9da0adbfd9c50dbf09607418f6a78fc7c97f3a3450722` (release `2026.11.14-5`). A re-cut moves the installer bytes
and the image with them, so every value in this section is measured from the
build above, not copied from an earlier cut.

The image's `.env`, read the way its boot reads it — the bootstrap key from
the boot partition, `cryptsetup open --readonly`, `mount -o ro,noload`:

```sh
$ losetup -fP --show -r "$OUT/odile-tenant-sealed.raw"
/dev/loop85
$ mount -o ro,noload /dev/loop85p2 /mnt/odl521boot
$ install -m600 /mnt/odl521boot/odile/luks-bootstrap.key /tmp/v.key
$ umount /mnt/odl521boot
$ cryptsetup open --readonly --key-file /tmp/v.key /dev/loop85p3 odl521
$ mount -o ro,noload /dev/mapper/odl521 /mnt/odl521root
$ grep -n 'odile-signin-placeholder\|^HERMES_DASHBOARD_BASIC_AUTH_' \
      /mnt/odl521root/home/tenant/.hermes/.env
4:# odile-signin-placeholder: 1
5:HERMES_DASHBOARD_BASIC_AUTH_USERNAME=tenant
6:HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$16384$8$1$2hHZ31uuLshJHEFo6z14Zw==$QtCbhsRcenhUwYaGKYMWx4HZHFbJ2dhJkBRX0r4+a6M=
7:HERMES_DASHBOARD_BASIC_AUTH_SECRET=<redacted>
$ sha256sum /mnt/odl521root/home/tenant/.hermes/.env
1d411e1b3d76d47c297d94d4538e4418a749818bf1f8da0dade365a068bc55b8  /mnt/odl521root/home/tenant/.hermes/.env
$ awk -F= '/^HERMES_DASHBOARD_BASIC_AUTH_SECRET=/{print $2}' \
      /mnt/odl521root/home/tenant/.hermes/.env | tr -d '\n' | sha256sum
6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38  -
$ ls /mnt/odl521root/var/lib/odile-bench/password
ls: cannot access '/mnt/odl521root/var/lib/odile-bench/password': No such file or directory
$ ls /mnt/odl521root/etc/odile/production
/mnt/odl521root/etc/odile/production
```

Does the hash accept a password? The scrypt parameters are in the value, so
the check needs nothing but the file:

```sh
$ /usr/bin/python3 - /mnt/odl521root/home/tenant/.hermes/.env <<'PY'
import base64, hashlib, sys
value = ""
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH="):
        value = line.rstrip("\n").split("=", 1)[1]
algorithm, n, r, p, salt, expected = value.split("$")
def accepts(password):
    derived = hashlib.scrypt(password.encode(), n=int(n), r=int(r), p=int(p),
                             salt=base64.b64decode(salt),
                             dklen=len(base64.b64decode(expected)), maxmem=0)
    return derived == base64.b64decode(expected)
print("hash prefix    ", value[:32] + "…")
print("is scrypt      ", algorithm == "scrypt")
print("accepts bench  ", accepts("odile-bench-not-a-secret"))
print("accepts owner  ", accepts("the-owner-password-2026-odl521"))
PY
hash prefix     scrypt$16384$8$1$2hHZ31uuLshJHEF…
is scrypt       True
accepts bench   False
accepts owner   False
```

The bench build of the same head keeps the fixture (a bench build has no
`/etc/odile/production`). Its LUKS key is the sealed blob, opened with the
tree's bench stand-in and the boot partition's measured command line:

```sh
$ sha256sum /var/tmp/odile-odl521-bench/odile-tenant-sealed.raw
f68c4d26078a103aa81c19862be7e3c27e7c0b65300f89f3edfd67cb28648d5a  /var/tmp/odile-odl521-bench/odile-tenant-sealed.raw
$ losetup -fP --show -r /var/tmp/odile-odl521-bench/odile-tenant-sealed.raw
/dev/loop85
$ mount -o ro,noload /dev/loop85p2 /mnt/odl521boot
$ /var/tmp/odile-odl521-bench/bench-snpreport --unseal \
      /mnt/odl521boot/odile/luks-vol.key.sealed /tmp/v.key \
      --cmdline-file /var/tmp/odile-odl521-bench/boot/cmdline
$ umount /mnt/odl521boot
$ cryptsetup open --readonly --key-file /tmp/v.key /dev/loop85p3 odl521b
$ mount -o ro,noload /dev/mapper/odl521b /mnt/odl521root
$ grep -n 'bench-password-sha256\|^HERMES_DASHBOARD_BASIC_AUTH_' \
      /mnt/odl521root/home/tenant/.hermes/.env
4:# bench-password-sha256: 47f758a4ad12daa47780a9d887c02a7a415324d58220856c85fa8d393e4ddaed
5:HERMES_DASHBOARD_BASIC_AUTH_USERNAME=tenant
6:HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$16384$8$1$ZS64PWRHOSoXmyR8DhPNTQ==$/OTtydst/6WmuGZPr8zRxiThttbb+Im1X2QJdTP8NvY=
7:HERMES_DASHBOARD_BASIC_AUTH_SECRET=<redacted>
$ ls /mnt/odl521root/var/lib/odile-bench/password
/mnt/odl521root/var/lib/odile-bench/password
$ ls /mnt/odl521root/etc/odile/production
ls: cannot access '/mnt/odl521root/etc/odile/production': No such file or directory
```

and the same hash check prints `accepts bench True`: the hash is the
documented fixture, the one the bench QEMU smoke signs in with. It never
reaches a tenant, because a tenant's image carries `/etc/odile/production`.
The bench build also keeps the plaintext fixture at
`/var/lib/odile-bench/password`; the production build writes neither that file
nor the fixture hash.

### 13.4 The first boot, before the visit

The image boots under QEMU TCG with the fixture seed service
(`deploy/image/firstboot-fixture.py serve --dir "$FX" --port 18081`) on the
host. The serial console carries one `odile-firstboot:` line:

```sh
$ FINAL=/var/tmp/odile-odl521-final
$ cp --sparse=always "$OUT/odile-tenant-sealed.raw" "$FINAL/A/odile-tenant-sealed.raw"
$ /usr/bin/python3 /var/tmp/odile-odl521-build/boot-probe.py --phase pre \
      --disk "$FINAL/A/odile-tenant-sealed.raw" \
      --uki "$OUT/odile-tenant.efi" --token "$(cat "$FX/token")" --out "$FINAL/bootA"
phase        pre
disk         /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 05177404cd7596fd590a54ef605b2a0239d9326f8aa32ba78a1d7bb856e03817
uki          /var/tmp/odile-odl521-prod/odile-tenant.efi sha256 6a586575d5d2b2dd0f353dbac1ee6b902ad9d72ab179d9fe830903c8d6b3f18a
sealed       PASS bb-sealed-unlock opened root_crypt before cryptroot ran
firstboot    PASS installed odl-bench (instance 45445445-0000-4000-8000-000000000454) · first-visit 200 · record 2026.11.1 pending key SHA256:/JPc7HpmYB6NS3NA6sZW9m4k936IrH+k8aJwDrWErNI (hq first_visit, first_boot -> first_visit) �� 401s
boot         513s wall
probes       in the guest's own loopback:
  gate up -> 302
  GET / -> 302 /login?next=%2F
  GET /login -> 200
  POST auth bench -> 401
  GET first-visit -> 200 form yes bytes 38032
  GET /verify -> 200
  verify row: sign-in hash   PASS  scrypt hash present in /home/tenant/.hermes/.env; no plaintext password
shutdown     guest sync, then SIGTERM (clean)
```

`boot-probe.py` is ODL-521's one-shot harness and is not part of the tree: it
boots the disk from its UKI under OVMF/TCG, waits for the unit's console line,
and runs the HTTP probes **inside the guest** over the serial console — the
image binds its Webapp and first-visit service to 127.0.0.1, which QEMU's
hostfwd cannot reach. Its serial transcript is `$FINAL/bootA/serial.log`; the
phase report is what is pasted here and in §13.5, §13.7 and §13.8.

`GET /` is 302 to the gate, `GET /login` is 200, the bench fixture's sign-in
is 401, and `/first-visit?t=` is the only page that answers 200 with a form.
`/verify` answers 302 to the gate before the visit; after the visit (§13.7) it
prints `sign-in hash PASS`, so the image is a real tenant image, not an open
one.

The same disk, read-only after the boot (the commands of §13.3; the harness
prints the values below with `check-disk.py --disk "$FINAL/A/…"`, which runs
exactly that mount and the hash test above):

```sh
disk            after the first boot /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 7d3789d93ac75fb7e5fd0d0f6f81b04f28e3984676afb4d9f900c14b289cfdb2
key             bootstrap key
.env            home/tenant/.hermes/.env 537 bytes sha256 1d411e1b3d76d47c297d94d4538e4418a749818bf1f8da0dade365a068bc55b8
marker          ['# odile-signin-placeholder: 1']
hash prefix     scrypt$16384$8$1$2hHZ31uuLshJHEF…
hash is scrypt  True
accepts bench   False
accepts owner   False
secret sha256   6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38
state           "signin_set_at": null,
state           "used_at": null,
plaintext file  absent
boot marker     present
firstboot log   same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
```

The `.env` sha256 and the secret sha256 are the build's: the first boot
printed `same` for the file and wrote nothing.

### 13.5 A second boot prints `same`

The same disk, booted again:

```sh
$ /usr/bin/python3 /var/tmp/odile-odl521-build/boot-probe.py --phase pre2 \
      --disk "$FINAL/A/odile-tenant-sealed.raw" \
      --uki "$OUT/odile-tenant.efi" --token "$(cat "$FX/token")" --out "$FINAL/bootA2"
phase        pre2
disk         /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 7d3789d93ac75fb7e5fd0d0f6f81b04f28e3984676afb4d9f900c14b289cfdb2
uki          /var/tmp/odile-odl521-prod/odile-tenant.efi sha256 6a586575d5d2b2dd0f353dbac1ee6b902ad9d72ab179d9fe830903c8d6b3f18a
sealed       PASS bb-sealed-unlock opened root_crypt before cryptroot ran
firstboot    PASS same
boot         116s wall
probes       in the guest's own loopback:
  gate up -> 302
  GET / -> 302 /login?next=%2F
  GET /login -> 200
  POST auth bench -> 401
  GET first-visit -> 200 form yes bytes 38032
  GET /verify -> 200
  verify row: sign-in hash   PASS  scrypt hash present in /home/tenant/.hermes/.env; no plaintext password
shutdown     guest sync, then SIGTERM (clean)
```

`odile-firstboot: same` is the console contract of a converged machine. The
installer's own log on the disk records the same line for the `.env` (the
`firstboot log` row of the read-only block below), and the check shows the
same `.env` sha256 as §13.4:

```sh
disk            after the second boot /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 fcff92151279a58426d97cfa4d571abeca09931fbe4fa0de12908cb882148a52
key             bootstrap key
.env            home/tenant/.hermes/.env 537 bytes sha256 1d411e1b3d76d47c297d94d4538e4418a749818bf1f8da0dade365a068bc55b8
marker          ['# odile-signin-placeholder: 1']
hash prefix     scrypt$16384$8$1$2hHZ31uuLshJHEF…
hash is scrypt  True
accepts bench   False
accepts owner   False
secret sha256   6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38
state           "signin_set_at": null,
state           "used_at": null,
plaintext file  absent
boot marker     present
firstboot log   same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
```

### 13.6 The installer rewrites every run before the visit

The shipped installer, in its `--root` scratch mode against the image's own
root mounted read-write, twice, before the visit:

```sh
$ ./installer/odile_install.py --root /mnt/odl521root --user tenant --first-visit \
      --first-visit-token "$(cat "$FX/token")" --token "$(cat "$FX/token")" \
      --instance-id 45445445-0000-4000-8000-000000000454 \
      --signature ./installer/odile_install.py.sig \
      --pubkey ./installer/odile-signing-key.pub \
      --slug odl-bench --port 9130 --public-url https://odl-bench.odile.fyi --json
env before       1d411e1b3d76d47c297d94d4538e4418a749818bf1f8da0dade365a068bc55b8
state            "signin_set_at": null
fixture          /mnt/odl521root/etc/odile/smoke.ok (a recorded smoke; the paid smoke is skipped)
=== shipped installer --first-visit: run 1 (before the visit)
write        /mnt/odl521root/etc/odile/key.env (the only inference credential; root-only)
rewrite      /mnt/odl521root/home/tenant/.hermes/.env (the sign-in hash is a placeholder until first visit)
write        the sign-in hash is a placeholder until first visit
env after run 1  c557b8e2aaca9ee01fe674a1eb7978fad3f8e0cd2117cc88c86a70e7a3a35b2b
=== shipped installer --first-visit: run 2 (before the visit)
same         /mnt/odl521root/etc/odile/key.env
rewrite      /mnt/odl521root/home/tenant/.hermes/.env (the sign-in hash is a placeholder until first visit)
write        the sign-in hash is a placeholder until first visit
env after run 2  cfa4e82079e8e397da153e5d1063ad48c86f1449ebe81b27735579185e3501eb
```

Two different hashes, both of a password nobody knows: the allowlist was
already valid on the second run, and the rule rewrote it anyway. That is the
exploit's second half dead — an image hash of a public password cannot survive
a run of the installer, let alone a boot. The read-only check after the two
runs shows the last placeholder and an unmoved secret:

`--root` runs the installer in scratch mode: on a real tenant the Webapp and
the first-visit service are already up, and the mounted guest is not running
them. The harness gives the installer the two answers it asks of a converged
machine — a gated stand-in on 127.0.0.1:9130 (`GET /` 302, `GET /api/sessions`
401) and `/health` on 8091 — and writes `/etc/odile/smoke.ok` so the paid chat
smoke is one the machine already recorded, not one this run repeats. The `.env`
path under test runs before both, and the two run hashes below are its output.

```sh
disk            after the installer runs /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 f6208a7cb132e0814be9ac64608a4d60ff15b3e0e2cf85984409e4b43d4d8bed
key             bootstrap key
.env            home/tenant/.hermes/.env 858 bytes sha256 cfa4e82079e8e397da153e5d1063ad48c86f1449ebe81b27735579185e3501eb
marker          (none)
hash prefix     scrypt$16384$8$1$2d6Q+rQKCWmOVGN…
hash is scrypt  True
accepts bench   False
accepts owner   False
secret sha256   6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38
state           "signin_set_at": null,
state           "used_at": null,
plaintext file  absent
boot marker     present
firstboot log   same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
```

### 13.7 The visit

`POST /first-visit` with the owner's password sets the sign-in, and the page
records it in the state the installer reads:

The second disk is a fresh copy of the built image, so its first boot fetches
the same one-time seed token the first disk's boot consumed; the fixture
re-arms that token (same 24-hour binding, same single-use rule) before this
boot:

```sh
$ cp --sparse=always "$OUT/odile-tenant-sealed.raw" "$FINAL/B/odile-tenant-sealed.raw"
$ ./deploy/image/firstboot-fixture.py prepare --dir "$FX" \
      --instance 45445445-0000-4000-8000-000000000454 --token-file "$FX/token"
$ pkill -f '^/usr/bin/python3 .*firstboot-fixture.py serve --dir $FX ' ; \
  nohup ./deploy/image/firstboot-fixture.py serve --dir "$FX" --port 18081 &  # restart on the re-armed DB
$ /usr/bin/python3 /var/tmp/odile-odl521-build/boot-probe.py --phase visit \
      --disk "$FINAL/B/odile-tenant-sealed.raw" \
      --uki "$OUT/odile-tenant.efi" --token "$(cat "$FX/token")" --out "$FINAL/bootB"
phase        visit
disk         /var/tmp/odile-odl521-final/B/odile-tenant-sealed.raw sha256 05177404cd7596fd590a54ef605b2a0239d9326f8aa32ba78a1d7bb856e03817
uki          /var/tmp/odile-odl521-prod/odile-tenant.efi sha256 6a586575d5d2b2dd0f353dbac1ee6b902ad9d72ab179d9fe830903c8d6b3f18a
sealed       PASS bb-sealed-unlock opened root_crypt before cryptroot ran
firstboot    PASS installed odl-bench (instance 45445445-0000-4000-8000-000000000454) �� first-visit 200 · record 2026.11.1 pending key SHA256:/JPc7HpmYB6NS3NA6sZW9m4k936IrH+k8aJwDrWErNI (hq first_visit) · 470s
boot         593s wall
probes       in the guest's own loopback:
  gate up -> 302
  POST first-visit -> 200 bytes 37893 done yes
  POST auth owner -> 200 cookie yes
  GET / with cookie -> 302 bytes 0
  GET /verify -> 200
  verify row: sign-in hash   PASS  scrypt hash present in /home/tenant/.hermes/.env; no plaintext password
shutdown     guest sync, then SIGTERM (clean)
```

The same page wrote the state the installer reads; the read-only check below
(§13.3's commands, on `$FINAL/B/odile-tenant-sealed.raw`) prints it. After it,
the owner's sign-in is 200, `/` is 200, and `/verify` still passes, and the
`.env` now carries the owner's hash while the session secret has not moved.
The `# odile-signin-placeholder: 1` line is a comment the page leaves in
place; the hash beside it is the owner's, which is what the check proves:

```sh
disk            after the visit /var/tmp/odile-odl521-final/B/odile-tenant-sealed.raw sha256 a9f61e776e5153302b77c04a32b0dbfdbc68142f57ae7459bc0b2364284f086a
key             bootstrap key
.env            home/tenant/.hermes/.env 537 bytes sha256 0f10559448e673c435657173e0edaca094df2af4fdd48fff0cdc4f18b3bed434
marker          ['# odile-signin-placeholder: 1']
hash prefix     scrypt$16384$8$1$kkmI96h3Ugl8uAE…
hash is scrypt  True
accepts bench   False
accepts owner   True
secret sha256   6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38
state           "signin_set_at": "2026-11-14T16:28:41Z",
state           "used_at": "2026-11-14T16:28:41Z",
plaintext file  absent
boot marker     present
firstboot log   same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
```

### 13.8 The owner's hash survives a reboot

```sh
$ /usr/bin/python3 /var/tmp/odile-odl521-build/boot-probe.py --phase reboot \
      --disk "$FINAL/B/odile-tenant-sealed.raw" \
      --uki "$OUT/odile-tenant.efi" --token "$(cat "$FX/token")" --out "$FINAL/bootC"
phase        reboot
disk         /var/tmp/odile-odl521-final/B/odile-tenant-sealed.raw sha256 a9f61e776e5153302b77c04a32b0dbfdbc68142f57ae7459bc0b2364284f086a
uki          /var/tmp/odile-odl521-prod/odile-tenant.efi sha256 6a586575d5d2b2dd0f353dbac1ee6b902ad9d72ab179d9fe830903c8d6b3f18a
sealed       PASS bb-sealed-unlock opened root_crypt before cryptroot ran
firstboot    PASS same
boot         102s wall
probes       in the guest's own loopback:
  gate up -> 302
  POST auth owner -> 200 cookie yes
  GET / with cookie -> 302 bytes 0
  GET /verify -> 200
  verify row: sign-in hash   PASS  scrypt hash present in /home/tenant/.hermes/.env; no plaintext password
shutdown     guest sync, then SIGTERM (clean)
```

and the installer, run again after the visit, keeps the owner's hash byte for
byte. The post-visit installer runs last, after the reboot, because in
`--root` scratch mode it renders the units with the host's mount path
(`/mnt/odl521root/...`); a boot after it would fail its mount namespace. The
reboot is taken on the clean units the first boot installed.

```sh
env before       0f10559448e673c435657173e0edaca094df2af4fdd48fff0cdc4f18b3bed434
state            "signin_set_at": "2026-11-14T16:28:41Z"
fixture          /mnt/odl521root/etc/odile/smoke.ok (a recorded smoke; the paid smoke is skipped)
=== shipped installer --first-visit: run 1 (after the visit)
write        /mnt/odl521root/etc/odile/key.env (the only inference credential; root-only)
same         /mnt/odl521root/home/tenant/.hermes/.env (mode 0600, 3 allowlisted keys, allowlist ok)
env after run 1  0f10559448e673c435657173e0edaca094df2af4fdd48fff0cdc4f18b3bed434
=== shipped installer --first-visit: run 2 (after the visit)
same         /mnt/odl521root/etc/odile/key.env
same         /mnt/odl521root/home/tenant/.hermes/.env (mode 0600, 3 allowlisted keys, allowlist ok)
env after run 2  0f10559448e673c435657173e0edaca094df2af4fdd48fff0cdc4f18b3bed434
```

The read-only check after the reboot shows the same `.env` sha256 as after the
visit, and `signin_set_at` still names the visit:

```sh
disk            after the reboot /var/tmp/odile-odl521-final/B/odile-tenant-sealed.raw sha256 50a503b8403f3ef5f2a04a37375a64b9d9d856270492d81da42c0d903f0e0729
key             bootstrap key
.env            home/tenant/.hermes/.env 537 bytes sha256 0f10559448e673c435657173e0edaca094df2af4fdd48fff0cdc4f18b3bed434
marker          ['# odile-signin-placeholder: 1']
hash prefix     scrypt$16384$8$1$kkmI96h3Ugl8uAE…
hash is scrypt  True
accepts bench   False
accepts owner   True
secret sha256   6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38
state           "signin_set_at": "2026-11-14T16:28:41Z",
state           "used_at": "2026-11-14T16:28:41Z",
plaintext file  absent
boot marker     present
firstboot log   same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
```

### 13.9 The session secret is ODL-522's

`HERMES_DASHBOARD_BASIC_AUTH_SECRET` is byte-identical in the built image
(`6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38`), after the first boot (`6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38`) and after the
visit (`6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38`); the sha256 is of the value, not of the line:

```sh
$ awk -F= '/^HERMES_DASHBOARD_BASIC_AUTH_SECRET=/{print $2}' <each .env> \
      | tr -d '\n' | sha256sum
6b5b57499b538890057c7ee221272b833e413cde412da6829efe9aafe50ede38  -
```

Every image built from this tree therefore shares one session secret, which is
the defect ODL-522 closes: that card makes the secret per-machine and carries
its own evidence. This card's rule is the *hash*: the secret does not move
when the hash is replaced, so the owner's sessions survive the first-visit
write.

### 13.10 What only a Blackbox can prove

The QEMU bench has no `/dev/kvm` and derives the seal from the command line,
not from `SNP_GET_DERIVED_KEY`. It cannot prove: that a real firmware extends
the measured command line this image expects; that the guest's own re-key
against the chip-derived seal leaves the disk openable; that the panel uploads
these bytes; or that a real first boot over the tenant's certificate answers
`/first-visit` on the tenant's own name. Those are ODL-454's and ODL-466's
arguments and the strict run's; this section proves what the *image* carries
and what the *boot* does with it.
