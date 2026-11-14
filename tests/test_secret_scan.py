"""Tests for the secret scan.

The scan is the M0 "no secret in git" proof: the tree is scanned by
`make secrets` inside `make check`, and the full history is scanned by
`tools/secret_scan.py --history`. These tests prove three things: the real
repository is clean, a planted private key and a planted provider token both
fail — in the working tree and in a commit that HEAD no longer carries — and a
finding names the commit, path, line and hash without ever printing the value.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import secret_scan  # noqa: E402

TOOL = ROOT / "tools" / "secret_scan.py"


def git_env() -> dict[str, str]:
    """A clean environment: a fixture repo must never inherit GIT_DIR.

    `.githooks/pre-commit` runs the suite with GIT_* pointing at the real
    repository; an inherited GIT_DIR would make `git init` in the fixture
    rewrite the caller's index (the ODL-101 bare-worktree incident).
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_AUTHOR_NAME": "Plant",
            "GIT_AUTHOR_EMAIL": "plant@example.com",
            "GIT_COMMITTER_NAME": "Plant",
            "GIT_COMMITTER_EMAIL": "plant@example.com",
        }
    )
    return env


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=git_env(), capture_output=True, text=True,
    )


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], env=git_env(), check=True, capture_output=True)
    return path


def commit(repo: Path, message: str) -> str:
    assert run_git(repo, "add", "-A").returncode == 0
    result = run_git(repo, "commit", "-q", "-m", message)
    assert result.returncode == 0, result.stderr
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()


def scan(*args: str, root: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args, "--root", str(root)],
        cwd=str(root), env=git_env(), capture_output=True, text=True,
    )


def fake_private_key(kind: str = "PRIVATE KEY") -> str:
    """A syntactically complete block; built at runtime so the suite is clean."""
    body = "\n".join("MIIB" + "A" * 60 for _ in range(2))
    return f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----\n"


def marker_block(body: str = "[REDACTED PRIVATE KEY]") -> str:
    """A redaction-marker block, assembled so this file holds no PEM literal."""
    begin = "-----" + "BEGIN OPENSSH PRIVATE KEY-----"
    end = "-----" + "END OPENSSH PRIVATE KEY-----"
    return f"{begin}\n{body}\n{end}\n"


def random_token() -> str:
    return base64.b64encode(hashlib.sha256(b"odile-secret-scan-fixture").digest()).decode()


def stripe_key() -> str:
    return "sk_" + "live_" + "Ab1" * 12


