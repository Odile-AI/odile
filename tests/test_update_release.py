#!/usr/bin/python3
"""The release cutter writes the whole bundle, signed pair included.

`update/odile_update.py release` used to write the seven names the release
schema defines and leave `odile_install.py.sig` and `odile-signing-key.pub` to
two hand-run `install` commands (docs/runbooks/update.md §5). A cut that skipped
them produced a bundle a stranger could download but not verify. These tests
hold the cutter to the bundle:

* a cut into an empty directory writes exactly the nine names a full bundle
  carries, the committed signature and key byte for byte, and the signature
  verifies the installer that travels beside it;
* the cut refuses (non-zero, no bundle) when the signature does not verify the
  installer bytes.

Hermetic: a scratch machine under a temp dir, a generated Ed25519 key for the
release documents, and the repository's committed installer pair. No network,
no tenant, no sleeps.

    /usr/bin/python3 -m unittest discover -s tests -p 'test_update_release.py' -v
"""

from __future__ import annotations

import base64
import contextlib
import functools
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "update" / "odile_update.py"
INSTALLER = ROOT / "installer" / "odile_install.py"
INSTALLER_SIG = ROOT / "installer" / "odile_install.py.sig"
PUBLISHED_KEY = ROOT / "installer" / "odile-signing-key.pub"

# The names a full cut writes: the three artifacts and the two documents with
# their signatures (seven), the optional Webapp bundle (eight), and the signed
# pair that makes the download verifiable on its own (§3, nine).
BUNDLE_FILES = {
    "release.json", "release.json.sig",
    "manifest.json", "manifest.json.sig",
    "odile_install.py", "odile_install.py.sig",
    "skills.tar.gz", "webapp.tar.gz",
    "odile-signing-key.pub",
}

try:  # `unittest discover -s tests` imports test modules top-level.
    from test_flow import CORE_RELEASE_FILES
except ImportError:  # `-m unittest tests.test_update_release` imports a package.
    from tests.test_flow import CORE_RELEASE_FILES


