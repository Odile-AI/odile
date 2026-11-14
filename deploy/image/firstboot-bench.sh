#!/bin/bash
# firstboot-bench.sh — the ODL-456 bench: a real seed service, a real signed
# image, and the first-boot unit doing the install with no terminal input.
#
#   ./deploy/image/firstboot-bench.sh prepare
#   ./deploy/image/firstboot-bench.sh build
#   ./deploy/image/firstboot-bench.sh boot --expect installed
#   ./deploy/image/firstboot-bench.sh boot --expect same
#   ./deploy/image/firstboot-bench.sh restore-fresh
#   ./deploy/image/firstboot-bench.sh boot --expect fail-closed
#   ./deploy/image/firstboot-bench.sh stop
#
# What is real: seed/seed.py (the production exchange, signature and ledger),
# the Ed25519 seed key that signs it and the release key's certificate over its
# public half (ODL-514), the signed UKI and the sealed disk, and the guest's own
# `odile-firstboot` + `install-stack.sh --seed` + first-visit page.
# What is a bench stand-in: QEMU TCG instead of SEV, and the guest-side
# `snpreport` derives from the measured command line (ODL-454 §1).
#
# The fixture directory (default /var/tmp/odile-456-bench) holds the seed
# database, the token (0600), the boot transcripts and a copy of the disk as
# built. The seed port defaults to 18081: 8081 is the live HQ service.
#
# `build` also puts the pin inside the guest (ODL-473): after `uki.sh` writes
# deploy/image/tenant-image.pin for the signed UKI, the disk is opened the way
# FIRSTBOOT.md §6 documents (`bench-snpreport --unseal`, cryptsetup, mount) and
# the pin is installed at /etc/odile/tenant-image.pin. The guest's boot-report
# stand-in and its /verify access report both read that file, so the seal line
# checks the measurement the user was given, not a value derived from itself.
# `restore-fresh` puts that copy back so the "already-used token" boot
# starts from a machine that never installed — the token is spent by then.
#
# Exit codes: 0 pass, 1 the boot or the evidence failed, 2 bad input or a
# missing prerequisite.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin
export PATH

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
DIR=${ODILE_FIRSTBOOT_DIR:-/var/tmp/odile-456-bench}
OUT=${ODILE_FIRSTBOOT_OUT:-$HERE/out-sealed}
INSTANCE=${ODILE_BENCH_IID:-45445445-0000-4000-8000-000000000454}
SEED_PORT=${ODILE_FIRSTBOOT_SEED_PORT:-18081}
# ODL-514: the fixture signs with the seed key and certifies it with the release
# key whose public half the image bakes. The defaults are the live pair; these
# overrides let a bench on a clean box use throwaways (and then boot an image
# that was built with the matching release public key).
SEED_KEY=${ODILE_FIRSTBOOT_SEED_KEY:-}
RELEASE_KEY=${ODILE_FIRSTBOOT_RELEASE_KEY:-}
GUEST_SEED_URL="http://10.0.2.2:$SEED_PORT/seed"
HOST_FIRST_VISIT_PORT=${ODILE_FIRSTBOOT_PAGE_PORT:-19133}
HOST_MANIFEST_PORT=${ODILE_FIRSTBOOT_MANIFEST_PORT:-19134}
FORCE=0
EXPECT=""

usage() {
    cat <<'EOF'
usage: firstboot-bench.sh {prepare|build|boot|restore-fresh|inspect-placement|tamper-manifest|stop|show|cleanup} [options]

  prepare          create the seed fixture and bind one token (idempotent)
  build            build the sealed disk (sealed-initrd, sealed-image, uki) with
                   the token in the measured command line; keep a pre-boot copy
  boot             boot the signed image and assert the first-boot line:
                   --expect installed|same|fail-closed
  restore-fresh    put the pre-boot disk copy back (used by the fail-closed run)
  inspect-placement  open the sealed disk read-only and print what the first
                   boot placed: mode/owner, sha256, byte identity with release/,
                   and the installer hash the manifest names
  tamper-manifest  append one line to the disk's manifest pair (the served copy
                   when the disk has one, else the image's), so the signature
                   no longer verifies
  stop             stop the fixture seed service
  show             print the fixture token prefix and ledger states
  cleanup          stop the service and remove the fixture directory

  --dir DIR        fixture directory (default /var/tmp/odile-456-bench)
  --out DIR        image directory (default deploy/image/out-sealed)
  --force          rebuild the image over an existing one
EOF
}

say() { printf '%-12s %s\n' "$1" "$2"; }

COMMAND=${1:-}
[ -n "$COMMAND" ] || { usage >&2; exit 2; }
shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR=$2; shift 2 ;;
        --out) OUT=$2; shift 2 ;;
        --expect) EXPECT=$2; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "firstboot-bench: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

