#!/bin/bash
# uki.sh — build the tenant unified kernel image, sign it, measure it twice,
# and pin the measurement (ODL-380, extended by ODL-454).
#
# Inputs are the kernel, initramfs and command line recorded from the disk
# image, so the UKI is built from the same definition the guest boots. The
# measurement is computed against Servury's published AmdSev firmware, not this
# builder's: the firmware bytes are downloaded from the public release
# directory, checked against SHA256SUMS, and fed to sev-snp-measure as
# --snp-ovmf-hash.
#
#   ./deploy/image/uki.sh --in deploy/image/out --dry-run
#   ./deploy/image/uki.sh --in deploy/image/out-sealed --sign --pin deploy/image/tenant-image.pin
#   ./deploy/image/uki.sh --in deploy/image/out-sealed --variant odl454-other
#
# The build is deterministic: two `ukify build` runs must produce the same
# unsigned UKI, or the script fails. Signing happens once, afterwards, because
# sbsign stamps a signing time into the PKCS#7 signature; the signed file is
# pinned by its sha256 and its measurement, and the unsigned file is pinned
# separately so a rebuild can be compared byte for byte.
#
# Outputs under the input directory:
#   odile-tenant.efi            the UKI (signed with --sign)
#   odile-tenant.efi.unsigned   the same build before signing
#   ovmf/OVMF.amdsev.fd         Servury's published firmware, hash-checked
#   measurements.txt            commands, hashes and the repeated runs
#   uki-inspect.txt             the UKI's sections
#   variant/cmdline-<token>     a changed-seal_id command line
#   variant/odile-tenant-<token>.efi  the UKI built from it (fail-closed test)
#   <pin>                       the JSON pin, when --pin is given
#
# Exit codes: 0 ok, 1 a build or check failed, 2 bad input or a missing tool.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin
export PATH

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IN=$HERE/out
DRY_RUN=0
SIGN=0
SIGN_KEY=${ODILE_IMAGE_SIGNING_KEY:-/root/.odile/image-signing.key}
SIGN_CERT=$HERE/odile-image-signing.crt
PIN=""
VARIANT=""
# Where the pin goes inside the image, and whether this run puts it there
# (ODL-947). `place-pin.sh` is the program the runbook used to name as a fifth
# command an operator had to remember; the walk that forgot it built a
# production image whose first boot printed `record skipped (no pinned release at
# /etc/odile/tenant-image.pin)` and never ran the release half — no Caddy, no
# /verify, no /first-visit, and nothing failed. The disk is in this input
# directory by construction (`sealed-image.sh` wrote it and the pin hashes it),
# so a production image's pin is placed by the command that writes it.
PLACE_PIN=${ODILE_PLACE_PIN:-$HERE/place-pin.sh}
NO_PLACE=0

RELEASE=2026.11.1
BASE_URL=${ODILE_BLACKBOX_BASE:-https://servury.com/downloads/blackbox}
OVMF_SHA256=8731846cdbcbb6aa5c624238b839544b1553f4c5af874450f043321b3ef8958b
VCPUS=${ODILE_BENCH_VCPUS:-2}
VCPU_TYPE=EPYC-v4
GUEST_FEATURES=0x1
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-1748736000}
export SOURCE_DATE_EPOCH

usage() {
    cat <<'EOF'
usage: uki.sh [--in DIR] [--sign] [--sign-key FILE] [--cert FILE]
              [--pin FILE] [--no-place-pin] [--variant TOKEN] [--vcpus N]
              [--dry-run]

  --in DIR       directory holding boot/vmlinuz, boot/initrd.img, boot/cmdline
                 and boot/uname (default: deploy/image/out)
  --sign         sign the UKI with sbsign through ukify; requires the image
                 signing key (default /root/.odile/image-signing.key) and the
                 committed certificate deploy/image/odile-image-signing.crt
  --sign-key F   signing key (default: $ODILE_IMAGE_SIGNING_KEY)
  --cert F       signing certificate (default: deploy/image/odile-image-signing.crt)
  --pin F        write the JSON pin after the measurements agree. On a
                 production image the pin is then placed inside
                 DIR/odile-tenant-sealed.raw with place-pin.sh (needs root: it
                 opens the sealed disk), so the image boots with its pin
  --no-place-pin keep the pin outside the disk. Refused for a production
                 image (--production wrote the disk this command is given);
                 accepted for a bench disk, whose own build places the pin
  --variant T    build a second UKI whose only change is seal_id=T; used to
                 prove a changed command line breaks the seal
  --vcpus N      the tenant's vCPU count (default $ODILE_BENCH_VCPUS, else 2).
                 The count is an input to the SNP measurement: a tenant with a
                 different number of cores measures differently, so the pin
                 must be built with the tenant's number, not the bench's.
  --dry-run      print the commands and change nothing
EOF
}

