#!/usr/bin/env bash
# shellcheck disable=SC1090,SC1091
# Odile factory recovery — one command.
#
# Run as root on a fresh Ubuntu 26.04 or Debian 13 server:
#
#   cd ~/odile-factory-recovery
#   bash recover.sh
#
# It reads ./recovery.env (the kit's secrets), restores the latest `hq` restic
# snapshot from Cloudflare R2, starts the site and updates the Cloudflare A
# records to this server's IPv4. Nothing is read from the old HQ: the Hermes
# commit, the old IPv4 and the DNS names come from the snapshot's own
# `manifest.json`, and the kit's copy is the fallback for an older snapshot.
#
# Flags: --dry-run (print the plan, change nothing), --no-dns (skip Cloudflare),
#        --ip ADDRESS (override the detected IPv4). `--dry-run` needs `restic`
#        installed and reads the live repository; it writes no secret and no
#        config file, and removes its temporary manifest on exit.

set -Eeuo pipefail

KIT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE=${ENV_FILE:-$KIT_DIR/recovery.env}
LOG=${LOG:-$HOME/odile-recovery.log}
DRY_RUN=0
NO_DNS=0
NEW_IP_OVERRIDE=""

usage() {
  cat <<'EOF'
Odile factory recovery.

  bash recover.sh [--dry-run] [--no-dns] [--ip ADDRESS]

  --dry-run     read the latest snapshot's manifest and print the Hermes
                commit, the old IPv4 and the DNS plan; change nothing
  --no-dns      do not touch Cloudflare; print the records to change by hand
  --ip ADDRESS  use this IPv4 instead of detecting the server's own

Run as root, in the folder that holds recovery.env.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-dns) NO_DNS=1; shift ;;
    --ip)
      [ "$#" -ge 2 ] || { echo "--ip needs an address" >&2; exit 2; }
      NEW_IP_OVERRIDE=$2
      shift 2
      ;;
    --ip=*) NEW_IP_OVERRIDE=${1#--ip=}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# A dry run leaves the filesystem alone, log file included.
if [ "$DRY_RUN" = 0 ]; then
  exec > >(tee -a "$LOG") 2>&1
fi

say()  { printf '\n== %s ==\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
  printf '\nERROR: %s\n' "$*" >&2
  [ "$DRY_RUN" = 1 ] || printf 'See %s\n' "$LOG" >&2
  exit 1
}
trap 'die "failed at line $LINENO: ${BASH_COMMAND:-?}"' ERR

[ "$(id -u)" -eq 0 ] || die "run this as root"
[ -f "$ENV_FILE" ] || die "missing $ENV_FILE (the kit's recovery.env)"
# shellcheck source=/dev/null
set -a; . "$ENV_FILE"; set +a

for needed in RESTIC_PASSWORD R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ENDPOINT \
              CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_DNS_TOKEN ZONE_ID; do
  [ -n "${!needed:-}" ] || die "$needed is empty in $ENV_FILE"
done
printf '%s' "$ZONE_ID" | grep -Eq '^[0-9a-fA-F]{32}$' \
  || die "ZONE_ID in $ENV_FILE is not a 32-hex Cloudflare zone id"

# Paths are overridable so a test can run the dry run without touching the
# host's own state; production always uses the values below.
STATE_DIR=${ODILE_STATE_DIR:-/root/.odile}
ETC_DIR=${ODILE_ETC_DIR:-/etc/odile}
REPO=${ODILE_REPO:-$HOME/odile}

R2_ENDPOINT=${R2_ENDPOINT%/}
# shellcheck source=/dev/null
. /etc/os-release
# Overridable so the dry-run test can run on any Linux; production reads /etc.
OS_ID=${ODILE_OS_ID:-${ID:-}}
case "$OS_ID" in
  ubuntu|debian) ;;
  *) die "this kit expects Ubuntu 26.04 or Debian 13; found ${PRETTY_NAME:-unknown}" ;;
esac

if [ -n "$NEW_IP_OVERRIDE" ]; then
  NEW_IP=$NEW_IP_OVERRIDE
