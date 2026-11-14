#!/bin/bash
# sealed-initrd.sh — rebuild Servury's published initramfs with the
# bb-sealed-unlock hook in the generated ORDER (ODL-454).
#
# The hook (spike/sealed-unlock/initramfs/bb-sealed-unlock) declares
# PREREQ="udev bb-wait" and is placed by `mkinitramfs` itself: cache_run_scripts
# runs tsort over every init-premount script's prereqs() and writes
# /scripts/init-premount/ORDER, which run_scripts() sources at boot. Copying a
# file into the directory without that ORDER line does nothing. This script
# therefore builds through Servury's own source tree and an initramfs-tools
# chroot, then *reads the ORDER back out of the built initrd* and fails if the
# hook is absent or misplaced.
#
# Two substitutions are made for the bench, and both are printed:
#
#   * /usr/bin/snpreport is deploy/image/bench-snpreport, the QEMU-TCG stand-in
#     for Servury's binary. The bench host has no /dev/sev-guest, so the real
#     derivation cannot run here; the stand-in keeps the interface and the
#     BBHK1 format and derives its key from the measured command line. The
#     real derivation was proven on the tenant in ODL-310.
#   * the build chroot gets one /etc/crypttab entry
#     (root_crypt /dev/sda3 none luks,initramfs) so cryptroot's own hook
#     generates /cryptroot/crypttab and a failed unseal reaches the passphrase
#     prompt — the documented fail-closed state — instead of a bare "Unable to
#     find root device". The stock release initrd ships an empty crypttab.
#
#   ./deploy/image/sealed-initrd.sh --dry-run
#   sudo ./deploy/image/sealed-initrd.sh --out deploy/image/out-sealed
#
# Outputs under --out:
#   boot/vmlinuz           Servury's published kernel, hash-checked
#   boot/initrd.img        the rebuilt initramfs, hook in ORDER
#   boot/uname             the kernel release
#   boot/os-release        Debian 13, from the build chroot
#   boot/initrd-order.txt  the ORDER and the three read-back checks
#   bench-snpreport        the compiled TCG stand-in
#
# Exit codes: 0 ok, 1 a build or check failed, 2 bad input or a missing tool.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin
export PATH

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
OUT=$HERE/out-sealed
RELEASE=2026.11.1
BASE_URL=${ODILE_BLACKBOX_BASE:-https://servury.com/downloads/blackbox}
# ODL-310 §14: 2026.11.1 is the release whose source tarball is published and
# whose initramfs scripts are byte-identical to 2026.11.2's.
VMLINUZ_SHA256=96758c40859c94163e5cf81399a107d679e984e251cb4e13aba4e7d92f5d14f2
INITRD_SHA256=8c2aff57573c4af11289553fa185c8f13613acca688a52c2e579d4ea9c2beaf7
SOURCE_SHA256=ef888e8881b56cf0138aab1f463ce250e7250dae88c24083680d7e9bad2994b6
HOOK=$REPO/spike/sealed-unlock/initramfs/bb-sealed-unlock
CHROOT=${ODILE_BLACKBOX_CHROOT:-/var/tmp/bb/chroot}
SOURCE=${ODILE_BLACKBOX_SOURCE:-/tmp/blackbox-images}
DRY_RUN=0
FORCE=0
# ODL-332: the production initrd carries Servury's *real* /usr/bin/snpreport
# (the one whose key comes from SNP_GET_DERIVED_KEY on the chip), because the
# tenant's disk must be sealed to the measured boot, not to a key derived from
# the public command line. The bench keeps the stand-in; the disk seal and the
# pin are computed with the stand-in either way (no AMD PSP on HQ).
REAL_SNP=""

usage() {
    cat <<'EOF'
usage: sealed-initrd.sh [--out DIR] [--release DIR] [--chroot DIR]
                        [--source DIR] [--snpreport FILE] [--force] [--dry-run]

  --out DIR      output directory (default: deploy/image/out-sealed)
  --release DIR  Servury 2026.11.1 artifacts (default: <out>/release; fetched
                 from the published URL and hash-checked when absent)
  --chroot DIR   initramfs-tools build chroot (default: /var/tmp/bb/chroot,
                 $ODILE_BLACKBOX_CHROOT)
  --source DIR   blackbox-images-2026.11.1 source tree (default:
                 /tmp/blackbox-images, $ODILE_BLACKBOX_SOURCE); the build
                 needs it, --dry-run does not
  --snpreport F  use Servury's real snpreport binary at F in the initrd instead
                 of the TCG stand-in (production; sha256 must be the release's
                 1e2a6c07…). The stand-in is still built for the disk seal.
  --force        replace an existing boot/initrd.img
  --dry-run      print the plan and change nothing (needs no --source)
EOF
}

say() { printf '%-12s %s\n' "$1" "$2"; }
SNP_REAL_SHA256=1e2a6c0782bebf471a6973a2bf1f0eee5fac86005a5ee56c5b8939ef40cacc77

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT=$2; shift 2 ;;
        --release) REL_DIR=$2; shift 2 ;;
        --chroot) CHROOT=$2; shift 2 ;;
        --source) SOURCE=$2; shift 2 ;;
        --snpreport) REAL_SNP=$2; shift 2 ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "sealed-initrd: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

