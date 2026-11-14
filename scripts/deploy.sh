#!/bin/sh
# Deploy this project directory to the runtime and restart the service.
#   ./scripts/deploy.sh        apply
#   ./scripts/deploy.sh -n     dry run
#   ./scripts/deploy.sh --allow-behind   deploy an old checkout (rollback only)
set -e
# The entry point lives in scripts/ — in this checkout and in the published
# tree, where tools/build_public_tree.py MOVEs the root entry point here.
# Resolve the repository root from the script's own path, not the caller's cwd:
# `dirname "$0"` alone is `scripts/`, and the rsync below would then copy the
# scripts directory over /opt/odile (measured 2026-11-14: 1305 file deletions
# planned, including app.py and every page under web/).
SCRIPT_DIR=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/.."
# A copy of this script outside scripts/ resolves one level too high (at the
# checkout root, `..` is /root). Refuse before any probe or write rather than
# rsync the wrong tree with --delete.
if [ ! -f app.py ]; then
  echo "deploy: refused: $(pwd) is not the repository root (no app.py; run scripts/deploy.sh from the checkout)" >&2
  exit 1
fi

DRY=0
ALLOW_BEHIND=0
for arg in "$@"; do
  case "$arg" in
    -n|--dry-run) DRY=1 ;;
    --allow-behind) ALLOW_BEHIND=1 ;;
    -h|--help)
      echo "usage: ./scripts/deploy.sh [-n|--dry-run] [--allow-behind]"
      echo "  -n, --dry-run    print the plan and write nothing; the main guard still runs"
      echo "  --allow-behind   deploy even though HEAD does not contain main (rollback only)"
      exit 0
      ;;
    *)
      echo "deploy: unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

echo "→ source  $(pwd)"
echo "→ runtime /opt/odile"

# Production is main-only: /opt/odile must come from a tree that contains main,
# or a branch deploy silently removes every card merged after the branch forked.
# Measured 2026-11-11: two deploys from pre-merge trees left /opt/odile without
# app.py's /roadmap route and without tools/roadmap_page.py, and the page
# answered 404 until a main deploy. Refuse before the first write (the group
# check and the rsync both come later), name the commits that are missing, and
# keep --allow-behind for a deliberate rollback (docs/runbooks/deploy.md §6).
if ! command -v git >/dev/null 2>&1; then
  echo "deploy: refused: git is not installed, so the deployer cannot prove HEAD contains main" >&2
  exit 1
fi
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "deploy: refused: $(pwd) is not a git checkout; production deploys from main" >&2
  exit 1
fi
MAIN_REF=refs/heads/main
if ! git rev-parse --verify --quiet "$MAIN_REF" >/dev/null; then
  echo "deploy: refused: no local main branch in $(pwd); production deploys from main" >&2
  exit 1
fi
if ! git rev-parse --verify --quiet HEAD >/dev/null; then
  echo "deploy: refused: this checkout has no commit at HEAD; production deploys from main" >&2
  exit 1
fi
HEAD_SHA=$(git rev-parse --short HEAD)
MAIN_SHA=$(git rev-parse --short "$MAIN_REF")
if git merge-base --is-ancestor "$MAIN_REF" HEAD; then
  echo "→ guard   HEAD $HEAD_SHA contains main $MAIN_SHA"
elif [ "$ALLOW_BEHIND" -eq 1 ]; then
  echo "deploy: warning: HEAD $HEAD_SHA does not contain main $MAIN_SHA; deploying anyway (--allow-behind)" >&2
else
  MISSING_COUNT=$(git rev-list --count "HEAD..$MAIN_REF")
  MISSING=$(git log -n 10 --format='%h %s' "HEAD..$MAIN_REF" \
    | awk 'NR == 1 { printf "%s", $0; next } { printf ", %s", $0 }')
  if [ "$MISSING_COUNT" -gt 10 ]; then
    MISSING="$MISSING, (+$((MISSING_COUNT - 10)) more)"
  fi
  echo "deploy: refused: HEAD $HEAD_SHA does not contain main $MAIN_SHA; $MISSING_COUNT commit(s) missing: $MISSING; deploy from main or a checkout that contains it, or pass --allow-behind for a deliberate rollback (docs/runbooks/deploy.md §6)" >&2
  exit 1
fi

# scripts/deploy.sh installs the tree root:odile 0750 so odile.service keeps its group
# read and any other uid gets nothing. Without the group, `install -d` would
# fail after the rsync has already rewritten the tree; fail before any write.
getent group odile >/dev/null 2>&1 || {
  echo "deploy: group odile does not exist (installer/odile_install.py creates it)" >&2
  exit 1
}

# The seed service runs /opt/odile/seed/seed.py; its hash before this deploy
# lets the deployer restart it only when the module it runs actually changed.
PREV_SEED=$(sha256sum /opt/odile/seed/seed.py 2>/dev/null | cut -d' ' -f1 || true)