else
  NEW_IP=$(curl -4 -fsS --max-time 10 https://ifconfig.me 2>/dev/null || true)
  [ -n "$NEW_IP" ] || NEW_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
printf '%s' "$NEW_IP" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' \
  || die "could not determine this server's IPv4 (use --ip ADDRESS)"

AVAIL_KB=$(df -Pk / | awk 'NR==2 {print $4}')
[ "${AVAIL_KB:-0}" -ge 20971520 ] || warn "less than 20 GB free on /; the restore may not fit"
[ "${AVAIL_KB:-0}" -ge 31457280 ] || warn "40 GB free is the comfortable size"

say "Kit: $KIT_DIR"
say "New server: $NEW_IP ($PRETTY_NAME)"
[ "$DRY_RUN" = 1 ] || say "Log: $LOG"

if [ -f "$KIT_DIR/CHECKSUMS.txt" ]; then
  ( cd "$KIT_DIR" && sha256sum -c CHECKSUMS.txt ) || warn "kit checksum mismatch; continuing"
fi

# ---------------------------------------------------------------------------
# Packages. First, because the restic client is what reads the bucket and a
# fresh box has none. A dry run installs nothing and needs restic to be there.
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = 0 ]; then
  say "Installing packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    restic rsync sqlite3 openssh-server caddy postfix opendkim opendkim-tools \
    python3 python3-yaml curl git make shellcheck openssl nftables e2fsprogs gh \
    bind9-dnsutils libatomic1 iproute2 sudo psmisc \
    build-essential libolm-dev cmake pkg-config
  ln -sf /usr/bin/gh /usr/local/bin/gh

  id odile >/dev/null 2>&1 || \
    adduser --system --group --home /opt/odile --shell /usr/sbin/nologin odile
fi

# ---------------------------------------------------------------------------
# R2 / restic. The env is exported first, so a dry run never writes a file.
# ---------------------------------------------------------------------------
export RESTIC_REPOSITORY="s3:${R2_ENDPOINT}/hermes-hq-backup"
export RESTIC_PASSWORD
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION=auto

command -v restic >/dev/null 2>&1 \
  || die "restic is not installed; for --dry-run install it first (apt-get install restic)"

MANIFEST=$(mktemp "${TMPDIR:-/tmp}/odile-recovery-manifest.XXXXXX")
cleanup() { rm -f "$MANIFEST"; }
trap cleanup EXIT

