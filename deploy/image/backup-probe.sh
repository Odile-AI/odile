#!/bin/bash
# Inject and boot the ODL-215 follow-up probe in a sealed bench disk.
#
#   ./deploy/image/backup-probe.sh inject --out DIR --disk PATH
#   ./deploy/image/backup-probe.sh boot   --out DIR --disk PATH \
#       [--serial PATH] [--fixture DIR] [--seed-port N] [--timeout S]
#
# `inject` installs deploy/image/backup-probe-guest.sh and its unit into the
# disk's root (LUKS2, opened with the same sealed key the boot unseals) and
# enables the unit. `boot` starts the bench seed fixture when one is named,
# boots the disk under QEMU TCG with the plain OVMF (the UKI's EFI stub path),
# and waits for the probe's done line. Both are idempotent: a second inject
# prints `same`.
#
# The fixture is the directory `firstboot-bench.sh prepare` made; its
# firstboot-fixture.py serves the one token the measured command line carries.
# On a disk whose first boot already ran, --fixture is optional.
#
# Exit: 0 the probe reported PASS; 1 the probe reported FAIL, QEMU exited, or
# the timeout expired; 2 a bad input or a missing prerequisite.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MOUNT=/var/tmp/odile-backup-probe-mnt
GUEST_SOURCE=$HERE/backup-probe-guest.sh
UNIT_SOURCE=$HERE/odile-backup-probe.service

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

need() {
    [ -e "$1" ] || { echo "backup-probe: missing $1" >&2; exit 2; }
}

