#!/bin/bash
# sealed-image.sh — build the sealed bench disk: a Debian 13 tenant root
# inside a LUKS2 container, a separate boot partition holding the sealed
# volume-key blob, and the command line the UKI will measure (ODL-454).
#
# One definition, three artefacts (the same pattern as the ODL-380 bench):
#   * the root filesystem is still installed by deploy/image/install-stack.sh,
#     twice, so the sealed image has the same tenant stack and the same
#     idempotency evidence as the plain bench image;
#   * the 64-byte LUKS passphrase V is random, is formatted as the disk's only
#     keyslot, is sealed to the exact command line in boot/cmdline, and the
#     plaintext is shredded — no human and no operator holds it;
#   * the throwaway boot key is an Ed25519 key generated here, whose *private
#     half is shredded before the build touches the disk*. Only the public key
#     goes on the command line (bb.key, as Servury's own initramfs expects), so
#     the image has zero usable user keys.
#
# The UKI itself is built by deploy/image/uki.sh from boot/{vmlinuz,initrd.img,
# cmdline,uname,os-release}; sealed-initrd.sh produces the first two. This
# script produces the disk and the sealed blob.
#
#   ./deploy/image/sealed-image.sh --dry-run
#   sudo ./deploy/image/sealed-image.sh --out deploy/image/out-sealed
#   sudo ./deploy/image/sealed-image.sh --out deploy/image/out-sealed \
#       --cmdline-file deploy/image/out-sealed/boot/cmdline   # rebuild from the pin
#
# Outputs under --out:
#   odile-tenant-sealed.raw        the disk (GPT: bios-boot, boot, LUKS root)
#   boot/cmdline                   the measured line, seal_id included
#   boot/thrownaway-key.pub        the only user key, public half
#   boot/luks-vol.key.sealed       the blob that goes on /dev/sda2
#   sealed/seal.txt                key id, blob hash, pin-relevant facts
#
# --cmdline-file reuses an exact measured line instead of generating a new
# throwaway key. That is how a disk is rebuilt to match an already-built UKI:
# the public key in the line is the only key the image ever had, and the blob
# is sealed to the same bytes the UKI embeds. It is also the per-tenant path:
# the panel gives the command line, the disk is sealed to it.
#
# Exit codes: 0 ok, 1 build failed, 2 bad input or a missing prerequisite,
# 3 image built but the host was damaged by the chroot mounts.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

# Before the option loop consumes them: the namespace re-exec below replays
# the exact command line this script was called with.
ORIG_ARGS=("$@")

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
OUT=$HERE/out-sealed
SIZE=8G
SUITE=trixie
MIRROR=http://deb.debian.org/debian
PASSWORD=${ODILE_BENCH_PASSWORD:-odile-bench-not-a-secret}
SEAL_ID=${ODILE_SEAL_ID:-odl454-bench-v1}
GUEST_NAME=${ODILE_GUEST_NAME:-odile-bench}
IID=${ODILE_BENCH_IID:-45445445-0000-4000-8000-000000000454}
# The network half of the measured line. A Blackbox needs the static form
# (`<ip>::<gw>:<mask>:<host>:<iface>:off`); the bench uses QEMU's DHCP.
IP_TOKEN=${ODILE_IP_TOKEN:-ip=dhcp}
BB_RELEASE=${ODILE_BB_RELEASE:-2026.11.1}
GUEST_SMOKE=1
DRY_RUN=0
FORCE=0
CMDLINE_IN=""
# ODL-332: the image a real tenant boots. It differs from the bench in three
# places only — a bootstrap key beside the blob (the build host has no AMD PSP,
# so the disk cannot be chip-sealed here; the first boot seals it), Servury's
# real snpreport installed in the root for that seal, and the tenant network
# resolver instead of QEMU's — and one thing is removed: the baked QEMU
# 10.0.2.15/24 unit, so the kernel `ip=` line and the panel's line decide.
PRODUCTION=0
# ODL-454 follow-up: the bench rehearsal of a real first boot. The disk carries
# the build-time bootstrap key and no blob (exactly the state a Blackbox sees,
# because a build host has no AMD PSP to seal to), and the seal unit runs with
# the bench stand-in so the first boot re-keys the volume, seals the guest key
# and shreds the bootstrap key. Production has its own switch; this one is for
# the bench, where the "chip" is the stand-in from sealed-initrd.sh.
FIRSTBOOT_SEAL=${ODILE_FIRSTBOOT_SEAL:-0}
TENANT_DNS=${ODILE_TENANT_DNS:-1.1.1.1 9.9.9.9}
TENANT_IP=${ODILE_TENANT_IP:-10.0.2.15}
MNT=${ODILE_SEALED_ROOTFS:-/var/tmp/odile-sealed-rootfs}