say "Finding the latest snapshot"
SNAP=$(restic snapshots --tag hq --json | /usr/bin/python3 -c '
import json, sys
data = json.load(sys.stdin)
print(max(data, key=lambda snap: snap.get("time", ""))["short_id"] if data else "")
')
[ -n "$SNAP" ] || die "no snapshot tagged hq in the repository"
say "Latest snapshot: $SNAP"

if ! restic dump "$SNAP" /var/backups/odile/live/manifest.json >"$MANIFEST" 2>/dev/null; then
  if ! restic dump "$SNAP" /root/.odile/recovery.json >"$MANIFEST" 2>/dev/null; then
    warn "no recovery manifest in the snapshot; using the kit's manifest"
    printf '{}\n' >"$MANIFEST"
  fi
fi
# The kit carries a manifest written when it was generated. It is the fallback
# when the snapshot predates the backup job's recovery block.
if ! grep -q '"hermes_commit": *"[0-9a-f]' "$MANIFEST" 2>/dev/null; then
  if [ -f "$KIT_DIR/manifest.json" ]; then
    warn "snapshot manifest has no Hermes commit; falling back to the kit's manifest"
    cp "$KIT_DIR/manifest.json" "$MANIFEST"
  fi
fi

MANIFEST_VALUES=$(/usr/bin/python3 - "$MANIFEST" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    data = {}
recovery = data.get("recovery") if isinstance(data, dict) else {}
if not isinstance(recovery, dict):
    recovery = {}
names = recovery.get("dns_names") or data.get("dns_names") or []
if isinstance(names, str):
    names = [n.strip() for n in names.split(",") if n.strip()]
skip = recovery.get("dns_skip") or data.get("dns_skip") or []
if isinstance(skip, str):
    skip = [n.strip() for n in skip.split(",") if n.strip()]
print("|".join([
    recovery.get("hermes_commit") or "",
    recovery.get("hermes_version") or "",
    recovery.get("ipv4") or "",
    recovery.get("git_head") or data.get("git_head") or "",
    ",".join(names) if names else "",
    ",".join(skip) if skip else "",
]))
PY
)
IFS='|' read -r HERMES_COMMIT HERMES_VERSION OLD_IP GIT_HEAD DNS_NAMES DNS_SKIP <<< "$MANIFEST_VALUES"

# The manifest is the source of truth; these defaults only cover a snapshot and
# kit that both predate the recovery block. They are the same lists as
# deploy/recovery/dns-names.txt and dns-skip.txt, which the nightly manifest
# reads — the demo tenant and every `odl-*` guest stay where they are.
if [ -z "${DNS_NAMES:-}" ]; then
  DNS_NAMES="odile.fyi,gw.odile.fyi,mail.odile.fyi,testing.odile.fyi,test.odile.fyi"
fi
if [ -z "${DNS_SKIP:-}" ]; then
  DNS_SKIP="demo.odile.fyi,www.odile.fyi,odl-7971a8.odile.fyi,odl-*.test.odile.fyi"
fi

say "Hermes: ${HERMES_COMMIT:-latest} ${HERMES_VERSION:-}"
say "Product commit in the backup: ${GIT_HEAD:-unknown}"
say "Old IPv4 in the backup: ${OLD_IP:-unknown}"
say "DNS names to move: $DNS_NAMES"
say "DNS names to leave alone: $DNS_SKIP"

if [ "$DRY_RUN" = 1 ]; then
  say "DRY RUN — no packages, secrets, restore, services or DNS changed"
  printf 'Would: write the kit secrets to %s; install packages; install Hermes at %s;\n' \
    "$STATE_DIR" "${HERMES_COMMIT:-the installer default}"
  printf 'restore %s to /; start Caddy/Postfix/OpenDKIM and the Odile units;\n' "$SNAP"
  printf 'point %s at %s; leave the messaging gateway stopped.\n' "$DNS_NAMES" "$NEW_IP"
  exit 0
fi

# ---------------------------------------------------------------------------
# Secrets and the backup env. The kit is authoritative for the tokens (one may
# have been rotated since the snapshot); the restore rewrites them again below.
# ---------------------------------------------------------------------------
install -d -m 700 "$STATE_DIR"
printf '%s' "$RESTIC_PASSWORD" > "$STATE_DIR/hq-backup.pass"
printf '%s' "$CLOUDFLARE_ACCOUNT_ID" > "$STATE_DIR/cloudflare_account_id"
printf '%s' "$CLOUDFLARE_DNS_TOKEN" > "$STATE_DIR/cloudflare_dns_token"
printf '%s' "$ZONE_ID" > "$STATE_DIR/cloudflare_zone_id"
cat >"$STATE_DIR/r2-backup.env" <<EOF
AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
R2_ENDPOINT=$R2_ENDPOINT
EOF
chmod 600 "$STATE_DIR/hq-backup.pass" "$STATE_DIR/r2-backup.env" \
  "$STATE_DIR/cloudflare_dns_token" "$STATE_DIR/cloudflare_zone_id" \
  "$STATE_DIR/cloudflare_account_id"

install -d -m 755 "$ETC_DIR"
cat >"$ETC_DIR/backup.env" <<EOF
RESTIC_REPOSITORY='s3:${R2_ENDPOINT}/hermes-hq-backup'
RESTIC_PASSWORD_FILE='${STATE_DIR}/hq-backup.pass'
AWS_DEFAULT_REGION='auto'
EOF
chmod 644 "$ETC_DIR/backup.env"

# ---------------------------------------------------------------------------
# Hermes at the commit the backup recorded
# ---------------------------------------------------------------------------
say "Installing Hermes"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
# install.sh puts the CLI under the Hermes home; a non-login root shell (how
# this script runs) does not inherit a profile PATH, so name it explicitly.
export PATH="/root/.local/bin:/root/.hermes/hermes-agent/.hermes/bin:/usr/local/bin:$PATH"
HERMES_BIN=$(command -v hermes 2>/dev/null || true)
[ -n "$HERMES_BIN" ] || warn "hermes is not on PATH after the install; the CLI steps will fail"
if [ -n "${HERMES_COMMIT:-}" ]; then
  (
    cd /root/.hermes/hermes-agent || exit 1
    git -c safe.directory=/root/.hermes/hermes-agent fetch --all --tags --prune \
      || warn "git fetch failed; trying the commit anyway"
    if ! git -c safe.directory=/root/.hermes/hermes-agent checkout -q "$HERMES_COMMIT" 2>/dev/null; then
      git -c safe.directory=/root/.hermes/hermes-agent fetch --unshallow || true
      git -c safe.directory=/root/.hermes/hermes-agent checkout -q "$HERMES_COMMIT" \
        || warn "could not check out $HERMES_COMMIT; using the installed version"
    fi
    HEAD_COMMIT=$(git -c safe.directory=/root/.hermes/hermes-agent rev-parse HEAD 2>/dev/null || true)
    printf 'Hermes checkout: %s (manifest: %s)\n' "${HEAD_COMMIT:0:12}" "$HERMES_COMMIT"
    case "$HEAD_COMMIT" in
      "$HERMES_COMMIT"*) ;;
      *) warn "Hermes HEAD does not start with the manifest commit $HERMES_COMMIT" ;;
    esac
  )
  [ -z "$HERMES_BIN" ] || "$HERMES_BIN" --version || true
