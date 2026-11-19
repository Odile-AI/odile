#!/usr/bin/python3
"""Tests for ODL-214: the signed app-layer manifest.

Hermetic: every fixture is a small scratch root under a temp dir, the keys are
generated with the system OpenSSL, and the HTTP test speaks to a loopback
server on an ephemeral port. No tenant, no live API, no sleeps.

Organised around the card's four lines:

* two fresh installs produce the same manifest except the timestamp;
* the signature verifies with the published key;
* a changed file breaks the signature check;
* the manifest is served at /manifest with `cache-control: no-store`.

ODL-399 adds one fact to the document: `hermes.install_script_sha256`, the
hash the installer required of the fetched `install.sh` before running it. The
fixture installer answers `--manifest` the way the real one does, so the
builder reads the pin from the same record a machine would carry.

The follow-up (t_e0475dba) adds `backup_sha256`, the tenant backup program the
nightly unit executes. It is read from the installed root's
`/usr/local/lib/odile/backup.py` — the file the installer wrote — and falls
back to the installer's own embedded pin when a scratch root predates that
step, because that pin is the bytes the installer would write. A tenant root
without the program is a refusal.

    /usr/bin/python3 -m unittest tests.test_manifest -v
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
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
SCRIPT = ROOT / "scripts" / "manifest.py"

COMMIT = "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19"
MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017-abliterated"
FIXED_NOW = "2026-11-08T00:00:00Z"
LATER = "2026-11-08T00:01:00Z"
FIXED_INSTALL_PIN = "abf338605dae6dd28d101673ac6aed7995c955745db46d47676bc591a7957209"
OTHER_INSTALL_PIN = "abf338605dae6dd28d101673ac6aed7995c955745db46d47676bc591a7957208"
BACKUP_PROGRAM = b"#!/usr/bin/python3\n# the fixture tenant backup program\nprint('backup')\n"
FIXED_BACKUP_PIN = hashlib.sha256(BACKUP_PROGRAM).hexdigest()
OTHER_BACKUP_PIN = hashlib.sha256(BACKUP_PROGRAM + b"# one byte more\n").hexdigest()

INSTALLER_SOURCE = """\
#!/usr/bin/python3
\"\"\"A fixture installer: `--manifest` prints the record scripts/manifest.py reads.\"\"\"
import json
import sys

PIN = "<PIN>"
BACKUP = "<BACKUP_PIN>"

if "--manifest" in sys.argv:
    print(json.dumps({"name": "odile_install", "version": "0.1.0",
                      "hermes_install_sha256": PIN, "backup_sha256": BACKUP}))
    raise SystemExit(0)
print("installer")
"""

FIXED_VERSIONS = {
    "caddy": "2.6.2",
    "curl": "8.14.1",
    "git": "2.53.0",
    "nodejs": "22.22.1",
    "npm": "12.0.2",
    "openssl": "3.5.5",
    "python3": "3.14.9",
    "restic": "0.18.0",
    "tmux": "3.5a",
}

DIST_INFO = """\
Metadata-Version: 2.1
Name: {name}
Version: {version}
Summary: fixture
"""

ODILE_HOME = """\
# Odile home

- You run as `{user}` and have NOPASSWD sudo. You can read every file on the
  machine.