say() { printf '%-12s %s\n' "$1" "$2"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --in) IN=$2; shift 2 ;;
        --sign) SIGN=1; shift ;;
        --sign-key) SIGN_KEY=$2; shift 2 ;;
        --cert) SIGN_CERT=$2; shift 2 ;;
        --pin) PIN=$2; shift 2 ;;
        --no-place-pin) NO_PLACE=1; shift ;;
        --variant) VARIANT=$2; shift 2 ;;
        --vcpus) VCPUS=$2; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "uki: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

IN=$(cd "$IN" 2>/dev/null && pwd) || { echo "uki: no such input directory: $IN" >&2; exit 2; }
VMLINUZ=$IN/boot/vmlinuz
INITRD=$IN/boot/initrd.img
CMDLINE_FILE=$IN/boot/cmdline
UNAME_FILE=$IN/boot/uname
OSREL_FILE=$IN/boot/os-release
for f in "$VMLINUZ" "$INITRD" "$CMDLINE_FILE" "$UNAME_FILE"; do
    [ -s "$f" ] || { echo "uki: missing or empty $f (run mkdisk.sh or sealed-initrd.sh first)" >&2; exit 2; }
done
CMDLINE=$(cat "$CMDLINE_FILE")

# --- the pin, and the disk that must carry it (ODL-947) ---------------------
# A `--production` image carries the bootstrap key `place-pin.sh` opens its
# container with, and sealed-image.sh records that in sealed/seal.txt. A bench
# disk is sealed to the chip (or to the bench stand-in) and has no bootstrap
# key, so its own build places the pin with the stand-in. Both are decided
# before the build does any work: a run that cannot place its pin must fail in
# seconds with a named sentence, not after a six-minute debootstrap.
DISK=$IN/odile-tenant-sealed.raw
SEAL_FILE=$IN/sealed/seal.txt
PRODUCTION=0
if [ -f "$SEAL_FILE" ] && grep -q '^production=yes$' "$SEAL_FILE"; then
    PRODUCTION=1
fi
PLACE=0
if [ "$PRODUCTION" = 1 ]; then
    PLACE=1
fi
if [ "$NO_PLACE" = 1 ]; then
    PLACE=0
fi

OVMF=$IN/ovmf/OVMF.amdsev.fd
UKI=$IN/odile-tenant.efi
# --snp-ovmf-hash with --mode snp asserts inside sev-snp-measure 0.0.13
# (gctx.update_zero_pages, measured 2026-11-08); the file itself is hashed and
# its sha256 is checked against the published manifest above, which is the
# same guarantee. --mode snp:ovmf-hash is a different digest and is not used.
measurement_uki=(sev-snp-measure --mode snp --ovmf "$OVMF"
    --kernel "$UKI" --vcpus "$VCPUS" --vcpu-type "$VCPU_TYPE" --guest-features "$GUEST_FEATURES")
measurement_unsigned=(sev-snp-measure --mode snp --ovmf "$OVMF"
    --kernel "$UKI.unsigned" --vcpus "$VCPUS" --vcpu-type "$VCPU_TYPE" --guest-features "$GUEST_FEATURES")
measurement_loose=(sev-snp-measure --mode snp --ovmf "$OVMF"
    --kernel "$VMLINUZ" --initrd "$INITRD" --append "$CMDLINE"
    --vcpus "$VCPUS" --vcpu-type "$VCPU_TYPE" --guest-features "$GUEST_FEATURES")