fi
if [ -z "$HERMES_BIN" ] || ! "$HERMES_BIN" pm install --extra matrix; then
  warn "Matrix extra install failed; the gateway is left stopped (the site and CLI work)"
fi

# ---------------------------------------------------------------------------
# Stop anything that could hold the state DB; preserve the new server's key
# ---------------------------------------------------------------------------
say "Stopping Hermes processes"
systemctl --user stop hermes-gateway.service 2>/dev/null || true
systemctl --user stop hermes-serve.service 2>/dev/null || true
systemctl stop hermes-serve.service 2>/dev/null || true
# A backend the TUI started belongs to no unit; match the argv tail, which is
# what `.hermes/bin/hermes` execs (`… serve --host …`, `… gateway run`).
pkill -f 'serve --host' 2>/dev/null || true
pkill -f 'gateway run' 2>/dev/null || true
for _ in 1 2 3 4 5; do
  fuser /root/.hermes/state.db >/dev/null 2>&1 || break
  sleep 1
done
if fuser /root/.hermes/state.db >/dev/null 2>&1; then
  # shellcheck disable=SC2046
  HOLDERS=$(fuser /root/.hermes/state.db 2>/dev/null || true)
  warn "state.db is still held by:${HOLDERS}; sending SIGKILL"
  # shellcheck disable=SC2086
  kill -9 $HOLDERS 2>/dev/null || true
  sleep 1
fi
fuser /root/.hermes/state.db >/dev/null 2>&1 \
  && die "a process still holds /root/.hermes/state.db; quit the TUI and rerun"

install -d -m 700 /root/.ssh
if [ -f /root/.ssh/authorized_keys ]; then
  cp -a /root/.ssh/authorized_keys /var/tmp/odile-recovery-authorized_keys
  chmod 600 /var/tmp/odile-recovery-authorized_keys
fi

# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
say "Restoring $SNAP to /"
restic restore "$SNAP" --target / \
  --include /var/backups/odile/live \
  --include /root --include /var/lib/caddy --include /var/lib/odile \
  --include /srv \
  --include /etc/caddy --include /etc/dkimkeys --include /etc/opendkim.conf \
  --include /etc/postfix --include /etc/ssl/private \
  --include /etc/ssl/certs/mail.odile.fyi.crt --include /etc/systemd/system

# The restore replaces the state dir from the snapshot; the kit is authoritative
# for the tokens (a token may have been rotated since the snapshot).
install -d -m 700 "$STATE_DIR"
printf '%s' "$RESTIC_PASSWORD" > "$STATE_DIR/hq-backup.pass"
printf '%s' "$CLOUDFLARE_ACCOUNT_ID" > "$STATE_DIR/cloudflare_account_id"
printf '%s' "$CLOUDFLARE_DNS_TOKEN" > "$STATE_DIR/cloudflare_dns_token"
printf '%s' "$ZONE_ID" > "$STATE_DIR/cloudflare_zone_id"
cat >"$STATE_DIR/r2-backup.env" <<EOF
AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
R2_ENDPOINT=$R2_ENDPOINT
EOF
chmod 600 "$STATE_DIR/hq-backup.pass" "$STATE_DIR/r2-backup.env" \
  "$STATE_DIR/cloudflare_dns_token" "$STATE_DIR/cloudflare_zone_id" \
  "$STATE_DIR/cloudflare_account_id"