# docs/growth is a host-only workspace (queue, log, research, assets) written by
# the growth cron jobs. It is not in git, so --delete must not remove it.
# .worktrees holds transient git worktree copies; never ship or delete them.
# Neither entry point ships. The tracked one is scripts/deploy.sh; this host
# also keeps a root-level copy of it (listed in .git/info/exclude). The paths
# are anchored to the transfer root, so a deploy from either layout leaves both
# out of /opt/odile and --delete cannot remove the copy that is not the rsync
# source.
set -- \
  --exclude '.git' \
  --exclude '.worktrees' \
  --exclude 'build' \
  --exclude '/deploy.sh' \
  --exclude '/scripts/deploy.sh' \
  --exclude 'docs/growth' \
  --exclude 'IDEA.md' \
  --exclude 'PROJECT.md' \
  --include '/tests/' \
  --include '/tests/fixtures/' \
  --include '/tests/fixtures/amd/' \
  --include '/tests/fixtures/amd/***' \
  --exclude '/tests/**' \
  --exclude '__pycache__'
if [ "$DRY" -eq 1 ]; then
  set -- --dry-run --itemize-changes "$@"
fi
rsync -a --delete "$@" ./ /opt/odile/

if [ "$DRY" -eq 1 ]; then
  echo "would install odile-attest-refresh.service and .timer, plus the"
  echo "odile.service drop-in that writes the report through the atomic wrapper"
  echo "would install /opt/odile as root:odile 0750 (an unprivileged uid cannot read it)"
  echo "would enable odile-attest-refresh.timer and refresh the report once"
  echo "would install, enable and probe the seed service and its /seed route:"
  /usr/bin/python3 deploy/seed/deploy_seed.py --dry-run --previous-seed-sha "$PREV_SEED"
  echo "would install odile-claim.service and odile-claim.timer"
  echo "would enable and start odile-claim.timer"
  echo "would install, enable and run odile-roadmap.service and odile-roadmap.timer"
  echo "(every fifteen minutes; the page is written to /var/lib/odile/roadmap.html)"
  echo "would record the checkout's short sha at /var/lib/odile/roadmap-source"
  echo "dry run only — nothing installed, service untouched"
  exit 0
fi

# The deployed tree is the roadmap (docs/backlog.yaml), the claims deck and the
# deploy scripts, not the product: no unprivileged uid may read it. `rsync -a`
# rewrites the destination root's mode from the checkout on every run
# (measured: 0750 back to 0755 on a redeploy), so this runs after the rsync,
# not once at install. odile.service runs as odile:odile and reads through the
# group; the mode is asserted here so a changed rsync flag fails the deploy
# loudly instead of silently reopening the tree.
install -d -o root -g odile -m 0750 /opt/odile
TREE=$(stat -c '%a %U:%G' /opt/odile)
if [ "$TREE" != "750 root:odile" ]; then
  echo "deploy: /opt/odile is $TREE, expected 750 root:odile" >&2
  exit 1
fi

install -m 644 odile.service /etc/systemd/system/odile.service
install -d -m 755 /etc/systemd/system/odile.service.d
install -m 644 deploy/attest-refresh/odile-service-atomic.conf \
  /etc/systemd/system/odile.service.d/attest-atomic.conf
install -m 644 deploy/attest-refresh/odile-attest-refresh.service \
  /etc/systemd/system/odile-attest-refresh.service
install -m 644 deploy/attest-refresh/odile-attest-refresh.timer \
  /etc/systemd/system/odile-attest-refresh.timer
install -m 644 deploy/claim-worker/odile-claim.service \
  /etc/systemd/system/odile-claim.service
install -m 644 deploy/claim-worker/odile-claim.timer \
  /etc/systemd/system/odile-claim.timer
install -m 644 deploy/roadmap/odile-roadmap.service \
  /etc/systemd/system/odile-roadmap.service
install -m 644 deploy/roadmap/odile-roadmap.timer \
  /etc/systemd/system/odile-roadmap.timer
systemctl daemon-reload
# The web service owns the queue file: it creates /var/lib/odile/waitlist.db on
# boot, and the claim worker (root) relies on that ownership for the WAL it
# writes beside it. Restart odile before the timer can run the worker.
systemctl restart odile
systemctl enable --now odile-attest-refresh.timer
systemctl start odile-attest-refresh.service

# The seed service and its /seed route (ODL-204, carried by ODL-332's
# follow-up). Idempotent: a converged host prints `same` and writes nothing.
# A missing or world-readable credential stops the deploy here, loudly.
/usr/bin/python3 deploy/seed/deploy_seed.py --previous-seed-sha "$PREV_SEED"

# The claim worker (ODL-220 follow-up). The timer is what makes a paid claim
# become a machine without a human; enable it after the web service has the
# queue file and start one run so the deploy itself proves the wiring.
systemctl enable --now odile-claim.timer
systemctl start odile-claim.service

# The roadmap page (ODL-490). /opt/odile carries no .git — the rsync excludes
# it — so the footer's source sha is recorded here, from the checkout this
# deploy ran from, and the generator reads it. The page is generated state:
# app.py serves the file, or one line of 503 until the first run writes it.
git rev-parse --short HEAD > /var/lib/odile/roadmap-source
chmod 644 /var/lib/odile/roadmap-source
systemctl enable --now odile-roadmap.timer
systemctl start odile-roadmap.service
sleep 1
systemctl is-active odile
systemctl is-active odile-attest-refresh.timer
systemctl is-active odile-claim.timer
systemctl is-active odile-roadmap.timer
curl -s -o /dev/null -w "page %{http_code}\n" https://odile.fyi/
curl -s -o /dev/null -w "verify %{http_code}\n" https://odile.fyi/verify
curl -s -o /dev/null -w "roadmap %{http_code}\n" https://odile.fyi/roadmap