usage() {
    cat <<'EOF'
usage: sealed-image.sh [--out DIR] [--size SIZE] [--suite SUITE] [--mirror URL]
                       [--rootfs DIR] [--password PW] [--seal-id TOKEN]
                       [--cmdline-file FILE] [--production] [--first-boot-seal]
                       [--no-guest-smoke] [--force] [--dry-run]

  --out DIR       output directory (default: deploy/image/out-sealed)
  --size SIZE     raw image size (default: 8G)
  --suite SUITE   Debian suite (default: trixie)
  --mirror URL    Debian mirror (default: http://deb.debian.org/debian)
  --rootfs DIR    chroot build tree, outside the repo (default:
                  /var/tmp/odile-sealed-rootfs, $ODILE_SEALED_ROOTFS)
  --password PW   bench Webapp password baked as a scrypt hash
  --seal-id TOK   the per-tenant token placed in the measured command line
                  (default: odl454-bench-v1); cannot be combined with
                  --cmdline-file
  --cmdline-file F use the exact measured line in FILE (one line); the boot
                 key is the public key already in bb.key=, not a new one
  --production   the real-tenant image: write the bootstrap key that lets the
                 first boot open the disk (the build host has no AMD PSP, so
                 the disk cannot be sealed to the tenant's chip here), install
                 the seal-provision unit, drop the QEMU network unit and use
                 --production's resolver. The disk is re-keyed on its first boot.
  --first-boot-seal
                 the bench rehearsal of that first boot: the same bootstrap
                 key and no blob, and the seal unit runs with the bench
                 stand-in in place of Servury's snpreport. Not a tenant image.
  --no-guest-smoke do not enable the boot-time smoke unit
  --force         overwrite an existing raw image and build tree
  --dry-run       print the plan and change nothing
EOF
}

say() { printf '%-12s %s\n' "$1" "$2"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT=$2; shift 2 ;;
        --size) SIZE=$2; shift 2 ;;
        --suite) SUITE=$2; shift 2 ;;
        --mirror) MIRROR=$2; shift 2 ;;
        --rootfs) MNT=$2; shift 2 ;;
        --password) PASSWORD=$2; shift 2 ;;
        --seal-id) SEAL_ID=$2; SEAL_ID_SET=1; shift 2 ;;
        --cmdline-file) CMDLINE_IN=$2; shift 2 ;;
        --production) PRODUCTION=1; shift ;;
        --first-boot-seal) FIRSTBOOT_SEAL=1; shift ;;
        --no-guest-smoke) GUEST_SMOKE=0; shift ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "sealed-image: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

RAW=$OUT/odile-tenant-sealed.raw
BOOT=$OUT/boot
SEALD=$OUT/sealed
SNP=$OUT/bench-snpreport
CMDLINE_FILE=$BOOT/cmdline
if [ "$PRODUCTION" = 1 ] && [ "$FIRSTBOOT_SEAL" = 1 ]; then
    echo "sealed-image: --production and --first-boot-seal are mutually exclusive" >&2
    exit 2
fi
if [ -n "$CMDLINE_IN" ] && [ "${SEAL_ID_SET:-0}" = 1 ]; then
    echo "sealed-image: --seal-id and --cmdline-file are mutually exclusive" >&2
    exit 2
fi
if [ -n "$CMDLINE_IN" ]; then
    [ -s "$CMDLINE_IN" ] || { echo "sealed-image: no command line at $CMDLINE_IN" >&2; exit 2; }
    # Read it before anything writes boot/cmdline: a rebuild-from-pin call
    # passes the very file this script is about to rewrite.
    GIVEN_CMDLINE=$(head -n 1 "$CMDLINE_IN")
    case " $GIVEN_CMDLINE " in
        *" root=/dev/mapper/root_crypt "*) ;;
        *) echo "sealed-image: $CMDLINE_IN does not start the root on /dev/mapper/root_crypt" >&2; exit 2 ;;
    esac
    case " $GIVEN_CMDLINE " in
        *" seal_id="*) ;;
        *) echo "sealed-image: $CMDLINE_IN has no seal_id= token" >&2; exit 2 ;;
    esac
    case " $GIVEN_CMDLINE " in
        *" bb.key="*) ;;
        *) echo "sealed-image: $CMDLINE_IN has no bb.key= token" >&2; exit 2 ;;
    esac
    # The release installer's boot check (ODL-396) reads the boot report's
    # command line and requires exactly one bb.ip4=; it records the machine's
    # address in /etc/odile/boot-record.json and exits 5 without it. Servury's
    # own line carries bb.ip4/bb.gw4/bb.ip6/bb.gw6/bb.dns, so a composed line
    # must too: a line without them installs the seed, then dies at the boot
    # check with no Caddy and no /verify (measured on ODL-332 round 4).
    IP4S=$(printf '%s\n' "$GIVEN_CMDLINE" | tr ' ' '\n' | sed -n 's/^bb\.ip4=//p' | wc -l)
    [ "$IP4S" -eq 1 ] || {
        echo "sealed-image: $CMDLINE_IN carries $IP4S bb.ip4= token(s); the installer's boot check requires exactly one" >&2
        exit 2
    }
fi
if [ "$DRY_RUN" = 1 ]; then
    if [ -n "$CMDLINE_IN" ]; then
        say would "reuse the measured line in $CMDLINE_IN and the public key already in bb.key= (no keygen)"
    else
        say would "generate a throwaway Ed25519 key, shred its private half, put only the public key in boot/cmdline"
    fi
    say would "truncate $SIZE $RAW and write GPT: bios-boot, 512 MiB ext4 boot, LUKS2 root"
    say would "cryptsetup luksFormat the root with a random 64-byte V, then debootstrap $SUITE into it"
    say would "run install-stack.sh twice under chroot ($([ "$GUEST_SMOKE" = 1 ] && echo 'with guest smoke' || echo 'no guest smoke'))"
    say would "install odile-firstboot.service (boot: odile.token -> /seed -> install-stack.sh --seed -> first-visit page)"
    say would "copy the release installer, its signed manifest pair, the listener and odile-manifest.service from release/ (ODL-472)"
    say would "seal V to boot/cmdline and write /odile/luks-vol.key.sealed on the boot partition"
    say would "prove the sealed key opens the LUKS container before committing"
    if [ "$PRODUCTION" = 1 ]; then
        say would "write the first-boot bootstrap key /odile/luks-bootstrap.key beside the blob (production: the disk seals to the chip on its first boot)"
        say would "install odile-seal-provision.service and Servury's real snpreport, drop the QEMU network unit, resolver: $TENANT_DNS"
    fi
    if [ "$FIRSTBOOT_SEAL" = 1 ]; then
        say would "write the first-boot bootstrap key /odile/luks-bootstrap.key and no blob (bench rehearsal: the hook opens with the build key, the booted system re-keys the volume)"
        say would "install odile-seal-provision.service with the bench stand-in as its snpreport (the first boot re-keys the volume)"
    fi
    say would "write $SEALD/seal.txt"
    exit 0