if [ "$DRY_RUN" = 1 ]; then
    say would "download $BASE_URL/$RELEASE/OVMF.amdsev.fd (sha256 $OVMF_SHA256)"
    say would "build $UKI twice from $VMLINUZ + $INITRD + boot/cmdline and compare the bytes"
    if [ "$SIGN" = 1 ]; then
        say would "sign with sbsign: key $SIGN_KEY, cert $SIGN_CERT"
    fi
    say would "run twice (signed): ${measurement_uki[*]}"
    say would "run twice (unsigned): ${measurement_unsigned[*]}"
    say would "run twice: ${measurement_loose[*]}"
    if [ -n "$VARIANT" ]; then
        say would "build $IN/variant/odile-tenant-$VARIANT.efi with seal_id=$VARIANT"
    fi
    if [ -n "$PIN" ]; then
        say would "write the pin to $PIN"
        if [ "$PLACE" = 1 ]; then
            say would "place it inside $DISK with $PLACE_PIN (the disk carries /etc/odile/tenant-image.pin)"
            if [ ! -f "$DISK" ] || [ ! -x "$PLACE_PIN" ] || [ "$(id -u)" != 0 ]; then
                say would "refuse: placing the pin needs $DISK, $PLACE_PIN and root"
            fi
        else
            say would "leave the pin outside the disk (not a production image, or --no-place-pin)"
        fi
    elif [ "$PRODUCTION" = 1 ]; then
        say would "refuse: a production image needs --pin, which is placed inside $DISK"
    fi
    exit 0
fi

# A production image's pin is not optional (ODL-947). Both refusals are named
# and come before the first write: the UKI alone is not a bootable image, and a
# disk whose /etc/odile/tenant-image.pin is missing boots with no Caddy and
# still reports success.
if [ "$PRODUCTION" = 1 ] && [ "$NO_PLACE" = 1 ]; then
    echo "uki: --no-place-pin on a production image. This disk would boot with no" >&2
    echo "     /etc/odile/tenant-image.pin: odile-firstboot prints 'record skipped" >&2
    echo "     (no pinned release ...)' and odile-release-setup never runs, so the" >&2
    echo "     machine has no Caddy, no /verify and no /first-visit, and nothing" >&2
    echo "     fails. Drop --no-place-pin (ODL-947)." >&2
    exit 2
fi
if [ "$PRODUCTION" = 1 ] && [ -z "$PIN" ]; then
    echo "uki: a production image needs its pin: pass --pin FILE. This command" >&2
    echo "     writes the pin and places it inside $DISK; without it the first" >&2
    echo "     boot prints 'record skipped (no pinned release ...)' and never runs" >&2
    echo "     the release half — no Caddy, no /verify, no /first-visit (ODL-947)." >&2
    exit 2
fi

if [ "$PLACE" = 1 ] && [ "$DRY_RUN" != 1 ]; then
    if [ ! -f "$DISK" ]; then
        echo "uki: --pin was given but there is no $DISK to carry it; a production" >&2
        echo "     image without its pin boots with no Caddy (ODL-947)" >&2
        exit 2
    fi
    if [ "$(id -u)" != 0 ]; then
        echo "uki: placing the pin opens the sealed disk, which needs root; run this" >&2
        echo "     command under sudo (or pass --no-place-pin, which a production" >&2
        echo "     image refuses)" >&2
        exit 2
    fi
    if [ ! -x "$PLACE_PIN" ]; then
        echo "uki: no place-pin program at $PLACE_PIN (set ODILE_PLACE_PIN)" >&2
        exit 2
    fi
fi

for tool in sev-snp-measure curl sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "uki: missing $tool" >&2; exit 2; }
done
[ -f /usr/lib/systemd/boot/efi/linuxx64.efi.stub ] || {
    echo "uki: missing /usr/lib/systemd/boot/efi/linuxx64.efi.stub (apt install systemd-boot-efi)" >&2
    exit 2
}
if [ "$SIGN" = 1 ]; then
    for tool in sbsign sbverify openssl; do
        command -v "$tool" >/dev/null 2>&1 || { echo "uki: missing $tool" >&2; exit 2; }
    done
    [ -s "$SIGN_KEY" ] || {
        echo "uki: missing signing key $SIGN_KEY (openssl req -x509 -newkey rsa:3072 ...)" >&2
        exit 2
    }
    [ -s "$SIGN_CERT" ] || { echo "uki: missing signing certificate $SIGN_CERT" >&2; exit 2; }
fi

mkdir -p "$IN/ovmf"

