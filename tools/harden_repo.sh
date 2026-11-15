#!/bin/sh
# Harden a fresh repository the moment it exists.
#
# The gates in the tree stop a leak at the commit and at the workflow. This
# script turns on the platform half, which no file in the tree can set: secret
# scanning, push protection, Dependabot, private vulnerability reporting,
# read-only default workflow permissions, a protected `main` whose required
# checks are the ones this repository actually runs, an immutable tag namespace,
# CodeQL default setup, a short artifact retention and an Actions allowlist
# pinned to full SHAs.
#
#     tools/harden_repo.sh Odile-AI/odile
#     tools/harden_repo.sh Odile-AI/odile --profile=public-open
#     tools/harden_repo.sh Odile-AI/odile --dry-run
#
# It is idempotent: every call is a PUT/PATCH/POST of the same target state,
# and the verify step reads every control back from the API. Exit 0 means every
# control that this plan makes available answered as asked. A control the plan
# cannot provide, or one deferred by a documented decision, is printed as a
# `gap` and counted — never silently claimed as on. A `FAIL` exits 1.
#
# Profiles:
#   public-open  the shape this project's public repository runs: the five real
#                job names as required checks, no admin enforcement and no PR
#                requirement, because the repository takes ordinary commits.
#   public       the same checks but administrators are bound and a pull request
#                is required (zero approvals).
#   mirror       a published copy a machine pushes directly: the rails that need
#                no second person, and no PR requirement.
#
# Some controls are organization-level (an org push-protection policy, an org
# ruleset, org-level SHA pinning, the code-security configuration that carries
# the two scanning fields below). A repository token cannot set them; step 13
# reads what it can and names the rest, so the gap is known, not silent.
set -eu

REPO=${1:-}
PROFILE=public-open
MODE=
shift 2>/dev/null || true
for arg in "$@"; do
    case "$arg" in
        --dry-run) MODE=--dry-run ;;
        --profile=*) PROFILE=${arg#--profile=} ;;
        *) echo "usage: harden_repo.sh OWNER/REPO [--profile=public-open|public|mirror] [--dry-run]" >&2; exit 2 ;;
    esac
done
[ -n "$REPO" ] || { echo "usage: harden_repo.sh OWNER/REPO [--profile=public-open|public|mirror] [--dry-run]" >&2; exit 2; }

# The job names in .github/workflows/check.yml are the required checks on every
# profile. `roadmap` renders from private inputs and only exists on HQ, so it is
# required on the mirror shape and not on the public tree.
case "$PROFILE" in
    public)      CONTEXTS_JSON='["check", "publish-lint", "history-secrets", "fresh-check", "demo-check"]'
                 CONTEXTS_WORDS='check publish-lint history-secrets fresh-check demo-check'
                 ENFORCE_ADMINS=true
                 PR_REVIEWS='{"required_approving_review_count": 0, "dismiss_stale_reviews": false, "require_code_owner_reviews": false}' ;;
    public-open) CONTEXTS_JSON='["check", "publish-lint", "history-secrets", "fresh-check", "demo-check"]'
                 CONTEXTS_WORDS='check publish-lint history-secrets fresh-check demo-check'
                 ENFORCE_ADMINS=false
                 PR_REVIEWS=null ;;
    mirror)      CONTEXTS_JSON='["check", "fresh-check", "demo-check", "roadmap", "history-secrets"]'
                 CONTEXTS_WORDS='check fresh-check demo-check roadmap history-secrets'
                 ENFORCE_ADMINS=false
                 PR_REVIEWS=null ;;
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
get() { gh api "$@"; }
# A read that may legitimately fail: a route a free plan does not serve, or an
# org endpoint a member token cannot see. Empty on failure; the caller decides
# whether that is a gap or a failure. `gh api` writes the API's error body to
# stdout on a 403/404, so the exit code — not the text — decides.
tryget() {
    if out=$(gh api "$@" 2>/dev/null); then
        printf '%s' "$out"
    fi
}