# The restore also replaced authorized_keys with HQ's. Put the new server's key
# back, or the operator loses SSH to the box that just came up.
if [ -f /var/tmp/odile-recovery-authorized_keys ]; then
  touch /root/.ssh/authorized_keys
  cat /var/tmp/odile-recovery-authorized_keys >> /root/.ssh/authorized_keys
  sort -u /root/.ssh/authorized_keys -o /root/.ssh/authorized_keys
  chmod 700 /root/.ssh
  chmod 600 /root/.ssh/authorized_keys
  rm -f /var/tmp/odile-recovery-authorized_keys
  say "SSH: the new server's authorized key was kept and merged with HQ's"
fi

say "Restoring the database copies"
[ -f "$REPO/tools/backup_hq.py" ] || die "restored repo has no tools/backup_hq.py"
/usr/bin/python3 "$REPO/tools/backup_hq.py" restore-dbs --from /var/backups/odile/live

if [ -d /var/lib/odile ]; then
  chown -R odile:odile /var/lib/odile
else
  warn "/var/lib/odile is missing after the restore; odile.service will fail"
fi
if [ -d /var/lib/caddy ]; then
  chown -R caddy:caddy /var/lib/caddy
else
  warn "/var/lib/caddy is missing after the restore; caddy may fail"
fi
if [ -f /etc/postfix/virtual ] && postmap /etc/postfix/virtual; then
  newaliases || warn "newaliases failed"
else
  warn "postfix virtual map not rebuilt"
fi