fi

[ -s "$BOOT/vmlinuz" ] && [ -s "$BOOT/initrd.img" ] || {
    echo "sealed-image: $BOOT/{vmlinuz,initrd.img} missing (run sealed-initrd.sh first)" >&2
    exit 2
}
[ -x "$SNP" ] || {
    echo "sealed-image: $SNP missing (run sealed-initrd.sh first)" >&2
    exit 2
}
[ -x "$HERE/install-stack.sh" ] || { echo "sealed-image: install-stack.sh is not executable" >&2; exit 2; }
for tool in debootstrap sfdisk losetup mkfs.ext4 mount chroot cryptsetup blkid \
            ssh-keygen base64 shred rsync; do
    command -v "$tool" >/dev/null 2>&1 || { echo "sealed-image: missing $tool" >&2; exit 2; }
done
[ "$(id -u)" -eq 0 ] || { echo "sealed-image: must run as root (loop mounts and chroot)" >&2; exit 2; }

# The host's / is a shared mount (the systemd default). A mount made under a
# shared parent propagates into every other mount namespace on the box, and an
# unmount in ours leaves the peer copies holding the dm device open: measured
# 2026-11-10, `cryptsetup close` answered "still in use" for 60 s, the build
# exited 3, and the mount table of the build's own namespace was empty (the
# holders were other processes' namespaces).
#
# Running `mount --make-rprivate /` in the host namespace is NOT the fix: it
# makes the *host* tree private. Measured 2026-11-10 on this box — the demo
# host's home mount read `private` after the build and its reconcile failed
# until `mount --make-shared` on that mount; a second report had a
# build bind-mounting the host devtmpfs and then `rm -rf`ing it. The build
# therefore re-execs itself inside a private mount namespace before its first
# mount. Everything below — loop partitions, the mapper root, the /proc, /dev
# and /sys bind mounts — lives and dies in that namespace; the host tree is
# never touched.
if [ "${ODILE_SEALED_IN_NS:-0}" != 1 ]; then
    command -v unshare >/dev/null 2>&1 || {
        echo "sealed-image: unshare is missing (util-linux); the build needs a private mount namespace" >&2
        exit 2
    }
    ODILE_SEALED_IN_NS=1 exec unshare -m --propagation private "$0" "${ORIG_ARGS[@]}"
fi
say ns "private mount namespace (unshare -m --propagation private)"
# Belt to the namespace brace: already private here, repeated so a caller that
# reuses the namespace still gets no propagation.
mount --make-rprivate / 2>/dev/null || true

if [ -e "$RAW" ] && [ "$FORCE" != 1 ]; then
    echo "sealed-image: $RAW exists; pass --force to rebuild it" >&2
    exit 2
fi
mkdir -p "$OUT" "$BOOT" "$SEALD"

# 0. The boot key. Either generated and shredded here, or — with
#    --cmdline-file — the public half already named by the measured line.
KEY=$BOOT/thrownaway-key
rm -f "$KEY" "$KEY.pub"
if [ -n "$CMDLINE_IN" ]; then
    BB_KEY=$(printf '%s\n' "$GIVEN_CMDLINE" | tr ' ' '\n' | sed -n 's/^bb.key=//p')
    printf '%s\n' "$BB_KEY" | base64 -d > "$KEY.pub" 2>/dev/null || {
        echo "sealed-image: bb.key= in $CMDLINE_IN is not base64" >&2; exit 2; }
    PUBFP=$(ssh-keygen -lf "$KEY.pub" 2>/dev/null | awk '{print $2}') || {
        echo "sealed-image: bb.key= in $CMDLINE_IN is not an SSH public key" >&2; exit 2; }
    CMDLINE=$GIVEN_CMDLINE
    SEAL_ID=$(printf '%s\n' "$CMDLINE" | tr ' ' '\n' | sed -n 's/^seal_id=//p')
    say key "$PUBFP (reused from the measured line; zero usable user keys)"
else
    say keygen "throwaway Ed25519 boot key (private half shredded)"
    ssh-keygen -q -t ed25519 -N '' -C 'odile-bench throwaway boot key' -f "$KEY"
    PUBFP=$(ssh-keygen -lf "$KEY.pub" | awk '{print $2}')
    BB_KEY=$(base64 -w0 < "$KEY.pub")
    shred -u "$KEY"
    [ ! -e "$KEY" ] || { echo "sealed-image: the throwaway private key survived shred" >&2; exit 1; }
    say key "$PUBFP (private half shredded; zero usable user keys)"
    # The measured line. Every token here is inside the UKI's .cmdline and is
    # the only per-tenant input the bench stand-in derives its key from.
    CMDLINE="root=/dev/mapper/root_crypt ro console=tty0 console=ttyS0,115200 consoleblank=0 systemd.show_status=1 $IP_TOKEN bb.release=$BB_RELEASE bb.host=$GUEST_NAME bb.iid=$IID bb.key=$BB_KEY seal_id=$SEAL_ID"
    # The first-boot channel (ODL-456): the instance id is already bb.iid; the
    # one-time setup token and the seed endpoint are measured alongside it when
    # the caller supplies them. A real tenant's line is composed at provision
    # and the disk sealed to it (--cmdline-file); the bench passes them here.
    if [ -n "${ODILE_SEED_TOKEN:-}" ]; then
        printf '%s' "$ODILE_SEED_TOKEN" | grep -Eq '^[0-9a-f]{64}$' || {
            echo "sealed-image: ODILE_SEED_TOKEN must be 64 lowercase hex" >&2; exit 2; }
        CMDLINE="$CMDLINE odile.token=$ODILE_SEED_TOKEN"
    fi
    if [ -n "${ODILE_SEED_URL:-}" ]; then
        printf '%s' "$ODILE_SEED_URL" | grep -Eq '^https?://[^ ]+$' || {
            echo "sealed-image: ODILE_SEED_URL is not an http(s) URL" >&2; exit 2; }
        CMDLINE="$CMDLINE odile.seed_url=$ODILE_SEED_URL"
    fi
