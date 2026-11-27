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

**The mirror is not a free choice.** `sealed-image.sh` debootstraps the tenant
root from the Debian snapshot the release installer pins —
`https://snapshot.debian.org/archive/debian/<OS_SNAPSHOT>`, the date in
`installer/odile_install.py` — and that is its default `--mirror`. Do not build
a tenant disk from `deb.debian.org`: it moves with every point release, while
the first boot's release installer installs the nine `OS_PACKAGE_PINS` from the
snapshot and refuses any other version. A disk from the moved archive therefore
ends the first boot at `=== rc=1` (measured 2026-11-21, t_30da4de3; §5 has the
failure and the converging rebuild). The two dates are one date: the script
reads `OS_SNAPSHOT` out of the installer, so bumping the pin moves the build
mirror with it, and `--mirror` exists only for a deliberate override — the run
prints a warning line when the mirror is not the pinned snapshot.

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

The disk command passes no `--mirror`: its default is the snapshot the release
installer pins (§2), the only archive whose packages match `OS_PACKAGE_PINS`.
The plan says which mirror it will use without building anything —
`./deploy/image/sealed-image.sh --dry-run` prints a `mirror` line, and `--help`
prints the resolved default.

**A production image's pin is placed by the command that writes it (ODL-947).**
For `sealed-image.sh --production`, the UKI step is the pin step:
`sudo ./deploy/image/uki.sh --in OUT --sign --pin OUT/tenant-image.pin` writes
the pin and then opens the sealed disk and installs it at
`/etc/odile/tenant-image.pin` — there is no fifth command to remember. `uki.sh`
refuses a production image with no `--pin` (exit 2, naming the `record skipped`
boot it would otherwise produce), refuses `--no-place-pin` on one, and needs
root because the disk is opened with its bootstrap key. A bench disk has no
bootstrap key and its own build (`firstboot-bench.sh build`) places the pin with
the bench stand-in, so §3's command above still needs neither sudo nor the
placement.

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

**The root is debootstrapped from the pinned snapshot, and the first boot
proves the set matches.** `debootstrap` fills `sda3` from
`https://snapshot.debian.org/archive/debian/<OS_SNAPSHOT>` — the date
`installer/odile_install.py` pins — and the release installer then installs its
nine `OS_PACKAGE_PINS` from that same snapshot, refusing any other version
rather than falling back. A disk built from `deb.debian.org` carries whatever
the live archive held that day; when the archive takes a point release between
builds, the two sets diverge and the first boot ends at the OS pins. Measured
2026-11-21 on the same `2026.11.21-2` tree (t_30da4de3), whose default mirror
was still `deb.debian.org`:

```
install      OS packages (9 pinned to Debian 13 trixie, snapshot 20261108T144109Z)
             converging curl 8.14.1-2+deb13u7 -> 8.14.1-2+deb13u5
             converging openssl 3.5.9-1+deb13u1 -> 3.5.7-1~deb13u2
error: the pinned OS packages are not in the snapshot 20261108T144109Z:
       ... E: Unable to correct problems, you have held broken packages.
       refusing to fall back to the latest version
=== rc=1
```

The same build from the snapshot converges, and that is now the default — no
flag is needed:

```sh
sudo ./deploy/image/sealed-image.sh --out … --production --cmdline-file … --force
```

The run names the archive it will use before it debootstraps; `--mirror URL`
overrides the default and prints the not-the-snapshot warning:

```
mirror       https://snapshot.debian.org/archive/debian/20261108T144109Z (the release installer's snapshot)
```

The disk then reads curl `8.14.1-2+deb13u5`, libcurl4t64 `8.14.1-2+deb13u5`,
openssl `3.5.7-1~deb13u2`, libssl3t64 `3.5.7-1~deb13u2` and git
`1:2.47.3-0+deb13u1`, and the installer's package step prints
`same OS packages (9 pinned to Debian 13 trixie, snapshot 20261108T144109Z)`.
Read it back from a built disk before publishing — mount `sda2`, open `sda3`
with `/odile/luks-bootstrap.key` (the production image's first-boot key), mount
the mapper at `$ROOT`, and query inside it:

```sh
$ sudo chroot "$ROOT" dpkg-query -W -f='${Package} ${Version}\n' \
    curl libcurl4t64 openssl libssl3t64 git
curl 8.14.1-2+deb13u5
libcurl4t64 8.14.1-2+deb13u5
openssl 3.5.7-1~deb13u2
libssl3t64 3.5.7-1~deb13u2
git 1:2.47.3-0+deb13u1
```

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
the root is trustworthy; a prefix of the volume may still mount and read clean,
but the tail is a mix of the two keys, so only the finished pass is a normal
boot.

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
before the `already provisioned` path finalizes. A stop at or after the data
pass is therefore recoverable on the next boot; a stop inside the pass costs the
remaining pass — finish it from a rescue shell if the guest does not come up —
never the machine.

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
   The first-boot tokens ride the same line: `odile.token=` (the setup
   token), `odile.seed_url=` (the seed endpoint, whose **host names the
   environment** — the boot record posts there and the installer's heartbeat
   receiver and default site name derive from it), and `odile.public_url=`
   (the name this tenant serves, when it is not `<bb.host>.<seed host>`; a
   moved name, or an environment whose tenant names are not shaped that way).
   `odile.public_url=` is what a bench tenant needs instead of a systemd
   drop-in, a second Caddy block and a `public_url` patch: the release setup
   passes it to the installer as `--public-url`, and the installer serves that
   one name for Caddy, the DNS and ACME waits and the certificate the recovery
   file pins (ODL-340 follow-up, §12.4 of `install.md`).
   Measured on the bench, 2026-11-19: a `--production` disk built from the
   `release/` of the `2026.11.19-3` cut (installer `00a84542…`, manifest
   `49850448…`; the tree now cuts `2026.11.25-4`, §12.6 of `install.md`) with
   the line above was written over the disposable tenant
   by the panel and booted. The installer logged `site
   odl-340a1e.test.odile.fyi (heartbeat https://test.odile.fyi/heartbeat)`, and
   the Caddy site, the DNS wait, the ACME wait (its own Let's Encrypt
   certificate, `sha256:70fff718…`) and `certificate_fingerprint` all used that
   one name; the setup ended `=== rc=0` with `installer_sha256 00a84542…`.
   `odile-firstboot` posted the boot record to the bench and the bench's spine
   answered `first_boot -> first_visit` (`record_source=first_boot`,
   13:20:10Z). The disk carried no `test-site.caddy`, no
   `odile-test-site.service`, no `odile-release-setup.service.d/` and no
   Caddyfile, and its tenant `config.yaml` still named the build's
   `http://tenant.odile.test:9130`; `check_bench.sh` passed all 16 checks
   against the new measurement `674cc40d…`. Evidence:
   `docs/evidence/a1-bench-followup.md` §9–§10.
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
   an offline disk, which is the alpha.0 threat model.
7. **The console log, off once the transcript is saved.** Reading the boot is
   what turning the log on is for, and the panel is explicit about what the
   switch costs: *the hypervisor keeps a transcript of everything this machine
   prints to its serial console … It is a plaintext file on the host: switch it
   off when you are done debugging and the file is emptied.*
   `tools/servury_cycle.py` (and `make uki-cycle` through it) arms the log,
   captures one boot, and turns the log off again on every exit path once the
   transcript is written — including a `--wait-for` timeout; `--keep-console`
   is the only way to leave it on. The switch lands a moment after the POST, and
   a guest that is still booting flushes its last lines into the file seconds
   later — measured twice in a row on the bench on 2026-11-19: the same 55-byte
   getty banner arrived 3–8 s after the off, *after* an immediate read-back had
   seen nothing. So the disarm does not take one read as proof: it watches the
   host's copy for up to 15 s, switches the log off again whenever a tail
   arrives, and prints "emptied" only after a read that succeeded and returned
   nothing — otherwise it says how many bytes are still there, without changing
   the cycle's exit code. Measured 2026-11-19: the cycle had armed the
   log and never disarmed it, and the tenant box carried **209 KB** of its own
   boot (kernel and systemd lines, hostname, instance id, file sha256s and an
   8-hex prefix of the first-visit token) until a human switched it off. After a
   cycle, `./tools/servury_panel.py --console --server <id> --action read`
   prints nothing and the console page reads **Console log · Off**. The
   measurement and the transcript are in
   `docs/ops/evidence/console-log-2026-11-19.md`.

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

## 13. The published image ships no usable sign-in, no shared session secret and no SSH host private key (ODL-521, ODL-522, ODL-523)

### 13.1 The rule

A published image carries no password a stranger can use.
`/home/tenant/.hermes/.env` holds a scrypt hash of a password generated in the
build and thrown away, with the marker that says so; the first boot replaces
that hash with another discarded one **or, when it carries a seed, with the
seed's verifier — the password the owner chose at registration** (B-once,
ODL-925) — and the session secret with this machine's own; the release
installer, when it runs seeded under `--first-visit`, writes that same seeded
verifier and keeps it, so a hash a person can use has a writer at provision and
the first visit is a **login**, not a setup. In the classic flow (no seed) the
first-visit page remains the only writer of a usable hash. The session-signing
secret
(`HERMES_DASHBOARD_BASIC_AUTH_SECRET`) is not that rule: the build has to write
one, so every disk built from one image would sign its cookies with the same
value. ODL-522 makes it the machine's: the first boot replaces it with a secret
generated on that machine, and every later write (the installer's, the page's)
keeps it byte for byte. §13.9 records that card.

Before this card the image carried the scrypt hash of the bench fixture
`odile-bench-not-a-secret`. `install-stack.sh --seed` kept it on the first
boot and `ensure_env(first_visit=True)` kept any existing hash, so a workspace
was open with a public password from the page's first 200 until the owner's
first visit (ODL-466 §0.2, finding 1; on the live tenant the window was
14:44:45Z–14:57:46Z and the URL is in Certificate Transparency logs).

### 13.2 The mechanism

Four writers, in order, and one state key.

1. **The build.** `sealed-image.sh --production` writes `/etc/odile/production`
   before `install-stack.sh` runs. The production branch writes
   `# odile-signin-placeholder: 1`, a scrypt hash of a password generated and
   discarded, and a fresh session secret; the file gets mode 0600 and three
   keys. The plaintext password file (`/var/lib/odile-bench/password`) is a
   bench artefact and is not written at all. A bench build (no
   `/etc/odile/production`) keeps the documented fixture password its QEMU
   smoke signs in with, under `# bench-password-sha256:`.
