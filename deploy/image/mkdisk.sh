#!/bin/bash
# mkdisk.sh — build a bootable Debian 13 disk image with the tenant stack.
#
# The image is the second artefact of the local image bench (ODL-380). It is
# built from the same definition as the container: debootstrap Debian 13, copy
# deploy/image/ into the root, run install-stack.sh under chroot, then install
# GRUB and a serial console. The tenant install is idempotent; a second
# install-stack run prints `same` and changes nothing.
#
#   ./deploy/image/mkdisk.sh --dry-run
#   sudo ./deploy/image/mkdisk.sh --out deploy/image/out --size 8G
#
# Outputs:
#   out/odile-tenant.raw     the bootable raw disk (GPT: BIOS boot + ext4 root)
#   out/odile-tenant.qcow2   the same disk, compressed for copying
#   out/boot/vmlinuz         the kernel, for smoke.sh and uki.sh
#   out/boot/initrd.img      the initramfs, for smoke.sh and uki.sh
#   out/boot/os-release      the image's own /etc/os-release, for uki.sh
#   out/boot/cmdline         the measured command line, for uki.sh
#   out/boot/uname           the kernel release, for ukify
#   out/boot.log             written later by smoke.sh image
#
# Exit codes: 0 ok, 1 build failed, 2 bad input or a missing prerequisite.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT=$HERE/out
SIZE=8G
SUITE=trixie
MIRROR=http://deb.debian.org/debian
PASSWORD=${ODILE_BENCH_PASSWORD:-odile-bench-not-a-secret}
GUEST_SMOKE=1
DRY_RUN=0
FORCE=0