fi
printf '%s\n' "$CMDLINE" > "$CMDLINE_FILE"
say cmdline "seal_id=$SEAL_ID, bb.host=$GUEST_NAME, bb.key=$PUBFP"

# 1. The disk: a 1 MiB BIOS-boot placeholder keeps the kernel's /dev/sda2 and
#    /dev/sda3 names identical to a Blackbox, so the hook's defaults are the
#    paths the boot actually uses.
say create "$RAW ($SIZE)"
rm -f "$RAW"
truncate -s "$SIZE" "$RAW"
sfdisk --quiet "$RAW" <<'EOF'
label: gpt
start=2048, size=2048, type=21686148-6449-6E6F-744E-656564454649, name="bios-boot"
start=4096, size=1048576, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="odile-boot"
start=1052672, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="odile-root"
EOF

LOOP=""
MAPPER=""
BOOT_MNT=""
HOST_DAMAGE=0
VOLKEY=""
KEYOUT=""
# Every mount point at or under $1, deepest first, straight from
# /proc/self/mounts. `findmnt -R "$MNT"` is not usable here: once the top
# mount is gone it reports nothing even though children are still mounted, and
# the `rm -rf` then recurses through a live /dev or /sys bind and deletes the
# *host's* device nodes (measured 2026-11-10: seven builds, /dev/loop* lost
# each time, the mapper unable to close, and exit 3 masking the damage).
mounts_under() {
    awk -v m="$1" '{ p=$2; gsub(/\\040/, " ", p); if (p == m || index(p, m "/") == 1) print p }' \
        /proc/self/mounts |
        awk '{ print gsub(/\//, "/") " " $0 }' | sort -rn | cut -d' ' -f2-
}

host_nodes_ok() {
    # A device node, not merely a file: a shell redirect recreates a missing
    # /dev/null as an ordinary file, which passes `-e` and hides the damage.
    if ! grep -q '^cgroup2 /sys/fs/cgroup ' /proc/mounts; then
        echo "sealed-image: HOST DAMAGE: /sys/fs/cgroup is not a cgroup2 mount" >&2
        HOST_DAMAGE=1
    fi
    for node in /dev/null /dev/tty /dev/urandom /dev/loop-control; do
        [ -c "$node" ] || { echo "sealed-image: HOST DAMAGE: $node is missing or not a device node" >&2; HOST_DAMAGE=1; }
    done
}

cleanup() {
    set +e
    # Unmount everything under MNT, deepest first: /dev/pts, /dev/shm,
    # /sys/fs/cgroup and friends are separate mounts inside the rbind copies,
    # and the parent cannot go until they do. Lazy unmount is the last resort
    # only: a lazy mount leaves the mapper open and `cryptsetup close` then
    # fails for a minute and blames a leak.
    for _pass in 1 2 3; do
        mapfile -t _targets < <(mounts_under "$MNT")
        [ "${#_targets[@]}" -eq 0 ] && break
        for _target in "${_targets[@]}"; do
            umount "$_target" 2>/dev/null || umount -l "$_target" 2>/dev/null
        done
        sleep 1
    done
    command -v udevadm >/dev/null 2>&1 && udevadm settle --timeout=15 >/dev/null 2>&1
    sync
    sleep 2
    host_nodes_ok
    leftover=$(mounts_under "$MNT" | head -n 1)
    if [ -n "$leftover" ]; then
        echo "sealed-image: $MNT still has a mount at $leftover; leaving the tree in place" >&2
        HOST_DAMAGE=1
        leftover=1
    fi
    # A close immediately after umount can race the kernel's release: measured
    # 2026-11-10, one build left dm-1 and its loop open while exiting 0. Retry,
    # then say so instead of leaking a mapper into the next run.
    # A device node or a /dev/mapper symlink survives the close; ask the
    # mapper and the loop whether they are actually still attached.
    if [ -n "$MAPPER" ]; then
        # Wait for the mapper to disappear, not merely for `close` to return:
        # status can still answer for a moment after the device is gone.
        for _ in $(seq 1 60); do
            cryptsetup status "${MAPPER##*/}" >/dev/null 2>&1 || break
            cryptsetup close "${MAPPER##*/}" >/dev/null 2>&1
            sleep 1
        done
        cryptsetup status "${MAPPER##*/}" >/dev/null 2>&1 && {
            echo "sealed-image: LEAK: ${MAPPER##*/} is still open" >&2
            HOST_DAMAGE=1
        }
    fi
    if [ -n "$BOOT_MNT" ] && mountpoint -q "$BOOT_MNT" 2>/dev/null; then
        umount "$BOOT_MNT" 2>/dev/null
    fi
    [ -n "$BOOT_MNT" ] && rmdir "$BOOT_MNT" 2>/dev/null
    if [ -n "$LOOP" ]; then
        for _ in $(seq 1 60); do
            losetup "$LOOP" >/dev/null 2>&1 || break
            losetup -d "$LOOP" >/dev/null 2>&1
            sleep 1
        done
        if losetup "$LOOP" >/dev/null 2>&1; then
            echo "sealed-image: note: $LOOP still attached; it releases with this process" >&2
        fi
    fi
    # Never `rm -rf` through a live mount: that is what deleted the host's
    # /dev/loop* nodes on every build before 2026-11-10. $leftover is 1 only
    # when the table above was non-empty.
    if [ "${leftover:-}" = 1 ]; then
        :
    else
        rm -rf "$MNT"
        host_nodes_ok
    fi
    [ -n "$VOLKEY" ] && shred -u "$VOLKEY" 2>/dev/null
    [ -n "$KEYOUT" ] && shred -u "$KEYOUT" 2>/dev/null
    if [ "$HOST_DAMAGE" = 1 ]; then
        exit 3
    fi
}
trap cleanup EXIT

