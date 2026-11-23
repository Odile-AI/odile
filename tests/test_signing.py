#!/usr/bin/python3
"""The offline signing ceremony and the seed certificate chain (ODL-514).

Two surfaces, both driven through the code that ships them:

* `make release-sign` and `make seed-cert`: the `/root` refusal (the release key
  is offline), the sha256 line printed before the signature file is written, a
  signature `openssl dgst -verify` accepts, and identical bytes on a re-run;
* the installer's `cached_seed()` and `verify_seed_body()`: a seed certified by
  the current release key, one certified by the key being retired, a third key
  refused with the reason on one line, and a seed with no certificate still
  verified against the release key.

Every key here is generated in a temporary directory; no live key, token or
database is touched.

    /usr/bin/python3 -m unittest tests.test_signing -v
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKE = ["make", "--no-print-directory", "-C", str(ROOT)]
INSTALLER = ROOT / "installer" / "odile_install.py"
PUBKEY = ROOT / "installer" / "odile-signing-key.pub"


@contextlib.contextmanager
def outside_root():
    """A scratch directory that is not under /root.

    The ceremony refuses a key under /root, and this harness's TMPDIR is under
    /root (`~/.hermes/.../cache/scratch`), so a fixture key there would be
    refused for the right reason and the test would prove nothing. /var/tmp is
    the tree's own convention for one-shot roots (AGENTS.md).
    """
    path = Path(tempfile.mkdtemp(prefix="odl514-sign-", dir="/var/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def load_installer():
    """Load the shipped installer by path, the way its own self-test is run."""
    spec = importlib.util.spec_from_file_location("odile_install_signing_test", INSTALLER)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load installer/odile_install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(MAKE + arguments, capture_output=True, text=True, timeout=120)


def keypair(directory: Path, name: str) -> tuple[Path, Path]:
    private = directory / f"{name}.pem"
    public = directory / f"{name}.pub"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
                   check=True, capture_output=True, timeout=60)
    os.chmod(private, 0o600)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                   check=True, capture_output=True, timeout=60)
    return private, public


def verifies(data: Path, signature: Path, public: Path) -> bool:
    result = subprocess.run(["openssl", "dgst", "-verify", str(public), "-signature",
                             str(signature), str(data)], capture_output=True, text=True)
    return "Verified OK" in result.stdout


class ReleaseSignTests(unittest.TestCase):
    def test_a_key_under_root_is_refused_before_anything_is_written(self) -> None:
        with outside_root() as directory:
            target = Path(directory) / "x"
            target.write_bytes(b"a release file\n")
            result = make(["release-sign", f"FILE={target}",
                           "KEY=/root/.odile/installer-signing.pem"])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("must be offline", result.stderr)
            self.assertIn("/root", result.stderr)
            self.assertFalse(Path(str(target) + ".sig").exists(),
                             "the refusal comes before any file is written")

    def test_a_scratch_key_signs_and_a_second_run_is_byte_identical(self) -> None:
        with outside_root() as directory:
            root = Path(directory)
            key, public = keypair(root, "release")
            target = root / "x"
            target.write_bytes(b"the bytes a release signature covers\n")
            first = make(["release-sign", f"FILE={target}", f"KEY={key}", f"PUBKEY={public}"])
            self.assertEqual(first.returncode, 0, first.stderr)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            lines = first.stdout.splitlines()
            self.assertEqual(lines[0], f"{digest}  {target}",
                             "the sha256sum line comes first")
            signature = Path(str(target) + ".sig")
            self.assertTrue(signature.is_file())
            raw = Path(directory) / "raw"
            raw.write_bytes(base64.b64decode(b"".join(signature.read_bytes().split())))
            self.assertTrue(verifies(target, raw, public))
            self.assertEqual(lines[1], base64.b64encode(raw.read_bytes()).decode("ascii"),
                             "the signature is printed before the file is written")
            first_bytes = signature.read_bytes()
            second = make(["release-sign", f"FILE={target}", f"KEY={key}", f"PUBKEY={public}"])
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(signature.read_bytes(), first_bytes,
                             "a second run produces identical bytes")
            self.assertEqual(signature.read_bytes().count(b"\n"), 2,
                             "base64 in 76-column lines plus a trailing newline")

    def test_the_default_pubkey_is_the_repo_key_and_names_the_override(self) -> None:
        with outside_root() as directory:
            root = Path(directory)
            key, _public = keypair(root, "scratch")
            target = root / "x"
            target.write_bytes(b"a release file\n")
            result = make(["release-sign", f"FILE={target}", f"KEY={key}"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(str(PUBKEY), result.stderr)
            self.assertIn("PUBKEY=", result.stderr,
                          "a scratch key without PUBKEY= is told how to finish")
            self.assertFalse(Path(str(target) + ".sig").exists())


class SeedCertTests(unittest.TestCase):
    def test_a_key_under_root_is_refused_and_no_certificate_is_written(self) -> None:
        committed = ROOT / "deploy" / "seed" / "seed-key.crt"
        before = committed.read_bytes()
        with outside_root() as directory:
            root = Path(directory)
            _seed_key, seed_public = keypair(root, "seed")
            out = root / "out" / "seed-key.crt"
            result = make(["seed-cert", f"FILE={seed_public}",
                           "KEY=/root/.odile/installer-signing.pem", f"OUT={out}"])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("must be offline", result.stderr)
            self.assertFalse(out.exists(), "no certificate is written")
        self.assertEqual(committed.read_bytes(), before)

    def test_a_scratch_key_writes_a_certificate_openssl_accepts_and_repeats(self) -> None:
        with outside_root() as directory:
            root = Path(directory)
            release_key, release_public = keypair(root, "release")
            _seed_key, seed_public = keypair(root, "seed")
            out = root / "out" / "seed-key.crt"
            first = make(["seed-cert", f"FILE={seed_public}", f"KEY={release_key}",
                          f"OUT={out}"])
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(len(out.read_bytes()), 64, "the certificate is the 64-byte signature")
            self.assertTrue(verifies(seed_public, out, release_public),
                            "openssl dgst -verify accepts deploy/seed/seed-key.crt")
            public_copy = out.with_name("seed-key.pub")
            self.assertEqual(public_copy.read_bytes(), seed_public.read_bytes())
            before = out.read_bytes()
            second = make(["seed-cert", f"FILE={seed_public}", f"KEY={release_key}",
                           f"OUT={out}"])
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(out.read_bytes(), before, "Ed25519 over the same bytes is the same bytes")

    def test_a_private_key_is_refused_as_the_file_argument(self) -> None:
        with outside_root() as directory:
            root = Path(directory)
            release_key, _release_public = keypair(root, "release")
            seed_key, _seed_public = keypair(root, "seed")
            out = root / "out" / "seed-key.crt"
            result = make(["seed-cert", f"FILE={seed_key}", f"KEY={release_key}", f"OUT={out}"])
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("private key", result.stderr)
            self.assertFalse(out.exists())


CONTRACT = {
    "slug": "odl-sign-test",
    "instance_id": "srv-sign-test",
    "near_api_key": "near-test-key",
    "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    "support_public_key": "ssh-ed25519 AAAA test@odile.fyi",
    "backup_repo_url": "sftp:odile-backup@backup.odile.fyi:/tenants/odl-sign-test",
    "hermes_version": "2026.11.1",
    # ODL-925: the seed carries the account's sign-in verifier; the cache
    # verifies the body through validate_seed, which now requires it.
    "sign_in_hash": "scrypt$16384$8$1$c2FsdA==$ZGs=",
    "mail_relay": {"host": "smtp.mail.odile.fyi", "port": 587,
                   "username": "odl-sign-test@mail.odile.fyi", "secret": "relay"},
}


class CachedSeedTests(unittest.TestCase):
    """The installer's cache check walks the certificate chain (ODL-514)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = load_installer()

    def cache(self, directory: Path, body: bytes, signature: bytes):
        layout = self.installer.Layout.scratch(
            root=directory / "root", user="odiletest", webapp_port=9140, public_url="http://odile-sim.test:9140")
        layout.etc.mkdir(parents=True, exist_ok=True)
        layout.seed_cache.write_bytes(body)
        layout.seed_sig.write_bytes(base64.b64encode(signature) + b"\n")
        return layout

    def seed_body(self, directory: Path, cert: bytes | None, *, signer: Path) -> tuple[bytes, bytes]:
        document = dict(CONTRACT)
        if cert is not None:
            seed_public = (directory / "seed.pub").read_bytes()
            document["signing_key_sha256"] = hashlib.sha256(seed_public).hexdigest()
            document["signing_key"] = seed_public.decode("ascii")
            document["signing_key_cert"] = base64.b64encode(cert).decode("ascii")
        path = directory / "seed.json"
        path.write_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        signature = directory / "seed.sig"
        subprocess.run(["openssl", "dgst", "-sign", str(signer), "-out", str(signature), str(path)],
                       check=True, capture_output=True, timeout=60)
        return path.read_bytes(), signature.read_bytes()

    def certificate(self, directory: Path, signer: Path) -> bytes:
        out = directory / f"{signer.stem}.crt"
        subprocess.run(["openssl", "dgst", "-sign", str(signer), "-out", str(out),
                        str(directory / "seed.pub")], check=True, capture_output=True, timeout=60)
        return out.read_bytes()

    def test_the_cache_accepts_a_seed_certified_by_the_current_key(self) -> None:
        with outside_root() as directory:
            root = Path(directory)
            release_key, release_public = keypair(root, "release")
            seed_key, seed_public = keypair(root, "seed")
            (root / "seed.pub").write_bytes(seed_public.read_bytes())
            body, signature = self.seed_body(root, self.certificate(root, release_key),
                                             signer=seed_key)
            layout = self.cache(root, body, signature)
            parsed = self.installer.cached_seed(layout, release_public, CONTRACT["instance_id"],
                                                CONTRACT["slug"])
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["slug"], CONTRACT["slug"])
            self.assertEqual(parsed["signing_key_sha256"], hashlib.sha256(seed_public.read_bytes()).hexdigest())

    def test_the_retired_key_certifies_a_seed_too(self) -> None:
        """The key being retired is a candidate, so a rotation does not strip a seed."""
        with outside_root() as directory:
            root = Path(directory)
            retired_key, retired_public = keypair(root, "retired")
            _new_key, new_public = keypair(root, "new")
            seed_key, seed_public = keypair(root, "seed")
            (root / "seed.pub").write_bytes(seed_public.read_bytes())
            body, _signature = self.seed_body(root, self.certificate(root, retired_key),
                                              signer=seed_key)
            parsed = json.loads(body)
            self.assertEqual(
                self.installer.certify_seed_key(parsed, [new_public, retired_public]),
                retired_public)
            # The installer's own directory carries the retired key beside the
            # kit key. Today both files hold the same key (no rotation yet), so
            # the identical bytes are tried once; after the CI key move
            # (ODL-517) they differ and both are candidates.
            prev = ROOT / "installer" / "odile-signing-key.prev.pub"
            self.assertTrue(prev.is_file(), "the key being retired ships for one release")
            candidates = self.installer.seed_key_candidates(PUBKEY)
            self.assertIn(PUBKEY, candidates)
            if prev.read_bytes() != PUBKEY.read_bytes():
                self.assertIn(prev, candidates)

    def test_a_third_key_is_refused_with_one_line(self) -> None:
        with outside_root() as directory:
            root = Path(directory)
            release_key, release_public = keypair(root, "release")
            third_key, _third_public = keypair(root, "third")
            seed_key, seed_public = keypair(root, "seed")
            (root / "seed.pub").write_bytes(seed_public.read_bytes())
            body, signature = self.seed_body(root, self.certificate(root, third_key),
                                             signer=seed_key)
            with self.assertRaises(SystemExit) as caught:
                self.installer.verify_seed_body(body, signature, release_public)
            message = str(caught.exception)
            self.assertTrue(message.startswith("error: "), message)
            self.assertNotIn("\n", message, "the reason is one line")
            self.assertIn("was not signed by", message)

    def test_a_seed_without_a_certificate_still_verifies_against_the_release_key(self) -> None:
        with outside_root() as directory:
            root = Path(directory)
            release_key, release_public = keypair(root, "release")
            body, signature = self.seed_body(root, None, signer=release_key)
            layout = self.cache(root, body, signature)
            parsed = self.installer.cached_seed(layout, release_public, CONTRACT["instance_id"],
                                                CONTRACT["slug"])
            self.assertIsNotNone(parsed)
            self.assertNotIn("signing_key_sha256", parsed)

    def test_a_certificate_over_another_key_is_refused(self) -> None:
        with outside_root() as directory:
            root = Path(directory)
            release_key, release_public = keypair(root, "release")
            seed_key, seed_public = keypair(root, "seed")
            _other_key, other_public = keypair(root, "other")
            (root / "seed.pub").write_bytes(seed_public.read_bytes())
            body, signature = self.seed_body(root, self.certificate(root, release_key),
                                             signer=seed_key)
            # The body names the real public key; the certificate is over another one.
            swapped = json.loads(body)
            swapped["signing_key_cert"] = base64.b64encode(
                self._over(root, release_key, other_public)).decode("ascii")
            forged = (json.dumps(swapped, sort_keys=True, separators=(",", ":")) + "\n").encode()
            with self.assertRaises(SystemExit) as caught:
                self.installer.verify_seed_body(forged, signature, release_public)
            self.assertIn("was not signed by", str(caught.exception))

    def test_a_substituted_seed_key_is_refused_by_its_digest(self) -> None:
        """`signing_key_sha256` names the key; another key cannot ride the same certificate."""
        with outside_root() as directory:
            root = Path(directory)
            release_key, release_public = keypair(root, "release")
            seed_key, seed_public = keypair(root, "seed")
            _other_key, other_public = keypair(root, "other")
            (root / "seed.pub").write_bytes(seed_public.read_bytes())
            body, signature = self.seed_body(root, self.certificate(root, release_key),
                                             signer=seed_key)
            swapped = json.loads(body)
            swapped["signing_key"] = other_public.read_text(encoding="utf-8")
            forged = (json.dumps(swapped, sort_keys=True, separators=(",", ":")) + "\n").encode()
            with self.assertRaises(SystemExit) as caught:
                self.installer.verify_seed_body(forged, signature, release_public)
            self.assertIn("signing_key_sha256 says", str(caught.exception))

    @staticmethod
    def _over(root: Path, signer: Path, target: Path) -> bytes:
        out = root / f"over-{target.stem}.crt"
        subprocess.run(["openssl", "dgst", "-sign", str(signer), "-out", str(out), str(target)],
                       check=True, capture_output=True, timeout=60)
        return out.read_bytes()


if __name__ == "__main__":
    unittest.main()
