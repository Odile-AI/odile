#!/bin/sh
# odile-release-setup.sh — the production first boot's second half (ODL-332).
#
# The image's own install-stack.sh brings the page up with the seed, unprivileged
# and with no network wait. This script then runs the *release installer* — the
# bytes ODL-472 placed at /usr/local/bin/odile-install.sh — in its first-visit
# mode, with the image's own user and ports, so the machine converges on the
# signed release's full tenant stack: Caddy + ACME on the tenant's name, the
# /verify surface, the per-user key file and the signed manifest placement. Running it with
# the same --user and --port as the image keeps one Webapp, not two.
#
# It is a oneshot after odile-firstboot.service, not before it: the page must
# answer first (the demo path), and the tenant-mode installer legitimately
# waits up to 15 minutes for DNS and for the certificate. A failure here leaves
# the machine reachable with the page up and writes one line to its log; it does
# not fail the boot.
#
# **It converges once** (t_ee5aac66). After its first run it writes a done
# marker under /var/lib/odile; every later boot reads the marker and exits
# without running the installer. That is what makes the owner's config survive
# a reboot: the installer's seven managed blocks (model, agent,
# custom_providers, dashboard, webapp, onboarding, auxiliary) and its .env
# allowlist are written on the first boot only, so a key the owner changed
# afterwards — `webapp.public_url`, the provider row, a key in `.env` — is not
# rewritten. The other direction is the same fact: a block the installer gains
# later is **not** merged into a machine that already ran this script, because
# no later boot reaches the installer at all — an existing demo or tenant gains
# `auxiliary.vision.model` (ODL-341-F1) by a re-image or by running this script
# with `--force`, never by a plain deploy. The marker records the
# run's exit code. A failed first run is not retried automatically; to retry it
# on purpose, remove the marker and run this script with `--force` (or
# ODILE_RELEASE_FORCE=1) — the log's earlier run line would otherwise make the
# backfill below re-write the marker instead of running the installer
# (docs/runbooks/demo-public.md §6).
#
# The measurement: the installer's --expect is the image pin, which is built
# for the tenant's vCPU count (uki.sh --vcpus). A pin that does not match this
# boot would make the installer exit 5 before it installs anything, so the
# script compares the pin with the machine's own boot report first
# (/var/lib/odile/attestation.json, ODL-397): when they disagree it installs
# with the report's measurement and logs the disagreement, and /verify still
# compares against the pin and says `the measurement is not --expect`. Caddy
# and the page come up either way; the number is never silently replaced.
#
# Every path is overridable so a harness can drive each branch without a
# machine (tests/test_release_setup.py); the defaults are the image's.
#
# Exit codes: 0 when the installer ran and this boot's access report is in
# place, 1 when the report is stale or its unit did not come up (ODL-947). A
# report is the machine's own record: before this check the boot logged
# `FAIL odile-access-report did not start` and still ended rc=0, so the console
# and the exit code disagreed and the support bundle kept whatever report an
# earlier boot had written. The release itself is not failed by either: Caddy,
# /verify and the page stand, the boot reaches multi-user, and the unit runs
# again on every boot so a failed write is repaired without a re-install.
#
# Two arguments the boot cannot get from a terminal (t_d95b8c13): the installer
# ends `rc=4` when the recovery file has not been confirmed and stdin is not a
# TTY, so this script passes `--i-have-copied-it` — the first-visit page and
# /verify are where the user downloads that file, and the machine keeps it at
# /etc/odile/recovery.txt. And `ODILE_RELEASE_PUBLIC_URL` overrides the public
# name the config carries: `bb.host` is the machine's slug, which is not the
# name an owner who moved the Webapp (ODL-503) answers on.
#
# ODL-340 follow-up: the environment a machine was built for is in its measured
# command line — `odile.seed_url=` (where the seed came from) and
# `odile.public_url=` (the name this tenant serves). This script passes both
# through, so a bench tenant serves its own name and posts its heartbeat to the
# bench with no drop-in, no second Caddy block and no config patch.
set -u