REL_DIR=${REL_DIR:-$OUT/release}
BOOT=$OUT/boot
KVER=6.12.115+deb13.1-amd64
[ -f "$HOOK" ] || { echo "sealed-initrd: missing hook $HOOK" >&2; exit 2; }
[ -x "$CHROOT/usr/sbin/mkinitramfs" ] || {
    echo "sealed-initrd: no initramfs-tools in $CHROOT (set --chroot)" >&2
    exit 2
}
# The dry run plans before anything is validated against the host: a machine
# that has never had the external blackbox-images tree still gets the plan,
# with the source named as missing. Only a real build needs it (11 Nov: the
# check sat above the dry-run branch and `make check` went red after a reboot
# emptied /tmp).
HAVE_SOURCE=0
if [ -f "$SOURCE/initramfs/conf/initramfs.conf" ]; then
    HAVE_SOURCE=1
fi

if [ "$DRY_RUN" = 1 ]; then
    if [ "$HAVE_SOURCE" = 0 ]; then
        say missing "no blackbox-images source at $SOURCE (set --source); a real build stops before this plan runs"
    fi
    say would "verify $REL_DIR/{vmlinuz,initrd.img} and the source tarball hash"
    say would "copy $KVER modules + vmlinuz into $CHROOT and run depmod"
    say would "compile deploy/image/bench-snpreport.c and install it in the chroot"
    if [ -n "$REAL_SNP" ]; then
        say would "use Servury's real snpreport $REAL_SNP in the initrd (sha256 $SNP_REAL_SHA256) and keep it at $OUT/real-snpreport for the disk's root"
    fi
    say would "mkdir /tmp/bb-conf-odl454: $SOURCE/initramfs/conf + bb-sealed-unlock"
    say would "chroot mkinitramfs -d /tmp/bb-conf-odl454 -o ... $KVER"
    say would "add /etc/crypttab root_crypt /dev/sda3 none luks,initramfs before mkinitramfs"
    say would "verify ORDER in the built initrd names the hook after bb-wait"
    say would "write $BOOT/{vmlinuz,initrd.img,uname,os-release,initrd-order.txt}"
    exit 0
fi

[ "$HAVE_SOURCE" = 1 ] || {
    echo "sealed-initrd: no blackbox-images source at $SOURCE (set --source)" >&2
    exit 2
}

for tool in unmkinitramfs cpio gzip sha256sum gcc depmod cmp; do
    command -v "$tool" >/dev/null 2>&1 || { echo "sealed-initrd: missing $tool" >&2; exit 2; }