token() { cat "$DIR/token"; }
seed_up() { curl -fsS --max-time 2 "http://127.0.0.1:$SEED_PORT/health" >/dev/null 2>&1; }

start_seed() {
    seed_up && { say same "seed service on 127.0.0.1:$SEED_PORT"; return 0; }
    say start "seed/seed.py serve on 127.0.0.1:$SEED_PORT (fixture $DIR)"
    /usr/bin/python3 "$HERE/firstboot-fixture.py" serve --dir "$DIR" --port "$SEED_PORT" \
        >"$DIR/seed.log" 2>&1 &
    echo $! > "$DIR/seed.pid"
    for _ in $(seq 1 60); do
        seed_up && return 0
        sleep 1
    done
    echo "firstboot-bench: the seed service did not start; see $DIR/seed.log" >&2
    return 1
}

stop_seed() {
    if [ -f "$DIR/seed.pid" ]; then
        kill "$(cat "$DIR/seed.pid")" 2>/dev/null || true
        rm -f "$DIR/seed.pid"
        say stop "seed service"
    fi
    # A service started by an earlier call may still hold the port.
    pkill -f "firstboot-fixture.py serve --dir $DIR" 2>/dev/null || true
}

# The guest's copy of the pin (ODL-473). /verify's seal line must check the
# measurement the user was given, and the bench's boot-report stand-in derives
# its document from the same file; both read /etc/odile/tenant-image.pin. The
# disk is already sealed when uki.sh writes the pin, so this opens it the way
# FIRSTBOOT.md §6 documents: `bench-snpreport --unseal` with the pinned line,
# cryptsetup, mount, write, close. Every mount lives in a private namespace, so
# the host's mount table is untouched. Prints `same` or `write`.
place_guest_pin() {
    local pin=$HERE/tenant-image.pin
    [ -s "$pin" ] || { echo "firstboot-bench: no pin at $pin (build the UKI first)" >&2; return 1; }
    [ -x "$OUT/bench-snpreport" ] || { echo "firstboot-bench: no $OUT/bench-snpreport" >&2; return 1; }
    # shellcheck disable=SC2016  # the body is expanded by the inner bash, not here
    unshare -m --propagation private bash -c '
set -euo pipefail
out=$1; pin=$2
key=$(mktemp /var/tmp/odile-pin-key.XXXXXX); chmod 600 "$key"
mnt=$(mktemp -d /var/tmp/odile-pin-mnt.XXXXXX)
loop=""; mapped=0
cleanup() {
    set +e
    mountpoint -q "$mnt" && umount "$mnt"
    [ "$mapped" = 1 ] && cryptsetup close odile-pin
    [ -n "$loop" ] && losetup -d "$loop"
    rmdir "$mnt" 2>/dev/null
    shred -u "$key"
}
trap cleanup EXIT
"$out/bench-snpreport" --cmdline-file "$out/boot/cmdline" \
    --unseal "$out/boot/luks-vol.key.sealed" "$key" >/dev/null
loop=$(losetup -fP --show "$out/odile-tenant-sealed.raw")
cryptsetup luksOpen --key-file "$key" "${loop}p3" odile-pin
mapped=1
mount /dev/mapper/odile-pin "$mnt"
install -d -m 0755 "$mnt/etc/odile"
if cmp -s "$pin" "$mnt/etc/odile/tenant-image.pin"; then
    echo same
else
    install -m 0644 "$pin" "$mnt/etc/odile/tenant-image.pin"
    echo write
fi
sync
' _ "$OUT" "$pin"
}

# The pin's raw_image_sha256 is the disk that boots, so it moves when the pin
# is placed inside the disk. Record the final bytes; the measurement fields are
# untouched (placing a file in the root filesystem does not change the UKI or
# the command line the measurement covers).
record_disk_hash() {
    /usr/bin/python3 - "$HERE/tenant-image.pin" "$OUT/odile-tenant-sealed.raw" <<'PY'
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
}

# The host-side disk drill (ODL-472): open the sealed root with the blob the
# boot itself unseals. `inspect-placement` proves what the first boot left —
# 0755 root, byte-identical to the release, the hash the manifest names — and
# `tamper-manifest` appends one line to the pair so the signature no longer
# verifies.
SEAL_LOOP=""
SEAL_MAPPER=""
SEAL_MNT=""
SEAL_KEY=""