def load_update():
    """The cutter as the command line loads it: a script, not a package."""
    spec = importlib.util.spec_from_file_location("odile_update_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module here
    spec.loader.exec_module(module)
    return module


class ReleaseCut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.update = load_update()
        self.root = self.update._self_test_root(self.base)
        # The exported installer must be the committed one: the repository's
        # signature is made over those bytes, which is the pair under test.
        shutil.copyfile(INSTALLER, self.root / "bootstrap" / "odile_install.py")
        self.layout = self.update.resolve_layout(self.root, "tester")
        self.manifest = self.update._self_test_manifest(
            self.layout, generated_at="2026-11-10T00:00:00Z")
        self.manifest_path = self.base / "manifest.json"
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.key = self.base / "release-key.pem"
        subprocess.run([self.update.openssl(), "genpkey", "-algorithm", "ED25519",
                        "-out", str(self.key)], check=True, capture_output=True)
        os.chmod(self.key, 0o600)
        self.out = self.base / "bundle"

    def tearDown(self):
        self.tmp.cleanup()

    def cut(self, **overrides):
        options = dict(release_id="2026.11.10-1", boot_kernel=os.uname().release,
                       out_dir=self.out, generated_at="2026-11-10T00:10:00Z",
                       key=self.key, with_webapp=True)
        options.update(overrides)
        manifest_path = options.pop("manifest_path", self.manifest_path)
        return self.update.build_release(self.layout, manifest_path, **options)

    def test_a_full_cut_writes_the_nine_file_bundle(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            document = self.cut()

        names = {path.name for path in self.out.iterdir()}
        self.assertEqual(names, BUNDLE_FILES)
        self.assertEqual(len(names), 9)
        # tests/test_flow.py serves CORE_RELEASE_FILES from release/; the cut
        # must produce every one of them without a manual install afterwards.
        self.assertLessEqual(set(CORE_RELEASE_FILES), names)
        self.assertNotIn("odile-signing-key.pub", CORE_RELEASE_FILES,
                         "the key is a copy beside the core names, not a schema artifact")

        # The two copies are the committed pair, byte for byte.
        self.assertEqual((self.out / "odile_install.py.sig").read_bytes(),
                         INSTALLER_SIG.read_bytes())
        self.assertEqual((self.out / "odile-signing-key.pub").read_bytes(),
                         PUBLISHED_KEY.read_bytes())

        # The signature that travels verifies the installer that travels, and
        # the installer is the one release.json names.
        signature = self.update.signature_bytes(
            (self.out / "odile_install.py.sig").read_bytes(), "odile_install.py.sig")
        self.assertTrue(self.update.verify_bytes(
            (self.out / "odile_install.py").read_bytes(), signature,
            self.out / "odile-signing-key.pub"))
        self.assertEqual(document["artifacts"]["installer"]["sha256"],
                         self.update.sha256_file(self.out / "odile_install.py"))

        # One line for each copy: a silent copy is a bundle nobody can audit.
        copy_lines = [line for line in output.getvalue().splitlines()
                      if line.startswith("copy")]
        self.assertEqual(len(copy_lines), 2, copy_lines)
        self.assertTrue(any("odile_install.py.sig" in line for line in copy_lines))
        self.assertTrue(any("odile-signing-key.pub" in line for line in copy_lines))

    def test_a_signature_that_does_not_verify_refuses_the_cut(self):
        # A real 64-byte signature with one byte moved: base64-valid, wrong.
        raw = bytearray(base64.b64decode(INSTALLER_SIG.read_text(encoding="ascii")))
        raw[0] ^= 0x01
        bad = self.base / "bad.sig"
        bad.write_bytes(self.update.signature_file_bytes(bytes(raw)))
        real = self.update.installer_pair_paths
        self.update.installer_pair_paths = lambda layout: (bad, PUBLISHED_KEY)
        try:
            with self.assertRaises(self.update.UpdateError) as caught:
                self.cut()
        finally:
            self.update.installer_pair_paths = real
        self.assertEqual(caught.exception.code, self.update.EXIT_BAD_SIGNATURE)
        self.assertFalse(self.out.exists(), "a refused cut created the bundle directory")

    def test_a_backup_pin_that_is_not_this_roots_program_refuses_the_cut(self):
        """The manifest row and the root's backup.py must be the same bytes."""
        data = dict(self.manifest)
        data["backup_sha256"] = "0" * 64
        wrong = self.base / "backup-wrong.json"
        wrong.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(self.update.UpdateError) as caught:
            self.cut(manifest_path=wrong)
        self.assertEqual(caught.exception.code, self.update.EXIT_BAD_INPUT)
        self.assertIn("backup_sha256", str(caught.exception))
        self.assertFalse(self.out.exists())


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """A static release server with no access log in the test output."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass


@unittest.skipUnless(os.geteuid() == 0, "the guest commands write outside a temp dir only as root")
class GuestCheckTests(unittest.TestCase):
    """The guest's `check` names the backup program, or says the release does not.

    The manifest is the carrier: `release.json` names the manifest by sha256 and
    the guest verifies that signature, so the new row travels inside the signed
    manifest. A manifest built before the row existed still verifies — the field
    is optional — and `check` prints a warning instead of refusing, so the
    published release keeps working until the next cut carries the row.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.update = load_update()
        self.root = self.update._self_test_root(self.base)
        shutil.copyfile(INSTALLER, self.root / "bootstrap" / "odile_install.py")
        self.layout = self.update.resolve_layout(self.root, "tester")
        self.manifest = self.update._self_test_manifest(
            self.layout, generated_at="2026-11-10T00:00:00Z")
        self.key = self.base / "release-key.pem"
        subprocess.run([self.update.openssl(), "genpkey", "-algorithm", "ED25519",
                        "-out", str(self.key)], check=True, capture_output=True)
        os.chmod(self.key, 0o600)
        self.pub = self.base / "release-key.pub"
        subprocess.run([self.update.openssl(), "pkey", "-in", str(self.key), "-pubout",
                        "-out", str(self.pub)], check=True, capture_output=True)
        self.servers: list[http.server.ThreadingHTTPServer] = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def publish(self, manifest: dict, name: str) -> str:
        out = self.base / name
        path = self.base / f"{name}.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.update.build_release(self.layout, path, release_id="2026.11.10-1",
                                      boot_kernel=os.uname().release, out_dir=out,
                                      generated_at="2026-11-10T00:10:00Z", key=self.key)
        handler = functools.partial(QuietHandler, directory=str(out))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/release.json"

    def check(self, url: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "check", "--root", str(self.root),
             "--user", "tester", "--release-url", url, "--pubkey", str(self.pub)],
            capture_output=True, text=True)

    def test_check_names_the_backup_program(self):
        result = self.check(self.publish(self.manifest, "with-backup"))
        self.assertEqual(result.returncode, 0, result.stderr)
        prefix = self.manifest["backup_sha256"][:12]
        self.assertIn(f"backup {prefix}", result.stdout)
        self.assertNotIn("warn", result.stdout + result.stderr)

    def test_a_manifest_without_the_row_is_warned_about_not_refused(self):
        older = dict(self.manifest)
        older.pop("backup_sha256")
        result = self.check(self.publish(older, "without-backup"))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        self.assertIn("warn", output)
        self.assertIn("backup_sha256", output)

    def test_apply_dry_run_warns_about_the_same_row(self):
        """`apply` shares `load_release`, so the warning is not `check`-only."""
        older = dict(self.manifest)
        older.pop("backup_sha256")
        url = self.publish(older, "without-backup-dry")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "apply", "--dry-run", "--root", str(self.root),
             "--user", "tester", "--release-url", url, "--pubkey", str(self.pub)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("backup_sha256", result.stderr)
        self.assertIn("dry run      nothing written", result.stdout)

    def test_a_malformed_row_is_refused(self):
        bad = dict(self.manifest)
        bad["backup_sha256"] = "not-a-sha256"
        with self.assertRaises(self.update.UpdateError) as caught:
            self.update.validate_manifest(bad)
        self.assertEqual(caught.exception.code, self.update.EXIT_INCONSISTENT)
        self.assertIn("backup_sha256", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