- Your workspace is `{workspace}`. Files you write there survive a restart.
- Inference goes straight to NEAR AI with this machine's own key.
"""


def load_manifest_script():
    """Load `scripts/manifest.py` as a module; it is a script, not a package."""
    spec = importlib.util.spec_from_file_location("odile_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, data: bytes, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def installer_source(pin: str = FIXED_INSTALL_PIN, backup_pin: str = FIXED_BACKUP_PIN) -> bytes:
    return (INSTALLER_SOURCE.replace("<PIN>", pin)
            .replace("<BACKUP_PIN>", backup_pin)).encode("utf-8")


def make_root(base: Path, name: str = "root", user: str = "tester",
              install_pin: str = FIXED_INSTALL_PIN, backup_pin: str = FIXED_BACKUP_PIN,
              backup_program: bytes = BACKUP_PROGRAM) -> Path:
    """A scratch root shaped like the installer leaves one."""
    root = base / name
    home = root / "home" / user
    hermes = home / ".hermes"
    write(root / "bootstrap/odile_install.py", installer_source(install_pin, backup_pin), 0o755)
    write(root / "usr/local/lib/odile/backup.py", backup_program, 0o755)
    write(root / "etc/odile/seed.json", json.dumps({
        "slug": "odl-test", "model": MODEL, "near_api_key": "near-secret",
    }).encode() + b"\n", 0o600)
    write(hermes / "tools/facts.json", json.dumps({
        "schema": 1,
        "packages": {
            "node": {"version": "26.12.1", "digest": "aa" * 32, "entry": "node-26.12.1-linux-x64"},
            "python": {"version": "3.14.9+2026", "digest": "bb" * 32, "entry": "python-3.14.9"},
        },
    }, indent=2).encode() + b"\n")

    venv = hermes / "installs/abc/environments/env1/venv"
    site = venv / "lib/python3.14/site-packages"
    for dist, dist_version in (("openai", "2.54.0"), ("httpx", "0.28.1")):
        write(site / f"{dist}-{dist_version}.dist-info/METADATA",
              DIST_INFO.format(name=dist, version=dist_version).encode())
    write(hermes / "installs/abc/facts.json", json.dumps({
        "schema": 1, "packages": {"venv": {"environment": str(venv)}},
    }).encode() + b"\n")

    write(hermes / "skills/.bundled_manifest", b"bundle fixture\n")
    write(hermes / "skills/odile-home/SKILL.md", ODILE_HOME.format(
        user=user, workspace=home / "workspace").encode())
    write(hermes / "skills/alpha/SKILL.md", b"# alpha\n")
    write(hermes / "skills/alpha/scripts/run.py", b"print('alpha')\n")
    write(hermes / "skills/beta/SKILL.md", b"# beta\n")
    write(hermes / "hermes-agent/pyproject.toml", b"[project]\nname = \"hermes-agent\"\n")
    write(hermes / "hermes-agent/uv.lock", b"lock fixture\n")
    write(hermes / "hermes-agent/package-lock.json", json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "hermes-agent", "version": "1.0.0"},
            "node_modules/js-yaml": {"version": "4.3.2"},
            "node_modules/@x/y": {"name": "@x/y", "version": "1.0.0"},
            "node_modules/workspace-link": {"link": True},
        },
    }, indent=2).encode() + b"\n")
    write(hermes / "hermes-agent/apps/desktop/dist-webapp/hermes-build.json", json.dumps({
        "schemaVersion": 1,
        "pinnedCommit": COMMIT,
        "completedAt": "2026-11-08T00:00:00.000Z",
        "node": "26.12.1",
        "inputs": {"sourceHash": "src" + "0" * 61},
        "outputHash": "out" + "0" * 61,
    }, indent=2).encode() + b"\n")
    return root


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_env() -> dict[str, str]:
    """A child git must not inherit the outer repo's index or work tree.

    `make check` runs from the pre-commit hook, where `GIT_INDEX_FILE` points
    at the repository being committed; a fixture repo inheriting it fails to
    commit. The same scrub protects `manifest.py` when an operator runs it from
    a git hook or a script with GIT_* exported.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, env=git_env())


def init_fixture_repo(checkout: Path) -> str:
    run_git("init", "-q", str(checkout))
    run_git("-C", str(checkout), "add", "-A")
    run_git("-C", str(checkout), "-c", "user.email=t@example.invalid",
            "-c", "user.name=test", "commit", "-q", "-m", "fixture")
    return run_git("-C", str(checkout), "rev-parse", "HEAD").stdout.strip()


class ManifestBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="odl214-build-"))
        self.root = make_root(self.tmp)
        self.module = load_manifest_script()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, root: Path | None = None, **kwargs):
        root = root or self.root
        defaults = dict(
            installer=root / "bootstrap/odile_install.py",
            os_versions=dict(FIXED_VERSIONS),
            now=FIXED_NOW,
        )
        defaults.update(kwargs)
        return self.module.build_manifest(root, **defaults)

    def test_build_records_every_pinned_fact(self):
        manifest = self.build()
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["generated_at"], FIXED_NOW)
        self.assertEqual(manifest["mode"], "scratch")
        self.assertEqual(manifest["model"], MODEL)
        self.assertEqual(manifest["installer_sha256"],
                         sha256((self.root / "bootstrap/odile_install.py").read_bytes()))
        self.assertNotIn("meter_sha256", manifest)
        self.assertIsNone(manifest["audit_sha256"])
        self.assertEqual(manifest["hermes"]["commit"], COMMIT)
        self.assertEqual(manifest["hermes"]["commit_source"], "build-metadata")
        self.assertEqual(manifest["hermes"]["install_script_sha256"], FIXED_INSTALL_PIN)
        self.assertEqual(manifest["hermes"]["lockfiles"]["uv.lock"],
                         sha256((self.root / "home/tester/.hermes/hermes-agent/uv.lock").read_bytes()))
        self.assertEqual(manifest["hermes"]["python_packages"],
                         ["httpx==0.28.1", "openai==2.54.0"])
        self.assertEqual(manifest["hermes"]["node_packages"],
                         ["@x/y==1.0.0", "js-yaml==4.3.2"])
        self.assertEqual(manifest["hermes"]["webapp"]["source_sha256"], "src" + "0" * 61)
        self.assertEqual(manifest["hermes"]["webapp"]["output_sha256"], "out" + "0" * 61)
        self.assertEqual(sorted(manifest["skills"]), ["alpha", "beta", "odile-home"])
        self.assertEqual(manifest["skills_bundle_sha256"],
                         sha256(b"bundle fixture\n"))
        self.assertEqual(manifest["packages"]["source"], "file")
        self.assertEqual(manifest["packages"]["os"], FIXED_VERSIONS)
        self.assertEqual(manifest["packages"]["missing"], [])
        self.assertEqual(manifest["packages"]["toolchain"]["node"]["version"], "26.12.1")

    def test_machine_facts_do_not_move_a_skill_hash(self):
        other = make_root(self.tmp, "root-b", user="someoneelse")
        first, second = self.build(), self.build(root=other)
        self.assertEqual(first["skills"]["odile-home"], second["skills"]["odile-home"])
        self.assertNotEqual(
            (self.root / "home/tester/.hermes/skills/odile-home/SKILL.md").read_bytes(),
            (other / "home/someoneelse/.hermes/skills/odile-home/SKILL.md").read_bytes(),
        )

    def test_the_seed_key_never_reaches_the_manifest(self):
        rendered = self.module.render(self.build()).decode("utf-8")
        self.assertNotIn("near-secret", rendered)
        self.assertNotIn("near_api_key", rendered)

    def test_a_changed_install_script_pin_changes_the_manifest(self):
        other = make_root(self.tmp, "root-b", install_pin=OTHER_INSTALL_PIN)
        before, after = self.build(), self.build(root=other)
        self.assertEqual(before["hermes"]["install_script_sha256"], FIXED_INSTALL_PIN)
        self.assertEqual(after["hermes"]["install_script_sha256"], OTHER_INSTALL_PIN)
        same, detail = self.module.diff_manifests(before, after)
        self.assertFalse(same)
        self.assertIn("install_script_sha256", detail)

    def test_an_installer_without_the_install_script_pin_is_refused(self):
        bare = write(self.tmp / "bare/odile_install.py",
                     b'#!/usr/bin/python3\nimport json\nprint(json.dumps({"name": "odile_install"}))\n',
                     0o755)
        with self.assertRaises(self.module.ManifestError) as raised:
            self.build(installer=bare)
        self.assertIn("hermes_install_sha256", str(raised.exception))

    def test_an_install_script_pin_that_is_not_a_sha256_is_refused(self):
        bad = make_root(self.tmp, "bad-pin", install_pin="abc")
        with self.assertRaises(self.module.ManifestError) as raised:
            self.build(root=bad)
        self.assertIn("hermes_install_sha256", str(raised.exception))

    def test_build_records_the_tenant_backup_program(self):
        manifest = self.build()
        installed = self.root / "usr/local/lib/odile/backup.py"
        self.assertEqual(manifest["backup_sha256"], sha256(installed.read_bytes()))
        self.assertEqual(manifest["backup_sha256"], FIXED_BACKUP_PIN)

    def test_a_changed_backup_program_moves_the_manifest_field(self):
        before = self.build()
        installed = self.root / "usr/local/lib/odile/backup.py"
        installed.write_bytes(installed.read_bytes() + b"# one byte more\n")
        after = self.build()
        self.assertEqual(before["backup_sha256"], FIXED_BACKUP_PIN)
        self.assertNotEqual(after["backup_sha256"], before["backup_sha256"])
        same, detail = self.module.diff_manifests(before, after)
        self.assertFalse(same)
        self.assertIn("backup_sha256", detail)

    def test_a_scratch_root_without_the_program_uses_the_installers_pin(self):
        (self.root / "usr/local/lib/odile/backup.py").unlink()
        manifest = self.build()
        self.assertEqual(manifest["backup_sha256"], FIXED_BACKUP_PIN)

    def test_a_tenant_root_without_the_program_is_refused(self):
        (self.root / "usr/local/lib/odile/backup.py").unlink()
        with self.assertRaises(self.module.ManifestError) as raised:
            self.module.collect_backup_hash(
                {"root": self.root, "mode": "tenant"},
                {"backup_sha256": FIXED_BACKUP_PIN})
        self.assertIn("backup.py", str(raised.exception))

    def test_a_scratch_root_without_program_or_pin_is_refused(self):
        (self.root / "usr/local/lib/odile/backup.py").unlink()
        bare = write(self.tmp / "bare2/odile_install.py",
                     ('#!/usr/bin/python3\nimport json\n'
                      f'print(json.dumps({{"hermes_install_sha256": "{FIXED_INSTALL_PIN}"}}))\n'
                      ).encode(), 0o755)
        with self.assertRaises(self.module.ManifestError) as raised:
            self.build(installer=bare)
        self.assertIn("backup_sha256", str(raised.exception))

    def test_two_roots_produce_the_same_manifest_except_the_timestamp(self):
        other = make_root(self.tmp, "root-b", user="otheruser")
        first = self.build(now=FIXED_NOW)
        second = self.build(root=other, now=LATER)
        self.assertNotEqual(self.module.render(first), self.module.render(second))
        same, detail = self.module.diff_manifests(first, second)
        self.assertTrue(same, detail)
        left, right = dict(first), dict(second)
        left.pop("generated_at")
        right.pop("generated_at")
        self.assertEqual(left, right)

    def test_a_changed_skill_changes_the_manifest(self):
        before = self.build()
        path = self.root / "home/tester/.hermes/skills/alpha/scripts/run.py"
        path.write_bytes(b"print('changed')\n")
        after = self.build()
        self.assertNotEqual(before["skills"]["alpha"], after["skills"]["alpha"])
        self.assertEqual(before["skills"]["beta"], after["skills"]["beta"])
        same, _detail = self.module.diff_manifests(before, after)
        self.assertFalse(same)

    def test_pycache_is_not_part_of_a_skill_hash(self):
        before = self.build()
        write(self.root / "home/tester/.hermes/skills/alpha/__pycache__/run.cpython-314.pyc", b"cache")
        after = self.build()
        self.assertEqual(before["skills"]["alpha"], after["skills"]["alpha"])

    def test_a_missing_installer_is_refused(self):
        with self.assertRaises(self.module.ManifestError) as raised:
            self.build(installer=self.tmp / "nope/odile_install.py")
        self.assertIn("installer", str(raised.exception))

    def test_a_missing_python_environment_is_refused(self):
        shutil.rmtree(self.root / "home/tester/.hermes/installs")
        with self.assertRaises(self.module.ManifestError) as raised:
            self.build()
        self.assertIn("Python environment", str(raised.exception))

    def test_a_bundle_from_another_commit_is_refused(self):
        checkout = self.root / "home/tester/.hermes/hermes-agent"
        run_git("init", "-q", str(checkout))
        run_git("-C", str(checkout), "add", "-A")
        run_git("-C", str(checkout), "-c", "user.email=t@example.invalid",
                "-c", "user.name=test", "commit", "-q", "-m", "fixture")
        build = checkout / "apps/desktop/dist-webapp/hermes-build.json"
        data = json.loads(build.read_text(encoding="utf-8"))
        data["pinnedCommit"] = "0" * 40
        build.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(self.module.ManifestError) as raised:
            self.build()
        self.assertIn("bundle", str(raised.exception).lower())

    def test_git_is_the_commit_source_when_a_checkout_exists(self):
        checkout = self.root / "home/tester/.hermes/hermes-agent"
        commit = init_fixture_repo(checkout)
        build = checkout / "apps/desktop/dist-webapp/hermes-build.json"
        data = json.loads(build.read_text(encoding="utf-8"))
        data["pinnedCommit"] = commit
        build.write_text(json.dumps(data), encoding="utf-8")
        manifest = self.build()
        self.assertEqual(manifest["hermes"]["commit"], commit)
        self.assertEqual(manifest["hermes"]["commit_source"], "git")

    def test_the_audit_script_hash_is_recorded_when_it_exists(self):
        audit = write(self.tmp / "audit.py", b"# handoff audit\n")
        manifest = self.build(audit=audit)
        self.assertEqual(manifest["audit_sha256"], sha256(b"# handoff audit\n"))

    def test_the_model_can_be_overridden(self):
        manifest = self.build(model="near/model-test")
        self.assertEqual(manifest["model"], "near/model-test")

    def test_dpkg_mode_refuses_a_missing_package(self):
        def runner(command):
            if command[-1] == "restic":
                return 1, "", "no packages found matching restic"
            return 0, "1.2.3\n", ""
        with self.assertRaises(self.module.ManifestError) as raised:
            self.module.collect_os_versions(mode="tenant", root=Path("/"), runner=runner)
        self.assertIn("restic", str(raised.exception))

    def test_dpkg_mode_records_the_installed_versions(self):
        def runner(command):
            return 0, "1:2.47.3-1\n", ""
        versions = self.module.collect_os_versions(mode="tenant", root=Path("/"), runner=runner)
        self.assertEqual(versions["source"], "dpkg")
        self.assertEqual(versions["os"]["git"], "1:2.47.3-1")
        self.assertEqual(versions["missing"], [])

    def test_probe_mode_records_a_version_from_the_binary_output(self):
        def runner(command):
            return 0, "restic 0.18.0 compiled with go1.24.4 on linux/amd64\n", ""
        versions = self.module.collect_os_versions(mode="scratch", root=self.root, runner=runner)
        self.assertEqual(versions["os"]["restic"], "0.18.0")
        self.assertEqual(versions["source"], "probe")


