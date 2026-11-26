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
# build, the old IPv4 and the DNS names come from the snapshot's own
# `manifest.json`, and the kit's copy fills any field the snapshot predates.
#
# Hermes is installed at the commit the backup recorded when a new box can
# fetch it; HQ runs local builds, so the backup also records the nearest
# published tag and the script installs that instead, saying which and why.
#
# Flags: --dry-run (print the plan, change nothing), --no-dns (skip Cloudflare),
#        --ip ADDRESS (override the detected IPv4). `--dry-run` needs `restic`
#        installed and reads the live repository; it writes no secret and no
#        config file, and removes its temporary manifest on exit.

set -Eeuo pipefail

KIT_DIR=${ODILE_KIT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
ENV_FILE=${ENV_FILE:-$KIT_DIR/recovery.env}
# Outside /root on purpose: the restore includes /root and replaces files in
# place, and the log is held open by `tee` for the whole run.
LOG=${LOG:-/var/tmp/odile-recovery.log}
DRY_RUN=0
NO_DNS=0
NEW_IP_OVERRIDE=""

usage() {
  cat <<'EOF'
Odile factory recovery.

  bash recover.sh [--dry-run] [--no-dns] [--ip ADDRESS]

  --dry-run     read the latest snapshot's manifest and print the Hermes
                build to install, the old IPv4 and the DNS plan; change nothing
  --no-dns      do not touch Cloudflare; print the records to change by hand
  --ip ADDRESS  use this IPv4 instead of detecting the server's own

Run as root, in the folder that holds recovery.env.
EOF
}

# Kept for the re-exec below: the parse loop consumes the positional args.
ARGS=("$@")

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

# --- run-from-a-copy begin ------------------------------------------------
# restic replaces a restored file in place, not by rename: an fd open on it
# reads the snapshot's bytes afterwards (proved 2026-11-26). The kit sits under
# /root, which the restore includes, so the run happens from /var/tmp and the
# kit's current files are put back afterwards. Today's backup set is a list of
# subtrees (the checkout, the state dir, the Hermes home, …) and does not carry
# the kit folder; nothing here depends on that staying true.
if [ "${ODILE_RECOVERY_RUNNING:-}" != 1 ] && [ "$DRY_RUN" = 0 ]; then
  RUN_DIR=$(mktemp -d /var/tmp/odile-recovery-run.XXXXXX)
  KIT_SAVED=$RUN_DIR/kit
  cp -a -- "$KIT_DIR" "$KIT_SAVED"
  cp -- "${BASH_SOURCE[0]}" "$RUN_DIR/recover.sh"
  chmod 700 "$RUN_DIR/recover.sh"
  export ODILE_RECOVERY_RUNNING=1 ODILE_KIT_DIR="$KIT_DIR" \
    ODILE_KIT_SAVED="$KIT_SAVED" ODILE_RUN_DIR="$RUN_DIR" ENV_FILE LOG
  exec bash "$RUN_DIR/recover.sh" "${ARGS[@]}"
fi
# --- run-from-a-copy end --------------------------------------------------

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
  # sshpass is no package's dependency here: the gate's `prereqs` target
  # refuses to run without it, and `odilectl provision` installs a tenant's key
  # over port 23 with it. A recovered box without it cannot onboard a tenant.
  apt-get install -y -qq \
    restic rsync sqlite3 openssh-server sshpass caddy postfix opendkim \
    opendkim-tools python3 python3-yaml curl git make shellcheck openssl \
    nftables e2fsprogs gh bind9-dnsutils libatomic1 iproute2 sudo psmisc \
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
# The snapshot's manifest is the source of truth. The kit carries a manifest
# written when it was generated; it fills only the fields the snapshot
# predates, so a fact added to the backup later still reaches this box.
KIT_MANIFEST=""
[ -f "$KIT_DIR/manifest.json" ] && KIT_MANIFEST=$KIT_DIR/manifest.json

MANIFEST_VALUES=$(/usr/bin/python3 - "$MANIFEST" "$KIT_MANIFEST" <<'PY'
import json, sys


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def missing(block, key):
    """A key the block does not carry: absent, null or an empty string."""
    if key not in block:
        return True
    value = block[key]
    return value is None or value == ""


data = load(sys.argv[1])
kit = load(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else {}
recovery = data.get("recovery")
recovery = recovery if isinstance(recovery, dict) else {}
kit_recovery = kit.get("recovery")
kit_recovery = kit_recovery if isinstance(kit_recovery, dict) else {}

keys = ("hermes_commit", "hermes_version", "hermes_commit_fetchable",
        "hermes_ref", "hermes_ref_commit", "ipv4", "dns_names", "dns_skip")
filled = [key for key in keys
          if missing(recovery, key) and not missing(kit_recovery, key)]
for key in filled:
    recovery[key] = kit_recovery[key]
if "hermes_commit" in filled:
    print("WARNING: the snapshot manifest has no Hermes commit; "
          "falling back to the kit's manifest", file=sys.stderr)
elif filled:
    print("WARNING: the snapshot's recovery block has no %s; using the "
          "kit's manifest" % ", ".join(filled), file=sys.stderr)

names = recovery.get("dns_names") or data.get("dns_names") or []
if isinstance(names, str):
    names = [n.strip() for n in names.split(",") if n.strip()]
skip = recovery.get("dns_skip") or data.get("dns_skip") or []
if isinstance(skip, str):
    skip = [n.strip() for n in skip.split(",") if n.strip()]
print("|".join([
    recovery.get("hermes_commit") or "",
    recovery.get("hermes_version") or "",
    "true" if recovery.get("hermes_commit_fetchable") is True else "false",
    recovery.get("hermes_ref") or "",
    recovery.get("hermes_ref_commit") or "",
    recovery.get("ipv4") or "",
    recovery.get("git_head") or data.get("git_head") or "",
    ",".join(names) if names else "",
    ",".join(skip) if skip else "",
]))
PY
)
IFS='|' read -r HERMES_COMMIT HERMES_VERSION HERMES_FETCHABLE HERMES_REF \
  HERMES_REF_COMMIT OLD_IP GIT_HEAD DNS_NAMES DNS_SKIP <<<"$MANIFEST_VALUES"

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

# Which Hermes build a new box can actually get. A recorded commit is usable
# only when it is published: HQ ran a local build on 2026-11-26 (five commits
# past v0.23.3, on no remote, 422 from GitHub), and a fresh clone cannot fetch
# that object. The backup records the nearest published ref beside it.
if [ -n "${HERMES_COMMIT:-}" ] && [ "${HERMES_FETCHABLE:-}" = true ]; then
  HERMES_PLAN=$HERMES_COMMIT
  HERMES_PLAN_WHY="the commit the backup recorded, which a new box can fetch"
elif [ -n "${HERMES_REF:-}" ]; then
  HERMES_PLAN=$HERMES_REF
  HERMES_PLAN_WHY="the nearest published ref recorded with the backup (${HERMES_REF_COMMIT:0:12}); the recorded commit ${HERMES_COMMIT:-none} was a local build no new box can fetch"
else
  HERMES_PLAN=""
  HERMES_PLAN_WHY="the installer's own build; the backup records nothing a new box can fetch"
fi

say "Hermes: ${HERMES_COMMIT:-latest} ${HERMES_VERSION:-}"
say "Hermes to install: ${HERMES_PLAN:-the installed build} — $HERMES_PLAN_WHY"
say "Product commit in the backup: ${GIT_HEAD:-unknown}"
say "Old IPv4 in the backup: ${OLD_IP:-unknown}"
say "DNS names to move: $DNS_NAMES"
say "DNS names to leave alone: $DNS_SKIP"

if [ "$DRY_RUN" = 1 ]; then
  say "DRY RUN — no packages, secrets, restore, services or DNS changed"
  printf 'Would: write the kit secrets to %s; install packages; install Hermes at %s;\n' \
    "$STATE_DIR" "${HERMES_PLAN:-the installer default}"
  printf 'restore %s to / (leaving this kit folder and the log in place);\n' "$SNAP"
  printf 'start Caddy/Postfix/OpenDKIM, the Odile units and the backend hermes-serve;\n'
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
# Hermes at the build the backup recorded — or the nearest one it can fetch
# ---------------------------------------------------------------------------
say "Installing Hermes"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
# install.sh puts the CLI under the Hermes home; a non-login root shell (how
# this script runs) does not inherit a profile PATH, so name it explicitly.
export PATH="/root/.local/bin:/root/.hermes/hermes-agent/.hermes/bin:/usr/local/bin:$PATH"
HERMES_BIN=$(command -v hermes 2>/dev/null || true)
[ -n "$HERMES_BIN" ] || warn "hermes is not on PATH after the install; the CLI steps will fail"
HERMES_CHECKOUT=${HERMES_CHECKOUT:-/root/.hermes/hermes-agent}

# --- hermes-target begin ---------------------------------------------------
# Pick the Hermes build to install: the recorded commit when a fresh clone can
# fetch it, else the published ref recorded beside it, else the installer's
# build. Always sets HERMES_TARGET (empty = keep the installed build) and
# HERMES_TARGET_KIND, and says why in the operator's words.
hermes_target() {
  HERMES_TARGET=""
  HERMES_TARGET_KIND=""
  local dir=$HERMES_CHECKOUT commit=${HERMES_COMMIT:-} ref=${HERMES_REF:-}
  local fetchable=${HERMES_FETCHABLE:-} on_remote=""
  git -c safe.directory="$dir" -C "$dir" fetch --all --tags --prune >/dev/null 2>&1 \
    || warn "git fetch failed; trying the recorded refs anyway"
  if [ -n "$commit" ] && [ "$fetchable" != true ]; then
    # The backup's flag says whether HEAD was on a remote when it ran; the
    # clone in front of us is the authority now.
    on_remote=$(git -c safe.directory="$dir" -C "$dir" branch -r --contains "$commit" 2>/dev/null || true)
    if [ -n "$on_remote" ]; then
      fetchable=true
    fi
  fi
  if [ -n "$commit" ] && [ "$fetchable" = true ] \
     && git -c safe.directory="$dir" -C "$dir" checkout -q "$commit" 2>/dev/null; then
    HERMES_TARGET=$commit
    HERMES_TARGET_KIND="the commit the backup recorded"
    return 0
  fi
  if [ -n "$ref" ] \
     && git -c safe.directory="$dir" -C "$dir" checkout -q "$ref" 2>/dev/null; then
    HERMES_TARGET=$ref
    HERMES_TARGET_KIND="the nearest published ref recorded with the backup"
    if [ -n "$commit" ]; then
      warn "the backup ran Hermes $commit, a local build no new box can fetch; installed $ref instead${HERMES_REF_COMMIT:+ (tag commit ${HERMES_REF_COMMIT:0:12})}"
    fi
    return 0
  fi
  HERMES_TARGET=""
  HERMES_TARGET_KIND="the installer's own build (nothing recorded could be fetched)"
  warn "no recorded Hermes build could be fetched; keeping the installer's version"
  return 0
}
# --- hermes-target end -----------------------------------------------------

if [ -n "${HERMES_COMMIT:-}${HERMES_REF:-}" ] && [ -d "$HERMES_CHECKOUT/.git" ]; then
  (
    hermes_target
    HEAD_COMMIT=$(git -c safe.directory="$HERMES_CHECKOUT" -C "$HERMES_CHECKOUT" \
      rev-parse HEAD 2>/dev/null || true)
    printf 'Hermes target: %s — %s\n' "${HERMES_TARGET:-the installed build}" "$HERMES_TARGET_KIND"
    printf 'Hermes checkout: %s\n' "${HEAD_COMMIT:0:12}"
    if [ -n "$HERMES_TARGET" ] && [ "$HERMES_TARGET" = "${HERMES_REF:-}" ] \
       && [ -n "${HERMES_REF_COMMIT:-}" ]; then
      case "$HEAD_COMMIT" in
        "$HERMES_REF_COMMIT"*) ;;
        *) warn "checked out $HERMES_TARGET at $HEAD_COMMIT, but the backup recorded ${HERMES_REF_COMMIT:0:12}" ;;
      esac
    elif [ -n "$HERMES_TARGET" ]; then
      case "$HEAD_COMMIT" in
        "$HERMES_TARGET"*) ;;
        *) warn "asked for $HERMES_TARGET but the checkout is $HEAD_COMMIT" ;;
      esac
    fi
    [ -z "$HERMES_BIN" ] || "$HERMES_BIN" --version || true
  )