CMDLINE=${ODILE_RELEASE_CMDLINE:-/proc/cmdline}
PIN=${ODILE_RELEASE_PIN:-/etc/odile/tenant-image.pin}
ATTEST=${ODILE_RELEASE_ATTEST:-/var/lib/odile/attestation.json}
INSTALLER=${ODILE_RELEASE_INSTALLER:-/usr/local/bin/odile-install.sh}
# The image's own copy of the release's detached signature and the published
# signing key. The installer verifies itself before it does anything, and its
# defaults look beside /usr/local/bin — where firstboot places the installer but
# not the pair. Naming them here is what a downloaded kit does for the user.
#
# `--token` is separate from `--first-visit-token` on purpose: the release
# installer's seed loader (load_or_fetch_seed) reads kit["setup_token"], which
# only --token or a kit file sets. Without it the installer exits 1 with
# "the recovery kit has no 64-hex setup token" *after* the boot check passes,
# even though odile-firstboot already wrote the signed cache it reuses
# (/etc/odile/seed.json + seed.sig). Measured on the tenant, round 5:
#   boot         release 2026.11.2 · /run/blackbox/attest.json
#   error: the recovery kit has no 64-hex setup token
#   === rc=1
SIGNATURE=${ODILE_RELEASE_SIGNATURE:-/usr/local/lib/odile-image/odile_install.py.sig}
PUBKEY=${ODILE_RELEASE_PUBKEY:-/etc/odile/odile-signing-key.pub}
LOG=${ODILE_RELEASE_LOG:-/var/log/odile-release-setup.log}
DONE=${ODILE_RELEASE_DONE:-/var/lib/odile/release-setup.done}
# The report the installer's own restart just rewrote (ODL-947), and the unit
# that writes it. Both are overridable so the harness can drive the stale and
# the fresh branch without a machine.
REPORT=${ODILE_RELEASE_REPORT:-/var/lib/odile/access-report.txt}
REPORT_UNIT=${ODILE_RELEASE_UNIT:-odile-access-report}
TOKEN=""
IID=""
HOST=""
SEED_URL=""
PUBLIC_TOKEN=""
#: An explicit retry ignores both the done marker and the log backfill below:
#: the whole point of `--force` is to run the signed installer again — removing
#: the marker alone is not enough on a machine whose own log already carries a
#: run line, because the backfill would re-write the marker instead.
FORCE=""
case "${1:-}" in
    --force) FORCE=1 ;;
esac
[ "${ODILE_RELEASE_FORCE:-}" = "1" ] && FORCE=1

mkdir -p "${DONE%/*}" 2>/dev/null || true

# The one line that stops the boot rewrite. Checked before anything else, so a
# converged machine does no work even when its command line changed.
if [ -s "$DONE" ] && [ -z "$FORCE" ]; then
    echo "odile-release-setup: already converged — $(tr '\n' ' ' < "$DONE"); nothing to do"
    exit 0
fi

# A machine that ran this script before the marker existed has the run in its
# own log. Backfill rather than run the token's seed exchange again: a
# second run of the installer is exactly the rewrite this marker prevents.
if [ -z "$FORCE" ] && [ -s "$LOG" ] && grep -q '^=== odile-release-setup ' "$LOG" 2>/dev/null; then
    LAST=$(grep '^=== odile-release-setup ' "$LOG" | tail -n 1)
    RC=$(grep '^=== rc=' "$LOG" | tail -n 1)
    printf 'backfilled from %s: %s %s\n' "$LOG" "$LAST" "$RC" > "$DONE"
    echo "odile-release-setup: already ran ($LAST $RC); marker backfilled, nothing to do"
    exit 0
fi