class RealRepositoryTest(unittest.TestCase):
    def test_the_tree_is_clean(self):
        result = scan("--tree")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: no secret", result.stdout)
        self.assertRegex(result.stdout, r"files scanned:\s+\d+")
        self.assertRegex(result.stdout, r"bytes scanned:\s+[\d,]+")

    def test_make_secrets_runs_the_tree_scan(self):
        result = subprocess.run(
            ["make", "-n", "secrets"], cwd=str(ROOT), capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tools/secret_scan.py --tree", result.stdout)

    def test_the_allowlist_names_its_reasons(self):
        for row in secret_scan.ALLOW:
            self.assertTrue(row.reason.strip(), row)
            self.assertIn(row.rule, {"pem-private-key", "provider-token", "credential-assignment", "high-entropy"})

    def test_a_value_scoped_row_does_not_allow_its_whole_path(self):
        self.assertTrue(
            secret_scan.allowlisted("high-entropy", "web/verify.html", "97BPLSG3phBe83GYHZx3pH1RRngAuQWt4vnm1myNaVvs")
        )
        self.assertFalse(secret_scan.allowlisted("high-entropy", "web/verify.html", "some-other-token"))

    def test_the_old_claim_key_row_is_history_only_and_value_scoped(self):
        # ODL-513 replaced the fixture with a marker. The old blob must stay
        # allowlisted for --history without reopening the path to a real key.
        self.assertFalse(
            secret_scan.allowlisted("pem-private-key", "tests/test_claim.py", "a-different-key")
        )

    def test_the_evidence_record_row_is_value_scoped(self):
        # The card's record quotes the fixture with its `PRIVATE_KEY = """` prefix,
        # which makes the scanned value 109 bytes and gives it this digest. The row
        # names that value, not the path: a real key pasted there still reports.
        begin = "-----" + "BEGIN OPENSSH PRIVATE KEY-----"
        end = "-----" + "END OPENSSH PRIVATE KEY-----"
        record = f'PRIVATE_KEY = """{begin}\n[REDACTED PRIVATE KEY]\n{end}'
        self.assertTrue(secret_scan.allowlisted(
            "pem-private-key", "docs/ops/evidence/ODL-513/evidence.txt", record))
        self.assertFalse(secret_scan.allowlisted(
            "pem-private-key", "docs/ops/evidence/ODL-513/evidence.txt", "a-real-key"))


class DetectionTest(unittest.TestCase):
    def test_a_complete_private_key_is_a_finding(self):
        candidates = secret_scan.scan_text(fake_private_key())
        self.assertEqual([(c.rule, c.line) for c in candidates], [("pem-private-key", 1)])

    def test_a_private_key_escaped_into_json_is_a_finding(self):
        text = '{"private_key": "' + fake_private_key().replace("\n", "\\n") + '"}\n'
        candidates = secret_scan.scan_text(text)
        self.assertEqual([c.rule for c in candidates], ["pem-private-key"])

    def test_a_header_without_an_end_is_a_redaction_marker(self):
        begin = "-----" + "BEGIN OPENSSH PRIVATE KEY-----"
        text = f"{begin}\n[REDACTED PRIVATE KEY]\n"
        self.assertEqual(secret_scan.scan_text(text), [])

    def test_a_complete_block_whose_only_body_is_the_marker_is_not_a_finding(self):
        # The redaction form a fixture uses: real markers, no key material. A
        # scanner that flagged this would make tests/test_claim.py noisy again.
        self.assertEqual(secret_scan.scan_text(marker_block()), [])

    def test_a_marker_line_above_a_real_body_is_still_a_finding(self):
        # The same fixture with a key put back must be caught: the exemption is
        # the marker *instead of* a body, not a marker anywhere in the block.
        body = "MIIB" + "A" * 60
        text = marker_block("[REDACTED PRIVATE KEY]\n" + body)
        self.assertEqual([c.rule for c in secret_scan.scan_text(text)], ["pem-private-key"])

    def test_public_key_and_certificate_blocks_are_public_material(self):
        body = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A" + "B" * 40
        for kind in ("PUBLIC KEY", "CERTIFICATE"):
            text = f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----\n"
            self.assertEqual(secret_scan.scan_text(text), [], kind)

    def test_a_provider_token_is_a_finding(self):
        candidates = secret_scan.scan_text(f"STRIPE_SECRET_KEY={stripe_key()}\n")
        self.assertEqual([c.rule for c in candidates], ["stripe"])

    def test_a_near_secret_key_is_a_finding(self):
        token = "ed25519:" + "A" * 88
        candidates = secret_scan.scan_text(f"key = {token}\n")
        self.assertIn("near-secret-key", [c.rule for c in candidates])

    def test_one_credential_is_one_finding(self):
        # An Anthropic key also fits the OpenAI shape; the report must not double it.
        token = "sk-ant-" + "aB3" * 12
        candidates = secret_scan.scan_text(f"ANTHROPIC_API_KEY={token}\n")
        self.assertEqual([c.rule for c in candidates], ["anthropic"])

    def test_a_high_entropy_token_is_a_finding(self):
        token = random_token()
        candidates = secret_scan.scan_text(f'const value = "{token}"\n')
        self.assertEqual([c.rule for c in candidates], ["high-entropy"])

    def test_a_high_entropy_assignment_is_a_finding(self):
        value = hashlib.sha256(b"another fixture").hexdigest()[:32]
        candidates = secret_scan.scan_text(f"API_TOKEN = {value}\n")
        self.assertEqual([c.rule for c in candidates], ["credential-assignment"])

    def test_placeholders_and_paths_are_not_findings(self):
        for line in (
            "API_TOKEN = your-api-key-goes-here\n",
            "SECRET = ${ODILE_SECRET}\n",
            "PASSWORD = [REDACTED]\n",
            "TOKEN = /var/lib/odile/setup.token\n",
            "GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')\n",
        ):
            self.assertEqual(secret_scan.scan_text(line), [], line)

    def test_a_finding_never_prints_the_value(self):
        token = random_token()
        candidates = secret_scan.scan_text(f'value = "{token}"\n')
        finding = secret_scan.Finding(candidates[0].rule, "x.txt", 3, candidates[0].value)
        rendered = finding.render()
        self.assertNotIn(token, rendered)
        self.assertIn("x.txt:3", rendered)
        self.assertIn("sha256:", rendered)


class ThrowawayCloneTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="secret-scan-")
        self.addCleanup(self._tmp.cleanup)
        self.repo = init_repo(Path(self._tmp.name) / "repo")
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        commit(self.repo, "clean")

    def test_a_clean_clone_passes_both_modes(self):
        result = scan("--all", root=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("commits scanned: 1", result.stdout)
        self.assertIn("PASS: no secret", result.stdout)

    def test_a_planted_private_key_fails(self):
        (self.repo / "leak.pem").write_text(fake_private_key(), encoding="utf-8")
        commit(self.repo, "plant a key")
        result = scan("--tree", root=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pem-private-key", result.stdout)
        self.assertIn("leak.pem:1", result.stdout)
        self.assertNotIn("BEGIN PRIVATE KEY", result.stdout)

    def test_a_planted_provider_token_fails(self):
        (self.repo / "leak.env").write_text(f"STRIPE_SECRET_KEY={stripe_key()}\n", encoding="utf-8")
        commit(self.repo, "plant a token")
        result = scan("--tree", root=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("stripe", result.stdout)
        self.assertIn("leak.env:1", result.stdout)
        self.assertNotIn(stripe_key(), result.stdout)

    def test_a_secret_removed_from_head_still_fails_history(self):
        (self.repo / "leak.pem").write_text(fake_private_key(), encoding="utf-8")
        planted = commit(self.repo, "plant a key")
        (self.repo / "leak.pem").unlink()
        commit(self.repo, "remove it from HEAD")
        tree = scan("--tree", root=self.repo)
        self.assertEqual(tree.returncode, 0, tree.stdout)
        history = scan("--history", root=self.repo)
        self.assertEqual(history.returncode, 1)
        self.assertIn(planted[:7], history.stdout)
        self.assertIn("leak.pem:1", history.stdout)

    def test_a_missing_checkout_exits_two(self):
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        result = scan("--tree", root=plain)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a git checkout", result.stderr)


if __name__ == "__main__":
    unittest.main()
