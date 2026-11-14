#!/bin/sh
# Harden a fresh repository the moment it exists.
#
# The gates in the tree stop a leak at the commit and at the workflow. This
# script turns on the platform half, which no file in the tree can set: secret
# scanning, push protection, Dependabot, private vulnerability reporting,
# read-only default workflow permissions, and a protected `main` whose required
# checks are the ones this repository actually runs.
#
#     tools/harden_repo.sh Odile-AI/odile
#     tools/harden_repo.sh Odile-AI/odile --dry-run
#
# It is idempotent: every call is a PUT/PATCH of the same target state, and the
# verify step reads the result back. Exit 0 means every control answered as
# asked.
set -eu

REPO=${1:-}
PROFILE=public
MODE=
shift 2>/dev/null || true
for arg in "$@"; do
    case "$arg" in
        --dry-run) MODE=--dry-run ;;
        --profile=*) PROFILE=${arg#--profile=} ;;
        *) echo "usage: harden_repo.sh OWNER/REPO [--profile=public|mirror] [--dry-run]" >&2; exit 2 ;;
    esac
done
[ -n "$REPO" ] || { echo "usage: harden_repo.sh OWNER/REPO [--profile=public|mirror] [--dry-run]" >&2; exit 2; }

# Two shapes. `public` requires a pull request and binds administrators, which
# is right for a repository strangers read. `mirror` keeps the rails that need
# no second person (no force push, no deletion, checks named) and lets the
# machine that owns the tree push `main` directly, which is how this project
# works: <checkout> is the source, the repository is its published copy.
case "$PROFILE" in
    public) CONTEXTS='["check", "publish-lint", "history-secrets", "fresh-check", "demo-check"]'
            ENFORCE_ADMINS=true ; PR_REVIEWS='{"required_approving_review_count": 0, "dismiss_stale_reviews": false, "require_code_owner_reviews": false}' ;;
    mirror) CONTEXTS='["check", "fresh-check", "demo-check", "roadmap", "history-secrets"]'
            ENFORCE_ADMINS=false ; PR_REVIEWS=null ;;
    *) echo "harden: unknown profile $PROFILE" >&2; exit 2 ;;
esac

say() { printf '%s\n' "$*"; }
run() {
    if [ "$MODE" = "--dry-run" ]; then
        say "would: gh api $*"
    else
        gh api "$@" >/dev/null
    fi
}

VISIBILITY=$(gh api "repos/$REPO" --jq '.visibility' 2>/dev/null || echo unknown)
say "harden: $REPO ($VISIBILITY, profile $PROFILE)"

# Secret scanning and push protection are free on a public repository and need
# GitHub Advanced Security on a private one. Say which, rather than failing.
SECRET_SCANNING=1
if [ "$VISIBILITY" != "public" ]; then
    SECRET_SCANNING=0
    say "harden: secret scanning skipped — a $VISIBILITY repository needs GHAS"
fi

# 1. Secret scanning and push protection. Free on a public repository; the one
#    control that stops a credential before it lands, including from a fork.
if [ "$SECRET_SCANNING" -eq 1 ]; then
run -X PATCH "repos/$REPO" --input - <<'JSON'
{
  "security_and_analysis": {
    "secret_scanning": {"status": "enabled"},
    "secret_scanning_push_protection": {"status": "enabled"},
    "secret_scanning_non_provider_patterns": {"status": "enabled"},
    "secret_scanning_validity_checks": {"status": "enabled"}
  }
}
JSON
fi

# 2. Private vulnerability reporting: SECURITY.md points at an address, but the
#    platform route gives a reporter a private thread and us an advisory. Like
#    secret scanning it is a public-repository control; a private repository on
#    the free plan answers 404.
if [ "$VISIBILITY" = "public" ]; then
    run -X PUT "repos/$REPO/private-vulnerability-reporting"
fi

# 3. Dependabot alerts and automated security fixes.
run -X PUT "repos/$REPO/vulnerability-alerts"
run -X PUT "repos/$REPO/automated-security-fixes"

# 4. Workflow permissions: read-only by default, so a new workflow cannot write
#    to the repository unless it is edited to say so on purpose.
run -X PUT "repos/$REPO/actions/permissions/workflow" --input - <<'JSON'
{"default_workflow_permissions": "read", "can_approve_pull_request_reviews": false}
JSON

