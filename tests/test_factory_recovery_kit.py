#!/usr/bin/python3
"""Tests for the factory recovery kit generator (sudden-loss restore).

The kit is five files (`README.md`, `recovery.env`, `recover.sh`,
`manifest.json`, `CHECKSUMS.txt`) and it is the *only* copy of the restic
passphrase and the R2/Cloudflare tokens once HQ is gone. The rules this module
holds:

* the file set is exactly five, nothing else travels with the kit;
* `recovery.env` is 0600 and the folder is 0700 — a world-readable kit is the
  passphrase on a shared disk;
* no secret reaches `README.md`, `manifest.json` or the generator's own stdout;
* a missing secret fails before anything is written, so nobody stores a kit
  that cannot restore;
* `--force` is required to replace a folder, and it removes a stray file;
* the Hermes commit / IPv4 / DNS names in the fallback manifest come from the
  same facts the nightly backup records (`hermes --version`, `ip route`, the
  tracked DNS lists), never from a secret.

Hermetic: a temporary state directory holds fixture secrets, the generator's
`hermes`/git probes are pinned through `ODILE_HERMES_VERSION` and
`ODILE_HERMES_COMMIT`, and nothing touches `/root/.odile`.

    /usr/bin/python3 -m unittest tests.test_factory_recovery_kit -v
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "factory_recovery_kit.py"
RECOVERY = ROOT / "deploy" / "recovery"
DNS_NAMES_FILE = RECOVERY / "dns-names.txt"
DNS_SKIP_FILE = RECOVERY / "dns-skip.txt"

# Planted values are assembled from pieces: a token-shaped literal in a tool
# argument can be redacted on write, which would make the "no secret" check
# pass for the wrong reason.
PASSPHRASE = "k" + "0" * 63
R2_KEY = "AKIA" + "R2KEY" + "7" * 13
R2_SECRET = "r2secret" + "9" * 24
ACCOUNT_ID = "acct" + "1" * 28
DNS_TOKEN = "dnstoken" + "5" * 30
ZONE_ID = "39068" + "3" * 27
SECRETS = (PASSPHRASE, R2_KEY, R2_SECRET, DNS_TOKEN)
KIT = ["CHECKSUMS.txt", "README.md", "manifest.json", "recover.sh", "recovery.env"]
HERMES_VERSION = "Hermes Agent v0.0.0-test (fixture) · stable · local " + "deadbeefdead"
HERMES_COMMIT = "deadbeefdead"


def read_names(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines
            if line.strip() and not line.lstrip().startswith("#")]


class KitTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.out = self.root / "kit"
        (self.state / "hq-backup.pass").write_text(PASSPHRASE, encoding="utf-8")
        (self.state / "cloudflare_account_id").write_text(ACCOUNT_ID, encoding="utf-8")
        (self.state / "cloudflare_dns_token").write_text(DNS_TOKEN, encoding="utf-8")
        (self.state / "cloudflare_zone_id").write_text(ZONE_ID, encoding="utf-8")
        (self.state / "r2-backup.env").write_text(
            "# fixture\n"
            f"AWS_ACCESS_KEY_ID={R2_KEY}\n"
            f"AWS_SECRET_ACCESS_KEY={R2_SECRET}\n"
            "R2_ENDPOINT=https://b118cf213a0c4b6287c5826741e04bcf.eu.r2.cloudflarestorage.com\n",
            encoding="utf-8",
        )

    def env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("RESTIC_", "AWS_", "R2_", "CLOUDFLARE_"))}
        env["ODILE_HERMES_VERSION"] = HERMES_VERSION
        env["ODILE_HERMES_COMMIT"] = HERMES_COMMIT
        return env

    def run_kit(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--state-dir", str(self.state), *args],
            capture_output=True, text=True, env=self.env(), timeout=120,
        )

    def write(self, text: str = "") -> subprocess.CompletedProcess:
        return self.run_kit("--out", str(self.out), *([text] if text else []))

    def manifest(self) -> dict:
        return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))


class FileSetTests(KitTestCase):
    def test_the_kit_is_exactly_five_files(self):
        result = self.write()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), KIT)

    def test_the_secrets_file_is_owner_only_and_the_folder_is_private(self):
        self.assertEqual(self.write().returncode, 0)
        env_mode = stat.S_IMODE((self.out / "recovery.env").stat().st_mode)
        script_mode = stat.S_IMODE((self.out / "recover.sh").stat().st_mode)
        dir_mode = stat.S_IMODE(self.out.stat().st_mode)
        self.assertEqual(env_mode, 0o600)
        self.assertEqual(script_mode, 0o700)
        self.assertEqual(dir_mode, 0o700)
        self.assertEqual(env_mode & 0o077, 0, "recovery.env has world/group bits")

    def test_every_secret_is_in_recovery_env_and_nothing_else_is_0644(self):
        self.assertEqual(self.write().returncode, 0)
        text = (self.out / "recovery.env").read_text(encoding="utf-8")
        for key in ("RESTIC_PASSWORD", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                    "R2_ENDPOINT", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_DNS_TOKEN",
                    "ZONE_ID"):
            self.assertIn(f"{key}=", text)
        for secret in SECRETS:
            self.assertIn(secret, text)
        for name in ("README.md", "manifest.json", "CHECKSUMS.txt"):
            mode = stat.S_IMODE((self.out / name).stat().st_mode)
            self.assertEqual(mode & 0o600, 0o600)

    def test_the_checksums_verify(self):
        self.assertEqual(self.write().returncode, 0)
        result = subprocess.run(
            ["sha256sum", "-c", "CHECKSUMS.txt"], cwd=self.out,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            len((self.out / "CHECKSUMS.txt").read_text(encoding="utf-8").splitlines()), 4
        )

    def test_the_script_and_guide_are_the_repository_copies(self):
        self.assertEqual(self.write().returncode, 0)
        self.assertEqual((self.out / "recover.sh").read_bytes(),
                         (RECOVERY / "recover.sh").read_bytes())
        self.assertEqual((self.out / "README.md").read_bytes(),
                         (RECOVERY / "README.md").read_bytes())


class NoSecretTests(KitTestCase):
    def test_no_secret_reaches_the_guide_the_manifest_or_stdout(self):
        result = self.write()
        self.assertEqual(result.returncode, 0, result.stderr)
        readme = (self.out / "README.md").read_text(encoding="utf-8")
        manifest = (self.out / "manifest.json").read_text(encoding="utf-8")
        for secret in SECRETS + (ACCOUNT_ID,):
            self.assertNotIn(secret, readme)
            self.assertNotIn(secret, manifest)
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)

    def test_the_manifest_carries_the_facts_the_restore_needs(self):
        self.assertEqual(self.write().returncode, 0)
        recovery = self.manifest()["recovery"]
        self.assertEqual(recovery["schema"], "odile.factory-recovery.v1")
        self.assertRegex(recovery["written_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(recovery["hermes_commit"], HERMES_COMMIT)
        self.assertEqual(recovery["hermes_version"], HERMES_VERSION)
        self.assertEqual(recovery["zone_id"], ZONE_ID)
        self.assertEqual(recovery["bucket"], "hermes-hq-backup")
        self.assertEqual(recovery["endpoint"], "https://b118cf213a0c4b6287c5826741e04bcf.eu.r2.cloudflarestorage.com")
        self.assertEqual(recovery["dns_names"], read_names(DNS_NAMES_FILE))
        self.assertEqual(recovery["dns_skip"], read_names(DNS_SKIP_FILE))
        self.assertRegex(recovery.get("ipv4", ""), r"^$|^(\d{1,3}\.){3}\d{1,3}$")
        self.assertTrue(recovery["hostname"])

    def test_the_demo_tenant_and_the_guests_are_never_moved(self):
        self.assertEqual(self.write().returncode, 0)
        recovery = self.manifest()["recovery"]
        self.assertIn("demo.odile.fyi", recovery["dns_skip"])
        self.assertIn("odl-7971a8.odile.fyi", recovery["dns_skip"])
        self.assertIn("odl-*.test.odile.fyi", recovery["dns_skip"])
        self.assertNotIn("demo.odile.fyi", recovery["dns_names"])
        self.assertEqual(recovery["dns_names"],
                         ["odile.fyi", "gw.odile.fyi", "mail.odile.fyi",
                          "testing.odile.fyi", "test.odile.fyi"])


class RefusalTests(KitTestCase):
    def test_a_missing_secret_fails_and_writes_nothing(self):
        (self.state / "hq-backup.pass").unlink()
        result = self.write()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restic passphrase", result.stderr)
        self.assertIn("hq-backup.pass", result.stderr)
        self.assertFalse(self.out.exists(), "a failed run left a partial kit")

    def test_a_short_passphrase_is_refused(self):
        (self.state / "hq-backup.pass").write_text("too-short", encoding="utf-8")
        result = self.write()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected 64", result.stderr)
        self.assertFalse(self.out.exists())

    def test_a_missing_r2_value_is_refused(self):
        (self.state / "r2-backup.env").write_text(
            f"AWS_ACCESS_KEY_ID={R2_KEY}\nR2_ENDPOINT=https://x.eu.r2.cloudflarestorage.com\n",
            encoding="utf-8",
        )
        result = self.write()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AWS_SECRET_ACCESS_KEY", result.stderr)
        self.assertFalse(self.out.exists())

    def test_a_malformed_zone_id_is_refused(self):
        (self.state / "cloudflare_zone_id").write_text("not-hex", encoding="utf-8")
        result = self.write()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("32 hex", result.stderr)

    def test_an_existing_folder_needs_force(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("old kit\n", encoding="utf-8")
        result = self.write()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--force", result.stderr)
        self.assertEqual([path.name for path in self.out.iterdir()], ["keep.txt"])

    def test_force_replaces_the_folder_and_removes_a_stray_file(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("old kit\n", encoding="utf-8")
        result = self.run_kit("--out", str(self.out), "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), KIT)

    def test_the_zone_id_can_be_overridden(self):
        other = "abcdef" + "0" * 26
        result = self.run_kit("--out", str(self.out), "--zone-id", other)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest()["recovery"]["zone_id"], other)
        env_text = (self.out / "recovery.env").read_text(encoding="utf-8")
        self.assertIn(f"ZONE_ID={other}", env_text)


if __name__ == "__main__":
    unittest.main()