# 1. Servury's firmware: the published manifest and the bytes must agree.
say fetch "$BASE_URL/$RELEASE/OVMF.amdsev.fd"
SUMS=$(mktemp)
trap 'rm -f "$SUMS"' EXIT
curl -fsSL --max-time 120 -o "$SUMS" "$BASE_URL/$RELEASE/SHA256SUMS"
MANIFEST_HASH=$(awk '$2 == "OVMF.amdsev.fd" { print $1 }' "$SUMS")
[ "$MANIFEST_HASH" = "$OVMF_SHA256" ] || {
    echo "uki: the published SHA256SUMS names $MANIFEST_HASH, expected $OVMF_SHA256" >&2
    exit 1
}
if [ -f "$OVMF" ] && [ "$(sha256sum "$OVMF" | cut -d' ' -f1)" = "$OVMF_SHA256" ]; then
    say same "$OVMF (matches the published hash)"
else
    curl -fsSL --max-time 300 -o "$OVMF" "$BASE_URL/$RELEASE/OVMF.amdsev.fd"
    GOT=$(sha256sum "$OVMF" | cut -d' ' -f1)
    [ "$GOT" = "$OVMF_SHA256" ] || { echo "uki: OVMF hash $GOT != $OVMF_SHA256" >&2; exit 1; }
    say fetched "$OVMF ($(stat -c %s "$OVMF") bytes, hash verified)"
fi

# 2. The UKI, from the image's own kernel and initramfs. .osrel must describe
# the image, not the builder: without --os-release ukify embeds the
# /etc/os-release of the machine that ran it (measured 2026-11-08: a Debian 13
# image's UKI said "Ubuntu 26.04.1 LTS" because the bench host is Ubuntu).
mkdir -p "$IN/build"
build_uki() {
    local out=$1
    shift
    local args=(/usr/bin/python3 /usr/lib/systemd/ukify build
        --linux "$VMLINUZ"
        --initrd "$INITRD"
        --cmdline "$CMDLINE"
        --uname "$(cat "$UNAME_FILE")"
        --output "$out")
    if [ -s "$OSREL_FILE" ]; then
        args+=(--os-release "@$OSREL_FILE")
    fi
    "${args[@]}" "$@" >/dev/null
}
say build "$UKI (run 1, unsigned)"
build_uki "$IN/build/odile-tenant.efi"
say build "$UKI (run 2, unsigned)"
build_uki "$IN/build/odile-tenant.efi.run2"
cmp -s "$IN/build/odile-tenant.efi" "$IN/build/odile-tenant.efi.run2" || {
    echo "uki: two unsigned builds differ; the UKI is not reproducible" >&2
    exit 1
}
mv -f "$IN/build/odile-tenant.efi" "$UKI.unsigned"
rm -f "$IN/build/odile-tenant.efi.run2"
say built "$UKI.unsigned ($(stat -c %s "$UKI.unsigned") bytes, identical twice)"

if [ "$SIGN" = 1 ]; then
    say sign "sbsign via ukify ($SIGN_CERT)"
    build_uki "$UKI" --secureboot-private-key "$SIGN_KEY" --secureboot-certificate "$SIGN_CERT"
    sbverify --cert "$SIGN_CERT" "$UKI" >/dev/null || {
        echo "uki: sbverify rejected the signed UKI" >&2
        exit 1
    }
    say signed "$UKI ($(stat -c %s "$UKI") bytes; sbverify OK)"
else
    ln -f "$UKI.unsigned" "$UKI"
fi
/usr/bin/python3 /usr/lib/systemd/ukify inspect "$UKI" > "$IN/uki-inspect.txt"

# 3. Two identical runs of each measurement. The UKI-as-kernel form is what a
#    QEMU `-kernel odile-tenant.efi` launch measures; the loose form is what
#    the firmware measures when it loads the kernel/initrd/cmdline from the
#    ESP. Both are recorded so the runbook can name which one it means.
say measure "signed UKI as the kernel blob (run 1)"
UKI_M1=$("${measurement_uki[@]}")
say measure "signed UKI as the kernel blob (run 2)"
UKI_M2=$("${measurement_uki[@]}")
[ "$UKI_M1" = "$UKI_M2" ] || { echo "uki: UKI measurements differ: $UKI_M1 vs $UKI_M2" >&2; exit 1; }

say measure "unsigned UKI as the kernel blob (run 1)"
UNS_M1=$("${measurement_unsigned[@]}")
say measure "unsigned UKI as the kernel blob (run 2)"
UNS_M2=$("${measurement_unsigned[@]}")
[ "$UNS_M1" = "$UNS_M2" ] || { echo "uki: unsigned measurements differ: $UNS_M1 vs $UNS_M2" >&2; exit 1; }

