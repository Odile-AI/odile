#!/usr/bin/python3
"""The platform hardener must read back every control it claims.

`tools/harden_repo.sh` PATCHed four secret-scanning fields, but its verify
block read two, and it printed "every control is on" while
`secret_scanning_non_provider_patterns` and `secret_scanning_validity_checks`
sat at `disabled` — the API answers 200 and changes nothing on a plan without
GitHub Secret Protection. These tests pin the fix: the two fields are read, a
field that stays off is a named `gap` rather than a silent pass, and a control
that fails to take exits 1.

The end-to-end cases run the real script against a fake `gh` on PATH. The fake
serves a fixed API state; writes are accepted and change nothing, which is
exactly the condition that produced the original lie.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "harden_repo.sh"

FAKE_GH = r'''#!/usr/bin/env python3
"""A `gh` that serves a fixed API state read from $FAKE_GH_STATE."""
import json, os, subprocess, sys

state = json.loads(open(os.environ["FAKE_GH_STATE"], encoding="utf-8").read())
args = sys.argv[1:]
if not args or args[0] != "api":
    sys.exit("fake gh: only `api` is implemented")
args = args[1:]
method = "GET"
path = None
jq = None
i = 0
while i < len(args):
    a = args[i]
    if a == "-X":
        method = args[i + 1]; i += 2; continue
    if a in ("--input", "-f", "-F"):
        if a == "--input" and args[i + 1] == "-":
            sys.stdin.read()
        i += 2; continue
    if a == "--jq":
        jq = args[i + 1]; i += 2; continue
    if a.startswith("-"):
        i += 1; continue
    path = a
    i += 1
if method != "GET":
    sys.exit(0)
if path == "user":
    print(json.dumps({"login": "savio-agent", "public_repos": 0}))
    sys.exit(0)
if path not in state:
    print(json.dumps({"message": "Not Found", "status": "404"}))
    sys.exit(1)
value = state[path]
if value == 204:
    sys.exit(0)
text = json.dumps(value)
if jq:
    p = subprocess.run(["jq", "-r", jq], input=text, text=True, capture_output=True)
    if p.returncode != 0:
        sys.exit(1)
    sys.stdout.write(p.stdout)
else:
    sys.stdout.write(text)
'''


def base_state(owner: str = "savio-agent") -> dict:
    repo = f"repos/{owner}/odile-probe"
    return {
        repo: {
            "visibility": "public",
            "owner": {"type": "User" if owner == "savio-agent" else "Organization"},
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
                "secret_scanning_non_provider_patterns": {"status": "enabled"},
                "secret_scanning_validity_checks": {"status": "enabled"},
                "dependabot_security_updates": {"status": "enabled"},
            },
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "delete_branch_on_merge": True,
            "allow_update_branch": True,
            "has_wiki": False,
            "has_projects": False,
        },
        f"{repo}/private-vulnerability-reporting": {"enabled": True},
        f"{repo}/vulnerability-alerts": 204,
        f"{repo}/actions/permissions/workflow": {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        f"{repo}/branches/main/protection": {
            "required_status_checks": {
                "strict": True,
                "contexts": ["check", "publish-lint", "history-secrets",
                             "fresh-check", "demo-check"],
            },
            "enforce_admins": {"enabled": False},
            "required_pull_request_reviews": None,
            "required_signatures": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_conversation_resolution": {"enabled": True},
        },
        f"{repo}/actions/permissions": {
            "enabled": True,
            "allowed_actions": "selected",
            "sha_pinning_required": True,
        },
        f"{repo}/actions/permissions/selected-actions": {
            "github_owned_allowed": False,
            "verified_allowed": False,
            "patterns_allowed": ["actions/checkout@*", "github/codeql-action@*"],
        },
        f"{repo}/actions/permissions/artifact-and-log-retention": {
            "days": 7, "maximum_allowed_days": 90},
        f"{repo}/actions/permissions/fork-pr-contributor-approval": {
            "approval_policy": "all_external_contributors"},
        f"{repo}/code-scanning/default-setup": {"state": "configured", "query_suite": "default"},
        f"{repo}/rulesets": [{"id": 1, "name": "release tags are immutable"}],
        f"{repo}/rulesets/1": {
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
            "rules": [{"type": t} for t in
                      ("deletion", "update", "non_fast_forward", "required_signatures")],
        },
    }


class HardenRepoScript(unittest.TestCase):
    def run_script(self, state: dict, *args: str) -> subprocess.CompletedProcess:
        tmp = Path(tempfile.mkdtemp(prefix="harden-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = tmp / "gh"
        fake.write_text(FAKE_GH, encoding="utf-8")
        fake.chmod(0o755)
        state_file = tmp / "state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{tmp}:{env['PATH']}"
        env["FAKE_GH_STATE"] = str(state_file)
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), "savio-agent/odile-probe", *args],
            text=True, capture_output=True, env=env)

    # -- profiles -----------------------------------------------------------
    def test_public_open_profile_is_accepted(self):
        result = self.run_script(base_state(), "--profile=public-open", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("contexts=check publish-lint history-secrets fresh-check demo-check",
                      result.stdout)
        self.assertIn("admins=false", result.stdout)
        self.assertNotIn("required_pull_request_reviews", result.stdout)

    def test_public_profile_binds_admins(self):
        result = self.run_script(base_state(), "--profile=public", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("admins=true", result.stdout)

    def test_unknown_profile_exits_two(self):
        result = self.run_script(base_state(), "--profile=public-openish", "--dry-run")
        self.assertEqual(result.returncode, 2)

    # -- the regression: two fields off must be a gap, not a silent pass -----
    def test_disabled_scanning_fields_are_read_and_named(self):
        state = base_state()
        repo = "repos/savio-agent/odile-probe"
        state[repo]["security_and_analysis"]["secret_scanning_non_provider_patterns"]["status"] = "disabled"
        state[repo]["security_and_analysis"]["secret_scanning_validity_checks"]["status"] = "disabled"
        result = self.run_script(state, "--profile=public-open")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gap   secret_scanning_non_provider_patterns = disabled", result.stdout)
        self.assertIn("gap   secret_scanning_validity_checks = disabled", result.stdout)
        self.assertNotIn("every control is on", result.stdout)
        self.assertIn("2 gap(s)", result.stdout)

    def test_every_control_on_is_printed_only_when_true(self):
        result = self.run_script(base_state(), "--profile=public-open")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("harden: every control is on", result.stdout)
        self.assertIn("0 gap(s)", result.stdout)

    def test_a_control_that_does_not_take_exits_one(self):
        state = base_state()
        state["repos/savio-agent/odile-probe/actions/permissions"]["sha_pinning_required"] = False
        result = self.run_script(state, "--profile=public-open")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  sha_pinning_required = false (want true)", result.stdout)

    def test_tag_ruleset_and_codeql_are_read_back(self):
        result = self.run_script(base_state(), "--profile=public-open")
        for label in ("tag_ruleset.target = tag", "tag_ruleset.enforcement = active",
                      "tag_ruleset.conditions = include=~ALL exclude=none",
                      "tag_ruleset.deletion = true", "tag_ruleset.update = true",
                      "tag_ruleset.non_fast_forward = true",
                      "tag_ruleset.required_signatures = true",
                      "codeql_default_setup = configured",
                      "codeql_query_suite = default",
                      "actions_enabled = true",
                      "has_wiki = false", "has_projects = false",
                      "artifact_and_log_retention_days = 7",
                      "fork_pr_contributor_approval = all_external_contributors",
                      "selected_actions.checkout = true", "selected_actions.codeql = true"):
            self.assertIn(label, result.stdout)

    # -- the other row that printed an ok it did not read --------------------
    def test_missing_dependabot_route_is_a_failure(self):
        state = base_state()
        del state["repos/savio-agent/odile-probe/vulnerability-alerts"]
        result = self.run_script(state, "--profile=public-open")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL  dependabot_alerts = not enabled (want enabled)", result.stdout)
        self.assertNotIn("every control is on", result.stdout)

    def test_a_ruleset_with_an_exclude_is_a_failure(self):
        state = base_state()
        state["repos/savio-agent/odile-probe/rulesets/1"]["conditions"]["ref_name"]["exclude"] = ["refs/tags/nightly-*"]
        result = self.run_script(state, "--profile=public-open")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  tag_ruleset.conditions", result.stdout)

    def test_a_non_default_codeql_query_suite_is_a_failure(self):
        state = base_state()
        state["repos/savio-agent/odile-probe/code-scanning/default-setup"]["query_suite"] = "extended"
        result = self.run_script(state, "--profile=public-open")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  codeql_query_suite = extended (want default)", result.stdout)

    # -- static: every field the script sets is read back --------------------
    def test_verify_block_reads_all_four_scanning_fields(self):
        text = SCRIPT.read_text(encoding="utf-8")
        verify = text.split("harden: verifying", 1)[1]
        for field in ("secret_scanning", "secret_scanning_push_protection",
                      "secret_scanning_non_provider_patterns",
                      "secret_scanning_validity_checks"):
            self.assertIn(field, verify, f"the verify block does not read {field}")

    def test_verify_block_reads_the_five_set_but_unread_fields(self):
        text = SCRIPT.read_text(encoding="utf-8")
        verify = text.split("harden: verifying", 1)[1]
        for field in ("has_wiki", "has_projects", "actions_enabled",
                      "tag_ruleset.conditions", "codeql_query_suite"):
            self.assertIn(field, verify, f"the verify block does not read {field}")

    def test_the_two_ignored_fields_are_patched_and_verified(self):
        text = SCRIPT.read_text(encoding="utf-8")
        setter, verify = text.split("harden: verifying", 1)
        for field in ("secret_scanning_non_provider_patterns",
                      "secret_scanning_validity_checks"):
            self.assertIn(field, setter)
            self.assertIn(field, verify)


if __name__ == "__main__":
    unittest.main()