VISIBILITY=$(tryget "repos/$REPO" --jq '.visibility')
[ -n "$VISIBILITY" ] || { say "harden: cannot read $REPO (gh auth status?)"; exit 2; }
OWNER_TYPE=$(tryget "repos/$REPO" --jq '.owner.type')
OWNER=${REPO%%/*}
ORG_PLAN=
if [ "$OWNER_TYPE" = "Organization" ]; then
    ORG_PLAN=$(tryget "orgs/$OWNER" --jq '.plan.name')
fi
say "harden: $REPO ($VISIBILITY, owner $OWNER_TYPE${ORG_PLAN:+, plan $ORG_PLAN}, profile $PROFILE)"

# Secret scanning and push protection are free on a public repository and need
# GitHub Advanced Security on a private one. Say which, rather than failing.
SECRET_SCANNING=1
if [ "$VISIBILITY" != "public" ]; then
    SECRET_SCANNING=0
    say "harden: secret scanning skipped — a $VISIBILITY repository needs GHAS"
fi

# 1. Secret scanning and push protection. Four fields are PATCHed here. Two of
#    them (non-provider patterns and validity checks) answer 200 on a plan
#    without GitHub Secret Protection and stay disabled — the read-back in step
#    12 is what tells the truth about them.
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
#    deletion. `public` binds administrators and requires a pull request;
#    `public-open` and `mirror` let the publishing machine push directly.
if [ "$MODE" = "--dry-run" ]; then
    say "would: PUT repos/$REPO/branches/main/protection  profile=$PROFILE contexts=$CONTEXTS_WORDS admins=$ENFORCE_ADMINS"
    PROTECTION=1
else
    PROTECTION_JSON=$(mktemp)
    cat > "$PROTECTION_JSON" <<JSON
{
  "required_status_checks": {"strict": true, "contexts": $CONTEXTS_JSON},
  "enforce_admins": $ENFORCE_ADMINS,
  "required_pull_request_reviews": $PR_REVIEWS,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": true
}
JSON
    if gh api -X PUT "repos/$REPO/branches/main/protection" --input "$PROTECTION_JSON" >/dev/null 2>"$PROTECTION_JSON.err"; then
        PROTECTION=1
    else
        # Branch protection on a private repository needs GitHub Pro or Team; on
        # the free plan a private repo has none. Say so instead of failing.
        PROTECTION=0
        say "harden: branch protection not applied — $(head -1 "$PROTECTION_JSON.err")"
    fi
    rm -f "$PROTECTION_JSON" "$PROTECTION_JSON.err"
fi

# 7. Actions policy: only the actions named here may run, and every one of them
#    must be pinned to a full commit SHA. The workflow uses actions/checkout and
#    CodeQL's default setup uses github/codeql-action; nothing else is needed.
run -X PUT "repos/$REPO/actions/permissions" --input - <<'JSON'
{"enabled": true, "allowed_actions": "selected", "sha_pinning_required": true}
JSON
run -X PUT "repos/$REPO/actions/permissions/selected-actions" --input - <<'JSON'
{"github_owned_allowed": false, "verified_allowed": false,
 "patterns_allowed": ["actions/checkout@*", "github/codeql-action@*"]}
JSON

# 8. Artifacts and logs are build debris on a public repository, not a record:
#    a week is enough to debug a red run, not enough to mine a machine.
run -X PUT "repos/$REPO/actions/permissions/artifact-and-log-retention" -F days=7

# 9. Fork-PR approval: every external contributor's workflow waits for a
#    maintainer, not only a first-timer's. The API's value is
#    `all_external_contributors`; `all` is not a valid value (422).
run -X PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" \
    -f approval_policy=all_external_contributors

# 10. Release tags are immutable and signed. A tag can be created but not moved
#     or deleted, and a tag push that introduces an unsigned commit is refused.
#     A tag placed on a commit that is already on the remote is not re-checked
#     (the rule reads the objects the push introduces), so signed commits on
#     main — step 12 — is what makes this universal.
TAG_RULESET='release tags are immutable'
if [ "$MODE" = "--dry-run" ]; then
    say "would: POST/PUT repos/$REPO/rulesets  tag=$TAG_RULESET rules=deletion,update,non_fast_forward,required_signatures"
else
    RULESET_ID=$(tryget "repos/$REPO/rulesets" --jq ".[] | select(.name == \"$TAG_RULESET\") | .id")
    if [ -n "$RULESET_ID" ]; then
        run -X PUT "repos/$REPO/rulesets/$RULESET_ID" --input - <<JSON
{
  "name": "$TAG_RULESET",
  "target": "tag",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
  "rules": [{"type": "deletion"}, {"type": "update"},
            {"type": "non_fast_forward"}, {"type": "required_signatures"}]
}
JSON
    else
        run -X POST "repos/$REPO/rulesets" --input - <<JSON
{
  "name": "$TAG_RULESET",
  "target": "tag",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
  "rules": [{"type": "deletion"}, {"type": "update"},
            {"type": "non_fast_forward"}, {"type": "required_signatures"}]
}
JSON
    fi
fi

# 11. CodeQL default setup. One PATCH; free on a public repository. It analyses
#     the languages GitHub detects on the first run — the read-back checks the
#     state, not a language list that is empty until then.
run -X PATCH "repos/$REPO/code-scanning/default-setup" --input - <<'JSON'
{"state": "configured", "query_suite": "default"}
JSON

if [ "$MODE" = "--dry-run" ]; then
    say "harden: dry run, nothing changed"
    exit 0
fi

# 12. Read it back. A call that returns 200 has not necessarily changed
#     anything — two of the four scanning fields above are the proof — so every
#     control is read from the API, and the only sentence this script may print
#     is the one its own read-backs support.
say "harden: verifying"
OK=0
GAP=0
FAIL=0
check() {
    label=$1; want=$2; got=$3
    if [ "$got" = "$want" ]; then
        say "  ok    $label = $got"; OK=$((OK + 1))
    else
        say "  FAIL  $label = $got (want $want)"; FAIL=$((FAIL + 1))
    fi
}
gap() {
    label=$1; got=$2; why=$3
    say "  gap   $label = $got ($why)"; GAP=$((GAP + 1))
}

if [ "$SECRET_SCANNING" -eq 1 ]; then
check "secret_scanning" "enabled" \
    "$(tryget "repos/$REPO" --jq '.security_and_analysis.secret_scanning.status')"
check "secret_scanning_push_protection" "enabled" \
    "$(tryget "repos/$REPO" --jq '.security_and_analysis.secret_scanning_push_protection.status')"
# The two fields the script used to ignore. They need GitHub Secret Protection
# (GHAS); the PATCH answers 200 without it and leaves them disabled, so the
# read-back is the only honest source. With GHAS present a disabled field is a
# failure; without it, a named gap.
GHAS=$(tryget "repos/$REPO" --jq '.security_and_analysis.advanced_security.status')
for field in secret_scanning_non_provider_patterns secret_scanning_validity_checks; do
    got=$(tryget "repos/$REPO" --jq ".security_and_analysis.$field.status")
    if [ "$got" = "enabled" ] || [ "$GHAS" = "enabled" ]; then
        check "$field" "enabled" "$got"
    else
        gap "$field" "${got:-unknown}" "needs GitHub Secret Protection (GHAS); owner plan ${ORG_PLAN:-user}"
    fi
done
fi
if [ "$VISIBILITY" = "public" ]; then
check "private_vulnerability_reporting" "true" \
    "$(tryget "repos/$REPO/private-vulnerability-reporting" --jq '.enabled')"
fi
check "default_workflow_permissions" "read" \
    "$(tryget "repos/$REPO/actions/permissions/workflow" --jq '.default_workflow_permissions')"
check "can_approve_pull_request_reviews" "false" \
    "$(tryget "repos/$REPO/actions/permissions/workflow" --jq '.can_approve_pull_request_reviews')"
check "allow_squash_merge" "true" "$(tryget "repos/$REPO" --jq '.allow_squash_merge')"
check "allow_merge_commit" "false" "$(tryget "repos/$REPO" --jq '.allow_merge_commit')"
check "allow_rebase_merge" "false" "$(tryget "repos/$REPO" --jq '.allow_rebase_merge')"
check "delete_branch_on_merge" "true" "$(tryget "repos/$REPO" --jq '.delete_branch_on_merge')"
check "allow_update_branch" "true" "$(tryget "repos/$REPO" --jq '.allow_update_branch')"
if tryget "repos/$REPO/vulnerability-alerts" >/dev/null; then
    check "dependabot_alerts" "enabled" "enabled"
else
    check "dependabot_alerts" "enabled" "not enabled"
fi
check "dependabot_security_updates" "enabled" \
    "$(tryget "repos/$REPO" --jq '.security_and_analysis.dependabot_security_updates.status')"

if [ "${PROTECTION:-0}" -eq 1 ]; then
check "allow_force_pushes" "false" \
    "$(tryget "repos/$REPO/branches/main/protection" --jq '.allow_force_pushes.enabled')"
check "allow_deletions" "false" \
    "$(tryget "repos/$REPO/branches/main/protection" --jq '.allow_deletions.enabled')"
check "strict_status_checks" "true" \
    "$(tryget "repos/$REPO/branches/main/protection" --jq '.required_status_checks.strict')"
check "required_conversation_resolution" "true" \
    "$(tryget "repos/$REPO/branches/main/protection" --jq '.required_conversation_resolution.enabled')"
check "enforce_admins" "$ENFORCE_ADMINS" \
    "$(tryget "repos/$REPO/branches/main/protection" --jq '.enforce_admins.enabled')"
GOT_CONTEXTS=$(tryget "repos/$REPO/branches/main/protection" --jq '.required_status_checks.contexts[]' | sort | tr '\n' ' ')
# shellcheck disable=SC2086  # the word list is the point: one context per line
WANT_CONTEXTS=$(printf '%s\n' $CONTEXTS_WORDS | sort | tr '\n' ' ')
check "required_status_checks" "$WANT_CONTEXTS" "$GOT_CONTEXTS"
if [ "$PR_REVIEWS" = "null" ]; then
    check "required_pull_request_reviews" "null" \
        "$(tryget "repos/$REPO/branches/main/protection" --jq '.required_pull_request_reviews // "null"')"
else
    check "required_pull_request_reviews.count" "0" \
        "$(tryget "repos/$REPO/branches/main/protection" --jq '.required_pull_request_reviews.required_approving_review_count')"
fi
# Signed commits on main are a deliberate deferral, not a silent hole: the rule
# checks every commit a push introduces, and today's branches still carry
# commits made before signing was configured. The signing path itself is proven
# (an SSH signing key plus commit.gpgsign); the requirement moves on once those
# branches have drained.
SIGNED=$(tryget "repos/$REPO/branches/main/protection" --jq '.required_signatures.enabled')
if [ "$SIGNED" = "true" ]; then
    check "required_signatures.main" "true" "$SIGNED"
else
    gap "required_signatures.main" "${SIGNED:-unknown}" "deferred: unsigned commits in open branches would be rejected; signing path proven"
fi
fi

check "allowed_actions" "selected" \
    "$(tryget "repos/$REPO/actions/permissions" --jq '.allowed_actions // empty')"
check "sha_pinning_required" "true" \
    "$(tryget "repos/$REPO/actions/permissions" --jq 'if .sha_pinning_required then "true" else "false" end')"
check "selected_actions.checkout" "true" \
    "$(tryget "repos/$REPO/actions/permissions/selected-actions" --jq 'any(.patterns_allowed[]?; startswith("actions/checkout@")) | tostring')"
check "selected_actions.codeql" "true" \
    "$(tryget "repos/$REPO/actions/permissions/selected-actions" --jq 'any(.patterns_allowed[]?; startswith("github/codeql-action@")) | tostring')"
check "artifact_and_log_retention_days" "7" \
    "$(tryget "repos/$REPO/actions/permissions/artifact-and-log-retention" --jq '.days')"
check "fork_pr_contributor_approval" "all_external_contributors" \
    "$(tryget "repos/$REPO/actions/permissions/fork-pr-contributor-approval" --jq '.approval_policy')"
check "codeql_default_setup" "configured" \
    "$(tryget "repos/$REPO/code-scanning/default-setup" --jq '.state')"

RULESET=$(tryget "repos/$REPO/rulesets" --jq ".[] | select(.name == \"$TAG_RULESET\") | .id")
if [ -n "$RULESET" ]; then
    check "tag_ruleset.target" "tag" \
        "$(tryget "repos/$REPO/rulesets/$RULESET" --jq '.target // empty')"
    check "tag_ruleset.enforcement" "active" \
        "$(tryget "repos/$REPO/rulesets/$RULESET" --jq '.enforcement // empty')"
    for rule in deletion update non_fast_forward required_signatures; do
        check "tag_ruleset.$rule" "true" \
            "$(tryget "repos/$REPO/rulesets/$RULESET" --jq "any(.rules[]?; .type == \"$rule\") | tostring")"
    done
else
    check "tag_ruleset" "present" "absent"
fi

# 13. The org-level controls a repository token cannot set. Read what the token
#     may read and name everything else; none of these can be closed from this
#     script, so none of them may be printed as on.
say "harden: org-level controls (read-only; an org owner token is required to set them)"
if [ "$OWNER_TYPE" = "Organization" ]; then
    for probe in \
        "push_protection_policy|orgs/$OWNER/code-security/configurations|[.[] | select(.name == \"default\") | .name] | first" \
        "org_ruleset|orgs/$OWNER/rulesets|[.[] | .name] | first" \
        "org_actions_policy|orgs/$OWNER/actions/permissions|.allowed_actions" \
        "org_fork_pr_approval|orgs/$OWNER/actions/permissions/fork-pr-contributor-approval|.approval_policy" \
        "org_sha_pinning|orgs/$OWNER/actions/permissions|.sha_pinning_required"
    do
        label=${probe%%|*}; rest=${probe#*|}; endpoint=${rest%%|*}; query=${rest#*|}
        value=$(tryget "$endpoint" --jq "$query")
        if [ -n "$value" ] && [ "$value" != "null" ]; then
            gap "org.$label" "$value" "read with a member token; set at org level"
        else
            gap "org.$label" "unreadable" "org owner token required (member token: 403/404)"
        fi
    done
    gap "org.members_can_delete_repositories" \
        "$(tryget "orgs/$OWNER" --jq '.members_can_delete_repositories')" \
        "the publishing account can delete the repository (the purge path); set at org level"
    gap "org.members_can_change_repo_visibility" \
        "$(tryget "orgs/$OWNER" --jq '.members_can_change_repo_visibility')" \
        "the publishing account can make the repository private; set at org level"
else
    say "  ok    org controls = none (owner is the user $OWNER)"
fi

say "harden: $OK ok, $GAP gap(s), $FAIL failure(s)"
[ "$FAIL" -eq 0 ] || { say "harden: one or more controls did not take"; exit 1; }
if [ "$GAP" -eq 0 ]; then
    say "harden: every control is on"
elif [ "${PROTECTION:-0}" -ne 1 ]; then
    say "harden: every available control is on; branch protection is skipped (private repo on a free plan), $GAP gap(s) named above"
else
    say "harden: every available control is on; $GAP gap(s) named above"
fi