say measure "loose kernel+initrd+cmdline (run 1)"
LOOSE_M1=$("${measurement_loose[@]}")
say measure "loose kernel+initrd+cmdline (run 2)"
LOOSE_M2=$("${measurement_loose[@]}")
[ "$LOOSE_M1" = "$LOOSE_M2" ] || { echo "uki: loose measurements differ: $LOOSE_M1 vs $LOOSE_M2" >&2; exit 1; }

CERT_SHA256=""
CERT_FP=""
if [ "$SIGN" = 1 ]; then
    CERT_SHA256=$(sha256sum "$SIGN_CERT" | cut -d' ' -f1)
    CERT_FP=$(openssl x509 -in "$SIGN_CERT" -noout -fingerprint -sha256 | cut -d= -f2)
fi

{
    printf 'release=%s\n' "$RELEASE"
    printf 'ovmf_url=%s/%s/OVMF.amdsev.fd\n' "$BASE_URL" "$RELEASE"
    printf 'ovmf_sha256=%s\n' "$OVMF_SHA256"
    printf 'uki=%s\n' "$UKI"
    printf 'uki_sha256=%s\n' "$(sha256sum "$UKI" | cut -d' ' -f1)"
    printf 'uki_unsigned_sha256=%s\n' "$(sha256sum "$UKI.unsigned" 2>/dev/null | cut -d' ' -f1 || true)"
    printf 'kernel_sha256=%s\n' "$(sha256sum "$VMLINUZ" | cut -d' ' -f1)"
    printf 'initrd_sha256=%s\n' "$(sha256sum "$INITRD" | cut -d' ' -f1)"
    printf 'cmdline=%s\n' "$CMDLINE"
    printf 'vcpus=%s vcpu_type=%s guest_features=%s\n' "$VCPUS" "$VCPU_TYPE" "$GUEST_FEATURES"
    printf 'command_uki=%s\n' "${measurement_uki[*]}"
    printf 'measurement_uki_run1=%s\n' "$UKI_M1"
    printf 'measurement_uki_run2=%s\n' "$UKI_M2"
    printf 'command_unsigned=%s\n' "${measurement_unsigned[*]}"
    printf 'measurement_unsigned_run1=%s\n' "$UNS_M1"
    printf 'measurement_unsigned_run2=%s\n' "$UNS_M2"
    printf 'command_loose=%s\n' "${measurement_loose[*]}"
    printf 'measurement_loose_run1=%s\n' "$LOOSE_M1"
    printf 'measurement_loose_run2=%s\n' "$LOOSE_M2"
    printf 'sign_cert=%s\n' "$SIGN_CERT"
    printf 'sign_cert_sha256=%s\n' "$CERT_SHA256"
    printf 'sign_cert_fingerprint=%s\n' "$CERT_FP"
} > "$IN/measurements.txt"

if [ -n "$VARIANT" ]; then
    VARIANT_CMDLINE=$(printf '%s\n' "$CMDLINE" | sed -E "s/(^| )seal_id=[^ ]*/\1seal_id=$VARIANT/")
    case " $VARIANT_CMDLINE " in
        *" seal_id=$VARIANT "*) ;;
        *) echo "uki: the base cmdline has no seal_id= token to change" >&2; exit 1 ;;
    esac
    mkdir -p "$IN/variant"
    printf '%s\n' "$VARIANT_CMDLINE" > "$IN/variant/cmdline-$VARIANT"
    variant_args=(/usr/bin/python3 /usr/lib/systemd/ukify build
        --linux "$VMLINUZ" --initrd "$INITRD" --cmdline "$VARIANT_CMDLINE"
        --uname "$(cat "$UNAME_FILE")" --output "$IN/variant/odile-tenant-$VARIANT.efi")
    if [ -s "$OSREL_FILE" ]; then
        variant_args+=(--os-release "@$OSREL_FILE")
    fi
    "${variant_args[@]}" >/dev/null
    say variant "$IN/variant/odile-tenant-$VARIANT.efi (seal_id=$VARIANT)"
fi

if [ -n "$PIN" ]; then
    /usr/bin/python3 - "$IN" "$PIN" "$UKI_M1" "$UKI_M2" "$UNS_M1" "$UNS_M2" \
        "$LOOSE_M1" "$LOOSE_M2" "$CERT_SHA256" "$CERT_FP" \
        "$VCPUS" "$VCPU_TYPE" "$GUEST_FEATURES" <<'PY'