usage() {
    cat <<'EOF'
usage: mkdisk.sh [--out DIR] [--size SIZE] [--suite SUITE] [--mirror URL]
                 [--rootfs DIR] [--password PW] [--no-guest-smoke] [--force]
                 [--dry-run]

  --out DIR        output directory (default: deploy/image/out)
  --size SIZE      raw image size (default: 8G)
  --suite SUITE    Debian suite (default: trixie)
  --mirror URL     Debian mirror (default: http://deb.debian.org/debian)
  --rootfs DIR     chroot build tree, outside the repo (default:
                   /var/tmp/odile-image-rootfs, $ODILE_IMAGE_ROOTFS)
  --password PW    bench Webapp password baked as a scrypt hash
  --no-guest-smoke do not enable the boot-time smoke unit
  --force          overwrite an existing raw image and build tree
  --dry-run        print the plan and change nothing
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
        --no-guest-smoke) GUEST_SMOKE=0; shift ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "mkdisk: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

RAW=$OUT/odile-tenant.raw
QCOW=$OUT/odile-tenant.qcow2
# The chroot tree lives outside the repository on purpose: `make check` lints
# every .sh and compiles every .py under the tree, and a debootstrapped root
# has hundreds of both. out/ holds only the disk, its qcow2 copy and the boot
# files.
MNT=${ODILE_IMAGE_ROOTFS:-/var/tmp/odile-image-rootfs}
BOOT=$OUT/boot

for tool in debootstrap sfdisk losetup mkfs.ext4 mount chroot qemu-img blkid; do
    command -v "$tool" >/dev/null 2>&1 || { echo "mkdisk: missing $tool" >&2; exit 2; }
done
[ "$(id -u)" -eq 0 ] || { echo "mkdisk: must run as root (debootstrap and loop mounts)" >&2; exit 2; }
[ -x "$HERE/install-stack.sh" ] || { echo "mkdisk: $HERE/install-stack.sh is not executable" >&2; exit 2; }

if [ "$DRY_RUN" = 1 ]; then
    say would "debootstrap $SUITE from $MIRROR into $MNT ($SIZE raw image $RAW)"
    say would "copy deploy/image/ into the root and run install-stack.sh (Hermes v0.23.0)"
    say would "serial console: ttyS0 autologin tenant (bench drill shell)"
    say would "install GRUB (i386-pc) on $RAW and regenerate the initramfs"
    say would "write $QCOW with qemu-img convert"
    say would "record kernel, initramfs and command line under $BOOT"
    exit 0
fi

if [ -e "$RAW" ] && [ "$FORCE" != 1 ]; then
    echo "mkdisk: $RAW exists; pass --force to rebuild it" >&2
    exit 2
fi

mkdir -p "$OUT" "$BOOT"

LOOP=""
HOST_DAMAGE=0
cleanup() {
    set +e
    if mountpoint -q "$MNT" 2>/dev/null; then
        umount -R "$MNT" 2>/dev/null || umount -R -l "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null
    fi
    sleep 1
    # Host-damage assertion first, because the failure this guards is silent
    # until every systemd unit on the box stops working. A shared rbind plus
    # `umount -R` propagates the unmount back to the host: on 2026-11-08 that
    # removed /sys/fs/cgroup and the builder's /dev nodes. The rslave mounts
    # below are the fix; this is the alarm. Exit code 3 means "image built,
    # host damaged", never a quiet zero.
    if ! grep -q '^cgroup2 /sys/fs/cgroup ' /proc/mounts; then
        echo "mkdisk: HOST DAMAGE: /sys/fs/cgroup is not a cgroup2 mount after cleanup" >&2
        HOST_DAMAGE=1
    fi
    for node in /dev/null /dev/tty; do
        [ -e "$node" ] || { echo "mkdisk: HOST DAMAGE: $node is gone after cleanup" >&2; HOST_DAMAGE=1; }
    done
    # mountpoint -q is not enough: a failed umount -R can leave /proc, /dev
    # or /sys mounted *under* $MNT while $MNT itself is already detached.
    # findmnt -R lists the whole subtree, so this catches a live rbind before
    # the rm -rf recurses through it.
    leftover=$(findmnt -R -n -o TARGET "$MNT" 2>/dev/null | head -n 1)
    if [ -n "$leftover" ]; then
        echo "mkdisk: $MNT still has a mount at $leftover; leaving the tree in place" >&2
        return
    fi
    if [ -n "$LOOP" ]; then
        losetup -d "$LOOP" 2>/dev/null
    fi
    rm -rf "$MNT"
    if [ "$HOST_DAMAGE" = 1 ]; then
        exit 3
    fi
}
trap cleanup EXIT

if [ -d "$MNT" ] && [ -n "$(ls -A "$MNT" 2>/dev/null)" ]; then
    if [ "$FORCE" = 1 ]; then
        rm -rf "$MNT"
    else
        echo "mkdisk: $MNT is not empty; pass --force to discard it" >&2
        exit 2
    fi
fi

say create "$RAW ($SIZE)"
rm -f "$RAW"
truncate -s "$SIZE" "$RAW"

# GPT: a 1 MiB BIOS boot partition for GRUB, then the root filesystem.
sfdisk --quiet "$RAW" <<'EOF'
label: gpt
start=2048, size=2048, type=21686148-6449-6E6F-744E-656564454649, name="bios-boot"
start=4096, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="odile-root"
EOF

LOOP=$(losetup -Pf --show "$RAW")
say loop "$LOOP"
for _ in $(seq 1 40); do
    [ -b "${LOOP}p2" ] && break
    sleep 0.25
done
[ -b "${LOOP}p2" ] || { echo "mkdisk: ${LOOP}p2 never appeared" >&2; exit 1; }

mkfs.ext4 -q -L odile-root "${LOOP}p2"
mkdir -p "$MNT"
mount "${LOOP}p2" "$MNT"
say mount "${LOOP}p2 -> $MNT"

INCLUDE=ca-certificates,curl,dbus,git,grub-pc,initramfs-tools,iproute2,libatomic1,libstdc++6,linux-image-amd64,passwd,procps,python3,systemd-sysv,util-linux,xz-utils
KEYRING=()
if [ -f /usr/share/keyrings/debian-archive-keyring.gpg ]; then
    KEYRING=(--keyring=/usr/share/keyrings/debian-archive-keyring.gpg)
fi
say debootstrap "$SUITE ($INCLUDE)"
debootstrap --variant=minbase --arch=amd64 --components=main \
    "${KEYRING[@]}" --include="$INCLUDE" "$SUITE" "$MNT" "$MIRROR"

install -d "$MNT/usr/local/lib/odile-image"
for f in install-stack.sh bench-heartbeat.sh bench.py smoke.sh odile-webapp-brand.py; do
    install -m 0755 "$HERE/$f" "$MNT/usr/local/lib/odile-image/$f"
done
for f in config.yaml tenant.service odile-bench-smoke.service \
         odile-bench-proxy.service odile-bench-proxy.socket \
         odile-bench-proxy-firstvisit.service odile-bench-proxy-firstvisit.socket \
         odile-bench-proxy-verify.service odile-bench-proxy-verify.socket \
         odile-bench-proxy-manifest.service odile-bench-proxy-manifest.socket \
         odile-webapp-brand.service odile-webapp-brand.path \
         odile-webapp-brand.conf webapp-brand.js \
         webapp-chrome.css webapp-chrome.js; do
    install -m 0644 "$HERE/$f" "$MNT/usr/local/lib/odile-image/$f"
done

ROOT_PARTUUID=$(blkid -s PARTUUID -o value "${LOOP}p2")
CMDLINE="root=PARTUUID=$ROOT_PARTUUID ro console=ttyS0,115200 console=tty0 quiet systemd.show_status=1"

# The image's own boot and network configuration. 10.0.2.15/24 is QEMU's user
# networking guest address; hostfwd targets it, and the smoke never needs DHCP.
{
    printf 'PARTUUID=%s / ext4 defaults 0 1\n' "$ROOT_PARTUUID"
} > "$MNT/etc/fstab"
printf 'odile-tenant\n' > "$MNT/etc/hostname"
printf '127.0.0.1 localhost odile-tenant tenant.odile.test\n10.0.2.15 odile-tenant tenant.odile.test\n' > "$MNT/etc/hosts"
printf 'nameserver 10.0.2.3\n' > "$MNT/etc/resolv.conf"

install -d "$MNT/etc/systemd/network"
cat > "$MNT/etc/systemd/network/20-odile.network" <<'EOF'
[Match]
Name=en*

[Network]
Address=10.0.2.15/24
Gateway=10.0.2.2
DNS=10.0.2.3
EOF

cat > "$MNT/etc/default/grub" <<EOF
GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_CMDLINE_LINUX_DEFAULT="$CMDLINE"
GRUB_CMDLINE_LINUX=""
GRUB_DISABLE_OS_PROBER=true
EOF

# A small initramfs (the guest has virtio and ext4, nothing exotic) boots
# faster under TCG and is the same initramfs the UKI measures. MODULES=dep
# alone cannot resolve a PARTUUID root at build time inside the chroot, so the
# virtio/ext4 drivers are named explicitly; without them the guest panics at
# "Unable to find root device".
cat > "$MNT/etc/initramfs-tools/initramfs.conf" <<'EOF'
MODULES=dep
COMPRESS=gzip
EOF
cat > "$MNT/etc/initramfs-tools/modules" <<'EOF'
# QEMU's virtio devices and the root filesystem.
virtio_pci
virtio_blk
virtio_net
ext4
EOF

# The bench drill shell. This builder makes the bench image only, so `tenant`
# logs in on ttyS0 with no password: that shell is what the drill and the
# guest smoke use (docs/runbooks/image.md §5.1). A production image comes from
# sealed-image.sh --production, which installs no autologin — the Servury
# panel's interactive console is the same port, so an autologin there would
# hand a panel holder a root-capable shell with no user credential (ODL-914).
install -d "$MNT/etc/systemd/system/serial-getty@ttyS0.service.d"
cat > "$MNT/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin tenant --keep-baud 115200,38400,9600 %I $TERM
EOF

# The chroot needs its kernel filesystems before anything calls into systemd:
# `systemctl enable` warns "/proc/ is not mounted. This is not a supported
# mode of operation" and may skip work without them.
mount --bind /proc "$MNT/proc"
# --rbind keeps the host's shared propagation unless the copy is marked
# slave: on 2026-11-08 `umount -R "$MNT"` propagated back to the host and
# unmounted /sys/fs/cgroup (breaking every systemd unit) and the builder's
# /dev nodes. A slave copy receives mounts from the host but never propagates
# an unmount upward. cleanup() also asserts both survived.
mount --rbind /dev "$MNT/dev"
mount --make-rslave "$MNT/dev"
mount --rbind /sys "$MNT/sys"
mount --make-rslave "$MNT/sys"
cp /etc/resolv.conf "$MNT/etc/resolv.conf"

for unit in systemd-networkd.service serial-getty@ttyS0.service; do
    chroot "$MNT" systemctl enable "$unit" >/dev/null
done

say chroot "install-stack.sh ($( [ "$GUEST_SMOKE" = 1 ] && echo 'with guest smoke' || echo 'no guest smoke'))"
chroot_env=(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=dumb)
install_args=(--password "$PASSWORD")
second_args=(--password "$PASSWORD")
if [ "$GUEST_SMOKE" = 1 ]; then
    install_args+=(--guest-smoke)
    second_args+=(--guest-smoke)
fi
"${chroot_env[@]}" chroot "$MNT" /usr/local/lib/odile-image/install-stack.sh "${install_args[@]}"

# The installer's own second run is the idempotency evidence.
say idempotent "second install-stack run (must print same)"
"${chroot_env[@]}" chroot "$MNT" /usr/local/lib/odile-image/install-stack.sh "${second_args[@]}"

# The bench resolver is QEMU's, not this builder's.
printf 'nameserver 10.0.2.3\n' > "$MNT/etc/resolv.conf"

say initramfs "update-initramfs -u -k all"
chroot "$MNT" update-initramfs -u -k all
say grub "grub-install --target=i386-pc $LOOP"
chroot "$MNT" update-grub
chroot "$MNT" grub-install --target=i386-pc --recheck "$LOOP"

KERNEL=$(find "$MNT/boot" -maxdepth 1 -name 'vmlinuz-*' -type f | sort -V | tail -n 1)
INITRD=$(find "$MNT/boot" -maxdepth 1 -name 'initrd.img-*' -type f | sort -V | tail -n 1)
[ -n "$KERNEL" ] || { echo "mkdisk: no kernel in $MNT/boot" >&2; exit 1; }
[ -n "$INITRD" ] || { echo "mkdisk: no initramfs in $MNT/boot" >&2; exit 1; }
cp "$KERNEL" "$BOOT/vmlinuz"
cp "$INITRD" "$BOOT/initrd.img"
cp "$MNT/etc/os-release" "$BOOT/os-release"
printf '%s\n' "$CMDLINE" > "$BOOT/cmdline"
basename "$KERNEL" | sed 's/^vmlinuz-//' > "$BOOT/uname"
say boot "kernel $(basename "$KERNEL"), initramfs $(du -h "$INITRD" | cut -f1)"

sync
cleanup
trap - EXIT
LOOP=""

say qcow2 "$QCOW"
qemu-img convert -O qcow2 -c "$RAW" "$QCOW"

RAW_BYTES=$(stat -c %s "$RAW")
QCOW_BYTES=$(stat -c %s "$QCOW")
say sizes "raw $((RAW_BYTES / 1024 / 1024)) MiB, qcow2 $((QCOW_BYTES / 1024 / 1024)) MiB"
say next "sudo $HERE/smoke.sh image --disk $RAW"
