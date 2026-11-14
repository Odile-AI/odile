#!/bin/bash
# ODL-215 follow-up proof, run inside a fresh QEMU bench guest as root.
#
# The bench image's first boot places the signed release installer at
# /usr/local/bin/odile-install.sh and the signed manifest at
# /etc/odile/manifest.json. This probe runs that installer — the step a tenant's
# user (or their agent) runs after first boot — and asserts what it left: the
# nightly backup program, its two units, the enabled timer, one unit run that
# writes /var/lib/odile/backup.json, and the heartbeat the production route
# accepted.
#
# The installer's tenant mode ends with a 15-minute wait for
# <slug>.odile.fyi DNS/ACME; the bench has no such record. The probe stops the
# installer as soon as it reaches that wait, which is after every placement
# step; the log it prints is the installer's own output.
#
# One substitution, and it is the repository only: the bench seed's URL names
# sftp:bench@backup.odile.test:/srv/odl-bench, and no such box exists yet
# (t_8eb17a54). The probe points the unit at a local restic repository created
# with the password in the guest's own recovery file, runs the unit, and puts
# the seed's URL back. Everything else — the program, the units, the paths, the
# heartbeat URL — is the installer's.
#
# Exit: 0 every assertion held; 1 one did not (the transcript says which).
set -u
exec >/dev/ttyS0 2>&1

FAILED=0
fail() { echo "FAIL         $*"; FAILED=1; }

echo "=== odile-backup-probe start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
INSTALLER=/usr/local/bin/odile-install.sh
TOKEN=$(sed -n 's/.*odile\.token=\([0-9a-f]\{64\}\).*/\1/p' /proc/cmdline)
IID=$(sed -n 's/.*bb\.iid=\([^ ]*\).*/\1/p' /proc/cmdline)
MEAS=$(/usr/bin/python3 -c 'import json;print(json.load(open("/etc/odile/tenant-image.pin"))["uki_signed"]["measurement"])' 2>/dev/null)
echo "token ${TOKEN:0:8}… instance $IID measurement ${MEAS:0:12}…"

echo "=== before the install"
systemctl is-enabled odile-backup-tenant.timer 2>&1 || true
ls -l /usr/local/lib/odile/backup.py 2>&1 || true

echo "=== remove the three artifacts, so the installer's own write lines are visible"
systemctl disable --now odile-backup-tenant.timer >/dev/null 2>&1 || true
rm -f /usr/local/lib/odile/backup.py \
    /etc/systemd/system/odile-backup-tenant.service \
    /etc/systemd/system/odile-backup-tenant.timer \
    /etc/systemd/system/timers.target.wants/odile-backup-tenant.timer
systemctl is-enabled odile-backup-tenant.timer 2>&1 || true

echo "=== the release installer the first boot placed"
sha256sum "$INSTALLER"
/usr/bin/python3 - "$INSTALLER" /etc/odile/manifest.json <<'PY'
import hashlib, json, sys
placed = hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()
manifest = json.load(open(sys.argv[2], encoding="utf-8"))
named = manifest.get("installer_sha256")
print(f"placed {placed}")
print(f"manifest installer_sha256 {named}")
print("same" if placed == named else "DIFFER")
PY
[ "$(sha256sum "$INSTALLER" | cut -d' ' -f1)" = "$(/usr/bin/python3 -c 'import json;print(json.load(open("/etc/odile/manifest.json"))["installer_sha256"])')" ] \
    || fail "the placed installer is not the one the manifest names"

echo "=== release installer run (the tenant install step)"
LOG=/var/tmp/odile-backup-probe-install.log
: >"$LOG"
/usr/bin/python3 -u "$INSTALLER" \
    --first-visit --first-visit-token "$TOKEN" \
    --token "$TOKEN" --instance-id "$IID" --expect "$MEAS" \
    --signature /usr/local/lib/odile-image/odile_install.py.sig \
    --pubkey /etc/odile/odile-signing-key.pub \
    --slug odl-bench --user tenant --port 9130 \
    --public-url https://odl-bench.odile.fyi >"$LOG" 2>&1 &
pid=$!
reached=0
for _ in $(seq 1 900); do
    if grep -q '^dns  ' "$LOG" 2>/dev/null; then reached=1; break; fi
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 2
done
sleep 3
kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true
echo "--- installer log (stopped at the DNS wait: $reached)"
cat "$LOG"
[ "$reached" = 1 ] || fail "the installer did not reach the DNS wait (it stopped earlier; see its log)"

echo "=== is-enabled"
systemctl is-enabled odile-backup-tenant.timer
[ "$(systemctl is-enabled odile-backup-tenant.timer 2>/dev/null)" = enabled ] \
    || fail "odile-backup-tenant.timer is not enabled"
systemctl is-active odile-backup-tenant.timer

echo "=== files"
stat -c '%U:%G %a %s %n' /usr/local/lib/odile/backup.py \
    /etc/systemd/system/odile-backup-tenant.service \
    /etc/systemd/system/odile-backup-tenant.timer
sha256sum /usr/local/lib/odile/backup.py
[ "$(stat -c '%U:%G %a' /usr/local/lib/odile/backup.py 2>/dev/null)" = "root:root 755" ] \
    || fail "the backup program is not root:root 0755"
echo "=== unit"
grep -E '^Exec(Start|StartPost)' /etc/systemd/system/odile-backup-tenant.service
echo "=== timer"
systemctl list-timers odile-backup-tenant.timer --no-pager
echo "=== the seed's repository URL"
cat /etc/odile/backup-repo.txt
cp /etc/odile/backup-repo.txt /var/tmp/odile-backup-repo.seed
trap 'cp /var/tmp/odile-backup-repo.seed /etc/odile/backup-repo.txt 2>/dev/null' EXIT

echo /var/lib/odile/backup-repo >/etc/odile/backup-repo.txt
echo "=== backup.py init"
/usr/local/lib/odile/backup.py init || fail "backup.py init failed"
echo "=== unit run"
systemctl start odile-backup-tenant.service
systemctl show odile-backup-tenant.service -p Result -p ExecMainStatus
[ "$(systemctl show odile-backup-tenant.service -p Result --value)" = success ] \
    || fail "the unit result is not success"
echo "=== status record"
cat /var/lib/odile/backup.json || fail "no status record at /var/lib/odile/backup.json"
[ -f /var/lib/odile/backup.json ] || fail "the unit run did not write /var/lib/odile/backup.json"
echo "=== journal"
journalctl -u odile-backup-tenant.service -n 24 --no-pager
journalctl -u odile-backup-tenant.service --no-pager | grep -q 'HQ said' \
    || fail "the unit's ExecStartPost did not report an accepted heartbeat"
echo "=== repository URL put back"
cp /var/tmp/odile-backup-repo.seed /etc/odile/backup-repo.txt
cat /etc/odile/backup-repo.txt

if [ "$FAILED" = 0 ]; then
    echo "=== odile-backup-probe PASS $(date -u +%Y-%m-%dT%H:%M:%SZ)"
else
    echo "=== odile-backup-probe FAIL $(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
echo "=== odile-backup-probe done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit "$FAILED"