seal_cleanup() {
    set +e
    if [ -n "$SEAL_MNT" ] && mountpoint -q "$SEAL_MNT" 2>/dev/null; then
        umount "$SEAL_MNT"
    fi
    [ -n "$SEAL_MNT" ] && rmdir "$SEAL_MNT" 2>/dev/null
    [ -n "$SEAL_MAPPER" ] && cryptsetup close "${SEAL_MAPPER##*/}" 2>/dev/null
    [ -n "$SEAL_LOOP" ] && losetup -d "$SEAL_LOOP" 2>/dev/null
    [ -n "$SEAL_KEY" ] && shred -u "$SEAL_KEY" 2>/dev/null
    SEAL_LOOP=""; SEAL_MAPPER=""; SEAL_MNT=""; SEAL_KEY=""
}

seal_open() {
    mode=${1:-ro}
    [ -s "$OUT/odile-tenant-sealed.raw" ] || { echo "firstboot-bench: no $OUT/odile-tenant-sealed.raw" >&2; exit 2; }
    [ -s "$OUT/boot/cmdline" ] || { echo "firstboot-bench: no $OUT/boot/cmdline" >&2; exit 2; }
    [ -x "$OUT/bench-snpreport" ] || { echo "firstboot-bench: no $OUT/bench-snpreport" >&2; exit 2; }
    [ -s "$OUT/boot/luks-vol.key.sealed" ] || { echo "firstboot-bench: no sealed volume key" >&2; exit 2; }
    trap seal_cleanup EXIT
    SEAL_KEY=$(mktemp /var/tmp/odile-472-key.XXXXXX)
    chmod 600 "$SEAL_KEY"
    "$OUT/bench-snpreport" --cmdline-file "$OUT/boot/cmdline" \
        --unseal "$OUT/boot/luks-vol.key.sealed" "$SEAL_KEY"
    SEAL_LOOP=$(losetup -Pf --show "$OUT/odile-tenant-sealed.raw")
    cryptsetup luksOpen --key-file "$SEAL_KEY" "${SEAL_LOOP}p3" odile-472-drill
    SEAL_MAPPER=/dev/mapper/odile-472-drill
    SEAL_MNT=$(mktemp -d /var/tmp/odile-472-mnt.XXXXXX)
    mount "-o$mode" "$SEAL_MAPPER" "$SEAL_MNT"
}