read -r CMDLINE_TEXT < "$CMDLINE" || true
# The command line is one line of space-separated tokens; split it into words.
# shellcheck disable=SC2086
for token in $CMDLINE_TEXT; do
    case "$token" in
        odile.token=*) TOKEN=${token#odile.token=} ;;
        odile.seed_url=*) SEED_URL=${token#odile.seed_url=} ;;
        odile.public_url=*) PUBLIC_TOKEN=${token#odile.public_url=} ;;
        bb.iid=*) IID=${token#bb.iid=} ;;
        bb.host=*) HOST=${token#bb.host=} ;;
    esac
done

[ -x "$INSTALLER" ] || { echo "odile-release-setup: no $INSTALLER" >&2; exit 0; }
[ -n "$TOKEN" ] || { echo "odile-release-setup: no setup token in the command line" >&2; exit 0; }
[ -n "$HOST" ] || { echo "odile-release-setup: no bb.host in the command line" >&2; exit 0; }
[ -s "$PIN" ] || { echo "odile-release-setup: no $PIN" >&2; exit 0; }

# The public name the Webapp answers to. `bb.host` is the machine's own slug;
# a machine whose owner moved its public name (`webapp.public_url`, ODL-503)
# keeps it when this script is re-run by hand, or the install would rewrite
# the config back to the slug and the owner's Host would answer 400
# `Invalid Host header` (t_d95b8c13).
#
# ODL-340 follow-up: a second environment names the machine in its measured
# command line (`odile.public_url=`), so a bench tenant needs no systemd
# drop-in. Precedence: ODILE_RELEASE_PUBLIC_URL (an operator re-run), then the
# measured token, then production's `<slug>.odile.fyi` (today's behaviour).
PUBLIC_TOKEN=${PUBLIC_TOKEN:-}
case "$PUBLIC_TOKEN" in
    *" "*) PUBLIC_TOKEN="" ;;
    http://*|https://*) ;;
    *) PUBLIC_TOKEN="" ;;
esac
PUBLIC_URL=${ODILE_RELEASE_PUBLIC_URL:-${PUBLIC_TOKEN:-https://$HOST.odile.fyi}}
# The environment that issued the seed (ODL-340 follow-up). The installer
# derives this machine's public name and its heartbeat receiver from it, and
# the boot record's reporter (odile-firstboot) already posts to it.
case "$SEED_URL" in
    *" "*) SEED_URL="" ;;
    http://*|https://*) ;;
    *) SEED_URL="" ;;
esac
SEED_URL=${SEED_URL:-https://odile.fyi/seed}

# The measurement this image was pinned to; the installer writes it into the
# boot record and /verify compares the machine with it.
MEAS=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["uki_signed"]["measurement"])' "$PIN" 2>/dev/null) || MEAS=""
[ -n "$MEAS" ] || { echo "odile-release-setup: cannot read the pin $PIN" >&2; exit 0; }

# The number this boot actually produced, when the machine wrote one. A pin
# built for another vCPU count (or another firmware) does not match it.
BOOT_MEAS=""
if [ -s "$ATTEST" ]; then
    BOOT_MEAS=$(/usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("measurement") or "")' "$ATTEST" 2>/dev/null) || BOOT_MEAS=""
fi
EXPECT=$MEAS
if [ -n "$BOOT_MEAS" ] && [ "$BOOT_MEAS" != "$MEAS" ]; then
    EXPECT=$BOOT_MEAS
    echo "odile-release-setup: the pin is $MEAS, this boot is $BOOT_MEAS; installing with the boot's own measurement" >&2
fi

mkdir -p "${LOG%/*}" || true
# Two fixes the first real boots paid for: the installer needs visudo and sshd
# to converge (the image carries both since this build), and its output must be
# readable on the serial console as well as in the log — a failed install is
# evidence, and the console is the only surface a machine with no Caddy has.
for tool in visudo sshd apt-get; do
    command -v "$tool" >/dev/null 2>&1 || \
        echo "odile-release-setup: $tool is missing from the image" >&2
done
for file in "$SIGNATURE" "$PUBKEY"; do
    [ -s "$file" ] || echo "odile-release-setup: $file is missing from the image" >&2