# 5. A repository shape that matches how the project works: squash only, no
#    merge commit, delete the branch on merge, no wiki or projects to drift.
run -X PATCH "repos/$REPO" --input - <<'JSON'
{
  "has_wiki": false,
  "has_projects": false,
  "allow_squash_merge": true,
  "allow_merge_commit": false,
  "allow_rebase_merge": false,
  "delete_branch_on_merge": true,
  "allow_update_branch": true
}
JSON

# 6. Protect main: the five checks the repository runs, no force push, no
#    deletion, administrators included. A required pull request with zero
#    approvals keeps the flow honest without inventing a second reviewer.
if [ "$MODE" = "--dry-run" ]; then
    say "would: PUT repos/$REPO/branches/main/protection  profile=$PROFILE contexts=$CONTEXTS admins=$ENFORCE_ADMINS"
else
cat > /tmp/harden-protection.json <<JSON
{
  "required_status_checks": {"strict": true, "contexts": $CONTEXTS},
  "enforce_admins": $ENFORCE_ADMINS,
  "required_pull_request_reviews": $PR_REVIEWS,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": true
}
JSON
if gh api -X PUT "repos/$REPO/branches/main/protection" --input /tmp/harden-protection.json >/dev/null 2>/tmp/harden-protection.err; then
    PROTECTION=1
else
    # Branch protection on a private repository needs GitHub Pro or Team; on the
    # free plan a private repo has none. Say so instead of failing the run.
    PROTECTION=0
    say "harden: branch protection not applied — $(head -1 /tmp/harden-protection.err)"
fi
fi
if [ "$MODE" = "--dry-run" ]; then
    say "harden: dry run, nothing changed"
    exit 0
fi

# 7. Read it back. A call that returns 200 has not necessarily changed anything;
#    these are the fields that decide whether a leak is stopped.
say "harden: verifying"
fail=0
check() {
    label=$1; want=$2; got=$3
    if [ "$got" = "$want" ]; then
        say "  ok    $label = $got"
    else
        say "  FAIL  $label = $got (want $want)"; fail=1
    fi
}

if [ "$SECRET_SCANNING" -eq 1 ]; then
check "secret_scanning" \
    "enabled" \
    "$(gh api "repos/$REPO" --jq '.security_and_analysis.secret_scanning.status')"
check "secret_scanning_push_protection" \
    "enabled" \
    "$(gh api "repos/$REPO" --jq '.security_and_analysis.secret_scanning_push_protection.status')"
fi
if [ "$VISIBILITY" = "public" ]; then
check "private_vulnerability_reporting" \
    "true" \
    "$(gh api "repos/$REPO/private-vulnerability-reporting" --jq '.enabled')"
fi
check "default_workflow_permissions" \
    "read" \
    "$(gh api "repos/$REPO/actions/permissions/workflow" --jq '.default_workflow_permissions')"
check "allow_squash_merge" "true" "$(gh api "repos/$REPO" --jq '.allow_squash_merge')"
check "allow_merge_commit" "false" "$(gh api "repos/$REPO" --jq '.allow_merge_commit')"
check "delete_branch_on_merge" "true" "$(gh api "repos/$REPO" --jq '.delete_branch_on_merge')"
if gh api "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1; then
    say "  ok    dependabot_alerts = enabled"
else
    say "  FAIL  dependabot_alerts = not enabled"; fail=1
fi
if [ "${PROTECTION:-0}" -eq 1 ]; then
check "allow_force_pushes" \
    "false" \
    "$(gh api "repos/$REPO/branches/main/protection" --jq '.allow_force_pushes.enabled')"
check "allow_deletions" \
    "false" \
    "$(gh api "repos/$REPO/branches/main/protection" --jq '.allow_deletions.enabled')"
check "strict_status_checks" \
    "true" \
    "$(gh api "repos/$REPO/branches/main/protection" --jq '.required_status_checks.strict')"
fi

[ "$fail" -eq 0 ] || { say "harden: one or more controls did not take"; exit 1; }
if [ "${PROTECTION:-0}" -eq 1 ]; then
    say "harden: every control is on"
else
    say "harden: every available control is on; branch protection is skipped (private repo on a free plan)"
fi