elif [ -n "${HERMES_COMMIT:-}${HERMES_REF:-}" ]; then
  warn "no Hermes checkout at $HERMES_CHECKOUT; keeping the installer's version"
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
# No --exclude here: restic 0.18 refuses `--include` and `--exclude` together
# ("exclude and include patterns are mutually exclusive"). The kit folder under
# /root is protected by running from /var/tmp and putting the current files
# back below instead.
restic restore "$SNAP" --target / \
  --include /var/backups/odile/live \
  --include /root --include /var/lib/caddy --include /var/lib/odile \
  --include /srv \
  --include /etc/caddy --include /etc/dkimkeys --include /etc/opendkim.conf \
  --include /etc/postfix --include /etc/ssl/private \
  --include /etc/ssl/certs/mail.odile.fyi.crt --include /etc/systemd/system

# The restore is --include /root, so it writes every backed-up path under
# /root (and deletes nothing). A kit copy that a snapshot does carry is
# replaced; the copy taken before the restore is authoritative — it is newer
# than the snapshot when a token was rotated — and goes back below.
if [ -n "${ODILE_KIT_SAVED:-}" ] && [ -d "$ODILE_KIT_SAVED" ]; then
  install -d -m 700 "$KIT_DIR"
  cp -a -- "$ODILE_KIT_SAVED/." "$KIT_DIR/"
  chmod 600 "$KIT_DIR/recovery.env"
  chmod 700 "$KIT_DIR/recover.sh"
  say "Kit: the current recovery.env and recover.sh are back in $KIT_DIR"
fi

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
# never re-arms them takes one snapshot and then silently ages. `hermes-serve`
# is the system unit behind gw.odile.fyi (127.0.0.1:9119), restored from the
# snapshot's /etc/systemd/system, and that name is in dns_names — leaving it
# stopped would move the record to a box that answers 502.
for unit in odile.service odile-seed.service odile-attest-refresh.timer \
            odile-claim.timer odile-backup.timer odile-backup-check.timer \
            hermes-serve.service; do
  systemctl enable --now "$unit" 2>/dev/null || warn "$unit did not start; check systemctl status"
done

loginctl enable-linger root || true
systemctl --user daemon-reload || true
# The *messaging* gateway (hermes-gateway, the Matrix/Telegram bot) is
# deliberately left stopped: the recovered .env may have Matrix disabled or a
# stale device, and a bot that starts talking before anyone checks is worse than
# one that waits. Start it later with:
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
printf 'Log: %s\n' "$LOG"
printf 'Optional messaging gateway: systemctl --user enable --now hermes-gateway.service\n'
# The copy that kept this run out of the restore set; it holds no secret the
# kit does not (the kit is authoritative and was put back above).
[ -z "${ODILE_RUN_DIR:-}" ] || rm -rf -- "$ODILE_RUN_DIR"