done
[ "$(id -u)" -eq 0 ] || { echo "sealed-initrd: must run as root (the chroot)" >&2; exit 2; }
if [ -s "$BOOT/initrd.img" ] && [ "$FORCE" != 1 ]; then
    echo "sealed-initrd: $BOOT/initrd.img exists; pass --force to rebuild it" >&2
    exit 2
fi
mkdir -p "$OUT" "$BOOT"

# 1. The published release: the manifest and the bytes must agree.
if [ ! -s "$REL_DIR/vmlinuz" ] || [ ! -s "$REL_DIR/initrd.img" ]; then
    say fetch "$BASE_URL/$RELEASE/{SHA256SUMS,vmlinuz,initrd.img}"
    mkdir -p "$REL_DIR"
    curl -fsSL --max-time 120 -o "$REL_DIR/SHA256SUMS" "$BASE_URL/$RELEASE/SHA256SUMS"
    for f in vmlinuz initrd.img; do
        curl -fsSL --max-time 600 -o "$REL_DIR/$f" "$BASE_URL/$RELEASE/$f"
    done
fi
for pair in "vmlinuz:$VMLINUZ_SHA256" "initrd.img:$INITRD_SHA256"; do
    f=${pair%%:*}; want=${pair#*:}
    got=$(sha256sum "$REL_DIR/$f" | cut -d' ' -f1)
    [ "$got" = "$want" ] || {
        echo "sealed-initrd: $f is $got, expected $want" >&2
        exit 1
    }
done
say verified "$REL_DIR/vmlinuz $VMLINUZ_SHA256"
say verified "$REL_DIR/initrd.img $INITRD_SHA256"

# The source tree is what makes the rebuild possible at all; if it is a
# tarball checkout, the README's own hash is the provenance.
if [ -f "$SOURCE/blackbox-images-$RELEASE.tar.gz" ]; then
    got=$(sha256sum "$SOURCE/blackbox-images-$RELEASE.tar.gz" | cut -d' ' -f1)
    [ "$got" = "$SOURCE_SHA256" ] || {
        echo "sealed-initrd: source tarball is $got, expected $SOURCE_SHA256" >&2
        exit 1
    }
    say verified "source tarball $SOURCE_SHA256"
fi

# 2. Give the chroot the release kernel's module tree (from the release
#    initrd itself, so no second download) and the kernel file. depmod then
#    makes the tree usable by mkinitramfs.
WORK=$OUT/work
rm -rf "$WORK"
mkdir -p "$WORK"
if [ ! -d "$CHROOT/lib/modules/$KVER" ]; then
    say extract "modules for $KVER from the release initrd"
    unmkinitramfs "$REL_DIR/initrd.img" "$WORK/release-initrd"
    [ -d "$WORK/release-initrd/lib/modules/$KVER" ] || {
        echo "sealed-initrd: the release initrd has no modules for $KVER" >&2
        exit 1
    }
    cp -a "$WORK/release-initrd/lib/modules/$KVER" "$CHROOT/lib/modules/"
fi
if [ ! -f "$CHROOT/boot/vmlinuz-$KVER" ]; then
    install -m 0644 "$REL_DIR/vmlinuz" "$CHROOT/boot/vmlinuz-$KVER"
fi
chroot "$CHROOT" depmod -a "$KVER"
say modules "$CHROOT/lib/modules/$KVER (depmod ok)"

# 3. The bench stand-in, compiled static like Servury's own snpreport.
say compile "deploy/image/bench-snpreport.c"
gcc -O2 -static -s -o "$OUT/bench-snpreport" "$HERE/bench-snpreport.c"
"$OUT/bench-snpreport" --selftest >/dev/null
SNP_BACKUP=""
cleanup() {
    set +e
    if [ -n "$SNP_BACKUP" ] && [ -f "$SNP_BACKUP" ]; then
        mv -f "$SNP_BACKUP" "$CHROOT/usr/local/lib/bb/snpreport"
    fi
    if [ -f "$CHROOT/etc/crypttab" ]; then rm -f "$CHROOT/etc/crypttab"; fi
    if [ -n "$CRYPTTAB_SAVED" ] && [ -f "$CRYPTTAB_SAVED" ]; then
        mv -f "$CRYPTTAB_SAVED" "$CHROOT/etc/crypttab"
    fi
    rm -rf "$CHROOT/tmp/bb-conf-odl454" "$CHROOT/tmp/odl454-out.img" \
        "$CHROOT/tmp/odl454-overlay" "$WORK/release-initrd" "$WORK/check"
    rm -f "$CHROOT/tmp/odl454-overlay.cpio.gz"
}
trap cleanup EXIT
mkdir -p "$CHROOT/usr/local/lib/bb"
if [ -f "$CHROOT/usr/local/lib/bb/snpreport" ]; then
    SNP_BACKUP=$(mktemp "$CHROOT/tmp/snpreport.real.XXXXXX")
    cp -p "$CHROOT/usr/local/lib/bb/snpreport" "$SNP_BACKUP"
fi
# ODL-332: the production initrd must derive the key on the chip. The real
# binary is Servury's own, extracted from the release initrd; the hash is
# pinned so a substituted or edited binary cannot pass as the release's.
SNP_INSTALL=$OUT/bench-snpreport
if [ -n "$REAL_SNP" ]; then
    [ -x "$REAL_SNP" ] || { echo "sealed-initrd: --snpreport $REAL_SNP is not executable" >&2; exit 2; }
    REAL_SHA=$(sha256sum "$REAL_SNP" | cut -d' ' -f1)
    [ "$REAL_SHA" = "$SNP_REAL_SHA256" ] || {
        echo "sealed-initrd: --snpreport sha256 $REAL_SHA is not Servury's release binary ($SNP_REAL_SHA256)" >&2
        exit 2
    }
    install -m 0755 "$REAL_SNP" "$OUT/real-snpreport"
    SNP_INSTALL=$OUT/real-snpreport
    say snpreport "Servury's release binary (sha256 $REAL_SHA) — chip-derived keys"
else
    say snpreport "bench stand-in (command-line-derived keys; no /dev/sev-guest on HQ)"
fi
install -m 0755 "$SNP_INSTALL" "$CHROOT/usr/local/lib/bb/snpreport"

# 4. The build conf: Servury's tree, plus the hook, plus the one generated
#    override the stock build already uses for dropbear.
CONF=$CHROOT/tmp/bb-conf-odl454
rm -rf "$CONF"
cp -a "$SOURCE/initramfs/conf" "$CONF"
mkdir -p "$CONF/conf.d" "$CONF/scripts/init-premount"
sed 's|^PREREQ="udev"|PREREQ="udev bb-attest"|' \
    "$CHROOT/usr/share/initramfs-tools/scripts/init-premount/dropbear" \
    > "$CONF/scripts/init-premount/dropbear"
grep -q 'bb-attest' "$CONF/scripts/init-premount/dropbear" || {
    echo "sealed-initrd: the dropbear prerequisite override did not apply" >&2
    exit 1
}
echo 'RESUME=none' > "$CONF/conf.d/resume"
install -m 0755 "$HOOK" "$CONF/scripts/init-premount/bb-sealed-unlock"
chmod 755 "$CONF"/hooks/* "$CONF"/scripts/*/*

# The release initrd's crypttab is empty; give the build hook one entry so a
# failed unseal reaches cryptroot's prompt. The `initramfs` option is the
# supported way to force an entry into the generated /cryptroot/crypttab.
CRYPTTAB_SAVED=""
if [ -f "$CHROOT/etc/crypttab" ]; then
    CRYPTTAB_SAVED=$(mktemp "$CHROOT/tmp/crypttab.real.XXXXXX")
    cp -p "$CHROOT/etc/crypttab" "$CRYPTTAB_SAVED"
fi
printf 'root_crypt /dev/sda3 none luks,initramfs\n' > "$CHROOT/etc/crypttab"

say mkinitramfs "$KVER (SOURCE_DATE_EPOCH=1748736000)"
chroot "$CHROOT" /bin/bash -lc \
    "export SOURCE_DATE_EPOCH=1748736000 LC_ALL=C.UTF-8 TZ=UTC; \
     mkinitramfs -d /tmp/bb-conf-odl454 -o /tmp/odl454-out.img $KVER" 2>&1 |
    grep -viE '^W: Possible missing firmware|for built-in driver' || true
[ -s "$CHROOT/tmp/odl454-out.img" ] || { echo "sealed-initrd: mkinitramfs produced nothing" >&2; exit 1; }
install -m 0644 "$CHROOT/tmp/odl454-out.img" "$BOOT/initrd.img"

# 5. The crypttab check happens with the other read-backs below.

# 6. Read the placement back out of the built initrd. This is the check that
#    catches the ORDER defect: the file being present is not enough.
say check "ORDER in $BOOT/initrd.img"
unmkinitramfs "$BOOT/initrd.img" "$WORK/check"
ORDER=$WORK/check/scripts/init-premount/ORDER
[ -f "$ORDER" ] || { echo "sealed-initrd: the initrd has no init-premount/ORDER" >&2; exit 1; }
grep -qx '/scripts/init-premount/bb-sealed-unlock "\$@"' "$ORDER" || {
    echo "sealed-initrd: the hook is not in ORDER" >&2
    cat "$ORDER" >&2
    exit 1
}
[ -f "$WORK/check/scripts/init-premount/bb-sealed-unlock" ] || {
    echo "sealed-initrd: the hook file is not in the initrd" >&2
    exit 1
}
cmp -s "$WORK/check/scripts/init-premount/bb-sealed-unlock" "$HOOK" || {
    echo "sealed-initrd: the initrd's hook differs from $HOOK" >&2
    exit 1
}
[ -f "$WORK/check/usr/bin/snpreport" ] || {
    echo "sealed-initrd: the initrd has no /usr/bin/snpreport" >&2
    exit 1
}
cmp -s "$WORK/check/usr/bin/snpreport" "$SNP_INSTALL" || {
    echo "sealed-initrd: the initrd's snpreport is not $SNP_INSTALL" >&2
    exit 1
}
grep -qx 'root_crypt /dev/sda3 none luks,initramfs' "$WORK/check/cryptroot/crypttab" || {
    echo "sealed-initrd: the generated crypttab does not name root_crypt" >&2
    exit 1
}
{
    printf 'kernel_release=%s\n' "$KVER"
    printf 'hook_sha256=%s\n' "$(sha256sum "$HOOK" | cut -d' ' -f1)"
    printf 'snpreport_sha256=%s\n' "$(sha256sum "$SNP_INSTALL" | cut -d' ' -f1)"
    printf 'snpreport_source=%s\n' "$([ -n "$REAL_SNP" ] && echo servury-release-chip-derived || echo bench-stand-in-cmdline-derived)"
    printf 'initrd_sha256=%s\n' "$(sha256sum "$BOOT/initrd.img" | cut -d' ' -f1)"
    printf 'order:\n'
    sed 's/^/  /' "$ORDER"
} > "$BOOT/initrd-order.txt"
tail -n +1 "$BOOT/initrd-order.txt"

install -m 0644 "$REL_DIR/vmlinuz" "$BOOT/vmlinuz"
printf '%s\n' "$KVER" > "$BOOT/uname"
if [ -s "$CHROOT/usr/lib/os-release" ]; then
    install -m 0644 "$CHROOT/usr/lib/os-release" "$BOOT/os-release"
else
    install -m 0644 "$CHROOT/etc/os-release" "$BOOT/os-release"
fi
say built "$BOOT/initrd.img ($(du -h "$BOOT/initrd.img" | cut -f1))"
say next "./deploy/image/sealed-image.sh --out $(printf '%q' "$OUT")"