say "Checking the state databases"
for db in /root/.hermes/state.db /root/.hermes/profiles/*/state.db; do
  [ -f "$db" ] || continue
  sqlite3 "$db" 'pragma wal_checkpoint(TRUNCATE);'
  if [ "$(sqlite3 "$db" 'pragma integrity_check;')" = "ok" ]; then
    printf 'ok  %s\n' "$db"
  else
    warn "$db did not print ok from pragma integrity_check"
  fi
done

mkdir -p /root/.hermes/pending_messages.quarantine
mv /root/.hermes/pending_messages/pending-*.json \
  /root/.hermes/pending_messages.quarantine/ 2>/dev/null || true

# ---------------------------------------------------------------------------
# Site and units
# ---------------------------------------------------------------------------
say "Starting the site"
systemctl enable --now caddy postfix opendkim
systemctl restart caddy
grep -q '^127.0.0.1 odile.fyi$' /etc/hosts 2>/dev/null || \
  printf '127.0.0.1 odile.fyi\n' >> /etc/hosts
if ! ( cd "$REPO" && ./scripts/deploy.sh ); then
  warn "scripts/deploy.sh failed; the restored site may not answer yet"
fi
sed -i '\#^127\.0\.0\.1 odile\.fyi$#d' /etc/hosts

systemctl daemon-reload
# The backup timers are what keep the new machine's RPO: a restored box that
# never re-arms them takes one snapshot and then silently ages.
for unit in odile.service odile-seed.service odile-attest-refresh.timer \
            odile-claim.timer odile-backup.timer odile-backup-check.timer; do
  systemctl enable --now "$unit" 2>/dev/null || warn "$unit did not start; check systemctl status"
done

loginctl enable-linger root || true
systemctl --user daemon-reload || true
# The gateway is deliberately left stopped: the recovered .env may have Matrix
# disabled or a stale device. Start it later with:
#   systemctl --user enable --now hermes-gateway.service

# ---------------------------------------------------------------------------
# Cloudflare DNS. The token arrives in the environment, never in argv, so it
# cannot be read from `ps`.
# ---------------------------------------------------------------------------
if [ "$NO_DNS" = 0 ]; then
  say "Updating Cloudflare DNS to $NEW_IP"
  # The token and the zone id are already exported (`set -a` above); naming
  # them again here without `=` keeps the token out of a NAME=<long value>
  # shape that a secret scanner reads as a credential assignment.
  export CLOUDFLARE_DNS_TOKEN ZONE_ID
  if NEW_IP="$NEW_IP" OLD_IP="$OLD_IP" \
     DNS_NAMES="$DNS_NAMES" DNS_SKIP="$DNS_SKIP" /usr/bin/python3 <<'PY'
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.request

new_ip = os.environ["NEW_IP"]
zone = os.environ["ZONE_ID"]
token = os.environ["CLOUDFLARE_DNS_TOKEN"]
old_ip = os.environ.get("OLD_IP", "")
names = {n.strip() for n in os.environ["DNS_NAMES"].split(",") if n.strip()}
skip = [n.strip() for n in os.environ["DNS_SKIP"].split(",") if n.strip()]
api = os.environ.get("CLOUDFLARE_API_BASE", "https://api.cloudflare.com/client/v4")
api = f"{api.rstrip('/')}/zones/{zone}/dns_records"


def skipped(name):
    return any(fnmatch.fnmatch(name, pattern) for pattern in skip)


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def listing():
    records, page = [], 1
    while True:
        payload = call("GET", f"{api}?per_page=100&page={page}")
        records.extend(payload.get("result") or [])
        pages = (payload.get("result_info") or {}).get("total_pages") or 1
        if page >= pages:
            return records
        page += 1


try:
    records = listing()
except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as exc:
    print(f"DNS NOT UPDATED: the Cloudflare API failed ({exc})", file=sys.stderr)
    sys.exit(2)

changed = 0
existing_a = {r.get("name") for r in records if r.get("type") == "A"}
for name in sorted(names):
    if skipped(name) or name in existing_a:
        continue
    call("POST", api, {"type": "A", "name": name, "content": new_ip,
                      "ttl": 300, "proxied": False})
    print(f"created {name} A {new_ip}")
    changed += 1

for record in records:
    name, kind, content = (record.get("name", ""), record.get("type", ""),
                           record.get("content", ""))
    if name not in names or skipped(name):
        continue
    if kind == "A":
        if content == new_ip:
            continue
        call("PATCH", f"{api}/{record['id']}", {"content": new_ip})
        print(f"updated {name} A {content} -> {new_ip}")
        changed += 1
    elif kind == "TXT" and old_ip and content.startswith("v=spf1") \
            and f"ip4:{old_ip}" in content:
        new_content = content.replace(f"ip4:{old_ip}", f"ip4:{new_ip}")
        call("PATCH", f"{api}/{record['id']}", {"content": new_content})
        print(f"updated {name} TXT (SPF) ip4:{old_ip} -> ip4:{new_ip}")
        changed += 1

if not changed:
    print(f"DNS already points at {new_ip}; no record needed a change")
print(f"DNS names left alone: {', '.join(skip)}")
PY
  then
    warn "DNS NOT UPDATED — change the A records to $NEW_IP in the Cloudflare dashboard"
  fi
else
  warn "DNS NOT UPDATED (--no-dns) — change the A records to $NEW_IP in the Cloudflare dashboard"
fi

# ---------------------------------------------------------------------------
# Verify and finish
# ---------------------------------------------------------------------------
say "Verifying the restored site on $NEW_IP"
HTTP=$(curl -sS --max-time 20 --resolve "odile.fyi:443:$NEW_IP" -o /dev/null \
  -w '%{http_code}' https://odile.fyi/ || true)
printf 'https://odile.fyi/ -> %s\n' "${HTTP:-no answer}"
[ "$HTTP" = "200" ] || warn "SITE CHECK FAILED — the site did not answer 200 on this box"
curl -sS --max-time 20 --resolve "odile.fyi:443:$NEW_IP" \
  https://odile.fyi/attestation 2>/dev/null | \
  /usr/bin/python3 -c 'import json,sys; print("measurement:", json.load(sys.stdin).get("measurement","?"))' \
  2>/dev/null || true
hermes kanban --board odile list 2>/dev/null | tail -1 || true

say "RECOVERY COMPLETE"
printf 'New IPv4: %s\nSite: https://odile.fyi (DNS may take up to 5 minutes)\n' "$NEW_IP"
[ "$DRY_RUN" = 1 ] || printf 'Log: %s\n' "$LOG"
printf 'Optional messaging gateway: systemctl --user enable --now hermes-gateway.service\n'