inspect_placement() {
    seal_open ro
    root=$SEAL_MNT
    installed=$root/usr/local/bin/odile-install.sh
    manifest=$root/etc/odile/manifest.json
    signature=$manifest.sig
    failed=0
    for path in "$installed" "$manifest" "$signature" \
                "$root/usr/local/lib/odile/manifest.py" \
                "$root/usr/local/lib/odile/odile-manifest.py" \
                "$root/etc/systemd/system/odile-manifest.service"; do
        if [ -e "$path" ]; then
            stat -c 'stat         %U:%G 0%a %s %n' "$path"
        else
            say missing "$path"
            failed=1
        fi
    done
    [ -f "$installed" ] && sha256sum "$installed" "$manifest" "$signature" || true
    for pair in "$installed:$REPO/release/odile_install.py" \
                "$manifest:$REPO/release/manifest.json" \
                "$signature:$REPO/release/manifest.json.sig"; do
        left=${pair%%:*}; right=${pair#*:}
        if [ -f "$left" ] && cmp -s "$left" "$right"; then
            say same "byte-identical to ${right#"$REPO"/}: ${left#"$root"}"
        else
            say differ "${left#"$root"} is not ${right#"$REPO"/}"
            failed=1
        fi
    done
    if [ -f "$installed" ] && [ -f "$manifest" ]; then
        placed=$(sha256sum "$installed" | cut -d' ' -f1)
        named=$(/usr/bin/python3 -c \
            'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["installer_sha256"])' \
            "$manifest")
        if [ "$placed" = "$named" ]; then
            say same "manifest installer_sha256 $named = the placed installer"
        else
            say differ "placed installer $placed, manifest names $named"
            failed=1
        fi
    fi
    if [ "$(stat -c '%U:%G %a' "$installed" 2>/dev/null)" = "root:root 755" ]; then
        say same "/usr/local/bin/odile-install.sh is root:root 0755"
    else
        say differ "/usr/local/bin/odile-install.sh is not root:root 0755"
        failed=1
    fi
    log=$root/var/log/odile-firstboot.log
    if [ -f "$log" ]; then
        say placement "the last release-placement block in /var/log/odile-firstboot.log:"
        awk '/=== odile-firstboot .*: release placement/{block=""} {block=block $0 "\n"} END{printf "%s", block}' "$log"
    else
        say missing "$log (the placement record)"
        failed=1
    fi
    seal_cleanup
    trap - EXIT
    return "$failed"
}

tamper_manifest() {
    seal_open rw
    target=$SEAL_MNT/etc/odile/manifest.json
    if [ -f "$target" ]; then
        say tamper "$target (the served pair)"
    else
        target=$SEAL_MNT/usr/local/lib/odile-image/manifest.json
        say tamper "$target (the image's pair)"
    fi
    [ -f "$target" ] || { echo "firstboot-bench: no manifest pair on the disk" >&2; exit 1; }
    printf 'tampered\n' >> "$target"
    sync
    seal_cleanup
    trap - EXIT
}

case "$COMMAND" in
    prepare)
        mkdir -p "$DIR"
        chmod 700 "$DIR"
        if [ -s "$DIR/token" ]; then
            say same "fixture $DIR (token $(head -c 8 "$DIR/token")…)"
        else
            prepare_args=(--dir "$DIR" --instance "$INSTANCE")
            [ -n "$SEED_KEY" ] && prepare_args+=(--key "$SEED_KEY")
            [ -n "$RELEASE_KEY" ] && prepare_args+=(--release-key "$RELEASE_KEY")
            /usr/bin/python3 "$HERE/firstboot-fixture.py" prepare \
                "${prepare_args[@]}" | tee "$DIR/prepare.log"
        fi
        ;;

    build)
        [ -s "$DIR/token" ] || { echo "firstboot-bench: run prepare first" >&2; exit 2; }
        if [ -s "$OUT/odile-tenant.efi" ] && [ "$FORCE" != 1 ]; then
            say same "$OUT/odile-tenant.efi exists (pass --force to rebuild)"
        else
            force_args=()
            if [ "$FORCE" = 1 ]; then force_args=(--force); fi
            if [ ! -s "$OUT/boot/initrd.img" ] || [ "$FORCE" = 1 ]; then
                "$HERE/sealed-initrd.sh" --out "$OUT" "${force_args[@]}"
            fi
            say build "sealed disk with the token in the measured line"
            ODILE_SEED_TOKEN="$(token)" ODILE_SEED_URL="$GUEST_SEED_URL" \
                "$HERE/sealed-image.sh" --out "$OUT" "${force_args[@]}"
            "$HERE/uki.sh" --in "$OUT" --sign --pin "$HERE/tenant-image.pin"
            pin_action=$(place_guest_pin)
            say "$pin_action" "the pin inside the guest (/etc/odile/tenant-image.pin)"
            record_disk_hash
            # The pin moved its own raw_image_sha256, so the guest copy is one
            # field stale; a second place writes the final bytes and `cmp` in
            # the evidence then compares the guest file with this one.
            pin_action=$(place_guest_pin)
            say "$pin_action" "the final pin inside the guest"
        fi
        if [ ! -s "$DIR/fresh.raw" ] || [ "$FORCE" = 1 ]; then
            say copy "pre-boot disk -> $DIR/fresh.raw"
            cp --sparse=always "$OUT/odile-tenant-sealed.raw" "$DIR/fresh.raw"
        fi
        ;;

    restore-fresh)
        [ -s "$DIR/fresh.raw" ] || { echo "firstboot-bench: no $DIR/fresh.raw" >&2; exit 2; }
        say restore "$DIR/fresh.raw -> $OUT/odile-tenant-sealed.raw"
        cp --sparse=always "$DIR/fresh.raw" "$OUT/odile-tenant-sealed.raw"
        ;;

    boot)
        case "$EXPECT" in
            installed|same|fail-closed) ;;
            *) echo "firstboot-bench: --expect installed|same|fail-closed" >&2; exit 2 ;;
        esac
        [ -s "$OUT/odile-tenant.efi" ] || { echo "firstboot-bench: build first" >&2; exit 2; }
        [ -s "$DIR/token" ] || { echo "firstboot-bench: run prepare first" >&2; exit 2; }
        start_seed
        "$HERE/smoke.sh" sealed \
            --disk "$OUT/odile-tenant-sealed.raw" \
            --uki "$OUT/odile-tenant.efi" \
            --expect opened \
            --expect-firstboot "$EXPECT" \
            --first-visit-port "$HOST_FIRST_VISIT_PORT" \
            --first-visit-token "$(token)" \
            --manifest-port "$HOST_MANIFEST_PORT" \
            --manifest-file "$REPO/release/manifest.json" \
            --manifest-out "$DIR/boot-$EXPECT/manifest.served.json" \
            --out "$DIR/boot-$EXPECT"
        ;;

    inspect-placement) inspect_placement ;;

    tamper-manifest) tamper_manifest ;;

    stop) stop_seed ;;
    show) /usr/bin/python3 "$HERE/firstboot-fixture.py" show --dir "$DIR" ;;
    cleanup)
        stop_seed
        rm -rf "$DIR"
        say clean "$DIR removed"
        ;;
    *) usage >&2; exit 2 ;;
esac