class ShippedInstallerTests(unittest.TestCase):
    """The installer and the builder must name the same install script."""

    def test_the_shipped_installer_records_the_pin_the_builder_reads(self):
        module = load_manifest_script()
        record = module.read_installer_manifest(ROOT / "installer" / "odile_install.py")
        self.assertRegex(record["hermes_install_sha256"], r"^[0-9a-f]{64}$")

    def test_the_shipped_installer_pins_the_backup_program_in_the_checkout(self):
        """The embedded pin and `scripts/backup.py` are the same bytes.

        `--self-test` compares them too; this is the row the release manifest
        records, so a release built from a root names the program a stranger
        can read here.
        """
        module = load_manifest_script()
        record = module.read_installer_manifest(ROOT / "installer" / "odile_install.py")
        self.assertRegex(record["backup_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(record["backup_sha256"],
                         sha256((ROOT / "scripts" / "backup.py").read_bytes()))


class SignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="odl214-key-"))
        cls.key = cls.tmp / "signing.pem"
        cls.pub = cls.tmp / "signing.pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(cls.key)],
                       check=True, capture_output=True)
        os.chmod(cls.key, 0o600)
        subprocess.run(["openssl", "pkey", "-in", str(cls.key), "-pubout", "-out", str(cls.pub)],
                       check=True, capture_output=True)
        cls.module = load_manifest_script()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_signature_roundtrip_and_one_byte_change(self):
        data = b'{"schema": 1, "generated_at": "2026-11-08T00:00:00Z"}\n'
        signature = self.module.sign_bytes(data, self.key)
        self.assertEqual(len(signature), 64)
        self.assertTrue(self.module.verify_bytes(data, signature, self.pub))
        changed = data.replace(b"1", b"2", 1)
        self.assertFalse(self.module.verify_bytes(changed, signature, self.pub))

    def test_a_wrong_key_does_not_verify(self):
        other = self.tmp / "other.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(other)],
                       check=True, capture_output=True)
        signature = self.module.sign_bytes(b"body\n", self.key)
        self.assertFalse(self.module.verify_bytes(b"body\n", signature, other))

    def test_a_signature_file_is_base64_and_read_back(self):
        signature = self.module.sign_bytes(b"body\n", self.key)
        path = self.tmp / "body.sig"
        self.module.write_signature(path, signature)
        text = path.read_text(encoding="utf-8").strip()
        self.assertEqual(base64.b64decode(text), signature)
        self.assertEqual(self.module.read_signature(path), signature)

    def test_a_world_readable_private_key_is_refused(self):
        loose = self.tmp / "loose.pem"
        shutil.copy2(self.key, loose)
        os.chmod(loose, 0o644)
        with self.assertRaises(self.module.ManifestError) as raised:
            self.module.sign_bytes(b"body\n", loose)
        self.assertIn("0600", str(raised.exception))


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="odl214-cli-"))
        cls.key = cls.tmp / "signing.pem"
        cls.pub = cls.tmp / "signing.pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(cls.key)],
                       check=True, capture_output=True)
        os.chmod(cls.key, 0o600)
        subprocess.run(["openssl", "pkey", "-in", str(cls.key), "-pubout", "-out", str(cls.pub)],
                       check=True, capture_output=True)
        cls.versions = cls.tmp / "versions.json"
        cls.versions.write_text(json.dumps(FIXED_VERSIONS), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.tmp, prefix="case-"))
        self.root = make_root(self.work)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, cwd=str(ROOT),
        )

    def build(self, out: str, *extra: str, root: Path | None = None) -> subprocess.CompletedProcess[str]:
        root = root or self.root
        return self.run_cli(
            "build", "--root", str(root),
            "--installer", str(root / "bootstrap/odile_install.py"),
            "--packages-json", str(self.versions),
            "--generated-at", FIXED_NOW, "--out", out, *extra,
        )

    def test_build_sign_verify_roundtrip(self):
        manifest = self.work / "a.json"
        built = self.build(str(manifest))
        self.assertEqual(built.returncode, 0, built.stderr)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["installer_sha256"], sha256(
            (self.root / "bootstrap/odile_install.py").read_bytes()))

        signature = self.work / "a.json.sig"
        signed = self.run_cli("sign", "--manifest", str(manifest), "--key", str(self.key),
                              "--out", str(signature))
        self.assertEqual(signed.returncode, 0, signed.stderr)

        verified = self.run_cli("verify", "--manifest", str(manifest),
                                "--signature", str(signature), "--pubkey", str(self.pub))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("signature: OK", verified.stdout)

    def test_a_changed_file_breaks_the_signature_check(self):
        manifest = self.work / "a.json"
        self.assertEqual(self.build(str(manifest)).returncode, 0)
        signature = self.work / "a.json.sig"
        self.assertEqual(self.run_cli("sign", "--manifest", str(manifest), "--key", str(self.key),
                                      "--out", str(signature)).returncode, 0)
        # The app layer changes; the signed manifest now describes the old tree.
        (self.root / "home/tester/.hermes/skills/beta/SKILL.md").write_bytes(b"# beta changed\n")
        rebuilt = self.work / "b.json"
        self.assertEqual(self.build(str(rebuilt)).returncode, 0)
        refused = self.run_cli("verify", "--manifest", str(rebuilt),
                               "--signature", str(signature), "--pubkey", str(self.pub))
        self.assertEqual(refused.returncode, 3)
        self.assertIn("signature: BAD", refused.stdout)

        # A byte flipped inside the signed bytes is caught the same way.
        tampered = self.work / "tampered.json"
        tampered.write_bytes(manifest.read_bytes() + b" ")
        flipped = self.run_cli("verify", "--manifest", str(tampered),
                               "--signature", str(signature), "--pubkey", str(self.pub))
        self.assertEqual(flipped.returncode, 3)

    def test_two_fresh_roots_diff_empty_except_the_timestamp(self):
        other = make_root(self.work, "root-b")
        first = self.work / "a.json"
        second = self.work / "b.json"
        self.assertEqual(self.build(str(first)).returncode, 0)
        self.assertEqual(self.build(str(second), "--generated-at", LATER, root=other).returncode, 0)
        same = self.run_cli("diff", str(first), str(second))
        self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
        self.assertIn("same (except generated_at)", same.stdout)

    def test_diff_reports_a_changed_package(self):
        first = self.work / "a.json"
        second = self.work / "b.json"
        self.assertEqual(self.build(str(first)).returncode, 0)
        other_versions = dict(FIXED_VERSIONS, caddy="2.10.0")
        path = self.work / "versions-b.json"
        path.write_text(json.dumps(other_versions), encoding="utf-8")
        self.assertEqual(self.build(str(second), "--packages-json", str(path)).returncode, 0)
        different = self.run_cli("diff", str(first), str(second))
        self.assertEqual(different.returncode, 1)
        self.assertIn("caddy", different.stdout)

    def test_a_changed_install_script_pin_fails_the_diff_and_the_signature(self):
        first = self.work / "a.json"
        self.assertEqual(self.build(str(first)).returncode, 0)
        signature = self.work / "a.json.sig"
        self.assertEqual(self.run_cli("sign", "--manifest", str(first), "--key", str(self.key),
                                      "--out", str(signature)).returncode, 0)
        other = make_root(self.work, "root-b", install_pin=OTHER_INSTALL_PIN)
        rebuilt = self.work / "b.json"
        self.assertEqual(self.build(str(rebuilt), root=other).returncode, 0)
        different = self.run_cli("diff", str(first), str(rebuilt))
        self.assertEqual(different.returncode, 1)
        self.assertIn("install_script_sha256", different.stdout)
        refused = self.run_cli("verify", "--manifest", str(rebuilt),
                               "--signature", str(signature), "--pubkey", str(self.pub))
        self.assertEqual(refused.returncode, 3)
        self.assertIn("signature: BAD", refused.stdout)

    def test_a_missing_installer_exits_2(self):
        failed = self.run_cli(
            "build", "--root", str(self.root), "--installer", str(self.work / "missing.py"),
            "--packages-json", str(self.versions), "--out", str(self.work / "x.json"),
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error:", failed.stderr)
        self.assertFalse((self.work / "x.json").exists())


class ServeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="odl214-serve-"))
        cls.key = cls.tmp / "signing.pem"
        cls.pub = cls.tmp / "signing.pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(cls.key)],
                       check=True, capture_output=True)
        os.chmod(cls.key, 0o600)
        subprocess.run(["openssl", "pkey", "-in", str(cls.key), "-pubout", "-out", str(cls.pub)],
                       check=True, capture_output=True)
        cls.manifest = write(cls.tmp / "manifest.json", b'{"schema": 1}\n')
        module = load_manifest_script()
        cls.signature = write(
            cls.tmp / "manifest.json.sig",
            base64.b64encode(module.sign_bytes(cls.manifest.read_bytes(), cls.key)) + b"\n",
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.module = load_manifest_script()
        self.server = self.module.make_server(self.manifest, self.signature, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_manifest_is_served_with_cache_control_no_store(self):
        status, headers, body = self.get("/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.manifest.read_bytes())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_the_served_bytes_are_the_signed_bytes(self):
        _status, _headers, body = self.get("/manifest")
        # The server must not re-serialise: the body verifies against the
        # signature over the file, byte for byte.
        self.assertEqual(body, self.manifest.read_bytes())
        self.assertTrue(self.module.verify_bytes(
            body, self.module.read_signature(self.signature), self.pub))

    def test_signature_is_served_with_no_store(self):
        status, headers, body = self.get("/manifest.sig")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.signature.read_bytes())
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_health_and_unknown_paths(self):
        status, _headers, body = self.get("/health")
        self.assertEqual((status, body), (200, b"ok\n"))
        status, _headers, _body = self.get("/nope")
        self.assertEqual(status, 404)

    def test_head_sends_no_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("HEAD", "/manifest")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