2. **The first boot.** `odile-firstboot.py` writes
   `/etc/odile/first-visit.json` with `signin_set_at` **null** — at that point
   the `.env` is still the image's placeholder, and the key is a fact about the
   `.env`, not about the seed — and then runs `install-stack.sh --seed`. The
   production branch sees a scrypt hash that rejects the fixture and, when the
   run carries a seed with `sign_in_hash` (every ODL-341 provisioned seed
   does), writes this machine's own session secret and **the seed's verifier**
   (the registration password), and only then stamps `signin_set_at` in the
   state it finds. The branch does not trust that stamp on its own: it writes
   the verifier whenever the `.env` does not already hold it, and keeps a hash
   the owner's own visit recorded (`used_at`/`first_login_at`), so a manual
   re-run cannot revert a reset. Once the `.env` holds the verifier it prints
   `same … (the seed's verifier; this machine's own secret)` and the file stays
   byte for byte. Before ODL-947 the state was stamped on the seed's mere
   presence, before any writer had run, so the branch kept the image's
   placeholder and the first visit served a login nobody could pass (release
   `2026.11.23-5`; the cold walk's gate blocker). Only a classic no-seed run
   writes another discarded hash (ODL-522, §13.9). A file with no scrypt hash
   at all (the vendor bootstrap leaves its own `.env.example` copy) or one that
   *does* accept the fixture is replaced either way. §13.4 and §13.5 below are
   the pre-change records, at their recorded heads.
3. **Every `--first-visit` run of the release installer.** `ensure_env` reads
   `/etc/odile/first-visit.json`; it writes the hash the seed carries when the
   run is seeded (ODL-925) and the `.env` does not already hold that verifier —
   or when `signin_set_at` is absent, as before — and a discarded one on a
   classic run, even when the allowlist looks valid, and keeps the session
   secret. It does not trust the stamp alone: a state stamped by the pre-ODL-947
   order can sit over the image's placeholder, so the seed's verifier being
   absent from the file is what triggers the write. A hash the owner's own visit
   recorded (`used_at`/`first_login_at`, a page setup or a reset) is kept. After
   `signin_set_at` is honest it prints
   `same … (mode 0600, 3 allowlisted keys, allowlist ok)` and keeps the owner's
   hash.
4. **The page.** In the seeded flow `POST /first-visit` **signs the owner in**:
   it verifies the registration password against the `.env` hash, writes
   `first_login_at` and `signin_set_at` in one state write, and shows the
   recovery code once. On a classic machine it writes the owner's scrypt hash
   and `signin_set_at`, and nothing else. Either way it replaces the hash line
   only: the machine's session secret is kept byte for byte, and a `.env` with
   no `HERMES_DASHBOARD_BASIC_AUTH_SECRET` is refused (500, token unspent)
   rather than written into a password-only file (ODL-522).

### 13.3 The build: a discarded hash and the marker

The production build, at head `1629b14ed71888bc9c398ac47b341dcba606452d`, with the fixture seed the boot will
use (`prepare` prints the one-time token once and stores it 0600):

```sh
$ OUT=/var/tmp/odile-odl521-prod
$ FX=/var/tmp/odile-odl521-boot
$ ./deploy/image/firstboot-fixture.py prepare --dir "$FX" \
      --instance 45445445-0000-4000-8000-000000000454
tenant     odl-bench
instance   45445445-0000-4000-8000-000000000454
expires    2026-11-15T17:47:49Z
token      f635b2fa63169bbac54cb9e445f347e69d69f5f70d1fece35f5e4bec44b35851 (minted)
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
pin raw_image_sha256 -> a17894cf0da9 (as built, pin inside)
release      installer ae2c839dabe4…, manifest 4caad797a75c…, listener manifest.py, unit odile-manifest.service
write        /home/tenant/.hermes/.env (mode 0600, tenant; a discarded sign-in, no usable password)
same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
=== production build rc=0 at 2026-11-14T17:54:47Z
$ sha256sum "$OUT/odile-tenant-sealed.raw"
a17894cf0da9f083e5375156f48814098512e34cebf492c03677078747c9a278  /var/tmp/odile-odl521-prod/odile-tenant-sealed.raw
```

The build ran at `git rev-parse HEAD` = `1629b14ed71888bc9c398ac47b341dcba606452d`. The commit that adds
this section changes only `docs/runbooks/image.md`, so the installer, the
release documents and the image bytes measured below are the ones that head
built. `main` then added `055709d` (one `docs/runbooks/demo-public.md` row) and
the branch merged it at `4ca0e01`. The review rounds after that tree moved only
the two runbooks and `tests/test_sealed_image.py` (round 2 makes six
HQ-only-fixture tests skip in a public checkout; `deploy/image/sealed-initrd.sh`
is byte-identical to the head's). At the last code head
`bf75c93eaa96c79bf1ae2c286c23cb7d807201d7` the diff below prints nothing, so
the handoff tree's build inputs are still the head's:

```sh
$ git diff --stat 1629b14..bf75c93 -- deploy installer release skills update \
      scripts web/first-visit.html web/verify.html attest.py
$ # (no output: outside those inputs the branch moves only docs/runbooks/*.md
  and tests/test_sealed_image.py)
```

The build also places the image's own pin inside the sealed root
(`place-pin.sh`, after `uki.sh`): `/verify`'s access report and the release
setup read the measurement from `/etc/odile/tenant-image.pin`, and without it
the first boot says `record skipped` and `/verify` has no 96-hex value to
print. **Measured at that head.** Since ODL-947 the UKI step is the pin step:
`uki.sh --in OUT --sign --pin OUT/tenant-image.pin` on a
`sealed-image.sh --production` disk writes the pin and places it itself, so the
`place-pin.sh` line is a same-bytes no-op and the production recipe has three
commands, not four. The transcripts below are the historical runs; running
their `place-pin.sh` line again prints `pin raw_image_sha256 -> …` and changes
nothing. One line of this head is a main-regression fix rather than the
sign-in rule: `deploy/image/odile-manifest.service` names `User=odile-manifest-svc`
beside `DynamicUser=yes`, because main's meter removal renamed its
`SupplementaryGroups` to `odile-manifest` — the same name systemd allocates
for the unit — and the first boot of the merged image stopped at
`Failed to update dynamic user credentials: User or group with specified name
already exists`. Without that line no boot reaches the sign-in evidence below.

The image embeds the release pair this tree cuts: installer `ae2c839dabe457a1ec17674a0f13290ab81c2b99735c2e6f1798193f6e148244`
(617862 bytes), manifest `4caad797a75c3585fba914f5f115451bbe8257f8370740f972dbac5e025b069b`, skills
`09be3cd1bd0556a9a8e9da0adbfd9c50dbf09607418f6a78fc7c97f3a3450722` (release `2026.11.14-7`). A re-cut moves the installer bytes
and the image with them, so every value in this section is measured from the
build above, not copied from an earlier cut.

The image's `.env`, read the way its boot reads it — the bootstrap key from
the boot partition, `cryptsetup open --readonly`, `mount -o ro,noload`:

```sh
$ losetup -fP --show -r "$OUT/odile-tenant-sealed.raw"
/dev/loop86
$ mount -o ro,noload /dev/loop86p2 /mnt/odl521boot
$ install -m600 /mnt/odl521boot/odile/luks-bootstrap.key /tmp/v.key
$ umount /mnt/odl521boot
$ cryptsetup open --readonly --key-file /tmp/v.key /dev/loop86p3 odl521
$ mount -o ro,noload /dev/mapper/odl521 /mnt/odl521root
$ grep -n 'odile-signin-placeholder\|^HERMES_DASHBOARD_BASIC_AUTH_' \
      /mnt/odl521root/home/tenant/.hermes/.env
4:# odile-signin-placeholder: 1
5:HERMES_DASHBOARD_BASIC_AUTH_USERNAME=tenant
6:HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$16384$8$1$Uz+SYj0LUVvRQdKc24UlpA==$<44 base64 chars, the derived key>
7:HERMES_DASHBOARD_BASIC_AUTH_SECRET=<redacted>
$ sha256sum /mnt/odl521root/home/tenant/.hermes/.env
da9af6678a9ad04745540d98f7b39834be822d97de73e87378bfa0a9b5702466  /mnt/odl521root/home/tenant/.hermes/.env
$ awk -F= '/^HERMES_DASHBOARD_BASIC_AUTH_SECRET=/{print $2}' \
      /mnt/odl521root/home/tenant/.hermes/.env | tr -d '\n' | sha256sum
47affc386e765ef8361de64985a215caffbae877042ede1707829a6358f4ce63  -
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
hash prefix     scrypt$16384$8$1$Uz+SYj0LUVvRQdK…
is scrypt       True
accepts bench   False
accepts owner   False
```

The bench build of the same head keeps the fixture (a bench build has no
`/etc/odile/production`). Its LUKS key is the sealed blob, opened with the
tree's bench stand-in and the boot partition's measured command line:

```sh
$ sha256sum /var/tmp/odile-odl521-bench/odile-tenant-sealed.raw
dd2e2bf6d7ef3e82b73a67e730fbd2d223d7ee4f6b5ce179e3e5c0f92a5c367a  /var/tmp/odile-odl521-bench/odile-tenant-sealed.raw
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
6:HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$16384$8$1$mDjFllG9gYppA08l3B0f9Q==$<44 base64 chars, the derived key>
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

These three transcripts (§13.4–§13.8) were measured at the **ODL-521 head**
`1629b14ed71888bc9c398ac47b341dcba606452d`, where the first boot left the
session secret alone. §13.9 measures the ODL-522 head, where the first boot
writes this machine's own secret; the hash rule, the page and the boot flow are
otherwise the same. The image boots under QEMU TCG with the fixture seed service
(`deploy/image/firstboot-fixture.py serve --dir "$FX" --port 18081`) on the
host. The serial console carries one `odile-firstboot:` line:

```sh
$ FINAL=/var/tmp/odile-odl521-final
$ cp --sparse=always "$OUT/odile-tenant-sealed.raw" "$FINAL/A/odile-tenant-sealed.raw"
$ /usr/bin/python3 /var/tmp/odile-odl521-build/boot-probe.py --phase pre \
      --disk "$FINAL/A/odile-tenant-sealed.raw" \
      --uki "$OUT/odile-tenant.efi" --token "$(cat "$FX/token")" --out "$FINAL/bootA"
phase        pre
disk         /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 a17894cf0da9f083e5375156f48814098512e34cebf492c03677078747c9a278
uki          /var/tmp/odile-odl521-prod/odile-tenant.efi sha256 4f41c3145637fa7a2fc1e78653dd02c18cdf24d6d80407fbf2f996a932efa551
sealed       PASS bb-sealed-unlock opened root_crypt before cryptroot ran
firstboot    PASS installed odl-bench (instance 45445445-0000-4000-8000-000000000454) �� first-visit 200 · record 2026.11.1 pending key SHA256:AGm5bfXbyCyXoUlAh59QTE13HHqcl0PYSfIlcgE4sV0 (hq first_visit, first_boot -> first_visit) · 239s
boot         339s wall
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
is 401, and `/first-visit` — the tokenless page — is the only page that answers
200 with a form; a `/first-visit?t=` request (valid, unknown or empty) never
renders the form, and answers 410 `already set` on a machine whose sign-in is
recorded. This is the pre-change record (ODL-521/ODL-522 heads): the
transcripts in §13.4–§13.9 are kept as they were measured, and their page is
the setup page, not B-once's login. ODL-926 re-measures them on the re-cut
image.
`/verify` is world-readable: it answers 200 before the visit as well as after
it (§13.4 and §13.7), and its sign-in row prints `sign-in hash PASS` on a
production image — a scrypt hash in `/home/tenant/.hermes/.env` and no
plaintext password — so the image is a real tenant image, not an open one.

The same disk, read-only after the boot (the commands of §13.3; the harness
prints the values below with `check-disk.py --disk "$FINAL/A/…"`, which runs
exactly that mount and the hash test above):

```sh
disk            after the first boot /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 d717f76d89e83679668e794ee0d305a6d7ecd02404f7b4cdae72993959fb0ae5
key             bootstrap key
.env            home/tenant/.hermes/.env 537 bytes sha256 da9af6678a9ad04745540d98f7b39834be822d97de73e87378bfa0a9b5702466
marker          ['# odile-signin-placeholder: 1']
hash prefix     scrypt$16384$8$1$Uz+SYj0LUVvRQdK…
hash is scrypt  True
accepts bench   False
accepts owner   False
secret sha256   47affc386e765ef8361de64985a215caffbae877042ede1707829a6358f4ce63
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
disk         /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 d717f76d89e83679668e794ee0d305a6d7ecd02404f7b4cdae72993959fb0ae5
uki          /var/tmp/odile-odl521-prod/odile-tenant.efi sha256 4f41c3145637fa7a2fc1e78653dd02c18cdf24d6d80407fbf2f996a932efa551
sealed       PASS bb-sealed-unlock opened root_crypt before cryptroot ran
firstboot    PASS same
boot         113s wall
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
disk            after the second boot /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 0e718cedfa5e604f9dd4fb21cd5bb2efb3d9e3a4b67c47e76e6451a7c680e431
key             bootstrap key
.env            home/tenant/.hermes/.env 537 bytes sha256 da9af6678a9ad04745540d98f7b39834be822d97de73e87378bfa0a9b5702466
marker          ['# odile-signin-placeholder: 1']
hash prefix     scrypt$16384$8$1$Uz+SYj0LUVvRQdK…
hash is scrypt  True
accepts bench   False
accepts owner   False
secret sha256   47affc386e765ef8361de64985a215caffbae877042ede1707829a6358f4ce63
state           "signin_set_at": null,
state           "used_at": null,
plaintext file  absent
boot marker     present
firstboot log   same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
```

### 13.6 The installer writes the seeded verifier before the visit (pre-change transcript)

The shipped installer, in its `--root` scratch mode against the image's own
root mounted read-write, twice, before the visit:

```sh
$ ./installer/odile_install.py --root /mnt/odl521root --user tenant --first-visit \
      --first-visit-token "$(cat "$FX/token")" --token "$(cat "$FX/token")" \
      --instance-id 45445445-0000-4000-8000-000000000454 \
      --signature ./installer/odile_install.py.sig \
      --pubkey ./installer/odile-signing-key.pub \
      --slug odl-bench --port 9130 --public-url https://odl-bench.odile.fyi --json
env before       da9af6678a9ad04745540d98f7b39834be822d97de73e87378bfa0a9b5702466
state            "signin_set_at": null
fixture          /mnt/odl521root/etc/odile/smoke.ok (a recorded smoke; the paid smoke is skipped)
=== shipped installer --first-visit: run 1 (before the visit)
write        /mnt/odl521root/etc/odile/key.env (the only inference credential; root-only)
rewrite      /mnt/odl521root/home/tenant/.hermes/.env (the sign-in hash is a placeholder until first visit)
write        the sign-in hash is a placeholder until first visit
env after run 1  26080b0dc62d3f554343e4047b4716e67bab5f5cf494c8e22774c78198907dc9
=== shipped installer --first-visit: run 2 (before the visit)
same         /mnt/odl521root/etc/odile/key.env
rewrite      /mnt/odl521root/home/tenant/.hermes/.env (the sign-in hash is a placeholder until first visit)
write        the sign-in hash is a placeholder until first visit
env after run 2  0f04dd3326d5cb9f813edd93883f05250bafff745cc00edfbf92b280e8cc684b
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
disk            after the installer runs /var/tmp/odile-odl521-final/A/odile-tenant-sealed.raw sha256 5057b4fd6589b88f86bad4d6483037554921c9aa304fdffcd3e5b1667087c39e
key             bootstrap key
.env            home/tenant/.hermes/.env 858 bytes sha256 0f04dd3326d5cb9f813edd93883f05250bafff745cc00edfbf92b280e8cc684b
marker          (none)
hash prefix     scrypt$16384$8$1$CB5OWGFX3oEwRlL…
hash is scrypt  True
accepts bench   False
accepts owner   False
secret sha256   47affc386e765ef8361de64985a215caffbae877042ede1707829a6358f4ce63
state           "signin_set_at": null,
state           "used_at": null,
plaintext file  absent
boot marker     present
firstboot log   same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
```

### 13.7 The visit

`POST /first-visit` with the password chosen at registration signs the owner
in on a seeded machine (a classic machine sets the sign-in here), and the page
records `first_login_at` and `signin_set_at` in the state the installer reads.
The transcript below is the pre-change record: it measured a classic disk, so
its page sets the sign-in and its installer runs write a placeholder.

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
disk         /var/tmp/odile-odl521-final/B/odile-tenant-sealed.raw sha256 a17894cf0da9f083e5375156f48814098512e34cebf492c03677078747c9a278
uki          /var/tmp/odile-odl521-prod/odile-tenant.efi sha256 4f41c3145637fa7a2fc1e78653dd02c18cdf24d6d80407fbf2f996a932efa551
sealed       PASS bb-sealed-unlock opened root_crypt before cryptroot ran
firstboot    PASS installed odl-bench (instance 45445445-0000-4000-8000-000000000454) · first-visit 200 �� record 2026.11.1 pending key SHA256:AGm5bfXbyCyXoUlAh59QTE13HHqcl0PYSfIlcgE4sV0 (hq first_visit) · 252s
boot         356s wall
probes       in the guest's own loopback:
  gate up -> 302
  POST first-visit -> 200 bytes 37893 done yes
  POST auth owner -> 200 cookie yes
  GET / with cookie -> 200 bytes 3317 -
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
disk            after the visit /var/tmp/odile-odl521-final/B/odile-tenant-sealed.raw sha256 46a5ec2979852a3b4f12f739726f41d68ca67a3ce9dc9132d2e22a46075591c1
key             bootstrap key
.env            home/tenant/.hermes/.env 537 bytes sha256 873f43ab0ff31db63641e5ed3e36663b42f10354139c9df19a259db96ef8b617
marker          ['# odile-signin-placeholder: 1']
hash prefix     scrypt$16384$8$1$2m+EqB1rnfAgnED…
hash is scrypt  True
accepts bench   False
accepts owner   True
secret sha256   47affc386e765ef8361de64985a215caffbae877042ede1707829a6358f4ce63
state           "signin_set_at": "2026-11-14T18:35:07Z",
state           "used_at": "2026-11-14T18:35:07Z",
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
disk         /var/tmp/odile-odl521-final/B/odile-tenant-sealed.raw sha256 46a5ec2979852a3b4f12f739726f41d68ca67a3ce9dc9132d2e22a46075591c1
uki          /var/tmp/odile-odl521-prod/odile-tenant.efi sha256 4f41c3145637fa7a2fc1e78653dd02c18cdf24d6d80407fbf2f996a932efa551
sealed       PASS bb-sealed-unlock opened root_crypt before cryptroot ran
firstboot    PASS same
boot         96s wall
probes       in the guest's own loopback:
  gate up -> 302
  POST auth owner -> 200 cookie yes
  GET / with cookie -> 200 bytes 3317 -
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
env before       873f43ab0ff31db63641e5ed3e36663b42f10354139c9df19a259db96ef8b617
state            "signin_set_at": "2026-11-14T18:35:07Z"
fixture          /mnt/odl521root/etc/odile/smoke.ok (a recorded smoke; the paid smoke is skipped)
=== shipped installer --first-visit: run 1 (after the visit)
write        /mnt/odl521root/etc/odile/key.env (the only inference credential; root-only)
same         /mnt/odl521root/home/tenant/.hermes/.env (mode 0600, 3 allowlisted keys, allowlist ok)
env after run 1  873f43ab0ff31db63641e5ed3e36663b42f10354139c9df19a259db96ef8b617
=== shipped installer --first-visit: run 2 (after the visit)
same         /mnt/odl521root/etc/odile/key.env
same         /mnt/odl521root/home/tenant/.hermes/.env (mode 0600, 3 allowlisted keys, allowlist ok)
env after run 2  873f43ab0ff31db63641e5ed3e36663b42f10354139c9df19a259db96ef8b617
```

The read-only check after the reboot shows the same `.env` sha256 as after the
visit, and `signin_set_at` still names the visit:

```sh
disk            after the reboot /var/tmp/odile-odl521-final/B/odile-tenant-sealed.raw sha256 167e02f7436677c8cd5b5395d59dd9abc15bb3a8c8d5d1ec09f381edba2e857e
key             bootstrap key
.env            home/tenant/.hermes/.env 537 bytes sha256 873f43ab0ff31db63641e5ed3e36663b42f10354139c9df19a259db96ef8b617
marker          ['# odile-signin-placeholder: 1']
hash prefix     scrypt$16384$8$1$2m+EqB1rnfAgnED…
hash is scrypt  True
accepts bench   False
accepts owner   True
secret sha256   47affc386e765ef8361de64985a215caffbae877042ede1707829a6358f4ce63
state           "signin_set_at": "2026-11-14T18:35:07Z",
state           "used_at": "2026-11-14T18:35:07Z",
plaintext file  absent
boot marker     present
firstboot log   same         /home/tenant/.hermes/.env (a discarded sign-in, no usable password)
```

### 13.9 The session secret is the machine's (ODL-522)

`HERMES_DASHBOARD_BASIC_AUTH_SECRET` is the key the dashboard's basic-auth
provider signs its stateless session tokens with (`plugins/dashboard_auth/basic`:
HMAC-SHA256 over a compact JSON payload, base64url,
`{"sub","kind","exp","jti"}`). The build must write one, so before this card
every disk built from one image signed its cookies with the same value — the
ODL-521 head measured `47affc38…` identical in the image, after the first boot
and after the visit. Three writers now make it the machine's:

1. **The build** writes a random secret, as it must.
2. **The first boot** (`install-stack.sh --seed` on a production image)
   replaces it with this machine's own value, and replaces the sign-in with
   the seed's verifier (the password chosen at registration) when the seed
   carries `sign_in_hash`; without one the file keeps a discarded hash no
   password accepts (ODL-521/ODL-522). It is the only writer that changes the
   secret, and it changes it once: when the `.env` already holds the seed's
   verifier the same branch prints
   `same … (the seed's verifier; this machine's own secret)`, and when the
   owner's own visit recorded the hash (`used_at`/`first_login_at`) it prints
   `same … (the owner's sign-in; this machine's own secret)`; either way it
   touches nothing, so a reboot never rotates the key under a live session.
   `signin_set_at` alone is not the keep signal: a state stamped by the
   pre-ODL-947 order sits over the image's placeholder, and the branch then
   writes the verifier and a fresh secret (ODL-947).
3. **Every later writer keeps it.** The release installer's `ensure_env` reads
   the old value and writes it back byte for byte; the page's `write_env_hash`
   replaces the hash line only, and refuses a `.env` with no
   `HERMES_DASHBOARD_BASIC_AUTH_SECRET` — a 500 with the token unspent —
   instead of writing a file that could not sign a cookie.

#### 13.9.1 The build, and the image's secret

The production build ran at `a651208`, the commit that merges `main` `fcc87b2`
(ODL-505's `-8`, installer `0c53a722…`) and re-cuts the release at that head
(`2026.11.15-3`, installer `bd3c8963…`, manifest `0574d0ac…`): the image inputs
(`deploy/image`, `release/`, `installer/`, the two pages, the unlock script,
`attest.py` and `skills/`) are that commit's. After the build, `main` took
t_ae833f3f's `-1` (a type re-cut of the two embedded tenant pages), the branch
merged it at `842006b` and re-cut the same id at that head
(`e0cec88a…`, manifest `4f0e3ef9…`), so the boot transcript below is the
`a651208` image's and this is the one moved input set:

```sh
$ git diff --name-only a651208 -- deploy/image release installer \
      web/first-visit.html web/verify.html scripts/verify_and_unlock.sh attest.py skills
installer/odile_install.py
installer/odile_install.py.sig
release/manifest.json
release/manifest.json.sig
release/odile_install.py
release/odile_install.py.sig
release/release.json
release/release.json.sig
web/first-visit.html
web/verify.html
```

No `deploy/image` byte moved, and `installer/first_visit.py` (`d1c761f0…`) is
the same file in both pairs: the image carries the `bd3c8963…` installer and
`web/first-visit.html` `4d7c4960…`, a build at that round's handoff head
carries `e0cec88a…` and the type re-cut page `810a596d…`. ODL-925 has since
re-cut both first-visit surfaces: its `-2` cut (`2026.11.23-2`) carries
`installer/first_visit.py`
`7ddf42c3…` (76,304 bytes) and `web/first-visit.html` `98208a4c…` (45,248
bytes), and its `-3` cut at this card's head (`2026.11.23-3`, installer
`36add6da…`) carries `753417a0…` (78,550 bytes) and `1a9e8615…` (45,743
bytes). The tree's cut, `2026.11.25-4` (installer `b3678523…`, §12.6 of
`install.md`), carries `installer/first_visit.py` `5dd8c97a…` (87,140 bytes),
`web/first-visit.html` `d69f6abb…` (47,046 bytes) and, new since ODL-926,
`scripts/heartbeat.py` `51981cec…` (34,356 bytes), with `templates/verify.html`
`1f5314db…` (42,959 bytes) unchanged; the pins are `FIRST_VISIT_SHA256`,
`FIRST_VISIT_PAGE_SHA256`, `HEARTBEAT_SHA256` and `VERIFY_TEMPLATE_SHA256` in
the installer.

The build's own head line and the fixture seed the boot uses:

```sh
$ OUT=/var/tmp/odile-odl522-prod
$ FX=/var/tmp/odile-odl522-boot
$ ./deploy/image/firstboot-fixture.py prepare --dir "$FX" \
      --instance 45445445-0000-4000-8000-000000000454
tenant     odl-bench
instance   45445445-0000-4000-8000-000000000454
expires    2026-11-16T03:00:46Z
$ ./deploy/image/sealed-initrd.sh --out "$OUT" --force
$ ODILE_GUEST_NAME=odl-bench \
  ODILE_IP_TOKEN="ip=dhcp bb.ip4=10.0.2.15/24 bb.gw4=10.0.2.2 bb.dns=10.0.2.3" \
  ODILE_SEED_TOKEN="$(cat "$FX/token")" \
  ODILE_SEED_URL="http://10.0.2.2:18081/seed" \
  ODILE_SEALED_ROOTFS=/var/tmp/odile-odl522-prod-rootfs \
      ./deploy/image/sealed-image.sh --out "$OUT" --production --force
release      installer bd3c89637380…, manifest 0574d0accfcd…, listener manifest.py, unit odile-manifest.service
$ ./deploy/image/uki.sh --in "$OUT" --sign --pin "$OUT/tenant-image.pin" --vcpus 2
$ ./deploy/image/place-pin.sh --disk "$OUT/odile-tenant-sealed.raw" --pin "$OUT/tenant-image.pin"
write
pin raw_image_sha256 -> 8919bc049a54 (as built, pin inside)
$ sha256sum "$OUT/odile-tenant-sealed.raw" "$OUT/odile-tenant.efi"
8919bc049a54667cb20637eef610ae53896964fb7c2d1c95393d0842215f60ba  /var/tmp/odile-odl522-prod/odile-tenant-sealed.raw
2e8a127034990d54239a86c09852e71d45d15c04fc1c214bd9cbe5048f7f2cd5  /var/tmp/odile-odl522-prod/odile-tenant.efi
$ sha256sum "$OUT/ovmf/OVMF.amdsev.fd"
8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b  /var/tmp/odile-odl522-prod/ovmf/OVMF.amdsev.fd
```

and the log's first line, which is what a rebuild reproduces:

```sh
$ head -1 /var/tmp/odile-odl522-build/build.log
=== build start 2026-11-15T03:00:45Z head=a65120824e389a0802bd1a4a42ab03e862219897
```

The image's `.env`, read with the same mount commands as §13.3 (the one-shot
harness prints these rows with `check-disk.py --disk … --phase image`):

```sh
$ /usr/bin/python3 /var/tmp/odile-odl522-build/check-disk.py \
      --disk "$OUT/odile-tenant-sealed.raw" --phase image \
      --out /var/tmp/odile-odl522-build/disk-image.txt \
      --secret-out /var/tmp/odile-odl522-build/image-secret.txt
disk               image /var/tmp/odile-odl522-prod/odile-tenant-sealed.raw sha256 8919bc049a54667cb20637eef610ae53896964fb7c2d1c95393d0842215f60ba
key                bootstrap key
.env               home/tenant/.hermes/.env 537 bytes sha256 78c4758aa4783861a522a8944bdeffff3374c2e298c5c0c5320318ff4fe58c5a
markers            ['# Odile tenant .env — generated by deploy/image/install-stack.sh, never edited', "# by hand. Exactly three keys: the owner's sign-in and the session signing", '# secret. No provider key: the inference credential is in /etc/odile/key.env.', '# odile-signin-placeholder: 1']
hash prefix        scrypt$16384$8$1$t7LA4SfeetYN36W…
secret sha256      71d4e59dbb08d97c1bf69dbab19a1d90d1d5427da694b7c65ef7cf3830ad21e2
secret file        /var/tmp/odile-odl522-build/image-secret.txt (mode 0600, the value only)
state              etc/odile/first-visit.json ABSENT
plaintext file     absent
production marker  present
firstboot log      var/log/odile-firstboot.log ABSENT
```

The secret's sha256 is over the value, not the line, and reproduces with:

```sh
$ sed -n 's/^HERMES_DASHBOARD_BASIC_AUTH_SECRET=//p' <the .env> | tr -d '\n' | sha256sum
71d4e59dbb08d97c1bf69dbab19a1d90d1d5427da694b7c65ef7cf3830ad21e2  -
```

#### 13.9.2 The first boot writes this machine's own secret

Disk `A` is a byte copy of the built image (`cp --sparse=always`; its sha256 is
the image's, `8919bc04…`), booted from its UKI under QEMU TCG with the distro
firmware (Servury's OVMF.amdsev.fd boots its built-in GRUB first and the bench
has no `sevsecret`, so the bench boot uses `/usr/share/ovmf/OVMF.fd`):

```sh
$ /usr/bin/python3 /var/tmp/odile-odl522-build/boot-probe.py --mode measure --phase pre \
      --disk /var/tmp/odile-odl522-final/A/odile-tenant-sealed.raw \
      --uki "$OUT/odile-tenant.efi" --ovmf /usr/share/ovmf/OVMF.fd \
      --expect installed --token "$(cat "$FX/token")" --out /var/tmp/odile-odl522-final/A/pre
$ grep -a 'odile-firstboot: installed' /var/tmp/odile-odl522-final/A/pre/serial.log
firstboot    odile-firstboot: installed odl-bench (instance 45445445-0000-4000-8000-000000000454) · first-visit 200 · record 2026.11.1 pending key SHA256:QVOWnv+LG7JThkE5wy8WCIcc92gcOIKDzynK+vtTdOU (hq first_visit, first_boot -> first_visit) · 583s
```

The boot wrote a new `.env`; the read-only check straight after it prints the
machine's secret, and the shell command above reproduces the digest:

```sh
$ /usr/bin/python3 /var/tmp/odile-odl522-build/check-disk.py \
      --disk /var/tmp/odile-odl522-final/A/odile-tenant-sealed.raw \
      --phase "after the first boot" --out /var/tmp/odile-odl522-build/disk-A-pre.txt
disk                 after the first boot /var/tmp/odile-odl522-final/A/odile-tenant-sealed.raw sha256 d40b3f110d57950840791095d366178ff31160bcf901faf175273590eacf47cf
.env                 home/tenant/.hermes/.env 537 bytes sha256 6f05d4fb94c8bf488fe5fdffbda1c71f9109b6d143bec3b28fef474626df2556
hash prefix          scrypt$16384$8$1$q53VwXNTHoV4t7u…
secret sha256        05b275eae990cefa7320c78cfa6b9b74a300eee0c1de56729e6804d63cc52eb8
state signin_set_at  None
firstboot log        write        /home/tenant/.hermes/.env (mode 0600, tenant; first boot: a discarded sign-in, this machine's own secret)
$ sed -n 's/^HERMES_DASHBOARD_BASIC_AUTH_SECRET=//p' <A's .env> | tr -d '\n' | sha256sum
05b275eae990cefa7320c78cfa6b9b74a300eee0c1de56729e6804d63cc52eb8  -
```

The two values differ (`71d4e59d…` the image's, `05b275ea…` the machine's) and
so do the `.env` bytes (`78c4758a…` → `6f05d4fb…`): the first boot replaced
the secret that shipped in the mounted image.

#### 13.9.3 A second boot keeps it

The same disk booted once more; its console printed
`odile-firstboot: same`, and the read-only check after it prints the first
boot's `.env` and secret unchanged:

```sh
$ /usr/bin/python3 /var/tmp/odile-odl522-build/boot-probe.py --mode measure --phase keep2 \
      --disk /var/tmp/odile-odl522-final/A/odile-tenant-sealed.raw \
      --uki "$OUT/odile-tenant.efi" --ovmf /usr/share/ovmf/OVMF.fd \
      --expect same --token "$(cat "$FX/token")" --out /var/tmp/odile-odl522-final/A/keep2
$ grep -a 'odile-firstboot: same' /var/tmp/odile-odl522-final/A/keep2/serial.log
tenant@odl-bench:~$ [   96.753713] python3[668]: odile-firstboot: same
$ /usr/bin/python3 /var/tmp/odile-odl522-build/check-disk.py \
      --disk /var/tmp/odile-odl522-final/A/odile-tenant-sealed.raw \
      --phase "after the second boot (same)" --out /var/tmp/odile-odl522-build/disk-A-after.txt
disk                 after the second boot (same) /var/tmp/odile-odl522-final/A/odile-tenant-sealed.raw sha256 a207cbf779c9e43bc9d3fe0e9224fb37133bc697108f61a6f0453aa06733c0e3
key                  bootstrap key
.env                 home/tenant/.hermes/.env 537 bytes sha256 6f05d4fb94c8bf488fe5fdffbda1c71f9109b6d143bec3b28fef474626df2556
markers              ['# Odile tenant .env — generated by deploy/image/install-stack.sh, never edited', "# by hand. Exactly three keys: the owner's sign-in and the session signing", '# secret. No provider key: the inference credential is in /etc/odile/key.env.', '# odile-signin-placeholder: 1']
hash prefix          scrypt$16384$8$1$q53VwXNTHoV4t7u…
secret sha256        05b275eae990cefa7320c78cfa6b9b74a300eee0c1de56729e6804d63cc52eb8
state signin_set_at  None
state used_at        None
plaintext file       absent
production marker    present
firstboot log        write        /home/tenant/.hermes/.env (mode 0600, tenant; first boot: a discarded sign-in, this machine's own secret)
```

A second machine, disk `B` — a fresh byte copy of the same image — wrote its
own `40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe`, so two tenants from one image no longer share a key.

#### 13.9.4 The page keeps it, and refuses a `.env` with none

Disk `B`'s later boot (its first boot had written `40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe`). The probe
posts with the secret line removed, restores it, then posts for real:

```sh
$ /usr/bin/python3 /var/tmp/odile-odl522-build/boot-probe.py --mode visit --phase visit \
      --disk /var/tmp/odile-odl522-final/B/odile-tenant-sealed.raw \
      --uki "$OUT/odile-tenant.efi" --ovmf /usr/share/ovmf/OVMF.fd --expect same \
      --token "$(cat "$FX/token")" --password the-ow...l522 \
      --image-secret-file /var/tmp/odile-odl522-build/image-secret.txt \
      --out /var/tmp/odile-odl522-final/B/visit4
firstboot             odile-firstboot: same
console               tenant shell on ttyS0
gate                  302
before_env_sha256     972d6578e0221ea080c7e1f0c91fb0fcfe15a348243dcd393109be6445372b08
before_secret_sha256  40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe
before_markers        ['# Odile tenant .env — generated by deploy/image/install-stack.sh, never edited', "# by hand. Exactly three keys: the owner's sign-in and the session signing", '# secret. No provider key: the inference credential is in /etc/odile/key.env.', '# odile-signin-placeholder: 1']
before_hash_prefix    scrypt$16384$8$1$8dZVty5v3XGI8h1…
before_signin_set_at  None
before_used_at        None
before_firstboot_log  write        /home/tenant/.hermes/.env (mode 0600, tenant; first boot: a discarded sign-in, this machine's own secret)
before_gateway_state  running pid=753 updated=2026-11-15T04:28:35.585917+00:00
before_gateway_log    2026-11-15 04:28:35,448 INFO gateway.run: Cron scheduler will tick 1 profile(s): ['default'] | 2026-11-15 04:28:35,571 INFO gateway.run: Gateway housekeeping started (interval=60s)
before_anonymous_api  401
before_root           302
refusal_status        500
refusal_body          The sign-in was not set. Try again.
refusal_used_at       None
refusal_env_restored  yes
visit_status          200
visit_done_page       yes
after_env_sha256      e94e12aca1e2f397de92193ad30fc1c172c2f4a735e87f1dcf2e6b7c5de0f200
after_secret_sha256   40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe
after_markers         ['# Odile tenant .env — generated by deploy/image/install-stack.sh, never edited', "# by hand. Exactly three keys: the owner's sign-in and the session signing", '# secret. No provider key: the inference credential is in /etc/odile/key.env.', '# odile-signin-placeholder: 1']
after_hash_prefix     scrypt$16384$8$1$/cl10ob/9Fu87ML…
after_signin_set_at   None
after_used_at         None
after_firstboot_log   write        /home/tenant/.hermes/.env (mode 0600, tenant; first boot: a discarded sign-in, this machine's own secret)
after_gateway_state   running pid=753 updated=2026-11-15T04:29:01.209588+00:00
after_gateway_log     2026-11-15 04:29:01,284 INFO gateway.run: Gateway housekeeping stopped | 2026-11-15 04:29:01,391 INFO gateway.run: Exiting with code 1 (signal-initiated shutdown without restart request) so the service manager can revive the gateway.
after_anonymous_api   no answer: [Errno 111] Connection refused
after_root            no answer: [Errno 111] Connection refused
login_status          200
login_at_cookie       hermes_session_at_a41622a8e12704df
pair_cookies          hermes_session_at_a41622a8e12704df + webapp_signin provider (no hermes_session_rt_a41622a8e12704df)
token_login           200
token_image           401
token_machine         200
token_random          401
done                  visit
boot         573s wall
shutdown     guest sync, then SIGTERM
```

With the secret line removed the page answers **500** with its retry sentence,
leaves `used_at` null and the file byte-identical (`refusal_env_restored yes`);
the same token then sets the owner's sign-in (200) and the `.env` hash changes
while the secret stays `40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe`. The state's own `used_at`/`signin_set_at`
are root-0600, so the guest's rows read `None`; the read-only check prints them:

```sh
$ /usr/bin/python3 /var/tmp/odile-odl522-build/check-disk.py \
      --disk /var/tmp/odile-odl522-final/B/odile-tenant-sealed.raw \
      --phase "after the visit" --out /var/tmp/odile-odl522-build/disk-B-visit.txt
disk                 after the visit /var/tmp/odile-odl522-final/B/odile-tenant-sealed.raw sha256 b812dfc354150454b67124cc7972c0d75c908a638b7d433167501e8de949ccf7
key                  bootstrap key
.env                 home/tenant/.hermes/.env 537 bytes sha256 e94e12aca1e2f397de92193ad30fc1c172c2f4a735e87f1dcf2e6b7c5de0f200
markers              ['# Odile tenant .env — generated by deploy/image/install-stack.sh, never edited', "# by hand. Exactly three keys: the owner's sign-in and the session signing", '# secret. No provider key: the inference credential is in /etc/odile/key.env.', '# odile-signin-placeholder: 1']
hash prefix          scrypt$16384$8$1$/cl10ob/9Fu87ML…
secret sha256        40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe
state signin_set_at  '2026-11-15T04:29:03Z'
state used_at        '2026-11-15T04:29:03Z'
plaintext file       absent
production marker    present
firstboot log        write        /home/tenant/.hermes/.env (mode 0600, tenant; first boot: a discarded sign-in, this machine's own secret)
```

The two behaviours are pinned by
`tests/test_first_visit.py::SignInTests.test_the_post_keeps_the_machines_secret`
and `test_the_post_refuses_an_env_with_no_secret`, and the installer's
`ensure_env` (the other writer) by `FirstVisitEnvTests`.

#### 13.9.5 The forge pair, with the refresh cookie withheld

The next boot of disk `B` — after `signin_set_at`, so the secret has survived a
visit. The probe mints three access tokens with the same payload shape the
provider signs (one with the image's secret, one with the machine's read from
the `.env`, one with a random 32 bytes), logs in with the owner's password for
the fourth, and sends each as `hermes_session_at_<gateway>` **without** the
`hermes_session_rt_<gateway>` cookie:

```sh
$ /usr/bin/python3 /var/tmp/odile-odl522-build/boot-probe.py --mode pair --phase reboot \
      --disk /var/tmp/odile-odl522-final/B/odile-tenant-sealed.raw \
      --uki "$OUT/odile-tenant.efi" --ovmf /usr/share/ovmf/OVMF.fd --expect same \
      --token "$(cat "$FX/token")" --password the-ow...l522 \
      --image-secret-file /var/tmp/odile-odl522-build/image-secret.txt \
      --cpus 1 --probe-timeout 1500 --out /var/tmp/odile-odl522-final/B/reboot2
firstboot           odile-firstboot: same
console             tenant shell on ttyS0
gate                302
boot_env_sha256     e94e12aca1e2f397de92193ad30fc1c172c2f4a735e87f1dcf2e6b7c5de0f200
boot_secret_sha256  40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe
boot_markers        ['# Odile tenant .env — generated by deploy/image/install-stack.sh, never edited', "# by hand. Exactly three keys: the owner's sign-in and the session signing", '# secret. No provider key: the inference credential is in /etc/odile/key.env.', '# odile-signin-placeholder: 1']
boot_hash_prefix    scrypt$16384$8$1$/cl10ob/9Fu87ML…
boot_signin_set_at  None
boot_used_at        None
boot_firstboot_log  write        /home/tenant/.hermes/.env (mode 0600, tenant; first boot: a discarded sign-in, this machine's own secret)
boot_gateway_state  running pid=741 updated=2026-11-15T04:46:29.644306+00:00
boot_gateway_log    2026-11-15 04:46:29,203 INFO gateway.run: Cron scheduler will tick 1 profile(s): ['default'] | 2026-11-15 04:46:29,602 INFO gateway.run: Gateway housekeeping started (interval=60s)
boot_anonymous_api  401
boot_root           302
login_status        200
login_at_cookie     hermes_session_at_a41622a8e12704df
pair_cookies        hermes_session_at_a41622a8e12704df + webapp_signin provider (no hermes_session_rt_a41622a8e12704df)
token_login         200
token_image         401
token_machine       200
token_random        401
done                pair
boot         551s wall
shutdown     guest sync, then SIGTERM
```

The minting is the provider's own `_sign` (`plugins/dashboard_auth/basic`), and
the secret resolution is its `_resolve_secret` (base64 first, then hex):

```sh
$ /usr/bin/python3 - <<'PY'
import base64, hashlib, hmac, json, sys, time
secret, kind = sys.argv[1], "access"
def key(raw):
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            value = decoder(raw)
            if len(value) >= 16:
                return value
        except Exception:
            pass
    return raw.encode()
body = json.dumps({"sub": "odile", "kind": kind, "exp": int(time.time()) + 3600,
                   "jti": "odl522probe"}, separators=(",", ":")).encode()
print(base64.urlsafe_b64encode(body + hmac.new(key(secret), body, hashlib.sha256).digest()).decode())
PY
```

The rows are the pair the ODL-466 probe lacked: a forged access token plus the
login's refresh cookie answers 200 whatever the secret is, because the middleware
refreshes; with only the access cookie and the non-secret `webapp_signin`
binding, the image's secret is **401** on this machine, the machine's own secret
**200**, a random secret **401**, and the login's real token **200**.

#### 13.9.6 After `signin_set_at`, a reboot keeps it byte for byte

The read-only check after the pair boot (which followed the visit boot) prints
the same secret as the first boot and the state the page wrote:

```sh
$ /usr/bin/python3 /var/tmp/odile-odl522-build/check-disk.py \
      --disk /var/tmp/odile-odl522-final/B/odile-tenant-sealed.raw \
      --phase "after the visit and the reboot" --out /var/tmp/odile-odl522-build/disk-B-after.txt
disk                 after the visit and the reboot /var/tmp/odile-odl522-final/B/odile-tenant-sealed.raw sha256 4fb4d0a9df14e421e7b35cd5d21b0463c0875788901400017d4d8f4dd889639c
key                  bootstrap key
.env                 home/tenant/.hermes/.env 537 bytes sha256 e94e12aca1e2f397de92193ad30fc1c172c2f4a735e87f1dcf2e6b7c5de0f200
markers              ['# Odile tenant .env — generated by deploy/image/install-stack.sh, never edited', "# by hand. Exactly three keys: the owner's sign-in and the session signing", '# secret. No provider key: the inference credential is in /etc/odile/key.env.', '# odile-signin-placeholder: 1']
hash prefix          scrypt$16384$8$1$/cl10ob/9Fu87ML…
secret sha256        40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe
state signin_set_at  '2026-11-15T04:29:03Z'
state used_at        '2026-11-15T04:29:03Z'
plaintext file       absent
production marker    present
firstboot log        write        /home/tenant/.hermes/.env (mode 0600, tenant; first boot: a discarded sign-in, this machine's own secret)
$ sed -n 's/^HERMES_DASHBOARD_BASIC_AUTH_SECRET=//p' <B's .env> | tr -d '\n' | sha256sum
40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe  -
```

`e94e12aca1e2f397de92193ad30fc1c172c2f4a735e87f1dcf2e6b7c5de0f200` is the `.env` the visit write produced, and `40a1ffd0ed7a250bacee413c94cbcc4a4ce3e5ac187d6d28f20413ff586984fe` is
the first boot's secret: the visit, the pair boot and the reboot before this
check moved neither the secret nor the owner's hash.

#### 13.9.7 What this does not prove

The bench is a QEMU TCG guest, so the boot above proves the image's first boot
and the machine's own writes, not a Blackbox's chip-derived seal (§13.10). The
one-shot harness (`boot-probe.py`, `check-disk.py`) is outside the tree, like
ODL-521's; their sha256s are `0d7340ffa6b71b376d4dcf3584ab2f8f3e925e50f24b8a73f81084eeb3f11cc8`
(`boot-probe.py`) and `db2b6ce61198493f8f313dc05da6f7e7e333e714f1e521bb089cf9150869c1dc`
(`check-disk.py`), and the phase reports they wrote are the rows quoted above.
This run's probe paces its serial writes: the guest's tty runs with IXON and
the host does not honour its XOFF, so a 14 KB heredoc in one write was
truncated (and a long command line lost bytes mid-token) on a loaded box; the
paced writer waits for the shell prompt between the heredoc and the command.
The harness, the reports and the build log are attached to the card as
`odl522-harness-evidence-r2.tar.gz`.


### 13.10 What only a Blackbox can prove

The QEMU bench has no `/dev/kvm` and derives the seal from the command line,
not from `SNP_GET_DERIVED_KEY`. It cannot prove: that a real firmware extends
the measured command line this image expects; that the guest's own re-key
against the chip-derived seal leaves the disk openable; that the panel uploads
these bytes; or that a real first boot over the tenant's certificate answers
`/first-visit` on the tenant's own name. Those are ODL-454's and ODL-466's
arguments and the strict run's; this section proves what the *image* carries
and what the *boot* does with it.

### 13.11 The image ships no SSH host private key (ODL-523)

#### 13.11.1 The rule

The built image carries **no** `/etc/ssh/ssh_host_*` file — not a private key
and not its public half — and every machine makes its own three host keys at its
first boot, before anything reads the boot record. Two disks built from one
image therefore present different ed25519 fingerprints, and the boot record HQ
keeps for each machine carries that machine's own fingerprint
(`tenant_state.detail.host_key`).

Before this card every disk built from the image carried debootstrap's
`/etc/ssh/ssh_host_{ed25519,rsa,ecdsa}_key` at 0600 root, and the live tenant
presented the image's ed25519 key `SHA256:IRqReY12…` (ODL-466 §0.2, finding 3).
SSH is not in the user path, so it was not a way in; it was a private key in a
public artifact, which AGENTS.md's "never add a standing credential to a tenant
machine" forbids.

#### 13.11.2 The mechanism

Three writers, in this order.

1. **debootstrap's `openssh-server` postinst** writes the keys at build time.
   Measured in the build root while `sealed-image.sh` was still in its
   `install-stack.sh` step, so before the removal in 2:

   ```sh
   $ pid=$(pgrep -f "install-stack.sh --password" | head -1)
   $ nsenter -t "$pid" -m -- ls /var/tmp/odile-odl523-prod-rootfs/etc/ssh/
   moduli  ssh_config  ssh_config.d  ssh_host_ecdsa_key  ssh_host_ecdsa_key.pub
   ssh_host_ed25519_key  ssh_host_ed25519_key.pub  ssh_host_rsa_key
   ssh_host_rsa_key.pub  sshd_config
   ```

   `sealed-image.sh` removes the build root at the end, so that listing is a
   measurement *during* a build; §13.11.4 plants the same file on a finished
   image's copy, which is the reproducible half.
2. **The build removes them, and fails if a private one survives.**
   `install-stack.sh` section `4b` removes every `/etc/ssh/ssh_host_*`, prints
   `remove the debootstrap SSH host keys (/etc/ssh/ssh_host_*)`, and then exits
   1 with `install-stack: the image still carries an SSH host private key` if
   `compgen -G "/etc/ssh/ssh_host_*_key"` still matches. The same section writes
   the drop-in
   `/etc/systemd/system/sshd-keygen.service.d/10-odile-hostkeys.conf`.
3. **The first boot makes this machine's own.** Debian's `sshd-keygen.service`
   carries `ConditionFirstBoot=yes` and the image ships an `/etc/machine-id`, so
   on a cloned disk that condition is false and the unit would never fire —
   hence the drop-in, which clears the condition (`[Unit]` then
   `ConditionFirstBoot=`), so `ssh-keygen -A` runs before `ssh.service`.
   Independently, `odile-firstboot.py`'s `ensure_host_keys()` runs
   `ssh-keygen -A -f /` **before** it calls `boot_record_facts()`, so the record
   carries this machine's key even if the unit has not run yet.
   `ssh-keygen -A` leaves existing keys alone: whichever path runs first
   generates them, the other reads `same`, and a later boot does not move the
   bytes. The console carries one `odile-hostkeys:` line in front of the one
   `odile-firstboot:` line.

#### 13.11.3 The build: zero host keys, and the drop-in

The production build is §13.3's five commands with the fixture seed the boots
use, run from this card's worktree on top of `main`'s `ef0bc27`. The build log
records the tree it built from — the three image inputs and the test, before
this section was written:

```sh
$ head -7 /var/tmp/odile-odl523-build/build.log
=== build start 2026-11-15T06:31:38Z head ef0bc27da49bdd2e3d3bdba3a05e6da635018037
=== tree status
 M deploy/image/install-stack.sh
 M deploy/image/odile-firstboot.py
 M deploy/image/odile-firstboot.service
 M tests/test_sealed_image.py
=== prepare the fixture seed
```

The commit that carries the work is
`4ea2f4c3c26fdc23aa0540d55d94434059d8dda0`; `git diff --stat ef0bc27 4ea2f4c3`
lists those four files, this section, and no other path (the unit's change is
its console-contract comment, not behaviour). The image's own copies of the
three build inputs are byte-for-byte that commit's, read at the mount below:

```sh
$ for p in usr/local/lib/odile-image/install-stack.sh \
           usr/local/lib/odile-image/odile-firstboot.py \
           etc/systemd/system/odile-firstboot.service; do
      sha256sum "/mnt/odl523root/$p"
  done
027bec3dba314db45fca5c6b6b0873724fed0809f60641f313305aad5be1fd14  /mnt/odl523root/usr/local/lib/odile-image/install-stack.sh
751d8384f9631749d97ee8e1977d130b6541220244676b33f7e4acc26b002878  /mnt/odl523root/usr/local/lib/odile-image/odile-firstboot.py
dbbe95ae8b94669ea2116cf0e787110c1ecb66022b78518e59a0782adc1b728d  /mnt/odl523root/etc/systemd/system/odile-firstboot.service
$ for p in deploy/image/install-stack.sh deploy/image/odile-firstboot.py \
           deploy/image/odile-firstboot.service; do
      git show 4ea2f4c:$p | sha256sum
  done
027bec3dba314db45fca5c6b6b0873724fed0809f60641f313305aad5be1fd14  -
751d8384f9631749d97ee8e1977d130b6541220244676b33f7e4acc26b002878  -
dbbe95ae8b94669ea2116cf0e787110c1ecb66022b78518e59a0782adc1b728d  -
```

The five commands, in order:

```sh
$ OUT=/var/tmp/odile-odl523-prod
$ FX=/var/tmp/odile-odl523-boot
$ ./deploy/image/firstboot-fixture.py prepare --dir "$FX" \
      --instance 45445445-0000-4000-8000-000000000454
$ ./deploy/image/sealed-initrd.sh --out "$OUT" --force
$ ODILE_GUEST_NAME=odl-bench \
  ODILE_IP_TOKEN="ip=dhcp bb.ip4=10.0.2.15/24 bb.gw4=10.0.2.2 bb.dns=10.0.2.3" \
  ODILE_SEED_TOKEN="$(cat "$FX/token")" \
  ODILE_SEED_URL="http://10.0.2.2:18081/seed" \
  ODILE_SEALED_ROOTFS=/var/tmp/odile-odl523-prod-rootfs \
      ./deploy/image/sealed-image.sh --out "$OUT" --production --force
$ ./deploy/image/uki.sh --in "$OUT" --sign --pin "$OUT/tenant-image.pin" --vcpus 2
$ ./deploy/image/place-pin.sh --disk "$OUT/odile-tenant-sealed.raw" \
      --pin "$OUT/tenant-image.pin"
write
pin raw_image_sha256 -> 4910ede1b463 (as built, pin inside)
=== production build rc=0 at 2026-11-15T06:42:38Z
$ sha256sum "$OUT/odile-tenant-sealed.raw" "$OUT/odile-tenant.efi"
4910ede1b4639f5ce9b89cc1ec40f4fd7c32af769e0f8bd219e0a4627f00cc3e  /var/tmp/odile-odl523-prod/odile-tenant-sealed.raw
450f156bc36e2bd738d2882cba5a268757e766da8102b9b247c0adc3e3fb8618  /var/tmp/odile-odl523-prod/odile-tenant.efi
```

The build's own log carries the two host-key lines (once, then `same` on the
idempotency rerun `sealed-image.sh` always does):

```sh
$ grep -n "hostkeys\|SSH host key" /var/tmp/odile-odl523-build/build.log
1002:write        /etc/systemd/system/sshd-keygen.service.d/10-odile-hostkeys.conf (ssh-keygen -A before sshd -t)
1003:remove       the debootstrap SSH host keys (/etc/ssh/ssh_host_*)
1052:same         /etc/systemd/system/sshd-keygen.service.d/10-odile-hostkeys.conf
1053:same         no SSH host key in the image
```

The mounted image, read the way §13.3 reads it:

```sh
$ RAW=/var/tmp/odile-odl523-prod/odile-tenant-sealed.raw
$ LOOP=$(losetup -fP --show -r "$RAW")
$ mount -o ro,noload "${LOOP}p2" /mnt/odl523boot
$ install -m600 /mnt/odl523boot/odile/luks-bootstrap.key /var/tmp/odile-odl523-build/v.key
$ umount /mnt/odl523boot
$ cryptsetup open --readonly --key-file /var/tmp/odile-odl523-build/v.key "${LOOP}p3" odl523img
$ mount -o ro,noload /dev/mapper/odl523img /mnt/odl523root
$ ls -la /mnt/odl523root/etc/ssh/
drwxr-xr-x  4 root root   4096 Nov 15 06:41 .
drwxr-xr-x 50 root root   4096 Nov 15 06:41 ..
-rw-r--r--  1 root root 592383 May  6  2026 moduli
-rw-r--r--  1 root root   1668 May  6  2026 ssh_config
drwxr-xr-x  2 root root   4096 Nov 15 06:37 ssh_config.d
-rw-r--r--  1 root root   3424 May  6  2026 sshd_config
drwxr-xr-x  2 root root   4096 May  6  2026 sshd_config.d
$ find /mnt/odl523root/etc/ssh -maxdepth 1 -name 'ssh_host_*' | wc -l
0
$ cat /mnt/odl523root/etc/systemd/system/sshd-keygen.service.d/10-odile-hostkeys.conf
# Odile: the image ships no private SSH host keys (ODL-523). The first boot
# generates this machine's own; there is no first boot left for
# ConditionFirstBoot to detect once the image is cloned, so clear it.
# ssh-keygen -A leaves existing keys alone, and this unit is Before=ssh.service.
[Unit]
ConditionFirstBoot=
$ grep -n 'ConditionFirstBoot' /mnt/odl523root/usr/lib/systemd/system/sshd-keygen.service
3:ConditionFirstBoot=yes
$ ls -la /mnt/odl523root/etc/systemd/system/multi-user.target.wants/ | grep ssh
lrwxrwxrwx  1 root root   35 Nov 15 06:38 ssh.service -> /usr/lib/systemd/system/ssh.service
$ ls -la /mnt/odl523root/etc/machine-id
-r--r--r--  1 root root 33 Nov 15 06:37 /mnt/odl523root/etc/machine-id
$ umount /mnt/odl523root; cryptsetup close odl523img; losetup -d "$LOOP"
$ grep -c odl523 /proc/mounts
0
```

Zero host keys, the drop-in that clears the condition Debian's unit sets, and an
`/etc/machine-id` that would make `ConditionFirstBoot` false on every clone —
the three facts the drop-in exists for. `ssh.service` is enabled, so sshd is up
on the booted machine (§13.11.5).

#### 13.11.4 The build's other outcome: a private key that survives

`bash /var/tmp/odile-odl523-build/negative.sh` copies the image, opens the copy,
plants an ed25519 host key exactly as debootstrap's postinst does, and runs the
image's own `install-stack.sh` (the build branch, no `--seed`) under `chroot` —
first with the real `rm`, then with an `rm` that ignores `/etc/ssh/ssh_host_*`
and is the real `rm` for everything else. The second run is the case the
assertion exists for:

```sh
$ bash /var/tmp/odile-odl523-build/negative.sh
loop         /dev/loop153 /var/tmp/odile-odl523-negative/negative.raw 4910ede1b4639f5ce9b89cc1ec40f4fd7c32af769e0f8bd219e0a4627f00cc3e
-- planted: -rw------- 419 /var/tmp/odile-odl523-negative/root/etc/ssh/ssh_host_ed25519_key
== positive control: the build removes the planted key
-- install-stack rc=0
remove       the debootstrap SSH host keys (/etc/ssh/ssh_host_*)
-- keys left: 0
-- planted: -rw------- 419 /var/tmp/odile-odl523-negative/root/etc/ssh/ssh_host_ed25519_key
== negative: a private host key survives the removal
-- install-stack rc=1
remove       the debootstrap SSH host keys (/etc/ssh/ssh_host_*)
install-stack: the image still carries an SSH host private key
-- keys left: 2
== cleanup
```

The script exits 0: both `install-stack` outcomes are inside it, and the
assertion's own `exit 1` is the `rc=1` line above. The `/dev/loop153` on the
first line is that run's free loop device — the number is whatever `losetup -f`
returned on that run and moves with what is already attached (a rerun took
`/dev/loop155`) — while the sha256 after the copy's path is the image's and does
not move.

So a build whose removal did not remove fails with its named error and a non-zero
status; the same key, when the removal works, leaves zero files behind. The
copy's mounts, mapper and loop device are released at the end (`grep -c odl523
/proc/mounts` → 0).

#### 13.11.5 Disk A, the first boot: this machine's key, then the record

A is a byte copy of the built image (`cp --sparse=always`; its sha256 is the
image's, `4910ede1…`), booted from its UKI under QEMU TCG with
`/usr/share/ovmf/OVMF.fd` (Servury's OVMF.amdsev.fd boots its built-in GRUB
first and the bench has no `sevsecret`, so the bench boot uses the distro
firmware). The harness is `/var/tmp/odile-odl523-build/boot-probe.py`; it reads
ttyS0 over a unix socket, runs the probes inside the guest (the image binds the
Webapp and the first-visit service to 127.0.0.1, which QEMU's hostfwd cannot
reach) and powers the guest down through ACPI.

```sh
$ /usr/bin/python3 /var/tmp/odile-odl523-build/boot-probe.py --phase pre \
      --disk /var/tmp/odile-odl523-final/A/odile-tenant-sealed.raw \
      --uki /var/tmp/odile-odl523-prod/odile-tenant.efi \
      --token "$(cat /var/tmp/odile-odl523-boot/token)" --expect installed \
      --out /var/tmp/odile-odl523-final/A/pre
phase        pre
disk         .../A/odile-tenant-sealed.raw sha256 4910ede1b4639f5ce9b89cc1ec40f4fd7c32af769e0f8bd219e0a4627f00cc3e
uki          .../odile-tenant.efi sha256 450f156bc36e2bd738d2882cba5a268757e766da8102b9b247c0adc3e3fb8618
ssh port     hostfwd 127.0.0.1:2300 -> guest 22
firstboot    PASS odile-firstboot: installed odl-bench (instance 45445445-0000-4000-8000-0000…
boot         570s wall
guest key    256 SHA256:2yDvzxtBi9rIcEfjjIUc/zHdRCZLaaoymNtQDWs+WKc root@odl-bench (ED25519)
sshd         active / enabled
wire key     SHA256:2yDvzxtBi9rIcEfjjIUc/zHdRCZLaaoymNtQDWs+WKc
ssh          rc 255 root@127.0.0.1: Permission denied (publickey,password).
probe        GET /verify -> 200
verify       4 PASS rows
probe        GET /first-visit -> 404
probe        GET / (Webapp gate) -> 000
shutdown     ACPI powerdown, exit 0
```

(The harness's console reader can match the `odile-firstboot:` line mid-arrival,
so its `firstboot` line stops at 93 columns; the serial line below is the whole
one. The `GET /first-visit` without a token is 404; §13.11.7 reads the page with
the token on a later boot.)

The console's own ordering, from `$OUT/A/pre/serial.log` (`tr '\r' '\n'` first:
the console wraps long lines with a carriage return):

```sh
$ tr '\r' '\n' < /var/tmp/odile-odl523-final/A/pre/serial.log \
      | grep -a 'odile-hostkeys\|odile-firstboot:'
tenant@odl-bench:~$ [  122.954616] python3[682]: odile-hostkeys: create the SSH host keys (3 generated by ssh-keygen -A)
[  556.076325] python3[682]: odile-firstboot: installed odl-bench (instance 45445445-0000-4000-8000-000000000454) · first-visit 200 · record 2026.11.1 pending key SHA256:7ruIxWkKeNStqHiE6jh2CJ4g2Z1OfyJyF1ny1oULYb0 (hq first_visit, first_boot -> first_visit) · 412s
```

The host-key line is at 122.95 s and the boot-record line at 556.08 s: the key
existed long before the record was read. The record line's `key SHA256:7ruI…` is
the *measured boot key* (the throwaway in the command line); this machine's SSH
key is in the record's `detail`, which HQ stored. `fixture-record.py --dir "$FX"`
reads the fixture's single `tenant_state` row, and B's later boot (§13.11.6)
replaced that row, so what is quoted here is the file the command printed after
this boot, kept in the build root; `cat` and `sha256sum` reproduce it:

```sh
$ cat /var/tmp/odile-odl523-build/record-A.txt
slug                 odl-bench
state                first_visit
release              2026.11.1
recorded_at          2026-11-15T07:14:32Z
detail.boot_key      ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBW0mNRc2zHqyCxjwkzfdIt+lubwOEBY/Repnbm1lqCn odile-bench throwaway boot key
detail.host_key      SHA256:2yDvzxtBi9rIcEfjjIUc/zHdRCZLaaoymNtQDWs+WKc
detail.first_visit   pending
token.instance_id    45445445-0000-4000-8000-000000000454
token.used_at        2026-11-15T07:07:39Z
event                first_visit first_boot -> first_visit 2026-11-15T07:14:32Z
event                first_boot provisioning -> first_boot 2026-11-15T06:20:02Z
event                provisioning claimed -> provisioning 2026-11-15T06:20:02Z
$ sha256sum /var/tmp/odile-odl523-build/record-A.txt
902711ecf644dc10004aaeab2d4031355e52b76ca0fe117f8652bff01664437c  /var/tmp/odile-odl523-build/record-A.txt
```

`detail.host_key` is the key A generated on its first boot (this section's
`guest key` line) — not the image's, because the image has none.
`detail.recorded_at` is the **first** record's stamp — the row's write time,
like `installed_at` — so B's later boot (§13.11.6) leaves it at A's
`07:14:32Z` and a replay writes no row at all; liveness is
`tenant_state.last_heartbeat`, not this field. The disk after the boot (an
excerpt of `disk-facts.sh`, which also prints the disk hash and the
first-visit state):

```sh
$ bash /var/tmp/odile-odl523-build/disk-facts.sh \
      /var/tmp/odile-odl523-final/A/odile-tenant-sealed.raw "A"
host keys     3 private, 3 public
-rw------- root 505 ssh_host_ecdsa_key
-rw------- root 411 ssh_host_ed25519_key
-rw------- root 2602 ssh_host_rsa_key
ed25519 pub   256 SHA256:2yDvzxtBi9rIcEfjjIUc/zHdRCZLaaoymNtQDWs+WKc root@odl-bench (ED25519)
rsa pub       3072 SHA256:u5MgN/3hPa0lTcJVBjrGn/60/U7uUU6+z/EWyqjOeQY root@odl-bench (RSA)
ecdsa pub     256 SHA256:MY0QDasNYK0iaFMBOtJwAsyE1J+er1cJXVl+xTHo8NA root@odl-bench (ECDSA)
sha256 keys   ebce6c2b7930a288340a9e1cbd2b773c689c94e47358460be45c8209061b4bef
sshd drop-ins
firstboot log
              === odile-firstboot 2026-11-15T07:07:18Z: ssh host keys
              create       the SSH host keys (3 generated by ssh-keygen -A)
              === odile-firstboot 2026-11-15T07:07:37Z: release placement
```

`sha256 keys` is the private half's digest, so "the same key on the next boot"
(§13.11.7) can be checked without a fingerprint lookup; the three private keys
are 0600 root and the three public halves are 0644, all `root@odl-bench`.

#### 13.11.6 Disk B, a second fresh disk from the same image: a different key

B is another `cp --sparse=always` of the same image (sha256 `4910ede1…`, before
its boot), booted with the token re-armed
(`firstboot-fixture.py prepare --dir "$FX" --token-file "$FX/token"`, the
documented bench rerun), and it does the first visit in that same boot:

```sh
$ /usr/bin/python3 /var/tmp/odile-odl523-build/boot-probe.py --phase pre \
      --disk /var/tmp/odile-odl523-final/B/odile-tenant-sealed.raw \
      --uki /var/tmp/odile-odl523-prod/odile-tenant.efi \
      --token "$(cat "$FX/token")" --expect installed \
      --visit odl523-bench-owner-passphrase --out /var/tmp/odile-odl523-final/B/pre
firstboot    PASS odile-firstboot: installed odl-bench (instance 45445445-0000-4000-8000-000000000454) · fi…
boot         487s wall
guest key    256 SHA256:cFf+ky0CZ0kzTk110hv9wa71p71Ks9B/3WamtJoDwfc root@odl-bench (ED25519)
sshd         active / enabled
wire key     SHA256:cFf+ky0CZ0kzTk110hv9wa71p71Ks9B/3WamtJoDwfc
ssh          rc 255 root@127.0.0.1: Permission denied (publickey,password).
probe        GET /verify -> 200
verify       4 PASS rows
probe        GET /first-visit?t=<token> -> 200
visit        POST /first-visit -> 200
probe        GET /verify after the visit -> 200
verify       4 PASS rows after the visit
probe        GET /first-visit after the visit -> 404
```

Two fingerprints from one image, read off the two public halves. The mount is
§13.11.3's, per disk; A has had later boots and B the hardened ones at this
reading, so the disk hashes are the post-boot ones while the keys are the first
boot's:

```sh
$ for d in A B; do
    RAW=/var/tmp/odile-odl523-final/$d/odile-tenant-sealed.raw
    LOOP=$(losetup -fP --show -r "$RAW")
    mount -o ro,noload "${LOOP}p2" /mnt/odl523boot
    install -m600 /mnt/odl523boot/odile/luks-bootstrap.key /tmp/v.key
    umount /mnt/odl523boot
    cryptsetup open --readonly --key-file /tmp/v.key "${LOOP}p3" odl523fp
    mount -o ro,noload /dev/mapper/odl523fp /mnt/odl523root
    echo "== $d $(sha256sum "$RAW" | cut -d' ' -f1)"
    ssh-keygen -lf /mnt/odl523root/etc/ssh/ssh_host_ed25519_key.pub
    umount /mnt/odl523root; cryptsetup close odl523fp; losetup -d "$LOOP"
  done
== A bb9b0e87222bfb4ce8d861d55340428528c835126e83c83eaa4f11921e0b7510
256 SHA256:2yDvzxtBi9rIcEfjjIUc/zHdRCZLaaoymNtQDWs+WKc root@odl-bench (ED25519)
== B 628ee9c56654b32f58c2b4fbaf223d17a24203159bae25dc8554a0dbda55b502
256 SHA256:cFf+ky0CZ0kzTk110hv9wa71p71Ks9B/3WamtJoDwfc root@odl-bench (ED25519)
```

B's boot record carries B's key — `record-B.txt`, the output of the same
command after B's boot:

```sh
$ grep '^detail.host_key' /var/tmp/odile-odl523-build/record-B.txt
detail.host_key      SHA256:cFf+ky0CZ0kzTk110hv9wa71p71Ks9B/3WamtJoDwfc
```

so the record follows the machine, not the image. B's first boot printed
`odile-hostkeys: same the SSH host keys (this machine's own, no new key
written)` at 89.51 s: on that boot `sshd-keygen.service` (the drop-in) won the
race and generated the keys before `odile-firstboot` looked — the two paths are
independent and either one is enough. B's `/first-visit` answered 200 with the
form before the visit and 404 after it (a spent token), and the page's bytes are
the image's (§13.11.8's `page` line, `sha256 810a596d…`).

The visit's 200 and the `/verify` PASS rows after it are what this card owes
ODL-521's page: the page still sets the sign-in, and `/verify` still prints its
four PASS rows afterwards. On disk A, where the visit was posted while the
tenant gateway was still starting under TCG, the page refused with `500` and its
journal line `first-visit: refused: the agent did not restart: Command
'['systemctl', 'restart', 'odile-agent']' timed out after 120 seconds` — a bench
timing limit, not the host-key change (the page writes nothing on that path and
`/verify` still answers 200).

#### 13.11.7 A later boot of A: `same`, the same bytes

```sh
$ /usr/bin/python3 /var/tmp/odile-odl523-build/boot-probe.py --phase same2 \
      --disk /var/tmp/odile-odl523-final/A/odile-tenant-sealed.raw \
      --uki /var/tmp/odile-odl523-prod/odile-tenant.efi --expect same \
      --out /var/tmp/odile-odl523-final/A/same2
firstboot    PASS odile-firstboot: same
boot         91s wall
guest key    256 SHA256:2yDvzxtBi9rIcEfjjIUc/zHdRCZLaaoymNtQDWs+WKc root@odl-bench (ED25519)
wire key     SHA256:2yDvzxtBi9rIcEfjjIUc/zHdRCZLaaoymNtQDWs+WKc
ssh          rc 255 root@127.0.0.1: Permission denied (publickey,password).
probe        GET /verify -> 200
verify       4 PASS rows
probe        GET /first-visit?t=<token> -> 200
```

with the console lines

```sh
$ tr '\r' '\n' < /var/tmp/odile-odl523-final/A/same2/serial.log \
      | grep -a 'odile-hostkeys\|odile-firstboot:'
[   71.905874] python3[676]: odile-hostkeys: same the SSH host keys (this machine's own, no new key written)
tenant@odl-bench:~$ [   82.995279] python3[676]: odile-firstboot: same
```

The key files' mtime is still 07:07:13 (the first boot) and the fingerprint and
the private half's `sha256 keys` digest are A's, so a later boot neither
rotates the key nor touches the bytes:

```sh
$ bash /var/tmp/odile-odl523-build/disk-facts.sh \
      /var/tmp/odile-odl523-final/A/odile-tenant-sealed.raw \
      "A, after a later boot (same2)"
== A, after a later boot (same2)
disk          bb9b0e87222bfb4ce8d861d55340428528c835126e83c83eaa4f11921e0b7510  /var/tmp/odile-odl523-final/A/odile-tenant-sealed.raw
host keys     3 private, 3 public
ed25519 pub   256 SHA256:2yDvzxtBi9rIcEfjjIUc/zHdRCZLaaoymNtQDWs+WKc root@odl-bench (ED25519)
sha256 keys   ebce6c2b7930a288340a9e1cbd2b773c689c94e47358460be45c8209061b4bef
firstboot log
              === odile-firstboot 2026-11-15T07:39:46Z: ssh host keys
              same         the SSH host keys (this machine's own, no new key written)
$ stat -c '%y %n' /mnt/odl523root/etc/ssh/ssh_host_ed25519_key
2026-11-15 07:07:13.183998634 +0000 /mnt/odl523root/etc/ssh/ssh_host_ed25519_key
```

(the `stat` is at a read-only mount of A's disk, the §13.11.6 loop; the mtime
is the first boot's.)

#### 13.11.8 The tenant's refusal: `Permission denied (publickey)`

The shipped image keeps Debian's own `sshd_config` (`#PasswordAuthentication
yes`, every line commented) and an empty `sshd_config.d`, so sshd advertises
`password` beside `publickey`; the release installer is what writes
`/etc/ssh/sshd_config.d/99-odile.conf` (its `SSHD_TEMPLATE`:
`PasswordAuthentication no`, `KbdInteractiveAuthentication no`,
`PermitRootLogin prohibit-password`), and on the bench it exits before that step
— `odile-release-setup.sh: error: boot check failed: the boot report status is
'not_confidential', not 'ok'` … `=== rc=5` — because TCG has no SEV. Writing
that file on disk B offline
(`bash /var/tmp/odile-odl523-build/harden-offline.sh`, sha256
`89a26b7bec6229078c2d84e24ae6e0c5ec989c9778afff2ddbff0d62cc5c4cae`) and booting
B again gives the tenant's answer — and B's ed25519 key is unchanged:

```sh
$ sha256sum /var/tmp/odile-odl523-build/harden-offline.sh
89a26b7bec6229078c2d84e24ae6e0c5ec989c9778afff2ddbff0d62cc5c4cae  /var/tmp/odile-odl523-build/harden-offline.sh
```

```sh
$ /usr/bin/python3 /var/tmp/odile-odl523-build/boot-probe.py --phase keyonly \
      --disk /var/tmp/odile-odl523-final/B/odile-tenant-sealed.raw \
      --uki /var/tmp/odile-odl523-prod/odile-tenant.efi --expect same \
      --out /var/tmp/odile-odl523-final/B/keyonly2
firstboot    PASS odile-firstboot: same
boot         83s wall
guest key    256 SHA256:cFf+ky0CZ0kzTk110hv9wa71p71Ks9B/3WamtJoDwfc root@odl-bench (ED25519)
sshd         active / enabled
wire key     SHA256:cFf+ky0CZ0kzTk110hv9wa71p71Ks9B/3WamtJoDwfc
ssh          rc 255 root@127.0.0.1: Permission denied (publickey).
ssh cmd      $ ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -p 2300 root@127.0.0.1 true
ssh inside   rc 255 root@127.0.0.1: Permission denied (publickey).
probe        GET /verify -> 200
verify       4 PASS rows
probe        GET /first-visit?t=<token> -> 404
page         /usr/local/lib/odile/first-visit.html sha256 810a596d561fb7ae28fcd67b864a836fe2c5b808dce223b89d573fd9d622ef29
```

The disk after that boot (`bash /var/tmp/odile-odl523-build/disk-facts.sh
/var/tmp/odile-odl523-final/B/odile-tenant-sealed.raw "B, after the hardened boots"`):

```sh
== B, after the hardened boots
disk          628ee9c56654b32f58c2b4fbaf223d17a24203159bae25dc8554a0dbda55b502  /var/tmp/odile-odl523-final/B/odile-tenant-sealed.raw
host keys     3 private, 3 public
-rw------- root 505 ssh_host_ecdsa_key
-rw------- root 411 ssh_host_ed25519_key
-rw------- root 2602 ssh_host_rsa_key
ed25519 pub   256 SHA256:cFf+ky0CZ0kzTk110hv9wa71p71Ks9B/3WamtJoDwfc root@odl-bench (ED25519)
rsa pub       3072 SHA256:73yFbeFt93Tc9i0fSS6ZPLqrLRsaIvYnzPH7WTkJEyc root@odl-bench (RSA)
ecdsa pub     256 SHA256:VExuExQM97jL9xd26x4W3kqpYJbp0LPHEr1CnHm4ZAw root@odl-bench (ECDSA)
sha256 keys   c184b078cf50a68c9672c599665031e961a8d8b61a635e479fa703a89778e498
sshd drop-ins 99-odile.conf
              PasswordAuthentication no
```

The fingerprint and the `sha256 keys` digest are B's first-boot key (`cFf+…`),
unmoved by the hardening or the third boot; `99-odile.conf` with
`PasswordAuthentication no` is the only new line beside the image's own.

`Permission denied (publickey)` is the tenant's refusal: the machine presents its
own key, and the only way in is a key the user holds. (The `-p 2300` is the
probe's hostfwd to the guest's 22; the guest sees `root@<guest> true`.)

#### 13.11.9 What only a Blackbox can prove

Two facts this card did not move: the tree's `web/first-visit.html` is
`810a596d561fb7ae28fcd67b864a836fe2c5b808dce223b89d573fd9d622ef29`
(`git show ef0bc27:web/first-visit.html | sha256sum`), the same bytes the image
serves; and the bench smoke set is unchanged —
`git diff --stat ef0bc27..4ea2f4c -- deploy/image/smoke.sh deploy/image/bench.py
deploy/image/odile-bench-smoke.service` prints nothing.

This section proves the *image* carries no host key, that the build fails when
one survives, and that each boot makes its own before the record. It does not
prove that Servury's firmware boots this UKI, nor that a Blackbox's `sshd` is
started by the same unit ordering; those are ODL-454's arguments. The bench also
cannot show the release installer's own `99-odile.conf` step (it needs the `ok`
boot report), so §13.11.8 writes the file the installer would write and boots it.