if [ -d "$MNT" ] && [ -n "$(ls -A "$MNT" 2>/dev/null)" ]; then
    if [ "$FORCE" = 1 ]; then
        rm -rf "$MNT"
    else
        echo "sealed-image: $MNT is not empty; pass --force to discard it" >&2
        exit 2
    fi
fi

LOOP=$(losetup -Pf --show "$RAW")
say loop "$LOOP"
for _ in $(seq 1 40); do
    [ -b "${LOOP}p3" ] && break
    sleep 0.25
done
[ -b "${LOOP}p3" ] || { echo "sealed-image: ${LOOP}p3 never appeared" >&2; exit 1; }

mkfs.ext4 -q -L odile-boot "${LOOP}p2"
BOOT_MNT=$(mktemp -d /var/tmp/odile-boot.XXXXXX)
mount "${LOOP}p2" "$BOOT_MNT"
say boot-part "${LOOP}p2 -> $BOOT_MNT"

VOLKEY=$(mktemp /var/tmp/odile-volkey.XXXXXX)
head -c 64 /dev/urandom > "$VOLKEY"
chmod 600 "$VOLKEY"
say luks "luksFormat ${LOOP}p3 (LUKS2, V is the only keyslot)"
cryptsetup --batch-mode --type luks2 --key-file "$VOLKEY" --label odile-root luksFormat "${LOOP}p3"
cryptsetup --key-file "$VOLKEY" luksOpen "${LOOP}p3" "odile-sealed-$$"
MAPPER=/dev/mapper/odile-sealed-$$
mkfs.ext4 -q -L odile-root "$MAPPER"
mkdir -p "$MNT"
mount "$MAPPER" "$MNT"
say mount "$MAPPER -> $MNT"

# udev is not decoration. debootstrap's minbase does not install it, and
# without systemd-udevd the first boot's `cryptsetup reencrypt` blocks forever
# in libdevmapper's udev wait (the dm table swap never gets its completion
# event), so the volume is never re-keyed. The same absence costs the image its
# serial getty: /dev/ttyS0 never becomes a systemd device unit, so no drill
# shell on the console. Found on the ODL-454 follow-up bench, 2026-11-13.
INCLUDE=ca-certificates,cryptsetup-bin,curl,dbus,git,iproute2,libatomic1,libstdc++6,openssl,passwd,procps,python3,systemd-sysv,udev,util-linux,xz-utils,sudo,openssh-server
KEYRING=()
if [ -f /usr/share/keyrings/debian-archive-keyring.gpg ]; then
    KEYRING=(--keyring=/usr/share/keyrings/debian-archive-keyring.gpg)
fi
say debootstrap "$SUITE ($INCLUDE)"
debootstrap --variant=minbase --arch=amd64 --components=main \
    "${KEYRING[@]}" --include="$INCLUDE" "$SUITE" "$MNT" "$MIRROR"

install -d "$MNT/usr/local/lib/odile-image"
for f in install-stack.sh bench-heartbeat.sh bench.py smoke.sh odile-firstboot.py bench-attest.py recovery-seed.py odile-webapp-brand.py; do
    install -m 0755 "$HERE/$f" "$MNT/usr/local/lib/odile-image/$f"
done
for f in config.yaml tenant.service odile-bench-smoke.service \
         odile-bench-proxy.service odile-bench-proxy.socket \
         odile-bench-proxy-firstvisit.service odile-bench-proxy-firstvisit.socket \
         odile-bench-attest.service odile-attest-boot.service odile-access-report.service \
         odile-bench-proxy-manifest.service odile-bench-proxy-manifest.socket \
         odile-firstboot.service odile-first-visit.service \
         odile-webapp-brand.service odile-webapp-brand.path \
         odile-webapp-brand.conf webapp-brand.js \
         webapp-chrome.css webapp-chrome.js \
         odile-access-report-user.conf; do
    install -m 0644 "$HERE/$f" "$MNT/usr/local/lib/odile-image/$f"
