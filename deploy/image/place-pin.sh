#!/bin/bash
# place-pin.sh — put the image's pin inside its sealed root (ODL-332).
#
# `uki.sh --pin` writes the JSON pin after it has measured the signed UKI. The
# machine's own /verify and the release setup read that file at
# /etc/odile/tenant-image.pin, and it must be the repo's file, not a value the
# machine derived about itself (ODL-473).
#
# The disk is already sealed when the pin exists, so this opens it the way the
# production first boot will: the boot partition carries the bootstrap key
# (`sealed-image.sh --production`), which opens the container until the chip's
# own seal replaces it and shreds the key. Every mount lives in a private mount
# namespace, so the host's mount table is untouched.
#
#   ./deploy/image/place-pin.sh \
#       --disk /var/tmp/odile-332-prod3/odile-tenant-sealed.raw \
#       --pin  /var/tmp/odile-332-prod3/tenant-image.pin
#
# The pin is written whole. Its `raw_image_sha256` is then set to the disk's
# final bytes, because placing a file in the root filesystem changes the disk;
# the guest copy keeps the as-built hash, and the measurement fields — the
# only ones the guest reads — are untouched.
#
# Exit codes: 0 written or already the same, 1 the disk could not be opened or
# written, 2 bad input or a missing prerequisite.
set -eu

DISK=""
PIN=""

usage() {
    cat <<'EOF'
usage: place-pin.sh --disk RAW --pin PIN

  --disk RAW   the sealed disk image (the bootstrap key is on its boot partition)
  --pin PIN    the JSON pin uki.sh wrote for this image
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --disk) DISK=$2; shift 2 ;;
        --pin) PIN=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

[ -n "$DISK" ] && [ -n "$PIN" ] || { usage >&2; exit 2; }
[ -f "$DISK" ] || { echo "place-pin: no disk at $DISK" >&2; exit 2; }
[ -s "$PIN" ] || { echo "place-pin: no pin at $PIN" >&2; exit 2; }
for tool in losetup cryptsetup; do
    command -v "$tool" >/dev/null \
        || { echo "place-pin: $tool is required" >&2; exit 2; }
done

# shellcheck disable=SC2016  # the body is expanded by the inner bash, not here
unshare -m --propagation private bash -c '
set -euo pipefail
disk=$1; pin=$2
mnt=$(mktemp -d /var/tmp/odile-pin-mnt.XXXXXX)
loop=""; mapped=0
cleanup() {
    set +e
    mountpoint -q "$mnt" && umount "$mnt"
    [ "$mapped" = 1 ] && cryptsetup close odile-placepin
    [ -n "$loop" ] && losetup -d "$loop"
    rmdir "$mnt" 2>/dev/null
}
trap cleanup EXIT
loop=$(losetup -fP --show "$disk")
key=$(mktemp /var/tmp/odile-pin-key.XXXXXX); chmod 600 "$key"
boot=$(mktemp -d /var/tmp/odile-pin-boot.XXXXXX)
mount -o ro "${loop}p2" "$boot"
[ -s "$boot/odile/luks-bootstrap.key" ] \
    || { echo "place-pin: no /odile/luks-bootstrap.key on ${loop}p2" >&2; exit 1; }
cp "$boot/odile/luks-bootstrap.key" "$key"
umount "$boot"; rmdir "$boot"
cryptsetup open --key-file "$key" "${loop}p3" odile-placepin
mapped=1
mount /dev/mapper/odile-placepin "$mnt"
install -d -m 0755 "$mnt/etc/odile"
if cmp -s "$pin" "$mnt/etc/odile/tenant-image.pin"; then
    echo same
else
    install -m 0644 "$pin" "$mnt/etc/odile/tenant-image.pin"
    sync
    echo write
fi
cmp -s "$pin" "$mnt/etc/odile/tenant-image.pin" \
    || { echo "place-pin: the disk does not carry the pin bytes" >&2; exit 1; }
' _ "$DISK" "$PIN"

# The disk now differs from the one uki.sh hashed. Record its final bytes in
# the repo pin; the measurement fields do not move (they cover the UKI, not the
# root filesystem).
/usr/bin/python3 - "$PIN" "$DISK" <<'PY'
import hashlib
import json
import sys


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


pin_path, disk = sys.argv[1], sys.argv[2]
with open(pin_path, encoding="utf-8") as handle:
    pin = json.load(handle)
pin["raw_image_sha256"] = sha(disk)
with open(pin_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(pin, indent=2, sort_keys=True) + "\n")
print("pin raw_image_sha256 -> %s (as built, pin inside)" % pin["raw_image_sha256"][:12])
PY