done
{
    echo "=== odile-release-setup $(date -u +%Y-%m-%dT%H:%M:%SZ) · $HOST · instance $IID"
    echo "=== expect $EXPECT (pin $MEAS, boot $BOOT_MEAS)"
    /usr/bin/python3 "$INSTALLER" \
        --first-visit --first-visit-token "$TOKEN" \
        --token "$TOKEN" \
        --instance-id "$IID" --expect "$EXPECT" \
        --signature "$SIGNATURE" --pubkey "$PUBKEY" \
        --slug "$HOST" --user tenant --port 9130 \
        --seed-url "$SEED_URL" \
        --public-url "$PUBLIC_URL" --i-have-copied-it --json 2>&1
    rc=$?
    echo "=== rc=$rc"
    printf 'ran %s · %s · instance %s · rc=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$HOST" "$IID" "$rc" > "$DONE"
} 2>&1 | tee -a "$LOG" || true

# The report the unit wrote, and the unit's own state (ODL-947). The installer
# above restarts odile-access-report; before this check the boot logged
# `FAIL odile-access-report did not start` and still ended rc=0, so the console,
# the log and the exit code disagreed and the support bundle kept whatever
# report an earlier boot had left. A oneshot with RemainAfterExit=yes stays
# `active` after a run that ended 0 and reads `failed` after one that did not,
# and the file's mtime is the other half of the same fact: a report older than
# this boot is named with its own timestamp instead of being passed off as
# fresh.
BOOT_EPOCH=$(awk '$1 == "btime" { print $2; exit }' /proc/stat 2>/dev/null) || BOOT_EPOCH=""
case "$BOOT_EPOCH" in
    ''|*[!0-9]*) BOOT_EPOCH=0 ;;
esac
BOOT_AT=$(date -u -d "@$BOOT_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || BOOT_AT="unknown"
REPORT_EPOCH=$(stat -c %Y "$REPORT" 2>/dev/null) || REPORT_EPOCH=""
case "$REPORT_EPOCH" in
    ''|*[!0-9]*) REPORT_EPOCH=0 ;;
esac
REPORT_AT=$(date -u -d "@$REPORT_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || REPORT_AT="unknown"
VERDICT=0
# The check's text goes through a file, not a pipe: a `{ … } | tee` body runs
# in a subshell and the verdict set inside it would be lost, which is exactly
# the class of bug this card is about.
CHECK=$(mktemp 2>/dev/null) || CHECK="/tmp/odile-release-setup.$$.check"
{
    if [ "$REPORT_EPOCH" -gt 0 ] && { [ "$BOOT_EPOCH" -eq 0 ] || [ "$REPORT_EPOCH" -ge "$BOOT_EPOCH" ]; }; then
        echo "ok           $REPORT written $REPORT_AT (this boot started $BOOT_AT)"
    elif [ "$REPORT_EPOCH" -gt 0 ]; then
        echo "FAIL         the access report is stale: $REPORT was written $REPORT_AT, this boot started $BOOT_AT"
        VERDICT=1
    else
        echo "FAIL         the access report was not written this boot: no $REPORT"
        VERDICT=1
    fi
    if command -v systemctl >/dev/null 2>&1; then
        # `systemctl is-active` prints the state and exits non-zero for
        # everything but `active`, so the text is read before the status.
        STATE=$(systemctl is-active "$REPORT_UNIT" 2>/dev/null) || true
        [ -n "$STATE" ] || STATE="unknown"
        if [ "$STATE" = "active" ]; then
            echo "ok           $REPORT_UNIT is active"
        else
            echo "FAIL         $REPORT_UNIT is $STATE — systemctl status $REPORT_UNIT names the cause"
            VERDICT=1
        fi
    fi
    if [ "$VERDICT" = 1 ]; then
        echo "=== odile-release-setup: the access report did not pass this boot (the installer's own rc is above) — rc=1"
    fi
} > "$CHECK" 2>&1
cat "$CHECK"
cat "$CHECK" >> "$LOG" 2>/dev/null || true
rm -f "$CHECK"

exit "$VERDICT"