done
# The first-visit page and service (ODL-457), the /verify page and the two
# programs that write and read its report (ODL-473), and the boot-report
# transformer (ODL-397). They are installed only into the image root; the
# first boot installs them to /usr/local/lib/odile/ with the seed, so a
# machine that never seeds never exposes a page. Absent on a tree where the
# card has not merged: the image still builds, the first boot skips it.
for pair in "installer/first_visit.py:first_visit.py:0755" \
            "web/first-visit.html:first-visit.html:0644" \
            "web/verify.html:verify.html:0644" \
            "installer/access_report.sh:access_report.sh:0755" \
            "scripts/verify_and_unlock.sh:verify_and_unlock.sh:0755" \
            "attest.py:attest.py:0755"; do
    src=${pair%%:*}; rest=${pair#*:}; dst=${rest%%:*}; mode=${rest##*:}
    if [ -f "$REPO/$src" ]; then
        install -m "$mode" "$REPO/$src" "$MNT/usr/local/lib/odile-image/$dst"
        say first-visit "$src -> /usr/local/lib/odile-image/$dst"
    else
        say first-visit "$src absent (the first boot will not install a page)"
    fi
done
# The seed response is signed by the seed key, which the release key certifies
# (ODL-514). The image bakes the release public key, and the key being retired
# beside it for one release, so a certificate cut by either verifies.
install -d "$MNT/etc/odile"
install -m 0644 "$REPO/installer/odile-signing-key.pub" "$MNT/etc/odile/odile-signing-key.pub"
say signing-key "$(sha256sum "$MNT/etc/odile/odile-signing-key.pub" | cut -d' ' -f1)"
if [ -f "$REPO/installer/odile-signing-key.prev.pub" ]; then
    install -m 0644 "$REPO/installer/odile-signing-key.prev.pub" \
        "$MNT/etc/odile/odile-signing-key.prev.pub"
    say signing-key "previous key $(sha256sum "$MNT/etc/odile/odile-signing-key.prev.pub" | cut -d' ' -f1)"
fi

# The release the image carries (ODL-472): the exact installer, the signed
# manifest pair and the listener, copied from release/ so the first boot has
# the release's own bytes to place at /usr/local/bin/odile-install.sh and
# /etc/odile/manifest.json. Three relations are proved before the copy: the
# manifest's signature verifies with the published key, the manifest names
# this installer's bytes, and the installer's own detached signature verifies.
# Without them the image would ship a manifest that describes another program.
RELEASE=$REPO/release
for name in odile_install.py odile_install.py.sig manifest.json manifest.json.sig; do
    [ -s "$RELEASE/$name" ] || { echo "sealed-image: release/$name is missing (ODL-472)" >&2; exit 2; }
done
"$REPO/scripts/manifest.py" verify --manifest "$RELEASE/manifest.json" \
    --signature "$RELEASE/manifest.json.sig" \
    --pubkey "$REPO/installer/odile-signing-key.pub" >/dev/null || {
    echo "sealed-image: the release manifest pair does not verify" >&2; exit 2; }
RELEASE_INSTALLER_SHA=$(sha256sum "$RELEASE/odile_install.py" | cut -d' ' -f1)
MANIFEST_INSTALLER_SHA=$(/usr/bin/python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["installer_sha256"])' \
    "$RELEASE/manifest.json")
[ "$RELEASE_INSTALLER_SHA" = "$MANIFEST_INSTALLER_SHA" ] || {
    echo "sealed-image: the release manifest names installer $MANIFEST_INSTALLER_SHA," \
         "not the release's $RELEASE_INSTALLER_SHA" >&2; exit 2; }
openssl dgst -verify "$REPO/installer/odile-signing-key.pub" \
    -signature <(base64 -d "$RELEASE/odile_install.py.sig") \
    "$RELEASE/odile_install.py" >/dev/null || {
    echo "sealed-image: the release installer's signature does not verify" >&2; exit 2; }
install -d "$MNT/usr/local/lib/odile-image"
install -m 0755 "$RELEASE/odile_install.py" "$MNT/usr/local/lib/odile-image/odile_install.py"
install -m 0644 "$RELEASE/odile_install.py.sig" "$MNT/usr/local/lib/odile-image/odile_install.py.sig"
install -m 0644 "$RELEASE/manifest.json" "$MNT/usr/local/lib/odile-image/manifest.json"
install -m 0644 "$RELEASE/manifest.json.sig" "$MNT/usr/local/lib/odile-image/manifest.json.sig"
install -m 0755 "$REPO/scripts/manifest.py" "$MNT/usr/local/lib/odile-image/manifest.py"
install -m 0755 "$HERE/odile-manifest.py" "$MNT/usr/local/lib/odile-image/odile-manifest.py"
install -m 0644 "$HERE/odile-manifest.service" "$MNT/usr/local/lib/odile-image/odile-manifest.service"
say release "installer $(printf '%s' "$RELEASE_INSTALLER_SHA" | cut -c1-12)…, manifest $(sha256sum "$RELEASE/manifest.json" | cut -c1-12)…, listener manifest.py, unit odile-manifest.service"
# The booted system's own copy of the pieces that explain the seal.
install -m 0755 "$HERE/../../spike/sealed-unlock/initramfs/bb-sealed-unlock" \
    "$MNT/usr/local/lib/odile-image/bb-sealed-unlock"
install -m 0755 "$HERE/../../spike/sealed-unlock/initramfs/check-sealed-boot.sh" \
    "$MNT/usr/local/sbin/check-sealed-boot.sh"
install -m 0755 "$SNP" "$MNT/usr/local/lib/odile-image/bench-snpreport"
# ODL-332: the booted system's own seal. bb-sealed-provision is the spike's
# script (proved against Servury's real snpreport on an SNP guest); the binary
# it calls is Servury's release snpreport, installed only when the initrd was
# built in production mode. The unit is enabled always: without the binary its
# ConditionPathExists is false and it never runs, so a bench image is unchanged.
install -m 0755 "$HERE/bb-sealed-provision" "$MNT/usr/local/lib/odile-image/bb-sealed-provision"
install -m 0644 "$HERE/odile-seal-provision.service" \
    "$MNT/etc/systemd/system/odile-seal-provision.service"
# ODL-332: the production image's second install half. The image's installer
# brings the page up; the release installer (placed by the first boot) then
# converges Caddy/ACME, /verify and the manifest. The unit is enabled only in
# production and conditions on the marker this block writes.
if [ "$PRODUCTION" = 1 ]; then
    install -m 0755 "$HERE/odile-release-setup.sh" \
        "$MNT/usr/local/lib/odile-image/odile-release-setup.sh"
    install -m 0644 "$HERE/odile-release-setup.service" \
        "$MNT/etc/systemd/system/odile-release-setup.service"
    install -d "$MNT/etc/odile"
    printf 'production image; the tenant install runs the release installer\n' \
        > "$MNT/etc/odile/production"
    chroot "$MNT" systemctl enable odile-release-setup.service >/dev/null
    say unit "odile-release-setup.service (release installer: Caddy, /verify, manifest)"
fi
if [ -f "$OUT/real-snpreport" ] && [ "$FIRSTBOOT_SEAL" = 0 ]; then
    install -m 0755 "$OUT/real-snpreport" "$MNT/usr/local/lib/odile-image/snpreport"
    say snpreport "Servury's release binary -> /usr/local/lib/odile-image/snpreport (first-boot seal)"
elif [ "$FIRSTBOOT_SEAL" = 1 ]; then
    # The bench has no AMD PSP: the booted system's seal must use the stand-in
    # (it derives from the measured command line, ODL-454 §1), or the unit is
    # inert and the re-key never runs. A tenant image installs the real binary.
    install -m 0755 "$SNP" "$MNT/usr/local/lib/odile-image/snpreport"
    say snpreport "the bench stand-in -> /usr/local/lib/odile-image/snpreport (first-boot re-key)"
else
    say snpreport "no $OUT/real-snpreport (bench build; the first-boot seal unit stays inert)"
fi
install -d "$MNT/etc/odile"
printf '%s\n' "$SEAL_ID" > "$MNT/etc/odile/seal-id"
install -m 0644 "$BOOT/thrownaway-key.pub" "$MNT/etc/odile/boot-key.pub"
# The strict bar's access report expects exactly one authorized key: this
# throwaway boot key, whose private half was shredded above. Root's
# authorized_keys is where the report and sshd both look.
install -d -m 0700 "$MNT/root/.ssh"
install -m 0600 "$BOOT/thrownaway-key.pub" "$MNT/root/.ssh/authorized_keys"

# The image's own boot and network configuration. The root is the mapper; the
# boot partition is deliberately not mounted at runtime, as on a Blackbox (the
# hook mounts it read-only, on demand, to read the blob).
{
    printf '/dev/mapper/root_crypt / ext4 defaults 0 1\n'
} > "$MNT/etc/fstab"
printf 'root_crypt /dev/sda3 none luks\n' > "$MNT/etc/crypttab"
printf '%s\n' "$GUEST_NAME" > "$MNT/etc/hostname"
printf '127.0.0.1 localhost %s tenant.odile.test\n%s %s tenant.odile.test\n' \
    "$GUEST_NAME" "$TENANT_IP" "$GUEST_NAME" > "$MNT/etc/hosts"
if [ "$PRODUCTION" = 1 ]; then
    # The tenant's resolver, not QEMU's 10.0.2.3. The line is baked because the
    # image has no systemd-resolved (a static file is the whole DNS setup), and
    # the interface itself comes from the measured cmdline's ip= through
    # systemd-network-generator.service (enabled below).
    : > "$MNT/etc/resolv.conf"
    for ns in $TENANT_DNS; do printf 'nameserver %s\n' "$ns" >> "$MNT/etc/resolv.conf"; done
else
    printf 'nameserver 10.0.2.3\n' > "$MNT/etc/resolv.conf"
fi

if [ "$PRODUCTION" = 1 ]; then
    # No baked 10.0.2.15/24: the kernel ip= line (measured) and the panel's
    # per-tenant line decide the address, so the same image boots on a Blackbox
    # and in QEMU (ip=dhcp there). A baked Name=en* static unit would win over
    # the generated one and give a tenant no network at all.
    say net "no baked network unit; the measured ip= line configures the interface"
else
    install -d "$MNT/etc/systemd/network"
    cat > "$MNT/etc/systemd/network/20-odile.network" <<'EOF'
[Match]
Name=en*

[Network]
Address=10.0.2.15/24
Gateway=10.0.2.2
DNS=10.0.2.3
EOF
fi

install -d "$MNT/etc/systemd/system/serial-getty@ttyS0.service.d"
cat > "$MNT/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin tenant --keep-baud 115200,38400,9600 %I $TERM
EOF

mount --bind /proc "$MNT/proc"
mount --rbind /dev "$MNT/dev"
mount --make-rslave "$MNT/dev"
mount --rbind /sys "$MNT/sys"
mount --make-rslave "$MNT/sys"
cp /etc/resolv.conf "$MNT/etc/resolv.conf"

install -m 0644 "$HERE/odile-firstboot.service" "$MNT/etc/systemd/system/odile-firstboot.service"
say unit "odile-firstboot.service (oneshot, before the tenant gateway)"
for unit in systemd-networkd.service systemd-network-generator.service \
            serial-getty@ttyS0.service odile-firstboot.service odile-seal-provision.service; do
    chroot "$MNT" systemctl enable "$unit" >/dev/null
done

say chroot "install-stack.sh ($([ "$GUEST_SMOKE" = 1 ] && echo 'with guest smoke' || echo 'no guest smoke'))"
chroot_env=(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=dumb)
install_args=(--password "$PASSWORD")
second_args=(--password "$PASSWORD")
# The guest smoke and its bench proxy sockets are the QEMU stand-ins; a
# production image has Caddy and the real boot report, so it takes neither.
if [ "$GUEST_SMOKE" = 1 ] && [ "$PRODUCTION" != 1 ]; then
    install_args+=(--guest-smoke)
    second_args+=(--guest-smoke)
fi
"${chroot_env[@]}" chroot "$MNT" /usr/local/lib/odile-image/install-stack.sh "${install_args[@]}"
say idempotent "second install-stack run (must print same)"
"${chroot_env[@]}" chroot "$MNT" /usr/local/lib/odile-image/install-stack.sh "${second_args[@]}"

# The bench resolver is QEMU's, not this builder's. The tenant's is not.
if [ "$PRODUCTION" = 1 ]; then
    : > "$MNT/etc/resolv.conf"
    for ns in $TENANT_DNS; do printf 'nameserver %s\n' "$ns" >> "$MNT/etc/resolv.conf"; done
else
    printf 'nameserver 10.0.2.3\n' > "$MNT/etc/resolv.conf"
fi

# 2. Seal V to the command line and write the blob on the boot partition.
KEY_ID=$("$SNP" --cmdline-file "$CMDLINE_FILE" --key-id)
BLOB=$BOOT/luks-vol.key.sealed
"$SNP" --cmdline-file "$CMDLINE_FILE" --seal "$VOLKEY" "$BLOB"
mkdir -p "$BOOT_MNT/odile"
if [ "$FIRSTBOOT_SEAL" = 1 ]; then
    # The rehearsal of a real first boot: the disk carries the build-time
    # bootstrap key and NO blob, which is the state a Blackbox sees (a build
    # host cannot seal to a chip that is not there). The hook opens with the
    # bootstrap key and says `opened-bootstrap`; the booted system's
    # bb-sealed-provision re-keys the volume and seals V to this "chip".
    install -m 0600 "$VOLKEY" "$BOOT_MNT/odile/luks-bootstrap.key"
    say bootstrap "/odile/luks-bootstrap.key (first boot only; the first boot re-keys the volume)"
else
    install -m 0600 "$BLOB" "$BOOT_MNT/odile/luks-vol.key.sealed"
    printf '0\n' > "$BOOT_MNT/odile/luks-vol.keyslot"
    chmod 600 "$BOOT_MNT/odile/luks-vol.keyslot"
fi

# 3. The round trip, before the disk is committed: unseal the blob with the
#    command line, and ask LUKS whether those bytes open the container.
KEYOUT=$(mktemp /var/tmp/odile-keyout.XXXXXX)
chmod 600 "$KEYOUT"
"$SNP" --cmdline-file "$CMDLINE_FILE" --unseal "$BLOB" "$KEYOUT"
cmp -s "$VOLKEY" "$KEYOUT" || { echo "sealed-image: the unsealed key is not V" >&2; exit 1; }
cryptsetup luksOpen --test-passphrase --key-file "$KEYOUT" "${LOOP}p3" || {
    echo "sealed-image: the sealed key does not open the LUKS container" >&2
    exit 1
}
say verify "seal -> unseal -> luksOpen --test-passphrase on ${LOOP}p3"
# ODL-332: the first boot of a real tenant has no chip-sealed blob yet (this
# host has no AMD PSP). The as-built volume key goes on the boot partition
# beside the blob; the hook uses it only when the blob is absent or does not
# unseal, and the booted system's odile-seal-provision replaces it with a
# chip-derived one: the volume is re-keyed (a new master key on the guest,
# which removes this keyslot and the build-time volume key with it), V is
# sealed to the chip and this file is shredded. It is not a standing
# credential: it is dead after the first boot, and the disk it opens is the
# fresh, publicly downloadable image.
if [ "$PRODUCTION" = 1 ]; then
    install -m 0600 "$VOLKEY" "$BOOT_MNT/odile/luks-bootstrap.key"
    say bootstrap "/odile/luks-bootstrap.key (first boot only; the first boot re-keys the volume)"
fi
shred -u "$KEYOUT"
KEYOUT=""
shred -u "$VOLKEY"
VOLKEY=""

sync
cleanup
trap - EXIT
LOOP=""
MAPPER=""
BOOT_MNT=""
rm -rf "$MNT"

# The pin names the disk by hash; 8 GiB of zeros and a small root read fast.
DISK_SHA=$(sha256sum "$RAW" | cut -d' ' -f1)
{
    printf 'seal_id=%s\n' "$SEAL_ID"
    printf 'cmdline=%s\n' "$CMDLINE"
    printf 'cmdline_sha256=%s\n' "$(sha256sum "$CMDLINE_FILE" | cut -d' ' -f1)"
    printf 'key_id=%s\n' "$KEY_ID"
    printf 'blob_sha256=%s\n' "$(sha256sum "$BLOB" | cut -d' ' -f1)"
    printf 'keyslot=0\n'
    printf 'boot_key=%s\n' "$PUBFP"
    printf 'boot_key_pub=%s\n' "$(cat "$BOOT/thrownaway-key.pub")"
    printf 'private_half=shredded\n'
    printf 'production=%s\n' "$([ "$PRODUCTION" = 1 ] && echo yes || echo no)"
    if [ "$PRODUCTION" = 1 ]; then
        printf 'bootstrap_key=/odile/luks-bootstrap.key\n'
        printf 'first_boot_seal=odile-seal-provision.service (reencrypts the volume with a guest key, removes the build keyslot, seals V, shreds the bootstrap key)\n'
        printf 'resolver=%s\n' "$TENANT_DNS"
    fi
    if [ "$FIRSTBOOT_SEAL" = 1 ]; then
        printf 'bootstrap_key=/odile/luks-bootstrap.key\n'
        printf 'first_boot_seal=odile-seal-provision.service (bench stand-in; reencrypts the volume with a guest key, removes the build keyslot, seals V, shreds the bootstrap key)\n'
    fi
    printf 'disk=%s\n' "$RAW"
    printf 'disk_sha256=%s\n' "$DISK_SHA"
} > "$SEALD/seal.txt"

say sealed "$BLOB ($(stat -c %s "$BLOB") bytes)"
say next "./deploy/image/uki.sh --in $(printf '%q' "$OUT") --sign"