import hashlib
import json
import pathlib
import sys

indir = pathlib.Path(sys.argv[1])
pin_path = pathlib.Path(sys.argv[2])
uki_m1, uki_m2, uns_m1, uns_m2, loose_m1, loose_m2 = sys.argv[3:9]
cert_sha256, cert_fp = sys.argv[9], sys.argv[10]
vcpus, vcpu_type, guest_features = sys.argv[11], sys.argv[12], sys.argv[13]


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


order_file = indir / "boot" / "initrd-order.txt"
order = []
hook_sha = ""
if order_file.is_file():
    for line in order_file.read_text().splitlines():
        if line.strip().startswith("/scripts/"):
            order.append(line.strip())
        if line.startswith("hook_sha256="):
            hook_sha = line.split("=", 1)[1]
seal = {}
seal_file = indir / "sealed" / "seal.txt"
if seal_file.is_file():
    for line in seal_file.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            seal[key] = value
cmdline = (indir / "boot" / "cmdline").read_text().strip()
seal_id = ""
for token in cmdline.split():
    if token.startswith("seal_id="):
        seal_id = token.split("=", 1)[1]
pin = {
    "schema": 1,
    "card": "ODL-456",
    "release": "2026.11.1",
    "ovmf_sha256": sha(indir / "ovmf" / "OVMF.amdsev.fd"),
    "stub_sha256": sha("/usr/lib/systemd/boot/efi/linuxx64.efi.stub"),
    "kernel_sha256": sha(indir / "boot" / "vmlinuz"),
    "kernel_release": (indir / "boot" / "uname").read_text().strip(),
    "initrd_sha256": sha(indir / "boot" / "initrd.img"),
    "hook_sha256": hook_sha,
    "order": order,
    "cmdline": cmdline,
    "seal_id": seal_id,
    "seal_key_id": seal.get("key_id", ""),
    "seal_blob_sha256": seal.get("blob_sha256", ""),
    "boot_key": seal.get("boot_key", ""),
    "boot_key_private_half": seal.get("private_half", ""),
    "vcpus": int(vcpus),
    "vcpu_type": vcpu_type,
    "guest_features": guest_features,
    "uki_unsigned": {"sha256": sha(indir / "odile-tenant.efi.unsigned"), "measurement": uns_m1},
    "uki_signed": {"sha256": sha(indir / "odile-tenant.efi"), "measurement": uki_m1},
    "signature": {
        "tool": "sbsign (through ukify)",
        "cert_sha256": cert_sha256,
        "cert_fingerprint_sha256": cert_fp,
        "verified": bool(cert_sha256),
    },
    "loose_measurement": loose_m1,
    "runs": {
        "uki_signed": [uki_m1, uki_m2],
        "uki_unsigned": [uns_m1, uns_m2],
        "loose": [loose_m1, loose_m2],
    },
    "raw_image_sha256": seal.get("disk_sha256", ""),
}
pin_path.write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n")
print(f"pinned       {pin_path}")
PY

    # The pin must travel **inside** the disk (ODL-947). It used to be a fifth
    # command an operator ran after this one, and the walk that omitted it built
    # a production image whose first boot printed `record skipped (no pinned
    # release at /etc/odile/tenant-image.pin)` and never ran the release half:
    # no Caddy, no /verify, no /first-visit, and nothing failed. The disk is in
    # this input directory by construction — `sealed-image.sh` wrote it and the
    # pin hashes it — so writing the pin and placing it are one step.
    if [ "$PLACE" = 1 ]; then
        say place "$PLACE_PIN --disk $DISK --pin $PIN"
        if ! "$PLACE_PIN" --disk "$DISK" --pin "$PIN"; then
            echo "uki: place-pin.sh failed: $DISK does not carry $PIN" >&2
            exit 1
        fi
        say pinned "the pin inside $DISK (/etc/odile/tenant-image.pin)"
    fi
fi

printf '\n%s\n' "signed UKI measurement (identical twice): $UKI_M1"
printf '%s\n' "unsigned UKI measurement (identical twice): $UNS_M1"
printf '%s\n' "loose measurement (identical twice): $LOOSE_M1"
printf '%s\n' "recorded in $IN/measurements.txt"