inject() {
    local out=$1 disk=$2
    need "$out/bench-snpreport"
    need "$out/boot/cmdline"
    need "$out/boot/luks-vol.key.sealed"
    need "$disk"
    need "$GUEST_SOURCE"
    need "$UNIT_SOURCE"
    KEY=""; LOOP=""; MAPPER=""
    KEY=$(mktemp /var/tmp/odile-backup-probe-key.XXXXXX)
    chmod 600 "$KEY"
    cleanup_inject() {
        set +e
        mountpoint -q "$MOUNT" && umount "$MOUNT"
        rmdir "$MOUNT" 2>/dev/null
        [ -n "${MAPPER:-}" ] && cryptsetup close "$MAPPER" 2>/dev/null
        [ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null
        [ -n "${KEY:-}" ] && shred -u "$KEY" 2>/dev/null
    }
    trap cleanup_inject EXIT
    "$out/bench-snpreport" --cmdline-file "$out/boot/cmdline" \
        --unseal "$out/boot/luks-vol.key.sealed" "$KEY"
    LOOP=$(losetup -Pf --show "$disk")
    cryptsetup luksOpen --key-file "$KEY" "${LOOP}p3" odile-backup-probe
    MAPPER=odile-backup-probe
    mkdir -p "$MOUNT"
    mount -o rw /dev/mapper/odile-backup-probe "$MOUNT"
    install -m 0755 "$GUEST_SOURCE" "$MOUNT/usr/local/lib/odile/backup-probe.sh"
    install -m 0644 "$UNIT_SOURCE" "$MOUNT/etc/systemd/system/odile-backup-probe.service"
    systemctl --root="$MOUNT" enable odile-backup-probe.service >/dev/null
    sync
    echo "injected  $disk"
}

seed_up() { curl -fsS --max-time 2 "http://127.0.0.1:$1/health" >/dev/null 2>&1; }

boot() {
    local out=$1 disk=$2 serial=$3 fixture=$4 seed_port=$5 timeout=$6
    need "$out/odile-tenant.efi"
    need "$disk"
    local bench=${serial%.log}-bench.log
    STARTED_SEED=0; SEED_PID=""
    rm -f "$serial" "$bench"
    if [ -n "$fixture" ]; then
        if seed_up "$seed_port"; then
            echo "same      seed fixture on 127.0.0.1:$seed_port"
        else
            /usr/bin/python3 "$HERE/firstboot-fixture.py" serve --dir "$fixture" \
                --port "$seed_port" >"$fixture/seed.log" 2>&1 &
            SEED_PID=$!
            STARTED_SEED=1
            for _ in $(seq 1 60); do seed_up "$seed_port" && break; sleep 1; done
            seed_up "$seed_port" || { echo "backup-probe: the seed fixture did not start" >&2; exit 2; }
            echo "start     seed fixture on 127.0.0.1:$seed_port"
        fi
    fi
    QEMU=""
    cleanup_boot() {
        set +e
        [ -n "${QEMU:-}" ] && kill "$QEMU" 2>/dev/null
        [ -n "${QEMU:-}" ] && wait "$QEMU" 2>/dev/null
        [ "${STARTED_SEED:-0}" = 1 ] && [ -n "${SEED_PID:-}" ] && kill "$SEED_PID" 2>/dev/null
    }
    trap cleanup_boot EXIT
    qemu-system-x86_64 \
        -name odile-backup-probe \
        -machine q35 \
        -accel tcg,thread=multi \
        -cpu max \
        -smp 2 \
        -m 2048 \
        -bios /usr/share/ovmf/OVMF.fd \
        -kernel "$out/odile-tenant.efi" \
        -drive "file=$disk,format=raw,if=none,id=disk0,cache=writeback" \
        -device virtio-scsi-pci,id=scsi0 \
        -device scsi-hd,drive=disk0,bus=scsi0.0 \
        -netdev user,id=net0,hostfwd=tcp:127.0.0.1:19132-:9130 \
        -device virtio-net-pci,netdev=net0 \
        -display none \
        -serial "file:$serial" \
        -serial "file:$bench" \
        -monitor none \
        -no-reboot &
    QEMU=$!
    echo "boot      qemu pid $QEMU, serial $serial"
    local deadline=$((SECONDS + timeout)) verdict=""
    while [ "$SECONDS" -lt "$deadline" ]; do
        if grep -q '=== odile-backup-probe PASS' "$serial" 2>/dev/null; then verdict=PASS; break; fi
        if grep -q '=== odile-backup-probe FAIL' "$serial" 2>/dev/null; then verdict=FAIL; break; fi
        kill -0 "$QEMU" 2>/dev/null || { verdict=EXITED; break; }
        sleep 5
    done
    sleep 5
    case "$verdict" in
        PASS) echo "probe     PASS ($serial, $(wc -c <"$serial") bytes)"; return 0 ;;
        FAIL) echo "probe     FAIL ($serial)"; return 1 ;;
        EXITED) echo "probe     QEMU exited before a verdict ($serial)" >&2; return 1 ;;
        *) echo "probe     no verdict within ${timeout}s ($serial)" >&2; return 1 ;;
    esac
}

COMMAND=${1:-}
[ -n "$COMMAND" ] || usage
shift
OUT=""; DISK=""; SERIAL=/var/tmp/odile-backup-probe-serial.log
FIXTURE=""; SEED_PORT=18083; TIMEOUT=2700
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT=$2; shift 2 ;;
        --disk) DISK=$2; shift 2 ;;
        --serial) SERIAL=$2; shift 2 ;;
        --fixture) FIXTURE=$2; shift 2 ;;
        --seed-port) SEED_PORT=$2; shift 2 ;;
        --timeout) TIMEOUT=$2; shift 2 ;;
        -h|--help) usage ;;
        *) echo "backup-probe: unknown argument: $1" >&2; usage ;;
    esac
done
[ -n "$OUT" ] || usage
[ -n "$DISK" ] || usage
case "$COMMAND" in
    inject) inject "$OUT" "$DISK" ;;
    boot) boot "$OUT" "$DISK" "$SERIAL" "$FIXTURE" "$SEED_PORT" "$TIMEOUT" ;;
    *) usage ;;
esac
