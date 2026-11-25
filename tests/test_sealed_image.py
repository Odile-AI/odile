#!/usr/bin/python3
"""Tests for the sealed image bench (ODL-454).

Two layers, both fast and hermetic:

* the C stand-in for Servury's snpreport is compiled once and exercised
  against Python's hashlib/hmac and against the spike's reference decoder.
  This is the piece the boot chain actually calls, so it is tested by running
  it, not by reading its source;
* the harness that decides whether a boot unsealed or failed closed is driven
  from a written serial file, the same way the host reads a real QEMU serial
  log.

The QEMU, debootstrap and LUKS runs are the card's evidence, not this suite.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.client
import http.server
import importlib.util
import io
import json
import os
import pathlib
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "image"))

import bench  # noqa: E402

SOURCE = ROOT / "deploy" / "image" / "bench-snpreport.c"
SEAL_REF = ROOT / "spike" / "sealed-unlock" / "seal_ref.py"
PIN = ROOT / "deploy" / "image" / "tenant-image.pin"
HOOK = ROOT / "spike" / "sealed-unlock" / "initramfs" / "bb-sealed-unlock"
# The spike tree (the sealed-unlock hook, its stub harness and the reference
# decoder) is HQ-only: `/spike/` is in .git/info/exclude and is not part of the
# published tree. Tests that drive those bytes skip here, with this reason,
# instead of failing a public checkout's `make check`; at HQ they run.
SPIKE_MISSING = (
    "spike/sealed-unlock is HQ-only (excluded in .git/info/exclude) and absent "
    "from this checkout; the hook, its stub harness and the reference decoder "
    "are exercised at HQ"
)
# `sealed-initrd.sh` reads the same HQ-only hook before its dry-run branch and
# before its `--source` check, so a public checkout cannot run either of the
# two script tests below; they run at HQ.
INITRD_HOOK_MISSING = (
    "sealed-initrd.sh reads the HQ-only hook "
    "spike/sealed-unlock/initramfs/bb-sealed-unlock before it plans or checks "
    "--source, and that file is absent from this checkout; the script tests are "
    "exercised at HQ"
)
SEED = b"odile-bench-vcek-NOT-a-secret-32"
IMAGE = ROOT / "deploy" / "image"


def console_lines(output: str) -> list[str]:
    """The console transcript's lines (ODL-523).

    The unit prints exactly one `odile-firstboot:` line. A boot that runs the
    first-boot steps prints one `odile-hostkeys:` line in front of it — the key
    this boot generated or kept — and nothing else reaches the console.
    """
    return [line for line in output.splitlines() if line.strip()]


def firstboot_line(output: str) -> str:
    """The one `odile-firstboot:` line, with the console contract asserted."""
    lines = console_lines(output)
    boot = [line for line in lines if line.startswith("odile-firstboot:")]
    host = [line for line in lines if line.startswith("odile-hostkeys:")]
    assert len(lines) == 2 and len(boot) == 1 and len(host) == 1, output
    assert lines[0] == host[0], output
    return boot[0]


# A real ed25519 public key line; the measured `bb.key=` is its base64.
BOOT_KEY_TEXT = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICFh7hQkST5vRDolO3yqwCARRugZahMdc+WgR69JsoXG "
    "odile-test throwaway boot key"
)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves a class's module through sys.modules, so a module
    # loaded by path must be registered before it is executed (the release
    # installer uses @dataclass at import time).
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


firstboot = _load("odile_firstboot", IMAGE / "odile-firstboot.py")
fixture = _load("odile_firstboot_fixture", IMAGE / "firstboot-fixture.py")

MANIFEST_CHECKER = IMAGE / "odile-manifest.py"


def ed25519_key(base: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A throwaway Ed25519 pair in the release's file shapes (PEM + SPKI)."""
    if not shutil.which("openssl"):
        raise unittest.SkipTest("openssl is missing")
    priv = base.with_suffix(".pem")
    pub = base.with_suffix(".pub")
    priv.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(priv)],
                   check=True, capture_output=True, timeout=60)
    os.chmod(priv, 0o600)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True, timeout=60)
    return priv, pub


def sign_release_file(path: pathlib.Path, key: pathlib.Path) -> pathlib.Path:
    """Sign `path` the way `manifest.py sign` writes a release: base64, 76 cols."""
    result = subprocess.run(["openssl", "dgst", "-sign", str(key), str(path)],
                            check=True, capture_output=True, timeout=60)
    signature = path.parent / (path.name + ".sig")
    signature.write_bytes(base64.encodebytes(result.stdout))
    return signature


RELEASE_INSTALLER = b"#!/usr/bin/python3\nprint('the release installer')\n"
RELEASE_LISTENER = b"#!/usr/bin/python3\nprint('the release listener')\n"


def release_fixture(directory: pathlib.Path, key: pathlib.Path, *,
                    installer: bytes = RELEASE_INSTALLER) -> pathlib.Path:
    """The release/ directory shape: installer, signed manifest pair, listener.

    The manifest names the installer by hash, which is the relation the first
    boot's placement and CLAIMS 4.6 depend on.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "odile_install.py").write_bytes(installer)
    document = {
        "schema": 1,
        "mode": "scratch",
        "installer_sha256": hashlib.sha256(installer).hexdigest(),
        "generated_at": "2026-11-11T00:00:00Z",
    }
    manifest = directory / "manifest.json"
    manifest.write_bytes((json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
    sign_release_file(manifest, key)
    (directory / "manifest.py").write_bytes(RELEASE_LISTENER)
    (directory / "odile-manifest.py").write_bytes(MANIFEST_CHECKER.read_bytes())
    (directory / "odile-manifest.service").write_bytes(
        (IMAGE / "odile-manifest.service").read_bytes())
    return directory


def expected_key(cmdline: str) -> bytes:
    return hmac.new(SEED, b"odile-bench-snp-v1\n" + cmdline.encode(), hashlib.sha256).digest()


class SnpreportTests(unittest.TestCase):
    """The stand-in, run as a binary: key derivation, format, exit codes."""

    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("gcc"):
            raise unittest.SkipTest("gcc is not installed; the image build needs it")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = pathlib.Path(cls.tmp.name)
        cls.bin = cls.dir / "snpreport"
        subprocess.run(
            ["gcc", "-O2", "-static", "-s", "-o", str(cls.bin), str(SOURCE)],
            check=True, timeout=120,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_tool(self, *args: str, cmdline: str | None = None) -> subprocess.CompletedProcess:
        command = [str(self.bin), *args]
        if cmdline is not None:
            path = self.dir / "cmdline"
            path.write_text(cmdline + "\n")
            command += ["--cmdline-file", str(path)]
        return subprocess.run(command, capture_output=True, text=True, timeout=60)

    def test_selftest_passes(self) -> None:
        result = self.run_tool("--selftest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("selftest ok", result.stdout)

    def test_fingerprint_matches_hashlib(self) -> None:
        data = self.dir / "abc"
        data.write_bytes(b"abc")
        result = self.run_tool("--fp", str(data))
        self.assertEqual(result.returncode, 0, result.stderr)
        import base64
        want = "SHA256:" + base64.b64encode(hashlib.sha256(b"abc").digest()).decode().rstrip("=")
        self.assertEqual(result.stdout.strip(), want)

    def test_the_key_is_hmac_over_the_measured_cmdline(self) -> None:
        result = self.run_tool("--key-hex", cmdline="root=/dev/mapper/root_crypt seal_id=bench")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), expected_key("root=/dev/mapper/root_crypt seal_id=bench").hex())

    def test_a_different_seal_id_derives_a_different_key(self) -> None:
        one = self.run_tool("--key-hex", cmdline="root=/dev/x seal_id=tenant-one")
        two = self.run_tool("--key-hex", cmdline="root=/dev/x seal_id=tenant-two")
        self.assertNotEqual(one.stdout, two.stdout)
        self.assertEqual(len(one.stdout.strip()), 64)

    def test_round_trip_and_fail_closed(self) -> None:
        plain = self.dir / "vol.key"
        plain.write_bytes(bytes(range(64)))
        blob = self.dir / "vol.blob"
        out = self.dir / "out.key"
        sealed = self.run_tool("--seal", str(plain), str(blob), cmdline="bb.host=one seal_id=a")
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        opened = self.run_tool("--unseal", str(blob), str(out), cmdline="bb.host=one seal_id=a")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertEqual(out.read_bytes(), plain.read_bytes())

        # The changed command line is the card's fail-closed case: rc 6, the
        # code Servury's snpreport returns and the hook treats as "leave the
        # prompt".
        changed = self.run_tool("--unseal", str(blob), str(out), cmdline="bb.host=one seal_id=b")
        self.assertEqual(changed.returncode, 6, changed.stderr)

    def test_a_tampered_blob_returns_6(self) -> None:
        plain = self.dir / "tamper.key"
        plain.write_bytes(b"x" * 64)
        blob = self.dir / "tamper.blob"
        self.run_tool("--seal", str(plain), str(blob), cmdline="seal_id=t")
        data = bytearray(blob.read_bytes())
        data[-1] ^= 1
        blob.write_bytes(bytes(data))
        result = self.run_tool("--unseal", str(blob), str(self.dir / "nope"), cmdline="seal_id=t")
        self.assertEqual(result.returncode, 6, result.stderr)

    @unittest.skipUnless(SEAL_REF.is_file(), SPIKE_MISSING)
    def test_the_spike_reference_unseals_our_blob(self) -> None:
        plain = self.dir / "ref.key"
        plain.write_bytes(bytes(range(64)))
        blob = self.dir / "ref.blob"
        cmdline = "root=/dev/mapper/root_crypt seal_id=ref"
        self.run_tool("--seal", str(plain), str(blob), cmdline=cmdline)
        out = self.dir / "ref.out"
        result = subprocess.run(
            [sys.executable, str(SEAL_REF), expected_key(cmdline).hex(), "unseal", str(blob), str(out)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(out.read_bytes(), plain.read_bytes())

    @unittest.skipUnless(SEAL_REF.is_file(), SPIKE_MISSING)
    def test_we_unseal_the_spike_reference_blob(self) -> None:
        plain = self.dir / "back.key"
        plain.write_bytes(bytes(range(64)))
        blob = self.dir / "back.blob"
        cmdline = "root=/dev/mapper/root_crypt seal_id=back"
        result = subprocess.run(
            [sys.executable, str(SEAL_REF), expected_key(cmdline).hex(), "seal", str(plain), str(blob)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = self.dir / "back.out"
        opened = self.run_tool("--unseal", str(blob), str(out), cmdline=cmdline)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertEqual(out.read_bytes(), plain.read_bytes())


class SealedHarnessTests(unittest.TestCase):
    """The boot-side decision: opened, prompt, and the extra evidence line."""

    def wait(self, text: str, expect: str, timeout: float = 5) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as tmp:
            serial = pathlib.Path(tmp) / "boot.log"
            serial.write_text(text)
            return bench.wait_sealed(serial, expect, timeout)

    def test_an_opened_boot_is_a_pass(self) -> None:
        ok, detail = self.wait(f"...\n{bench.SEALED_OPENED}\n", "opened")
        self.assertTrue(ok, detail)

    def test_a_prompt_is_the_documented_fail_closed_state(self) -> None:
        ok, detail = self.wait(
            f"bb-sealed-unlock: the blob is not sealed to this boot (unseal rc 6); leaving cryptroot's prompt\n"
            f"{bench.CRYPTROOT_PROMPT} \n",
            "prompt",
        )
        self.assertTrue(ok, detail)
        self.assertIn("failed the seal", detail)

    def test_a_prompt_without_the_hook_failure_is_not_a_fail_closed_pass(self) -> None:
        # The stock image also prompts; only the hook's own rc line proves the
        # prompt is the documented fail-closed state rather than a missing hook.
        ok, detail = self.wait(f"{bench.CRYPTROOT_PROMPT} \n", "prompt")
        self.assertFalse(ok, detail)
        self.assertIn("never reported a seal failure", detail)

    def test_a_prompt_after_an_open_is_not_a_fail_closed_pass(self) -> None:
        ok, detail = self.wait(f"{bench.SEALED_OPENED}\n{bench.CRYPTROOT_PROMPT}\n", "prompt")
        self.assertFalse(ok, detail)

    def test_a_missing_line_fails(self) -> None:
        ok, detail = self.wait("nothing here\n", "opened", timeout=0.1)
        self.assertFalse(ok)
        self.assertIn("no '", detail)

    def test_the_guest_sealed_line_is_parsed_and_is_not_a_sixth_check(self) -> None:
        text = "\n".join(
            f"ODILE-BENCH {name} PASS detail" for name in bench.CHECK_NAMES
        ) + "\nODILE-BENCH sealed-unlock PASS state opened; root_crypt active\n"
        checks, _ = bench.parse_guest(text)
        self.assertEqual(set(checks), set(bench.CHECK_NAMES))
        sealed = bench.parse_guest_sealed(text)
        self.assertIsNotNone(sealed)
        assert sealed is not None
        self.assertTrue(sealed.ok)

    def test_a_plain_image_has_no_sealed_check(self) -> None:
        with mock.patch.object(bench, "SEALED_STATE", pathlib.Path("/nonexistent/state")), \
                mock.patch.object(bench, "root_mapper_source", return_value="/dev/sda1"):
            self.assertIsNone(bench.check_sealed_unlock())

    def test_the_root_mount_is_read_from_proc_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mounts = pathlib.Path(tmp) / "mounts"
            mounts.write_text(
                "/dev/sda1 /boot ext4 rw 0 0\n"
                "/dev/mapper/root_crypt / ext4 rw,relatime 0 0\n"
                "proc /proc proc rw 0 0\n"
            )
            self.assertEqual(bench.root_mapper_source(mounts), "/dev/mapper/root_crypt")

    def test_a_missing_mounts_file_has_no_source(self) -> None:
        self.assertEqual(bench.root_mapper_source(pathlib.Path("/nonexistent/mounts")), "")

    @staticmethod
    def mapper_tree(tmp: str, dm_name: str) -> pathlib.Path:
        root = pathlib.Path(tmp)
        (root / "dev" / "mapper").mkdir(parents=True)
        (root / "dev" / "dm-0").write_text("")
        (root / "dev" / "mapper" / "root_crypt").symlink_to("../dm-0")
        (root / "sys" / "class" / "block" / "dm-0" / "dm").mkdir(parents=True)
        (root / "sys" / "class" / "block" / "dm-0" / "dm" / "name").write_text(dm_name + "\n")
        return root

    def test_the_mapper_name_comes_from_sysfs_not_the_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(bench.mapper_is_active("/dev/mapper/root_crypt", root=self.mapper_tree(tmp, "root_crypt")))
        with tempfile.TemporaryDirectory() as tmp:
            root = self.mapper_tree(tmp, "some_other_name")
            self.assertFalse(
                bench.mapper_is_active("/dev/mapper/root_crypt", root=root),
                "a symlink called root_crypt must not pass when the kernel's dm name differs",
            )

    def test_a_non_device_source_is_not_a_mapper(self) -> None:
        self.assertFalse(bench.mapper_is_active("overlay"))
        self.assertFalse(bench.mapper_is_active(""))

    def test_the_guest_check_names_the_root_device_without_cryptsetup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "state"
            state.write_text("opened\n")
            with mock.patch.object(bench, "SEALED_STATE", state), \
                    mock.patch.object(bench, "root_mapper_source", return_value="/dev/mapper/root_crypt"), \
                    mock.patch.object(bench, "mapper_is_active", return_value=True), \
                    mock.patch.object(bench.shutil, "which", return_value=None):
                check = bench.check_sealed_unlock()
        assert check is not None
        self.assertTrue(check.ok, check.detail)
        self.assertIn("/dev/mapper/root_crypt", check.detail)
        self.assertIn("dm name root_crypt", check.detail)

    def test_an_unsealed_state_fails_even_with_the_mapper_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "state"
            state.write_text("unseal-failed rc=6\n")
            with mock.patch.object(bench, "SEALED_STATE", state), \
                    mock.patch.object(bench, "root_mapper_source", return_value="/dev/mapper/other"), \
                    mock.patch.object(bench, "mapper_is_active", return_value=True):
                check = bench.check_sealed_unlock()
        assert check is not None
        self.assertFalse(check.ok)
        self.assertIn("unseal-failed rc=6", check.detail)

    def test_the_uki_boot_uses_ovmf_kernel_and_a_scsi_disk(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_popen(command, **kwargs):  # noqa: ANN001 - subprocess API
            captured["command"] = command
            return mock.MagicMock()

        with mock.patch.object(bench.subprocess, "Popen", side_effect=fake_popen):
            bench.boot_qemu_uki(
                pathlib.Path("/var/tmp/x.raw"), pathlib.Path("/var/tmp/x.efi"), pathlib.Path("/var/tmp/OVMF.fd"),
                pathlib.Path("/var/tmp/boot.log"), pathlib.Path("/var/tmp/bench.log"), 19132, 2048, 2,
            )
        command = captured["command"]
        self.assertEqual(command[command.index("-bios") + 1], "/var/tmp/OVMF.fd")
        self.assertEqual(command[command.index("-kernel") + 1], "/var/tmp/x.efi")
        self.assertIn("virtio-scsi-pci,id=scsi0", command)
        self.assertIn("scsi-hd,drive=disk0,bus=scsi0.0", command)
        serials = [command[i + 1] for i, value in enumerate(command) if value == "-serial"]
        self.assertEqual(serials, ["file:/var/tmp/boot.log", "file:/var/tmp/bench.log"])
        self.assertTrue(any("hostfwd=tcp:127.0.0.1:19132" in arg for arg in command))

    def test_a_tmpfs_disk_warns_and_names_var_tmp(self) -> None:
        # ODL-335: the sealed bench boots an 8 GB raw disk. On tmpfs that is
        # 8 GB of host memory plus the guest's RAM, and `df /` never shows it.
        with mock.patch.object(bench.subprocess, "Popen", return_value=mock.MagicMock()):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                bench.boot_qemu_uki(
                    pathlib.Path("/tmp/x.raw"), pathlib.Path("/var/tmp/x.efi"), pathlib.Path("/var/tmp/OVMF.fd"),
                    pathlib.Path("/var/tmp/boot.log"), pathlib.Path("/var/tmp/bench.log"), 19132, 2048, 2,
                )
        warning = err.getvalue()
        self.assertIn("/tmp/x.raw", warning)
        self.assertIn("/var/tmp", warning)
        self.assertEqual(len(warning.strip().splitlines()), 1, warning)


class ScriptTests(unittest.TestCase):
    """The shell entry points: dry runs name the right work and change nothing."""

    @unittest.skipUnless(HOOK.is_file(), INITRD_HOOK_MISSING)
    def test_sealed_initrd_dry_run_needs_no_source_and_names_it_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Only the chroot is a dry-run input; the blackbox-images source is
            # not. A host that has never had it must still get the plan — on
            # 11 Nov the check before the dry-run branch made `make check` red
            # after every reboot, because the fixture lived in /tmp (tmpfs).
            chroot = pathlib.Path(tmp) / "chroot"
            (chroot / "usr" / "sbin").mkdir(parents=True)
            mkinitramfs = chroot / "usr" / "sbin" / "mkinitramfs"
            mkinitramfs.write_text("#!/bin/sh\n")
            mkinitramfs.chmod(0o755)
            missing = pathlib.Path(tmp) / "no-such-blackbox-images"
            out = pathlib.Path(tmp) / "out"
            env = dict(os.environ, ODILE_BLACKBOX_SOURCE=str(missing))
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "sealed-initrd.sh"),
                 "--dry-run", "--out", str(out), "--chroot", str(chroot)],
                capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"no blackbox-images source at {missing}", result.stdout)
            self.assertIn("bb-sealed-unlock", result.stdout)
            self.assertIn("mkinitramfs", result.stdout)
            self.assertFalse(out.exists(), "sealed-initrd --dry-run created its output")

    @unittest.skipUnless(HOOK.is_file(), INITRD_HOOK_MISSING)
    def test_sealed_initrd_build_without_a_source_exits_2_and_names_the_flag(self) -> None:
        # The dry run plans; the build needs the tree. Removing the check must
        # not turn a missing source into a half-run build.
        with tempfile.TemporaryDirectory() as tmp:
            chroot = pathlib.Path(tmp) / "chroot"
            (chroot / "usr" / "sbin").mkdir(parents=True)
            mkinitramfs = chroot / "usr" / "sbin" / "mkinitramfs"
            mkinitramfs.write_text("#!/bin/sh\n")
            mkinitramfs.chmod(0o755)
            missing = pathlib.Path(tmp) / "no-such-blackbox-images"
            out = pathlib.Path(tmp) / "out"
            env = dict(os.environ, ODILE_BLACKBOX_SOURCE=str(missing))
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "sealed-initrd.sh"),
                 "--out", str(out), "--chroot", str(chroot)],
                capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn(f"no blackbox-images source at {missing}", result.stderr)
            self.assertIn("--source", result.stderr)
            self.assertFalse(out.exists(), "the build made its output before the source check")

    def test_sealed_image_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "out"
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "sealed-image.sh"),
                 "--dry-run", "--out", str(out)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("shred its private half", result.stdout)
            self.assertIn("luksFormat", result.stdout)
            self.assertFalse((out / "odile-tenant-sealed.raw").exists())

    def test_sealed_image_dry_run_names_the_release_placement(self) -> None:
        # ODL-472: the image must carry the release installer and the signed
        # manifest pair, or the first boot has nothing to place and CLAIMS 4.6
        # hashes a path that does not exist.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["bash", str(IMAGE / "sealed-image.sh"), "--dry-run",
                 "--out", str(pathlib.Path(tmp) / "out")],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release/", result.stdout)
        self.assertIn("manifest", result.stdout)
        self.assertIn("odile-manifest.service", result.stdout)

    def test_uki_dry_run_names_signing_and_pinning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            boot = pathlib.Path(tmp) / "boot"
            boot.mkdir()
            for name in ("vmlinuz", "initrd.img"):
                (boot / name).write_bytes(b"x")
            (boot / "cmdline").write_text("root=/dev/x seal_id=bench\n")
            (boot / "uname").write_text("6.12.0-test\n")
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "uki.sh"),
                 "--dry-run", "--in", tmp, "--sign", "--pin", str(pathlib.Path(tmp) / "pin.json")],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("8731846c", result.stdout)
            self.assertIn("sbsign", result.stdout)
            self.assertIn("write the pin", result.stdout)

    def test_sealed_image_re_execs_into_a_private_mount_namespace(self) -> None:
        # The first fix ran `mount --make-rprivate /` in the host namespace and
        # made the *host* tree private (the demo reconcile broke until it was
        # made shared again). The re-exec must come after the dry-run exit and
        # before the first mount.
        text = (ROOT / "deploy" / "image" / "sealed-image.sh").read_text()
        reexec = 'exec unshare -m --propagation private "$0" "${ORIG_ARGS[@]}"'
        self.assertIn(reexec, text)
        self.assertIn("ODILE_SEALED_IN_NS", text)
        self.assertLess(text.index("ORIG_ARGS=(\"$@\")"), text.index("while [ $# -gt 0 ]"),
                        "the original argv must be saved before the option loop consumes it")
        self.assertLess(text.index('if [ "$DRY_RUN" = 1 ]'), text.index(reexec),
                        "--dry-run must not need a mount namespace")
        self.assertLess(text.index(reexec), text.index("\nmount --make-rprivate"))

    def test_the_namespace_re_exec_replays_the_arguments_and_leaves_the_host_alone(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("sealed-image.sh refuses a non-root caller before the re-exec")
        if not shutil.which("unshare"):
            self.skipTest("unshare is missing")

        def root_propagation() -> list[str]:
            for line in pathlib.Path("/proc/self/mountinfo").read_text().splitlines():
                fields = line.split()
                if len(fields) > 6 and fields[4] == "/":
                    return [field for field in fields[6:] if field.startswith("shared:")]
            return []

        before = root_propagation()
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "out"
            boot = out / "boot"
            boot.mkdir(parents=True)
            for name in ("vmlinuz", "initrd.img"):
                (boot / name).write_bytes(b"x")
            (boot / "uname").write_text("6.12.0-test\n")
            snp = out / "bench-snpreport"
            snp.write_text("#!/bin/sh\nexit 0\n")
            snp.chmod(0o755)
            # An existing disk makes the child exit right after the re-exec, so
            # the test stops before any mount is made.
            (out / "odile-tenant-sealed.raw").write_bytes(b"")
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "sealed-image.sh"), "--out", str(out)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("private mount namespace", result.stdout)
            # The message names the --out the caller passed, not the default:
            # the re-exec replayed the command line.
            self.assertIn(str(out), result.stderr)
            self.assertIn("exists; pass --force", result.stderr)
        self.assertEqual(root_propagation(), before,
                         "the build changed the host mount propagation")

    @staticmethod
    def _cmdline_file(tmp: str, name: str, text: str) -> pathlib.Path:
        path = pathlib.Path(tmp) / name
        path.write_text(text + "\n")
        return path

    def test_sealed_image_can_reuse_an_exact_measured_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            line = (
                "root=/dev/mapper/root_crypt ro bb.host=odile-bench "
                "bb.ip4=192.0.2.10/24 bb.gw4=192.0.2.1 "
                "bb.key=c3NoLWVkMjU1MTkgQUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUFZU3o5SWVwbHJWenRXNVN5V2ZBcU9uNGlKMG5tS0NpZjhqNHlMMXExTzAgb2RpbGUtYmVuY2ggdGhyb3dhd2F5IGJvb3Qga2V5Cg== "
                "seal_id=odl454-bench-v1"
            )
            cmdline = self._cmdline_file(tmp, "cmdline", line)
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "sealed-image.sh"),
                 "--dry-run", "--out", str(pathlib.Path(tmp) / "out"),
                 "--cmdline-file", str(cmdline)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("reuse the measured line", result.stdout)
            self.assertNotIn("generate a throwaway", result.stdout)

    def test_sealed_image_rejects_a_cmdline_without_an_ip4_token(self) -> None:
        """Round 4's defect: the installer's boot check needs one bb.ip4=."""
        with tempfile.TemporaryDirectory() as tmp:
            line = (
                "root=/dev/mapper/root_crypt ro bb.host=odile-bench "
                "bb.key=c3NoLWVkMjU1MTkgQUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUFZU3o5SWVwbHJWenRXNVN5V2ZBcU9uNGlKMG5tS0NpZjhqNHlMMXExTzAgb2RpbGUtYmVuY2ggdGhyb3dhd2F5IGJvb3Qga2V5Cg== "
                "seal_id=odl454-bench-v1"
            )
            cmdline = self._cmdline_file(tmp, "cmdline", line)
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "sealed-image.sh"),
                 "--dry-run", "--out", str(pathlib.Path(tmp) / "out"),
                 "--cmdline-file", str(cmdline)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("bb.ip4=", result.stderr)
            self.assertIn("exactly one", result.stderr)

    def test_sealed_image_rejects_a_cmdline_without_a_seal_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = self._cmdline_file(
                tmp, "cmdline", "root=/dev/mapper/root_crypt ro bb.key=c3NoZQ==")
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "sealed-image.sh"),
                 "--dry-run", "--out", str(pathlib.Path(tmp) / "out"),
                 "--cmdline-file", str(cmdline)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("no seal_id=", result.stderr)

    def test_sealed_image_rejects_seal_id_with_cmdline_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmdline = self._cmdline_file(
                tmp, "cmdline", "root=/dev/mapper/root_crypt ro bb.key=c3NoZQ== seal_id=one")
            result = subprocess.run(
                ["bash", str(ROOT / "deploy" / "image" / "sealed-image.sh"),
                 "--dry-run", "--out", str(pathlib.Path(tmp) / "out"),
                 "--cmdline-file", str(cmdline), "--seal-id", "two"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("mutually exclusive", result.stderr)


# ---------------------------------------------------------------------------
# The builders' copy lists, derived from the installer they feed
#
# install-stack.sh runs in the image from /usr/local/lib/odile-image/ and
# opens its units as `$HERE/<name>`. Each builder decides for itself which
# deploy/image/ files to put in that directory, so a name added to
# install-stack.sh reaches neither image until both lists learn it — and the
# lists have drifted twice: ODL-457's first-visit proxy pair reached
# sealed-image.sh and not mkdisk.sh, so on `main` at af4b65c the bench build
# died eight minutes into a debootstrap; then ODL-236's follow-up taught
# install-stack.sh to read the /verify proxy pair and mkdisk.sh did not learn
# them, which this check names. The names below are read out of
# install-stack.sh itself, so the test adds no third copy of the list.
#
# The reader is deliberately small: it walks the script's logical lines
# (backslash continuations joined), expands the `for` loops it is inside
# (resolving the shell's `${x%%:*}` / `${x##*:}` field splits), and notes the
# enclosing `if` conditions so a unit that only the guest-smoke or the seed
# branch reads is not required of a builder that never runs that branch.
IMAGE_LIB_DIR = "/usr/local/lib/odile-image"

_FOR_RE = re.compile(r"^for\s+(\w+)\s+in\s+(.*?);\s*do$")
# `while`/`until` carry no loop variable, but their `done` must not close a
# `for` this reader is tracking — sealed-image.sh has retry loops inside `if`
# blocks, and popping the wrong frame would leak the wrong gate onto a line.
_LOOP_RE = re.compile(r"^(?:while|until)\s+.*;\s*do$")
_IF_RE = re.compile(r"^(if|elif)\s+(.*?);\s*then$")
_ASSIGN_RE = re.compile(r"(\w+)=\$\{(\w+)(#{1,2}|%{1,2})(\*:|:\*)\}")
_HERE_RE = re.compile(r"\$HERE/([A-Za-z0-9_.@+-]+)")
# `[ -f "$HERE/x" ]` asks whether a file exists; it installs nothing, and a
# builder that ships none of an optional group is allowed to (install-stack.sh
# prints `skip`). A reference only counts when the line uses the file.
_PROBE_RE = re.compile(r"-[ef]\s+\"?\$HERE/[A-Za-z0-9_.@+-]+\"?")
_IMAGE_LIB_RE = re.compile(re.escape(IMAGE_LIB_DIR) + r"/([A-Za-z0-9_.@+-]+)")
# What puts a file into the image: only these commands. A `say`/`printf` line
# that prints the destination is a log line, not a copy.
_COPY_RE = re.compile(r"^\s*(?:install|cp|mv|ln|rsync)\s")
# The three files whose presence opens install-stack.sh's /verify block. A
# builder that ships none of them skips the block (mkdisk.sh's bench image);
# one that ships any must ship all three, or the image loses the page it was
# built to serve.
_VERIFY_SURFACE = ("verify.html", "access_report.sh", "verify_and_unlock.sh")


def _logical_lines(text: str) -> list[str]:
    """The script's lines with backslash continuations joined."""
    out: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if pending:
            line = f"{pending} {line.strip()}"
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        out.append(line)
    if pending:
        out.append(pending)
    return out


def _field_removal(value: str, op: str, pattern: str) -> str:
    """The shell's `${x#*:}`, `${x##*:}`, `${x%:*}` and `${x%%:*}`."""
    if ":" not in value:
        return value
    if pattern == "*:":            # a prefix ending at a colon
        return value.split(":", 1)[1] if op == "#" else value.rsplit(":", 1)[1]
    return value.rsplit(":", 1)[0] if op == "%" else value.split(":", 1)[0]


def _resolve(name: str, loop_var: str, item: str, env: dict, depth: int = 0) -> str | None:
    """A variable's value for one item of the enclosing `for` list."""
    if name == loop_var:
        return item
    if name not in env or depth > 8:
        return None
    src, op, pattern = env[name]
    base = _resolve(src, loop_var, item, env, depth + 1)
    return None if base is None else _field_removal(base, op, pattern)


def _bindings(stack: list[dict]) -> list[dict]:
    """Every variable binding the enclosing `for` loops make for one line."""
    combos: list[dict] = [{}]
    for frame in stack:
        if frame["kind"] != "for":
            continue
        options = []
        for item in frame["items"]:
            bind = {frame["var"]: item}
            for name in frame["env"]:
                value = _resolve(name, frame["var"], item, frame["env"])
                if value is not None:
                    bind[name] = value
            options.append(bind)
        combos = [{**combo, **bind} for combo in combos for bind in options]
    return combos


def _substitute(line: str, bind: dict) -> str:
    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return bind.get(name, match.group(0))
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", repl, line)


def _pop(stack: list[dict], kinds: tuple[str, ...]) -> None:
    """Close the frame this `done`/`fi` belongs to, if it is the top one."""
    if stack and stack[-1]["kind"] in kinds:
        stack.pop()


def _walk(text: str):
    """Yield (line, enclosing `if` conditions) per executed line.

    `for` loop variables are substituted from the loop's own items (nested one
    level per loop), so `"$HERE/$f"` reaches the caller as
    `"$HERE/bench.py"` and the rest of this module needs no variable syntax.
    """
    stack: list[dict] = []
    for line in _logical_lines(text):
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        match = _FOR_RE.match(body)
        if match:
            stack.append({"kind": "for", "var": match.group(1),
                          "items": shlex.split(match.group(2)), "env": {}})
            continue
        if _LOOP_RE.match(body):
            stack.append({"kind": "loop"})
            continue
        if body == "done":
            _pop(stack, ("for", "loop"))
            continue
        if body == "fi":
            _pop(stack, ("if",))
            continue
        if body in ("else", "then"):
            continue
        match = _IF_RE.match(body)
        if match:
            if match.group(1) == "if":
                stack.append({"kind": "if", "cond": match.group(2)})
            else:
                for frame in reversed(stack):
                    if frame["kind"] == "if":
                        frame["cond"] = match.group(2)
                        break
        assignments = _ASSIGN_RE.findall(body)
        if assignments:
            for frame in reversed(stack):
                if frame["kind"] == "for":
                    for name, src, op, pattern in assignments:
                        frame["env"][name] = (src, op, pattern)
                    break
        conditions = [frame["cond"] for frame in stack if frame["kind"] == "if"]
        for bind in _bindings(stack):
            yield _substitute(line, bind), conditions


def _gate(conditions: list[str]) -> frozenset[str]:
    """Which branch a reference sits in: always, guest-smoke, seed or verify.

    An `if` this module does not recognise is transparent: it neither adds nor
    removes a requirement (the inner `[ -f ... ]` guards on individual files
    are the common case).
    """
    gates = set()
    for cond in conditions:
        if "SEED_FILE" in cond:
            gates.add("seed")
        if "GUEST_SMOKE" in cond:
            gates.add("guest-smoke")
        if any(name in cond for name in _VERIFY_SURFACE):
            gates.add("verify")
    return frozenset(gates or {"always"})


def install_stack_reads(text: str) -> dict[str, frozenset[str]]:
    """Name -> the branches that read it out of install-stack.sh's own `$HERE`."""
    reads: dict[str, set[str]] = {}
    for line, conditions in _walk(text):
        if "$HERE/" not in line:
            continue
        if "$HERE/" not in _PROBE_RE.sub("", line):
            continue  # every reference on this line is an existence probe
        for name in _HERE_RE.findall(line):
            reads.setdefault(name, set()).update(_gate(conditions))
    return {name: frozenset(gates) for name, gates in reads.items()}


def image_dir_files(text: str) -> set[str]:
    """The names a builder installs into /usr/local/lib/odile-image/.

    Only a copy command's *destination* counts. A `say`/`echo` line that
    mentions the path announces something; it does not put a file there, and a
    builder whose only mention of a unit is a log line does not ship it. (The
    first version of this counted the path anywhere on a line, so a `say` line
    silenced the check — a false negative the reviewer probed.)
    """
    names: set[str] = set()
    for line, _conditions in _walk(text):
        if not _COPY_RE.match(line):
            continue
        match = _IMAGE_LIB_RE.search(line.split()[-1].strip("\"'"))
        if match:
            names.add(match.group(1))
    return names


def _runs_flag(flag: str, builder_text: str, installed: set[str],
               scripts: dict[str, str]) -> bool:
    """Whether this builder makes install-stack.sh run with `flag`.

    The builder's own command line is the first place to look; the second is a
    program it ships into the image — `odile-firstboot.py` is what runs
    `install-stack.sh --seed` on the sealed image's first boot. install-stack.sh
    itself is in both copy lists, so it is excluded: it is the callee, and its
    own `--seed` text says nothing about who calls it that way.
    """
    if flag in builder_text:
        return True
    return any(flag in text for name, text in scripts.items()
               if name in installed and name != "install-stack.sh")


def copy_list_gaps(scripts: dict[str, str]) -> list[str]:
    """Every file install-stack.sh reads that a builder that needs it omits."""
    reads = install_stack_reads(scripts["install-stack.sh"])
    verify_group = {name for name, gates in reads.items() if "verify" in gates}
    gaps: list[str] = []
    for builder in ("sealed-image.sh", "mkdisk.sh"):
        text = scripts.get(builder, "")
        installed = image_dir_files(text)
        active = {
            "guest-smoke": _runs_flag("--guest-smoke", text, installed, scripts),
            "seed": _runs_flag("--seed", text, installed, scripts),
            "verify": bool(installed & verify_group),
        }
        for name in sorted(reads):
            if not all(key == "always" or active[key] for key in reads[name]):
                continue
            if name not in installed:
                gates = ", ".join(sorted(reads[name]))
                gaps.append(f"{builder}: {IMAGE_LIB_DIR}/{name} ({gates})")
    return gaps


class CopyListTests(unittest.TestCase):
    """Both builders copy what install-stack.sh reads out of its own directory.

    install-stack.sh runs *inside* the image, from /usr/local/lib/odile-image/,
    and opens its units as `$HERE/<name>`. Each builder keeps a private list of
    deploy/image/ files to put there. The lists drifted: ODL-457's first-visit
    proxy pair reached sealed-image.sh and not mkdisk.sh, so on `main` at
    af4b65c the bench build died eight minutes into a debootstrap with

        sed: can't read /usr/local/lib/odile-image/odile-bench-proxy-firstvisit.socket

    The thing under test is a copy list, and a copy list is text, so these
    tests read the three scripts — no debootstrap, no root, no image. The names
    are derived from install-stack.sh's own sed/install/cmp lines and its `for`
    lists (`install_stack_reads`), so the suite adds no third copy of the list
    to drift out of step. What stays per-builder is the *need*: a list is only
    required of a builder that causes install-stack.sh to run that branch —
    `--guest-smoke` on its own command line, or a program it ships (the first
    boot) that runs install-stack.sh `--seed`.
    """

    def _scripts(self) -> dict[str, str]:
        # odile-firstboot.py is here because it is the program that runs
        # `install-stack.sh --seed` in the guest; that is how the seed units
        # become required of sealed-image.sh and of no other builder.
        return {
            name: (IMAGE / name).read_text(encoding="utf-8")
            for name in ("install-stack.sh", "sealed-image.sh", "mkdisk.sh",
                         "odile-firstboot.py")
        }

    def test_the_installer_names_the_units_it_reads_out_of_its_own_directory(self) -> None:
        # A pin on the derivation itself: if the reader stops seeing a `for`
        # list or a `sed` line, the two tests below go quiet instead of red.
        reads = install_stack_reads(self._scripts()["install-stack.sh"])
        self.assertEqual(reads["odile-bench-proxy-firstvisit.socket"],
                         frozenset({"guest-smoke"}))
        self.assertEqual(reads["odile-bench-proxy-manifest.service"],
                         frozenset({"guest-smoke"}))
        self.assertEqual(reads["odile-bench-proxy-verify.socket"],
                         frozenset({"guest-smoke"}),
                         "ODL-236's follow-up: the /verify stand-in pair")
        self.assertEqual(reads["webapp-brand.js"], frozenset({"always"}),
                         "the brand pair list is a `for` loop of name:mode items")
        self.assertEqual(reads["verify.html"], frozenset({"verify"}))
        self.assertEqual(reads["recovery-seed.py"], frozenset({"seed"}))
        for name in ("tenant.service", "config.yaml", "bench-heartbeat.sh",
                     "bench.py", "smoke.sh", "odile-webapp-brand.conf"):
            self.assertEqual(reads.get(name), frozenset({"always"}), name)
        # The chroot call names install-stack.sh itself; the file is not read
        # from $HERE and is copied by both builders for another reason.
        self.assertNotIn("install-stack.sh", reads)

    def test_both_builders_copy_every_file_install_stack_reads(self) -> None:
        gaps = copy_list_gaps(self._scripts())
        self.assertEqual(gaps, [],
                         "install-stack.sh reads these and no builder copies them:\n"
                         + "\n".join(gaps))

    def test_a_builder_that_loses_one_unit_is_named_by_the_check(self) -> None:
        # The red-first plant: the exact drift that ODL-914 repaired by hand.
        scripts = self._scripts()
        dropped = "odile-bench-proxy-firstvisit.socket"
        line = ("         odile-bench-proxy-firstvisit.service "
                "odile-bench-proxy-firstvisit.socket \\\n")
        self.assertIn(line, scripts["mkdisk.sh"])
        scripts["mkdisk.sh"] = scripts["mkdisk.sh"].replace(
            line, "         odile-bench-proxy-firstvisit.service \\\n")
        self.assertIn(dropped, scripts["sealed-image.sh"],
                      "the plant must leave the other builder intact")
        self.assertEqual(
            copy_list_gaps(scripts),
            [f"mkdisk.sh: {IMAGE_LIB_DIR}/{dropped} (guest-smoke)"],
        )

    def test_a_builder_that_only_mentions_a_unit_has_not_copied_it(self) -> None:
        # A `say` line that prints the destination path is an announcement,
        # not a copy. The first version of image_dir_files() counted the path
        # anywhere on a line, so this shape silenced the check.
        scripts = self._scripts()
        unit = "odile-bench-proxy-verify.service"
        line = ("         odile-bench-proxy-verify.service "
                "odile-bench-proxy-verify.socket \\\n")
        self.assertIn(line, scripts["mkdisk.sh"])
        scripts["mkdisk.sh"] = scripts["mkdisk.sh"].replace(
            line, "         odile-bench-proxy-verify.socket \\\n")
        scripts["mkdisk.sh"] += (
            f'say note "would install {IMAGE_LIB_DIR}/{unit}"\n')
        self.assertEqual(
            copy_list_gaps(scripts),
            [f"mkdisk.sh: {IMAGE_LIB_DIR}/{unit} (guest-smoke)"],
        )

    def test_the_seed_units_are_required_only_of_the_builder_that_first_boots(self) -> None:
        scripts = self._scripts()
        bench = image_dir_files(scripts["mkdisk.sh"])
        sealed = image_dir_files(scripts["sealed-image.sh"])
        self.assertNotIn("odile-firstboot.py", bench)
        self.assertIn("odile-firstboot.py", sealed)
        for name in ("odile-first-visit.service", "first_visit.py", "first-visit.html"):
            self.assertNotIn(name, bench, "the bench image has no first boot")
            self.assertIn(name, sealed)
        self.assertEqual(copy_list_gaps(scripts), [])
        # ...and sealed-image.sh's own need is enforced, not just satisfied:
        # drop the unit its first boot starts and the check names it.
        scripts["sealed-image.sh"] = scripts["sealed-image.sh"].replace(
            "odile-firstboot.service odile-first-visit.service \\\n",
            "odile-firstboot.service \\\n")
        self.assertEqual(
            copy_list_gaps(scripts),
            [f"sealed-image.sh: {IMAGE_LIB_DIR}/odile-first-visit.service (seed)"],
        )


class PinTests(unittest.TestCase):
    """The committed pin is the release record; a stranger reads it, not a log."""

    def test_the_pin_exists_and_its_runs_agree(self) -> None:
        self.assertTrue(PIN.is_file(), f"{PIN} is missing; build the image and pin it")
        pin = json.loads(PIN.read_text())
        self.assertEqual(pin["card"], "ODL-456")
        self.assertEqual(pin["release"], "2026.11.1")
        for name in ("uki_signed", "uki_unsigned", "loose"):
            runs = pin["runs"][name]
            self.assertEqual(len(runs), 2, name)
            self.assertEqual(runs[0], runs[1], f"{name} measurements differ")
            self.assertRegex(runs[0], r"^[0-9a-f]{96}$")
        self.assertTrue(pin["signature"]["verified"])
        self.assertRegex(pin["signature"]["cert_sha256"], r"^[0-9a-f]{64}$")

    @unittest.skipUnless(HOOK.is_file(), SPIKE_MISSING)
    def test_the_pin_carries_the_hook_at_the_end_of_the_order(self) -> None:
        pin = json.loads(PIN.read_text())
        hooks = [line for line in pin["order"] if "bb-sealed-unlock" in line]
        self.assertEqual(len(hooks), 1, pin["order"])
        self.assertEqual(hooks[0], '/scripts/init-premount/bb-sealed-unlock "$@"')
        self.assertEqual(pin["order"][-1], hooks[0], "the hook must be the last init-premount script")
        self.assertTrue(any("bb-wait" in line for line in pin["order"][:-1]), pin["order"])
        self.assertEqual(pin["hook_sha256"], hashlib.sha256(HOOK.read_bytes()).hexdigest())
        self.assertGreaterEqual(len(pin["order"]), 4)

    def test_the_pin_has_the_seal_id_and_no_private_key(self) -> None:
        pin = json.loads(PIN.read_text())
        self.assertEqual(pin["seal_id"], pin["cmdline"].split("seal_id=")[1].split()[0])
        self.assertIn("root=/dev/mapper/root_crypt", pin["cmdline"])
        self.assertIn("bb.key=", pin["cmdline"])
        self.assertEqual(pin["boot_key_private_half"], "shredded")
        self.assertNotIn("PRIVATE KEY", PIN.read_text())
        self.assertEqual(pin["seal_key_id"], "key_sha256=" + expected_key(pin["cmdline"]).hex())


class FirstbootUnitTests(unittest.TestCase):
    """The cmdline facts and the state formats the boot writes (ODL-456)."""

    IID = "45445445-0000-4000-8000-000000000454"
    TOKEN = "ab" * 32

    def test_cmdline_tokens_are_key_value_and_the_last_wins(self) -> None:
        text = "root=/dev/x bb.iid=one odile.token=aa odile.token=bb"
        tokens = firstboot.parse_cmdline(text)
        self.assertEqual(tokens["bb.iid"], "one")
        self.assertEqual(tokens["odile.token"], "bb")
        facts = firstboot.cmdline_facts("bb.iid=one odile.token=%s" % self.TOKEN)
        self.assertEqual(facts["instance_id"], "one")
        self.assertEqual(facts["token"], self.TOKEN)
        self.assertEqual(facts["seed_url"], firstboot.DEFAULT_SEED_URL)

    def test_a_custom_seed_url_is_read_from_the_cmdline(self) -> None:
        facts = firstboot.cmdline_facts(
            "bb.iid=one odile.seed_url=http://10.0.2.2:8081/seed")
        self.assertEqual(facts["seed_url"], "http://10.0.2.2:8081/seed")

    def test_the_first_visit_state_is_the_token_hash_and_nothing_else(self) -> None:
        state = json.loads(firstboot.first_visit_state(self.TOKEN))
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["token_sha256"], hashlib.sha256(self.TOKEN.encode()).hexdigest())
        self.assertIsNone(state["used_at"])
        self.assertIsNone(state["recovery_hash"])
        self.assertNotIn(self.TOKEN, json.dumps(state))

    def test_the_state_is_written_once_and_a_spent_page_stays_spent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "first-visit.json"
            self.assertTrue(firstboot.write_first_visit_state(str(path), self.TOKEN))
            before = path.read_bytes()
            self.assertFalse(firstboot.write_first_visit_state(str(path), "cd" * 32))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_seed_validator_refuses_a_credential_field(self) -> None:
        seed = {
            "slug": "odl-bench", "instance_id": self.IID, "near_api_key": "k",
            "model": "m", "support_public_key": "ssh-ed25519 x",
            "backup_repo_url": "sftp:x", "mail_relay": {
                "host": "h", "port": 587, "username": "u", "secret": "s"},
            "hermes_version": "v", "sign_in_hash": "scrypt$16384$8$1$c2FsdA==$ZGs=",
            "password": "hunter2",
        }
        with self.assertRaises(firstboot.Refusal) as caught:
            firstboot.validate_seed(seed, self.IID)
        self.assertIn("password", caught.exception.reason)

    def test_the_seed_validator_requires_this_instance(self) -> None:
        seed = {
            "slug": "odl-bench", "instance_id": "another", "near_api_key": "k",
            "model": "m", "support_public_key": "ssh-ed25519 x",
            "backup_repo_url": "sftp:x", "mail_relay": {
                "host": "h", "port": 587, "username": "u", "secret": "s"},
            "hermes_version": "v", "sign_in_hash": "scrypt$16384$8$1$c2FsdA==$ZGs=",
        }
        with self.assertRaises(firstboot.Refusal):
            firstboot.validate_seed(seed, self.IID)

    def test_the_seed_validator_accepts_a_rebuild_and_refuses_a_broken_one(self) -> None:
        seed = {
            "slug": "odl-bench", "instance_id": self.IID, "near_api_key": "k",
            "model": "m", "support_public_key": "ssh-ed25519 x",
            "backup_repo_url": "sftp:x", "mail_relay": {
                "host": "h", "port": 587, "username": "u", "secret": "s"},
            "hermes_version": "v", "sign_in_hash": "scrypt$16384$8$1$c2FsdA==$ZGs=",
            "rebuild": {"kdf": "hmac-sha256-ctr-v1", "sealed": "00aa",
                        "tag": "00" * 32},
        }
        self.assertIs(firstboot.validate_seed(seed, self.IID), seed)
        for broken, reason in (
            ({"kdf": "aes-256-gcm", "sealed": "00aa", "tag": "00" * 32}, "kdf"),
            ({"kdf": "hmac-sha256-ctr-v1", "sealed": "0aa", "tag": "00" * 32}, "sealed"),
            ({"kdf": "hmac-sha256-ctr-v1", "sealed": "00aa", "tag": "00"}, "tag"),
        ):
            with self.assertRaises(firstboot.Refusal) as caught:
                firstboot.validate_seed(dict(seed, rebuild=broken), self.IID)
            self.assertIn(reason, caught.exception.reason)

    def test_the_boot_record_reads_the_measured_key_and_the_pin_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pin = root / "tenant-image.pin"
            pin.write_text(json.dumps({"release": "2026.11.1"}), encoding="utf-8")
            line = ("root=/dev/mapper/root_crypt bb.iid=%s bb.host=odl-bench bb.key=%s"
                    % (self.IID, base64.b64encode(BOOT_KEY_TEXT.encode()).decode()))
            record, fingerprint, reason, note = firstboot.boot_record_facts(
                line, boot_report_path=str(root / "absent-attest.json"),
                pin_path=str(pin), boot_key_path=str(root / "absent.pub"),
                boot_record_path=str(root / "absent.json"),
                host_key_path=str(root / "absent-host.pub"),
                first_visit_path=str(root / "absent.json"),
            )
        self.assertEqual(reason, "")
        self.assertEqual(record["release"], "2026.11.1")
        self.assertEqual(record["first_visit"], "pending")
        self.assertEqual(record["cmdline"], line)
        self.assertTrue(fingerprint.startswith("SHA256:"))

    def test_the_boot_record_prefers_the_pin_and_falls_back_to_the_boot_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pin = root / "pin.json"
            pin.write_text(json.dumps({"release": "2026.11.1"}), encoding="utf-8")
            boot_record = root / "boot-record.json"
            boot_record.write_text(json.dumps({"release": "2026.10.1"}), encoding="utf-8")
            absent = str(root / "absent-attest.json")
            self.assertEqual(
                firstboot.pinned_release(boot_report_path=absent, pin_path=str(pin),
                                         boot_record_path=str(boot_record)),
                ("2026.11.1", ""))
            pin.unlink()
            self.assertEqual(
                firstboot.pinned_release(boot_report_path=absent, pin_path=str(pin),
                                         boot_record_path=str(boot_record)),
                ("2026.10.1", ""))

    def test_a_multi_line_or_unknown_key_is_not_a_public_key(self) -> None:
        self.assertIsNone(firstboot.public_key_line("ssh-ed25519 AAAA\nssh-ed25519 BBBB"))
        self.assertIsNone(firstboot.public_key_line("ssh-dss AAAA"))
        parsed = firstboot.public_key_line(BOOT_KEY_TEXT)
        self.assertEqual(parsed[0], BOOT_KEY_TEXT)
        self.assertRegex(parsed[1], r"^SHA256:[A-Za-z0-9+/]{43}$")

    def test_a_shared_disks_boot_key_is_refreshed_from_the_line(self) -> None:
        """One disk, a per-machine line: the measured key wins (t_556631d6).

        A recycled disk image is shared across machines, so its baked
        `/etc/odile/boot-key.pub` names the key of whichever machine the disk
        was built for. The line that boots carries this machine's own `bb.key`,
        and the file is only the verify surface's copy of the measured key —
        so the boot refreshes it from the line and posts the record instead of
        skipping the POST on a disagreement. A second boot finds the file
        already equal and writes nothing.
        """
        other = (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL7Tzl2nYSnGVbP/Vfp9wJwD7q2Wt2mJKT5p8g4oWn7Z "
            "odile-other"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pin = root / "pin.json"
            pin.write_text(json.dumps({"release": "2026.11.1"}), encoding="utf-8")
            key_file = root / "boot-key.pub"
            key_file.write_text(other + "\n", encoding="utf-8")
            line = ("root=/dev/mapper/root_crypt bb.iid=%s bb.key=%s"
                    % (self.IID, base64.b64encode(BOOT_KEY_TEXT.encode()).decode()))

            def facts(**kwargs):
                return firstboot.boot_record_facts(
                    line, boot_report_path=str(root / "absent-attest.json"),
                    pin_path=str(pin), boot_key_path=str(key_file),
                    boot_record_path=str(root / "absent.json"),
                    host_key_path=str(root / "absent-host.pub"),
                    first_visit_path=str(root / "absent.json"), **kwargs)

            # A read-only probe (--check) must not touch the image's file.
            record, fingerprint, reason, note = facts(refresh=False)
            untouched = key_file.read_text(encoding="utf-8")
            record, fingerprint, reason, note = facts()
            stored = key_file.read_text(encoding="utf-8")
            second = facts()

        self.assertEqual(untouched, other + "\n")
        self.assertEqual(reason, "")
        self.assertEqual(record["release"], "2026.11.1")
        self.assertEqual(record["cmdline"], line)
        self.assertEqual(stored, BOOT_KEY_TEXT + "\n")
        self.assertEqual(fingerprint, firstboot.public_key_line(BOOT_KEY_TEXT)[1])
        self.assertIn("refreshed", note)
        self.assertEqual(second[2], "")
        self.assertEqual(second[3], "", "a converged file must not be rewritten or noted")
        self.assertEqual(second[1], fingerprint)

    def test_a_line_with_no_bb_key_is_refused_not_read_from_the_image(self) -> None:
        """No `bb.key=` means no measured key: the record is skipped.

        The image's own copy is the *build's* key, and on a shared disk that
        is another machine's. Nothing in this boot measures it, so it is never
        reported as this machine's boot key — the refusal stays for a missing
        line key even when the image carries one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pin = root / "pin.json"
            pin.write_text(json.dumps({"release": "2026.11.1"}), encoding="utf-8")
            key_file = root / "boot-key.pub"
            key_file.write_text(BOOT_KEY_TEXT + "\n", encoding="utf-8")
            record, fingerprint, reason, _ = firstboot.boot_record_facts(
                "root=/dev/mapper/root_crypt bb.iid=%s" % self.IID,
                pin_path=str(pin), boot_key_path=str(key_file),
                boot_record_path=str(root / "absent.json"),
                host_key_path=str(root / "absent-host.pub"),
                first_visit_path=str(root / "absent.json"),
            )
            stored = key_file.read_text(encoding="utf-8")
        self.assertIsNone(record)
        self.assertEqual(fingerprint, "")
        self.assertIn("no bb.key=", reason)
        self.assertEqual(stored, BOOT_KEY_TEXT + "\n")



class HostKeyTests(unittest.TestCase):
    """ODL-523: the image ships no host key; the boot makes one.

    The image build removes debootstrap's /etc/ssh/ssh_host_*_key and asserts
    none is left; this exercises the other half, the first boot's own
    `ssh-keygen -A`, against a real ssh-keygen and a private root, so the test
    host's /etc/ssh is never read or written.
    """

    def setUp(self) -> None:
        if not shutil.which("ssh-keygen"):
            raise unittest.SkipTest("ssh-keygen is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def generate(self, root: pathlib.Path) -> tuple[str, str]:
        return firstboot.ensure_host_keys(str(root))

    def keys(self, root: pathlib.Path) -> list[pathlib.Path]:
        return [pathlib.Path(p) for p in firstboot.host_key_paths(str(root))]

    def fingerprint(self, root: pathlib.Path) -> str:
        result = subprocess.run(
            ["ssh-keygen", "-lf", str(root / "etc" / "ssh" / "ssh_host_ed25519_key.pub")],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()[1]

    def test_a_bare_root_gets_this_machines_own_keys(self) -> None:
        verb, detail = self.generate(self.root)
        self.assertEqual(verb, "create", detail)
        for key in self.keys(self.root):
            self.assertTrue(key.is_file(), key)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600, key)
            self.assertTrue(key.with_name(key.name + ".pub").is_file(), key)
        self.assertIn("3 generated", detail)

    def test_a_second_run_writes_nothing_and_the_bytes_do_not_move(self) -> None:
        self.generate(self.root)
        before = {str(key): key.read_bytes() for key in self.keys(self.root)}
        verb, detail = self.generate(self.root)
        self.assertEqual(verb, "same", detail)
        self.assertEqual({str(key): key.read_bytes() for key in self.keys(self.root)}, before)

    def test_two_machines_do_not_share_a_host_key(self) -> None:
        other = self.root / "other"
        self.generate(self.root)
        self.generate(other)
        self.assertNotEqual(self.fingerprint(self.root), self.fingerprint(other))

    def test_a_missing_ssh_keygen_is_a_warning_not_a_failed_boot(self) -> None:
        verb, detail = firstboot.ensure_host_keys(str(self.root), "/nonexistent/ssh-keygen")
        self.assertEqual(verb, "warn", detail)
        self.assertIn("sshd-keygen.service", detail)

    def test_the_keys_exist_before_the_boot_record_is_read(self) -> None:
        """ODL-398's record carries the host key, so the keys come first.

        A merged first boot that reads the record before it runs ssh-keygen
        posts a `detail` with no `host_key` on the first boot — the one boot
        the record exists for.
        """
        order: list[str] = []
        seen: dict[str, bool] = {}
        real_ensure = firstboot.ensure_host_keys

        def ensure(root=firstboot.SSH_ROOT, keygen=firstboot.SSH_KEYGEN):
            order.append("host keys")
            return real_ensure(root, keygen)

        def record(cmdline, **kwargs):
            order.append("boot record")
            seen["key_present"] = pathlib.Path(kwargs["host_key_path"]).is_file()
            return None, "", "test", ""

        cmdline = self.root / "cmdline"
        cmdline.write_text("root=/dev/mapper/root_crypt ro\n", encoding="utf-8")
        argv = [
            "--cmdline", str(cmdline), "--state", str(self.root / "state.json"),
            "--seed-cache", str(self.root / "seed.json"),
            "--signing-key", str(self.root / "key.pub"),
            "--installer", str(self.root / "installer.sh"), "--first-visit-port", "0",
            "--log", str(self.root / "log"), "--release-dir", str(self.root / "release"),
            "--installer-copy", str(self.root / "install.sh"),
            "--etc-dir", str(self.root / "etc"), "--odile-lib", str(self.root / "lib"),
            "--manifest-unit", str(self.root / "unit"),
            "--ssh-root", str(self.root / "ssh-root"),
            "--ssh-keygen", shutil.which("ssh-keygen") or "ssh-keygen",
            "--pin", str(self.root / "pin.json"),
            "--boot-key", str(self.root / "boot-key.pub"),
            "--boot-record", str(self.root / "record.json"),
            "--host-key", str(self.root / "ssh-root/etc/ssh/ssh_host_ed25519_key.pub"),
            "--first-visit-state", str(self.root / "first-visit.json"),
        ]
        with mock.patch.object(firstboot, "ensure_host_keys", ensure), \
                mock.patch.object(firstboot, "boot_record_facts", record), \
                mock.patch.object(firstboot, "place_release", lambda **kwargs: None), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = firstboot.main(argv)
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(order, ["host keys", "boot record"], output.getvalue())
        self.assertTrue(seen["key_present"],
                        "the record was read before this machine's host key existed")


class FirstbootExchangeTests(unittest.TestCase):
    """The program against the real seed service: exchange, refusal, cache."""

    IID = "45445445-0000-4000-8000-000000000454"
    SLUG = "odl-bench"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        os.chmod(self.dir, 0o700)
        self.seed = _load("odile_seed_test", ROOT / "seed" / "seed.py")
        # ODL-514: two keys. The release key signs the installer, manifest and
        # release and certifies the seed key; the seed key signs the body. The
        # test keeps them distinct so a direct check cannot pass by accident.
        self.rpriv, self.rpub = self._key(self.dir / "release")
        self.priv, self.pub = self._key(self.dir / "seed")
        self.server, self.token = self._service(self.dir / "seed.db", self.priv,
                                                self.rpriv, self.rpub)
        self.installer = self.dir / "installer.sh"
        self.installer.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$FAKE_INSTALLER_LOG"\n'
            'exit "${FAKE_INSTALLER_RC:-0}"\n')
        self.installer.chmod(0o755)
        self.log = self.dir / "installer.log"
        self.cmdline = self.dir / "cmdline"
        self.state = self.dir / "firstboot.json"
        self.cache = self.dir / "etc" / "seed.json"
        # ODL-472: the image carries a release/ directory; the first boot
        # places its installer and signed manifest pair before it runs.
        self.release = release_fixture(self.dir / "release", self.rpriv)
        self.installer_copy = self.dir / "bin" / "odile-install.sh"
        self.unit_target = self.dir / "systemd" / "odile-manifest.service"
        # The measured boot record's inputs (ODL-398 follow-up): the pin the
        # image was built from, and the first-visit state the machine writes.
        self.pin = self.dir / "tenant-image.pin"
        self.pin.write_text(json.dumps({"release": "2026.11.1"}), encoding="utf-8")
        self.boot_key_path = self.dir / "boot-key.pub"
        # ODL-523: the first boot runs `ssh-keygen -A` under --ssh-root, and
        # the boot record reads the key it wrote — the image's path, so the
        # record carries the key this machine generated, not a fixture.
        self.ssh_root = self.dir / "ssh-root"
        self.keygen = pathlib.Path(shutil.which("ssh-keygen") or "ssh-keygen")
        self.host_key_path = self.ssh_root / "etc" / "ssh" / "ssh_host_ed25519_key.pub"
        self.first_visit_state = self.dir / "etc" / "first-visit.json"
        # ODL-350: the gate host is not the image (the same rule as --ssh-root,
        # ODL-523). `bb.host` re-derives the machine's name into two files; the
        # image ships the build tenant's slug and the marker domain, so these
        # start as image bytes under this temp dir rather than at the machine's
        # own /etc.
        self.hostname = self.dir / "etc" / "hostname"
        self.hosts = self.dir / "etc" / "hosts"
        self.hostname.parent.mkdir(parents=True, exist_ok=True)
        self.hostname.write_text("odl-imagebuild\n", encoding="utf-8")
        self.hosts.write_text(
            "127.0.0.1 localhost odl-imagebuild tenant.odile.test\n"
            "10.0.2.15 odl-imagebuild tenant.odile.test\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    @staticmethod
    def _key(base: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl is missing")
        priv = base.with_suffix(".pem")
        pub = base.with_suffix(".pub")
        priv.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(priv)],
                       check=True, capture_output=True, timeout=60)
        os.chmod(priv, 0o600)
        subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)],
                       check=True, capture_output=True, timeout=60)
        return priv, pub

    def _identity(self, seed_key: pathlib.Path, release_key: pathlib.Path) -> tuple[bytes, bytes]:
        """(seed public key, the release key's certificate over those bytes)."""
        public = subprocess.run(["openssl", "pkey", "-in", str(seed_key), "-pubout"],
                                check=True, capture_output=True, timeout=60).stdout
        source = self.dir / "identity.pub"
        source.write_bytes(public)
        certificate = self.dir / "identity.crt"
        subprocess.run(["openssl", "dgst", "-sign", str(release_key), "-out",
                        str(certificate), str(source)],
                       check=True, capture_output=True, timeout=60)
        return public, certificate.read_bytes()

    def _service(self, db: pathlib.Path, key: pathlib.Path,
                 release_key: pathlib.Path | None = None,
                 release_pub: pathlib.Path | None = None):
        config = self.dir / "seed-config.json"
        config.write_text(json.dumps({
            "near_api_key": "bench-near-key", "model": "bench-model",
            "support_public_key": "ssh-ed25519 bench", "backup_repo_url": "sftp:bench",
            "hermes_version": "v0.23.0",
            # ODL-469: a seed config without the relay's PEM is refused now.
            "mail_relay": {"host": "h", "port": 587, "username": "u", "secret": "s",
                           "tls_ca": ("-----BEGIN CERTIFICATE-----\n"
                                      "MIIBhTCCASugAwIBAgIUTESTCA00000000000000000000\n"
                                      "-----END CERTIFICATE-----\n")},
        }))
        os.chmod(config, 0o600)
        con = self.seed.connect(str(db))
        try:
            self.seed.ensure_schema(con)
            con.execute("INSERT INTO tenants (slug, email, state, created_at)"
                        " VALUES (?, ?, 'invited', '2026-11-10T00:00:00Z')",
                        (self.SLUG, "bench@odile.test"))
            # ODL-925: the seed carries the account's verifier as sign_in_hash,
            # and the first boot refuses a seed without one.
            con.execute(
                "INSERT INTO accounts (slug, email, password_hash, confirm_token_hash,"
                " confirm_expires_at, trial_started_at, trial_ends_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.SLUG, "bench@odile.test",
                 "scrypt$16384$8$1$c2FsdA==$ZGs=", "0" * 64,
                 "2026-11-11T00:00:00Z", "2026-11-10T00:00:00Z",
                 "2026-11-24T00:00:00Z", "2026-11-10T00:00:00Z"))
            con.commit()
            # The first boot's record writes the tenant spine; the fixture HQ
            # database carries the same tables and one order at first_boot.
            from odilectl import state as state_module

            con.executescript(state_module.SCHEMA)
            con.execute(
                "UPDATE tenants SET state = 'provisioned', server_id = ? WHERE slug = ?",
                (self.IID, self.SLUG))
            con.execute(
                "INSERT INTO tenant_state (slug, state, server_id, initramfs_host_key,"
                " detail, created_at, updated_at)"
                " VALUES (?, 'first_boot', ?, ?, '{}', ?, ?)",
                (self.SLUG, self.IID, "SHA256:bench-initramfs",
                 "2026-11-10T00:00:00Z", "2026-11-10T00:00:00Z"))
            con.commit()
            binding = self.seed.bind(con, self.SLUG, self.IID)
        finally:
            con.close()

        class Quiet(self.seed.Handler):
            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

        server = self.seed.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        server.daemon_threads = True
        server.config = json.loads(config.read_text())
        server.config_path = str(config)
        server.rebuilds_path = str(self.dir / "seed-rebuilds.json")
        # ODL-924: the tenant's sub-account pair, read per exchange like the
        # rebuild entries. The fixture pair is the bench's own (inert) one, so
        # the seed's `backup` block and the first boot's path are proved
        # without a Storage Box.
        server.backup_pairs_path = str(self.dir / "seed-backup-pairs.json")
        self.seed.set_backup_pair(server.backup_pairs_path, self.SLUG,
                                  fixture.fixture_pair(self.SLUG))
        server.signing_key = str(key)
        if release_key is not None:
            # The service carries the pair it would load from disk: the seed
            # public key and the release key's certificate over it.
            server.identity = self._identity(key, release_key)
        server.db_path = str(db)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, binding["token"]

    def port(self) -> int:
        return self.server.server_address[1]

    def write_cmdline(self, token: str | None = None, instance: str = IID,
                      url: str | None = None) -> None:
        line = ["root=/dev/mapper/root_crypt", "bb.iid=" + instance,
                "bb.host=" + self.SLUG,
                "bb.key=" + base64.b64encode(BOOT_KEY_TEXT.encode()).decode()]
        if token is not None:
            line.append("odile.token=" + token)
        line.append("odile.seed_url=" + (url or "http://127.0.0.1:%d/seed" % self.port()))
        self.cmdline.write_text(" ".join(line) + "\n")

    def run_boot(self, *, installer: pathlib.Path | None = None,
                 pubkey: pathlib.Path | None = None, first_visit_port: int = 0,
                 release: pathlib.Path | None = None,
                 recovery: pathlib.Path | None = None,
                 recovery_seed: pathlib.Path | None = None) -> tuple[int, str]:
        argv = [
            "--cmdline", str(self.cmdline), "--state", str(self.state),
            "--seed-cache", str(self.cache), "--signing-key", str(pubkey or self.rpub),
            "--installer", str(installer or self.installer),
            "--first-visit-port", str(first_visit_port),
            "--log", str(self.dir / "installer-run.log"),
            "--release-dir", str(release or self.release),
            "--installer-copy", str(self.installer_copy),
            "--etc-dir", str(self.dir / "etc"),
            "--odile-lib", str(self.dir / "lib"),
            "--manifest-unit", str(self.unit_target),
            # ODL-523: the gate host is not the image: ssh-keygen writes under
            # this root, never the test host's /etc/ssh.
            "--ssh-root", str(self.ssh_root),
            "--ssh-keygen", str(self.keygen),
            "--boot-report", str(self.dir / "attest.json"),
            "--pin", str(self.pin),
            "--boot-key", str(self.boot_key_path),
            "--boot-record", str(self.dir / "boot-record.json"),
            "--host-key", str(self.host_key_path),
            "--first-visit-state", str(self.first_visit_state),
            # ODL-350: the first boot re-derives `bb.host` into these two
            # files. Left to their defaults they are the gate host's own
            # /etc/hostname and /etc/hosts — a bind mount on a CI runner
            # (EBUSY in write_atomic's replace) and a silent rename at HQ.
            "--hostname-path", str(self.hostname),
            "--hosts-path", str(self.hosts),
        ]
        # The argv the boot actually ran, so a test can assert which paths the
        # program was told to use instead of inferring them from what it wrote.
        self.last_argv = argv
        if recovery is not None:
            argv += ["--recovery", str(recovery)]
        if recovery_seed is not None:
            argv += ["--recovery-seed", str(recovery_seed)]
        output = io.StringIO()
        env = {"FAKE_INSTALLER_LOG": str(self.log)}
        # The gate host is not the image: no unit may be enabled on it.
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(firstboot, "SYSTEMD_DIR", str(self.dir / "no-systemd")), \
                contextlib.redirect_stdout(output):
            code = firstboot.main(argv)
        return code, output.getvalue()

    def ledger_used(self) -> str | None:
        con = self.seed.connect(str(self.dir / "seed.db"))
        try:
            row = con.execute("SELECT used_at FROM seed_tokens").fetchone()
            return row["used_at"]
        finally:
            con.close()

    # -- a rebuild: the seed carries the old repository password, sealed ------

    REBUILD_PASSWORD = "test-rebuild-password-2026-0001"

    def seal_rebuild(self, password: str, *, key_token: str | None = None) -> None:
        """Put a sealed rebuild entry in this fixture's seed config.

        `key_token` seals under another token than the one the cmdline carries;
        the entry's `token_hash` still matches this boot's token, so the seed
        is *served* and the guest has to refuse the tag — the stale-seal path.
        """
        entry = self.seed.seal_backup_password(password, key_token or self.token)
        entry["token_hash"] = hashlib.sha256(self.token.encode()).hexdigest()
        path = self.dir / "seed-rebuilds.json"
        path.write_text(json.dumps({self.SLUG: entry}), encoding="utf-8")
        os.chmod(path, 0o600)

    def capture_installer(self, recovery: pathlib.Path) -> pathlib.Path:
        """An installer that records the recovery file it sees when it starts."""
        proof = self.dir / "recovery-at-installer.txt"
        path = self.dir / "capture-installer.sh"
        path.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_INSTALLER_LOG\"\n"
            f"grep '^Backup password:' '{recovery}' > '{proof}' || exit 9\n")
        path.chmod(0o755)
        return path

    def test_a_rebuild_seed_writes_the_old_password_before_the_installer(self) -> None:
        self.write_cmdline(self.token)
        self.seal_rebuild(self.REBUILD_PASSWORD)
        recovery = self.dir / "etc" / "recovery.txt"
        code, output = self.run_boot(
            installer=self.capture_installer(recovery), recovery=recovery,
            recovery_seed=IMAGE / "recovery-seed.py")
        self.assertEqual(code, 0, output)
        self.assertIn("odile-firstboot: installed %s" % self.SLUG, output)
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
        text = recovery.read_text(encoding="utf-8")
        password = re.search(r"^Backup password:[ \t]*(\S+)[ \t]*$", text, re.M)
        self.assertIsNotNone(password, text)
        assert password is not None
        self.assertEqual(password.group(1), self.REBUILD_PASSWORD)
        self.assertIn("Tenant:              %s" % self.SLUG, text)
        self.assertIn("Instance:            %s" % self.IID, text)
        # The installer saw the old password already in place: the write is
        # before it runs, not after the first-visit unit's ExecStartPre.
        self.assertIn(self.REBUILD_PASSWORD,
                      (self.dir / "recovery-at-installer.txt").read_text(encoding="utf-8"))
        # The value appears in no argv and no log line.
        self.assertNotIn(self.REBUILD_PASSWORD,
                         (self.dir / "installer.log").read_text(encoding="utf-8"))
        self.assertNotIn(self.REBUILD_PASSWORD,
                         (self.dir / "installer-run.log").read_text(encoding="utf-8"))

    def test_a_first_install_seed_leaves_the_recovery_file_to_the_unit(self) -> None:
        self.write_cmdline(self.token)
        recovery = self.dir / "etc" / "recovery.txt"
        code, output = self.run_boot(recovery=recovery,
                                     recovery_seed=IMAGE / "recovery-seed.py")
        self.assertEqual(code, 0, output)
        self.assertFalse(recovery.exists(),
                         "a first install mints its password in recovery-seed.py, not here")

    def test_a_seed_sealed_for_another_token_fails_closed(self) -> None:
        self.write_cmdline(self.token)
        self.seal_rebuild(self.REBUILD_PASSWORD, key_token="d4" * 32)
        recovery = self.dir / "etc" / "recovery.txt"
        code, output = self.run_boot(recovery=recovery,
                                     recovery_seed=IMAGE / "recovery-seed.py")
        self.assertEqual(code, 0, output)
        self.assertEqual(firstboot_line(output),
                         "odile-firstboot: fail-closed: the seed's rebuild "
                         "password does not open with this setup token")
        self.assertFalse(recovery.exists())
        self.assertFalse(self.log.exists(), "the installer must not run")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertFalse(state["ok"])

    def test_the_first_boot_exchanges_installs_and_records_the_state(self) -> None:
        self.write_cmdline(self.token)
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        lines = console_lines(output)
        self.assertEqual(len(lines), 2, output)  # one hostkeys line, one firstboot line
        self.assertTrue(lines[0].startswith("odile-hostkeys: create "), output)
        self.assertTrue(lines[1].startswith("odile-firstboot: installed %s" % self.SLUG), output)
        self.assertIn("first-visit not checked", lines[1])

        cache = json.loads(self.cache.read_text())
        self.assertEqual(cache["slug"], self.SLUG)
        self.assertEqual(cache["instance_id"], self.IID)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        # The release installer's cache contract: the exact signed bytes and a
        # base64 signature beside them, both 0600, verifying against the key
        # the image carries.
        sig_path = self.cache.with_suffix(".sig")
        self.assertTrue(sig_path.is_file())
        self.assertEqual(sig_path.stat().st_mode & 0o777, 0o600)
        signature = base64.b64decode(sig_path.read_text().strip(), validate=True)
        self.assertEqual(len(signature), 64)
        with tempfile.NamedTemporaryFile() as sig_file:
            sig_file.write(signature)
            sig_file.flush()
            verified = subprocess.run(
                ["openssl", "dgst", "-verify", str(self.pub), "-signature", sig_file.name],
                input=self.cache.read_bytes(), capture_output=True, timeout=30)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        # ODL-514: the body is signed by the seed key, not the release key. The
        # release key signs the certificate over the seed public key; a direct
        # check of the body against the release key must fail, so this test
        # cannot pass with the old one-key model.
        seed_public = subprocess.run(["openssl", "pkey", "-in", str(self.priv), "-pubout"],
                                     check=True, capture_output=True, timeout=60).stdout
        self.assertEqual(cache["signing_key"], seed_public.decode("ascii"))
        self.assertEqual(cache["signing_key_sha256"], hashlib.sha256(seed_public).hexdigest())
        certificate = base64.b64decode(cache["signing_key_cert"], validate=True)
        self.assertEqual(len(certificate), 64)
        with tempfile.TemporaryDirectory() as cert_tmp:
            cert_dir = pathlib.Path(cert_tmp)
            (cert_dir / "seed.pub").write_bytes(seed_public)
            (cert_dir / "cert.bin").write_bytes(certificate)
            cert_ok = subprocess.run(
                ["openssl", "dgst", "-verify", str(self.rpub), "-signature",
                 str(cert_dir / "cert.bin"), str(cert_dir / "seed.pub")],
                capture_output=True, timeout=30)
            self.assertEqual(cert_ok.returncode, 0, cert_ok.stderr)
        with tempfile.NamedTemporaryFile() as sig_file:
            sig_file.write(signature)
            sig_file.flush()
            not_release = subprocess.run(
                ["openssl", "dgst", "-verify", str(self.rpub), "-signature", sig_file.name],
                input=self.cache.read_bytes(), capture_output=True, timeout=30)
        self.assertNotEqual(not_release.returncode, 0, "the body must not verify against the release key")
        state = json.loads(self.state.read_text())
        self.assertTrue(state["ok"])
        self.assertEqual(state["seed_sha256"],
                         hashlib.sha256(self.cache.read_bytes()).hexdigest())
        first_visit = json.loads((self.dir / "etc" / "first-visit.json").read_text())
        self.assertEqual(first_visit["token_sha256"],
                         hashlib.sha256(self.token.encode()).hexdigest())
        self.assertNotIn(self.token, (self.dir / "etc" / "first-visit.json").read_text())
        calls = self.log.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("--seed " + str(self.cache), calls[0])
        self.assertIn("--first-visit-token " + self.token, calls[0])
        self.assertIsNotNone(self.ledger_used())

    def test_the_first_boot_reports_the_measured_key_and_the_pin(self) -> None:
        self.write_cmdline(self.token)
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        self.assertIn("record 2026.11.1 pending key SHA256:", output)
        con = self.seed.connect(str(self.dir / "seed.db"))
        con.row_factory = self.seed.sqlite3.Row
        try:
            tenant = con.execute(
                "SELECT state, ssh_public_key, release FROM tenants WHERE slug = ?",
                (self.SLUG,)).fetchone()
            spine = con.execute(
                "SELECT state, release, initramfs_host_key, detail FROM tenant_state"
                " WHERE slug = ?", (self.SLUG,)).fetchone()
            events = con.execute(
                "SELECT kind, from_state, to_state FROM tenant_state_events"
                " WHERE slug = ? ORDER BY id", (self.SLUG,)).fetchall()
        finally:
            con.close()
        self.assertEqual(tenant["ssh_public_key"], BOOT_KEY_TEXT)
        self.assertEqual(tenant["release"], "2026.11.1")
        self.assertEqual(tenant["state"], "installed")
        self.assertEqual(spine["state"], "first_visit")
        self.assertEqual(spine["release"], "2026.11.1")
        self.assertIsNone(spine["initramfs_host_key"])
        detail = json.loads(spine["detail"])
        self.assertEqual(detail["boot_key"], BOOT_KEY_TEXT)
        self.assertEqual(detail["first_visit"], "pending")
        # ODL-523: the record carries the key THIS boot generated, read back
        # through ssh-keygen — not a fixture and not the image's (the image has
        # none). This is the ordering assertion at the behaviour level.
        listed = subprocess.run(["ssh-keygen", "-lf", str(self.host_key_path)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(detail["host_key"], listed.stdout.split()[1])
        self.assertTrue(self.host_key_path.is_file(), self.host_key_path)
        log = (self.dir / "installer-run.log").read_text(encoding="utf-8")
        self.assertIn("ssh host keys", log)
        self.assertIn("create", log)
        self.assertEqual([(row["kind"], row["from_state"], row["to_state"])
                          for row in events],
                         [("first_visit", "first_boot", "first_visit")])

    def test_the_seed_carries_the_pair_and_the_rebuild_marker(self) -> None:
        """ODL-924: the guest gets the repository URL, both keys, the host key."""
        self.write_cmdline(self.token)
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        cache = json.loads(self.cache.read_text())
        pair = fixture.fixture_pair(self.SLUG)
        self.assertEqual(cache["backup_repo_url"], pair["repo_url"])
        self.assertEqual(cache["backup"]["repo_url"], pair["repo_url"])
        self.assertEqual(cache["backup"]["readonly_repo_url"], pair["readonly_repo_url"])
        self.assertEqual(cache["backup"]["subaccount_username"], pair["subaccount_username"])
        self.assertEqual(cache["backup"]["readonly_subaccount_id"],
                         pair["readonly_subaccount_id"])
        self.assertEqual(cache["backup"]["known_hosts"], pair["known_hosts"])
        self.assertTrue(cache["backup"]["ssh_private_key"].startswith("-----BEGIN"))
        self.assertTrue(cache["backup"]["readonly_ssh_private_key"].startswith("-----BEGIN"))
        self.assertEqual(cache["backup"]["previous_instance"], "")
        self.assertNotIn(":22/", cache["backup_repo_url"])

        # The row's own marker (written at a reopen) wins over the pair's.
        con = self.seed.connect(str(self.dir / "seed.db"))
        try:
            con.execute("UPDATE tenant_state SET detail = ? WHERE slug = ?",
                        (json.dumps({"previous_instance": "srv-old-0001"}), self.SLUG))
            con.commit()
            binding = self.seed.bind(con, self.SLUG, self.IID)
        finally:
            con.close()
        self.write_cmdline(binding["token"])
        if self.cache.is_file():
            self.cache.unlink()   # force a fresh exchange, not the first boot's cache
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        cache = json.loads(self.cache.read_text())
        self.assertEqual(cache["backup"]["previous_instance"], "srv-old-0001")

    def test_a_reboot_after_the_first_visit_reports_ready_and_keeps_one_line(self) -> None:
        self.write_cmdline(self.token)
        self.assertEqual(self.run_boot()[0], 0)
        state = json.loads(self.first_visit_state.read_text())
        state["signin_set_at"] = "2026-11-12T14:57:46Z"
        self.first_visit_state.write_text(json.dumps(state), encoding="utf-8")
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        self.assertEqual(firstboot_line(output), "odile-firstboot: same")
        con = self.seed.connect(str(self.dir / "seed.db"))
        con.row_factory = self.seed.sqlite3.Row
        try:
            spine = con.execute(
                "SELECT state, detail FROM tenant_state WHERE slug = ?",
                (self.SLUG,)).fetchone()
        finally:
            con.close()
        self.assertEqual(spine["state"], "ready")
        self.assertEqual(json.loads(spine["detail"])["first_visit"], "done")
        log = (self.dir / "installer-run.log").read_text()
        self.assertIn("record      2026.11.1 done key SHA256:", log)

    def test_a_missing_pin_skips_the_record_and_keeps_the_old_row(self) -> None:
        self.pin.unlink()
        self.write_cmdline(self.token)
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        self.assertIn("record skipped (no pinned release", output)
        con = self.seed.connect(str(self.dir / "seed.db"))
        con.row_factory = self.seed.sqlite3.Row
        try:
            tenant = con.execute(
                "SELECT ssh_public_key, release FROM tenants WHERE slug = ?",
                (self.SLUG,)).fetchone()
            spine = con.execute(
                "SELECT state, initramfs_host_key FROM tenant_state WHERE slug = ?",
                (self.SLUG,)).fetchone()
        finally:
            con.close()
        self.assertIsNone(tenant["ssh_public_key"])
        self.assertIsNone(tenant["release"])
        self.assertEqual(spine["state"], "first_boot")
        self.assertEqual(spine["initramfs_host_key"], "SHA256:bench-initramfs")

    def test_a_key_file_that_disagrees_with_the_measured_line_posts_the_record(self) -> None:
        other = (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL7Tzl2nYSnGVbP/Vfp9wJwD7q2Wt2mJKT5p8g4oWn7Z "
            "odile-other"
        )
        self.boot_key_path.write_text(other + "\n", encoding="utf-8")
        self.write_cmdline(self.token)
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        self.assertNotIn("record skipped", output)
        self.assertIn("record 2026.11.1 pending key SHA256:", output)
        self.assertIn("refreshed", output)
        self.assertEqual(self.boot_key_path.read_text(encoding="utf-8"),
                         BOOT_KEY_TEXT + "\n")
        con = self.seed.connect(str(self.dir / "seed.db"))
        con.row_factory = self.seed.sqlite3.Row
        try:
            spine = con.execute(
                "SELECT state, detail FROM tenant_state WHERE slug = ?",
                (self.SLUG,)).fetchone()
        finally:
            con.close()
        self.assertEqual(spine["state"], "first_visit")
        self.assertEqual(json.loads(spine["detail"])["boot_key"], BOOT_KEY_TEXT)


    def test_a_second_boot_is_same_and_calls_no_installer(self) -> None:
        self.write_cmdline(self.token)
        self.assertEqual(self.run_boot()[0], 0)
        first_boot_at = json.loads(self.state.read_text())["first_boot_at"]
        time.sleep(1.1)
        code, output = self.run_boot()
        self.assertEqual(code, 0)
        self.assertEqual(firstboot_line(output), "odile-firstboot: same")
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertEqual(json.loads(self.state.read_text())["first_boot_at"], first_boot_at)

    def test_check_reports_installed_only_after_an_install(self) -> None:
        argv = ["--cmdline", str(self.cmdline), "--state", str(self.state),
                "--seed-cache", str(self.cache), "--signing-key", str(self.rpub),
                "--installer", str(self.installer), "--log", str(self.dir / "log"),
                "--check"]
        with mock.patch.dict(os.environ, {"FAKE_INSTALLER_LOG": str(self.log)}):
            self.assertEqual(firstboot.main(argv), 1)
            self.write_cmdline(self.token)
            self.run_boot()
            self.assertEqual(firstboot.main(argv), 0)

    def test_a_missing_token_fails_closed_with_one_firstboot_line(self) -> None:
        self.write_cmdline(None)
        code, output = self.run_boot()
        self.assertEqual(code, 0)
        self.assertEqual(firstboot_line(output),
                         "odile-firstboot: fail-closed: no setup token in the command line")
        self.assertFalse(self.cache.exists())

    # -- ODL-350: the gate host is not the image ------------------------------

    @staticmethod
    def option(argv: list[str], flag: str) -> str:
        if flag not in argv:
            raise AssertionError("%s is not in the boot argv: %r" % (flag, argv))
        return argv[argv.index(flag) + 1]

    def test_the_first_boot_names_the_host_inside_the_temp_dir(self) -> None:
        """`bb.host` re-derives the name into this dir, not the gate host's /etc.

        The program defaults both paths to `/etc`. On a runner `/etc/hostname`
        is a bind mount, so the rename behind `write_atomic` fails with EBUSY
        (run 45318362637); at HQ the same default silently renamed the box.
        """
        self.write_cmdline(self.token)
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.option(self.last_argv, "--hostname-path"),
                         str(self.hostname))
        self.assertEqual(self.option(self.last_argv, "--hosts-path"),
                         str(self.hosts))
        self.assertEqual(self.hostname.read_text(encoding="utf-8"),
                         self.SLUG + "\n")
        hosts = self.hosts.read_text(encoding="utf-8")
        self.assertIn(" %s tenant.odile.test" % self.SLUG, hosts)
        self.assertNotIn("odl-imagebuild", hosts)

    def test_the_first_boot_writes_nothing_outside_its_temp_dir(self) -> None:
        """No path the exchange hands `write_atomic` leaves this temp dir.

        `write_atomic` is the writer behind the leak: it renamed the gate
        host's own `/etc/hostname` through `os.replace`, and on the runner the
        bind mount made that rename fail with EBUSY. Recording every path it is
        handed names exactly the writes the argv omission misplaced.
        """
        self.write_cmdline(self.token)
        writes: list[str] = []
        real = firstboot.write_atomic

        def record(path, data, mode):
            writes.append(str(path))
            return real(path, data, mode)

        with mock.patch.object(firstboot, "write_atomic", record):
            code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        self.assertIn(str(self.hostname), writes)
        self.assertIn(str(self.hosts), writes)
        outside = [path for path in writes
                   if not pathlib.Path(path).is_relative_to(self.dir)]
        self.assertEqual(outside, [], writes)

    def test_a_used_token_fails_closed_and_the_machine_is_not_half_installed(self) -> None:
        self.write_cmdline(self.token)
        self.assertEqual(self.run_boot()[0], 0)
        self.state.unlink()
        self.cache.unlink()
        code, output = self.run_boot()
        self.assertEqual(code, 0)
        self.assertEqual(firstboot_line(output), "odile-firstboot: fail-closed: seed token used")
        self.assertFalse(self.cache.exists())
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_a_token_bound_to_another_instance_fails_closed(self) -> None:
        self.write_cmdline(self.token, instance="some-other-instance")
        code, output = self.run_boot()
        self.assertEqual(code, 0)
        self.assertIn("bound to another instance", output)
        self.assertFalse(self.cache.exists())

    def test_a_certificate_from_another_key_fails_closed(self) -> None:
        other_priv, other_pub = self._key(self.dir / "k2")
        other_release = release_fixture(self.dir / "release-other", other_priv)
        self.write_cmdline(self.token)
        code, output = self.run_boot(pubkey=other_pub, release=other_release)
        self.assertEqual(code, 0)
        self.assertIn("seed signing key certificate was not signed by", output)
        self.assertIn(str(other_pub), output)
        self.assertFalse(self.cache.exists())
        # The service signs after it spends the token (seed.redeem's UPDATE is
        # the single-use gate), so the exchange consumed it even though this
        # guest could not verify the answer. What matters here: no cache, no
        # install, one line, and an operator rotates rather than retries.
        self.assertIsNotNone(self.ledger_used())

    @staticmethod
    def _signed(body: dict, key: pathlib.Path) -> tuple[bytes, bytes]:
        raw = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
        signature = subprocess.run(
            ["openssl", "dgst", "-sign", str(key), "-out", "/dev/stdout"],
            input=raw, check=True, capture_output=True, timeout=60).stdout
        return raw, signature

    def test_a_legacy_seed_without_a_certificate_verifies_against_the_release_key(self) -> None:
        """An installed tenant's seed carries no certificate; it must still open."""
        body = {"slug": self.SLUG, "instance_id": self.IID}
        raw, signature = self._signed(body, self.rpriv)
        self.assertEqual(firstboot.verify_seed_body(raw, signature, str(self.rpub)), body)
        # The same body signed by another key is refused, one line.
        raw, signature = self._signed(body, self.priv)
        with self.assertRaises(firstboot.Refusal) as caught:
            firstboot.verify_seed_body(raw, signature, str(self.rpub))
        self.assertEqual(caught.exception.reason, "seed signature does not verify")

    def test_a_certificate_signed_by_the_retired_key_is_accepted(self) -> None:
        """The one-release key move: the prev pub beside the current one verifies."""
        prev_priv, prev_pub = self._key(self.dir / "prev")
        public, certificate = self._identity(self.priv, prev_priv)
        body = {
            "signing_key": public.decode("ascii"),
            "signing_key_sha256": hashlib.sha256(public).hexdigest(),
            "signing_key_cert": base64.b64encode(certificate).decode("ascii"),
        }
        raw, signature = self._signed(body, self.priv)
        current = self.dir / "odile-signing-key.pub"
        current.write_bytes(self.rpub.read_bytes())
        (self.dir / "odile-signing-key.prev.pub").write_bytes(prev_pub.read_bytes())
        self.assertEqual(firstboot.verify_seed_body(raw, signature, str(current)), body)
        # Without the retired half beside it, the same certificate is refused.
        (self.dir / "odile-signing-key.prev.pub").unlink()
        with self.assertRaises(firstboot.Refusal) as caught:
            firstboot.verify_seed_body(raw, signature, str(current))
        self.assertIn("certificate was not signed by", caught.exception.reason)

    def test_a_failed_installer_retries_from_the_cache(self) -> None:
        self.write_cmdline(self.token)
        with mock.patch.dict(os.environ, {"FAKE_INSTALLER_RC": "1"}):
            code, output = self.run_boot()
        self.assertEqual(code, 0)
        self.assertIn("fail-closed: installer failed (rc 1)", output)
        self.assertTrue(self.cache.exists())
        # The token is spent; the retry must use the cache, not /seed (410).
        code, output = self.run_boot()
        self.assertEqual(code, 0)
        self.assertTrue(firstboot_line(output).startswith(
            "odile-firstboot: installed %s" % self.SLUG), output)
        # ODL-523: the retried boot finds this machine's own keys and says so.
        self.assertTrue(console_lines(output)[0].startswith("odile-hostkeys: same "), output)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)


    def test_the_first_boot_places_the_installer_and_the_signed_pair_before_it_installs(self) -> None:
        self.write_cmdline(self.token)
        code, output = self.run_boot()
        self.assertEqual(code, 0, output)
        self.assertIn("installed %s" % self.SLUG, output)
        placed = self.installer_copy.read_bytes()
        self.assertEqual(placed, (self.release / "odile_install.py").read_bytes())
        self.assertEqual(self.installer_copy.stat().st_mode & 0o777, 0o755)
        manifest = self.dir / "etc" / "manifest.json"
        self.assertEqual(manifest.read_bytes(), (self.release / "manifest.json").read_bytes())
        self.assertEqual((self.dir / "etc" / "manifest.json.sig").read_bytes(),
                         (self.release / "manifest.json.sig").read_bytes())
        self.assertEqual(hashlib.sha256(placed).hexdigest(),
                         json.loads(manifest.read_text())["installer_sha256"])
        self.assertTrue((self.dir / "lib" / "manifest.py").is_file())
        self.assertTrue((self.dir / "lib" / "odile-manifest.py").is_file())
        self.assertTrue(self.unit_target.is_file())
        # The placement log is the record a second boot is compared against.
        log = (self.dir / "installer-run.log").read_text()
        self.assertTrue(any(line.split()[0] == "install" and str(self.installer_copy) in line
                            for line in log.splitlines()), log)
        self.assertTrue(any(line.split()[0] == "install" and str(manifest) in line
                            for line in log.splitlines()), log)

    def test_the_second_boot_places_the_same_bytes_and_logs_same(self) -> None:
        self.write_cmdline(self.token)
        self.assertEqual(self.run_boot()[0], 0)
        first_log = (self.dir / "installer-run.log").read_text()
        code, output = self.run_boot()
        self.assertEqual(code, 0)
        self.assertEqual(firstboot_line(output), "odile-firstboot: same")
        second_log = (self.dir / "installer-run.log").read_text()[len(first_log):]
        self.assertTrue(any(line.split()[0] == "same" and str(self.installer_copy) in line
                            for line in second_log.splitlines()), second_log)
        self.assertTrue(any(line.split()[0] == "same" and str(self.dir / "etc" / "manifest.json") in line
                            for line in second_log.splitlines()), second_log)


class ReleasePlacementTests(unittest.TestCase):
    """ODL-472: the image's release becomes the machine's, idempotently."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.priv, self.pub = ed25519_key(self.dir / "k")
        self.release = release_fixture(self.dir / "release", self.priv)
        self.installer = self.dir / "bin" / "odile-install.sh"
        self.etc = self.dir / "etc"
        self.lib = self.dir / "lib" / "odile"
        self.unit = self.dir / "systemd" / "odile-manifest.service"
        self.log = self.dir / "firstboot.log"
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def systemctl(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if argv[:1] == ["is-active"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def kwargs(self, **overrides) -> dict:
        values = dict(
            release_dir=str(self.release),
            installer_target=str(self.installer),
            etc_dir=str(self.etc),
            lib_dir=str(self.lib),
            unit_target=str(self.unit),
            signing_key=str(self.pub),
            log_path=str(self.log),
        )
        values.update(overrides)
        return values

    def place(self, **overrides) -> list[str]:
        return firstboot.place_release(systemctl=self.systemctl, **self.kwargs(**overrides))

    def paths(self) -> list[pathlib.Path]:
        return [
            self.installer,
            self.etc / "manifest.json",
            self.etc / "manifest.json.sig",
            self.lib / "manifest.py",
            self.lib / "odile-manifest.py",
            self.unit,
        ]

    def test_the_release_installer_and_the_signed_pair_land_byte_identical(self) -> None:
        lines = self.place()
        self.assertEqual(self.installer.read_bytes(), (self.release / "odile_install.py").read_bytes())
        self.assertEqual(self.installer.stat().st_mode & 0o777, 0o755)
        manifest = self.etc / "manifest.json"
        self.assertEqual(manifest.read_bytes(), (self.release / "manifest.json").read_bytes())
        self.assertEqual((self.etc / "manifest.json.sig").read_bytes(),
                         (self.release / "manifest.json.sig").read_bytes())
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.etc / "manifest.json.sig").stat().st_mode & 0o777, 0o644)
        # The relation CLAIMS 4.6 reads: the placed installer is the one the
        # placed manifest names.
        self.assertEqual(hashlib.sha256(self.installer.read_bytes()).hexdigest(),
                         json.loads(manifest.read_text())["installer_sha256"])
        self.assertEqual((self.lib / "manifest.py").read_bytes(), RELEASE_LISTENER)
        self.assertEqual((self.lib / "manifest.py").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.lib / "odile-manifest.py").read_bytes(),
                         MANIFEST_CHECKER.read_bytes())
        self.assertEqual(self.unit.read_bytes(), (IMAGE / "odile-manifest.service").read_bytes())
        self.assertEqual(self.unit.stat().st_mode & 0o777, 0o644)
        self.assertTrue(any(line.startswith("install") and str(self.installer) in line
                            for line in lines), lines)
        # The listener is started through systemd, not left on disk.
        self.assertIn(["enable", "odile-manifest.service"], self.calls)
        self.assertIn(["start", "odile-manifest.service"], self.calls)
        self.assertIn(["daemon-reload"], self.calls)
        self.assertTrue((self.dir / "firstboot.log").is_file())

    def test_a_second_placement_is_same_and_rewrites_nothing(self) -> None:
        self.place()
        before = {path: path.stat().st_mtime_ns for path in self.paths()}
        self.calls.clear()
        lines = self.place()
        after = {path: path.stat().st_mtime_ns for path in self.paths()}
        self.assertEqual(before, after, "the second placement rewrote a file")
        verbs = [line.split()[0] for line in lines]
        self.assertTrue(set(verbs) <= {"same", "verify"}, lines)
        self.assertNotIn(["daemon-reload"], self.calls, "the unit was unchanged")
        self.assertIn(["start", "odile-manifest.service"], self.calls)

    def test_a_machine_with_its_own_pair_keeps_it(self) -> None:
        self.place()
        newer = b"#!/usr/bin/python3\nprint('newer')\n"
        other = release_fixture(self.dir / "other", self.priv, installer=newer)
        # `odile_update.py apply` writes the installer and the pair together
        # (update/odile_update.py apply_change, write_records).
        self.installer.write_bytes((other / "odile_install.py").read_bytes())
        os.chmod(self.installer, 0o755)
        (self.etc / "manifest.json").write_bytes((other / "manifest.json").read_bytes())
        (self.etc / "manifest.json.sig").write_bytes((other / "manifest.json.sig").read_bytes())
        lines = self.place()
        self.assertEqual((self.etc / "manifest.json").read_bytes(),
                         (other / "manifest.json").read_bytes(),
                         "the machine's own manifest was overwritten")
        self.assertEqual(self.installer.read_bytes(), newer,
                         "the installer the machine's own pair names was overwritten")
        # The relation CLAIMS 4.6 reads, on a machine that was updated: the
        # placed installer is the one the kept manifest names.
        self.assertEqual(hashlib.sha256(self.installer.read_bytes()).hexdigest(),
                         json.loads((other / "manifest.json").read_text())["installer_sha256"])
        self.assertTrue(any(line.startswith("keep") and "manifest.json" in line
                            for line in lines), lines)
        self.assertTrue(any(line.startswith("keep") and str(self.installer) in line
                            for line in lines), lines)
        # And it converges: a third placement rewrites neither file.
        before = {path: path.stat().st_mtime_ns for path in self.paths()}
        self.place()
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in self.paths()},
                         "the kept installer or pair was rewritten")

    def test_a_machine_pair_that_does_not_name_the_installer_fails_closed(self) -> None:
        self.place()
        other = release_fixture(self.dir / "other", self.priv,
                                installer=b"#!/usr/bin/python3\nprint('newer')\n")
        (self.etc / "manifest.json").write_bytes((other / "manifest.json").read_bytes())
        (self.etc / "manifest.json.sig").write_bytes((other / "manifest.json.sig").read_bytes())
        for state, want in (("mismatched", RELEASE_INSTALLER), ("missing", None)):
            with self.subTest(state=state):
                if state == "missing":
                    self.installer.unlink()
                with self.assertRaises(firstboot.Refusal) as caught:
                    self.place()
                self.assertIn("manifest does not name", caught.exception.reason)
                self.assertEqual((self.etc / "manifest.json").read_bytes(),
                                 (other / "manifest.json").read_bytes(),
                                 "the pair was overwritten before the refusal")
                if want is None:
                    self.assertFalse(self.installer.exists(),
                                     "an installer the pair does not name was placed")
                else:
                    self.assertEqual(self.installer.read_bytes(), want,
                                     "the refused boot rewrote the installer")

    def test_a_tampered_image_pair_fails_closed_before_anything_is_written(self) -> None:
        with open(self.release / "manifest.json", "ab") as handle:
            handle.write(b"tampered\n")
        with self.assertRaises(firstboot.Refusal) as caught:
            self.place()
        self.assertIn("manifest signature does not verify", caught.exception.reason)
        self.assertFalse(self.installer.exists(), "the installer was placed before the check")
        self.assertFalse((self.etc / "manifest.json").exists())
        self.assertEqual(self.calls, [], "a unit was touched for a pair that does not verify")

    def test_a_tampered_installed_pair_fails_closed(self) -> None:
        self.place()
        with open(self.etc / "manifest.json", "ab") as handle:
            handle.write(b"tampered\n")
        with self.assertRaises(firstboot.Refusal) as caught:
            self.place()
        self.assertIn("manifest signature does not verify", caught.exception.reason)

    def test_a_missing_release_artifact_is_named(self) -> None:
        (self.release / "odile_install.py").unlink()
        with self.assertRaises(firstboot.Refusal) as caught:
            self.place()
        self.assertIn("release installer", caught.exception.reason)

    def test_a_chroot_places_files_and_starts_no_unit(self) -> None:
        lines = firstboot.place_release(**self.kwargs())
        self.assertTrue(self.installer.is_file())
        self.assertTrue(self.unit.is_file())
        self.assertFalse(any("odile-manifest.service" in " ".join(line.split()) for line in lines
                             if line.startswith("start")), lines)


class ManifestCheckerTests(unittest.TestCase):
    """The gate the unit runs: verify the release pair, then serve its bytes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.priv, self.pub = ed25519_key(self.dir / "k")
        self.release = release_fixture(self.dir / "release", self.priv)
        self.manifest = self.release / "manifest.json"
        self.signature = self.release / "manifest.json.sig"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_check(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(MANIFEST_CHECKER), "--check",
             "--manifest", str(self.manifest), "--signature", str(self.signature),
             "--pubkey", str(self.pub)],
            capture_output=True, text=True, timeout=60)

    def test_the_checker_accepts_the_signed_pair(self) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)
        self.assertIn(json.loads(self.manifest.read_text())["installer_sha256"][:12],
                      result.stdout)

    def test_the_checker_refuses_a_tampered_manifest(self) -> None:
        with open(self.manifest, "ab") as handle:
            handle.write(b"tampered\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("does not verify", result.stdout + result.stderr)

    def test_the_checker_refuses_a_signature_that_is_not_base64(self) -> None:
        self.signature.write_text("not base64 at all\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not base64", result.stdout + result.stderr)

    def test_the_checker_refuses_a_missing_pubkey(self) -> None:
        (self.dir / "k.pub").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("signing key", result.stdout + result.stderr)

    def serve(self) -> tuple[subprocess.Popen, int]:
        process = subprocess.Popen(
            [sys.executable, str(MANIFEST_CHECKER), "serve",
             "--manifest", str(self.manifest), "--signature", str(self.signature),
             "--pubkey", str(self.pub), "--program", str(ROOT / "scripts" / "manifest.py"),
             "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert process.stdout is not None
        # The checker prints its verification line, then execs the listener
        # which prints the bound port.
        line = process.stdout.readline()
        if "listening" not in line:
            self.assertIn("OK", line, line)
            line = process.stdout.readline()
        self.assertIn("listening", line, line)
        port = int(line.rsplit(":", 1)[1].split()[0])
        return process, port

    def test_serve_serves_the_signed_bytes_with_no_store(self) -> None:
        process, port = self.serve()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            connection.request("GET", "/manifest")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertEqual(body, self.manifest.read_bytes())
            connection.close()
        finally:
            process.terminate()
            process.wait(timeout=30)

    def test_serve_refuses_a_tampered_pair_and_never_listens(self) -> None:
        with open(self.manifest, "ab") as handle:
            handle.write(b"tampered\n")
        result = subprocess.run(
            [sys.executable, str(MANIFEST_CHECKER), "serve",
             "--manifest", str(self.manifest), "--signature", str(self.signature),
             "--pubkey", str(self.pub), "--program", str(ROOT / "scripts" / "manifest.py"),
             "--port", "0"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("listening", result.stdout)
        self.assertIn("does not verify", result.stdout + result.stderr)


class ManifestUnitTests(unittest.TestCase):
    """The unit the first boot installs: check first, then listen on loopback."""

    def test_the_unit_checks_the_pair_before_the_listener_starts(self) -> None:
        unit = (IMAGE / "odile-manifest.service").read_text(encoding="utf-8")
        self.assertIn("ExecStartPre=/usr/bin/python3 /usr/local/lib/odile/odile-manifest.py --check", unit)
        self.assertIn("--manifest /etc/odile/manifest.json", unit)
        self.assertIn("--signature /etc/odile/manifest.json.sig", unit)
        self.assertIn("--pubkey /etc/odile/odile-signing-key.pub", unit)
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/lib/odile/odile-manifest.py serve", unit)
        self.assertIn("--program /usr/local/lib/odile/manifest.py", unit)
        self.assertIn("--bind 127.0.0.1", unit)
        self.assertIn("--port 8012", unit)
        self.assertIn("DynamicUser=yes", unit)
        self.assertLess(unit.index("ExecStartPre="), unit.index("ExecStart="))

    def test_the_bench_proxy_placeholder_has_a_substitution(self) -> None:
        installer = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        for name in ("odile-bench-proxy-manifest.socket", "odile-bench-proxy-manifest.service"):
            text = (IMAGE / name).read_text(encoding="utf-8")
            for placeholder in sorted(set(re.findall(r"__[A-Z_]+__", text))):
                self.assertIn(f"s|{placeholder}|", installer,
                              f"{name}: {placeholder} has no substitution in install-stack.sh")

    def test_the_listener_program_is_the_release_copy_of_manifest_py(self) -> None:
        # The first boot installs the image's copy under the name the unit
        # runs; the image build copies scripts/manifest.py there (sealed-image).
        build = (IMAGE / "sealed-image.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/manifest.py", build)
        self.assertIn("odile-manifest.py", build)
        self.assertIn("odile-manifest.service", build)


class ManifestBenchTests(unittest.TestCase):
    """The host's read of /manifest through the forwarded guest port."""

    @staticmethod
    def serve(body: bytes, cache: str | None = "no-store") -> http.server.ThreadingHTTPServer:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib signature
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if cache:
                    self.send_header("Cache-Control", cache)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_the_served_bytes_are_the_signed_bytes(self) -> None:
        payload = b'{"schema": 1}\n'
        server = self.serve(payload)
        try:
            check = bench.manifest_served(server.server_address[1], payload, timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(check.ok, check.detail)
        self.assertIn("no-store", check.detail)

    def test_a_different_body_is_not_the_signed_pair(self) -> None:
        server = self.serve(b'{"schema": 2}\n')
        try:
            check = bench.manifest_served(server.server_address[1], b'{"schema": 1}\n', timeout=0.3)
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(check.ok)
        self.assertIn("different", check.detail)

    def test_a_missing_no_store_header_fails(self) -> None:
        payload = b"{}\n"
        server = self.serve(payload, cache=None)
        try:
            check = bench.manifest_served(server.server_address[1], payload, timeout=0.3)
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(check.ok)
        self.assertIn("cache-control", check.detail)

    def test_a_refused_manifest_is_a_pass_only_when_nothing_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.server_close()  # a port nothing listens on
        check = bench.manifest_refused(port, attempts=1)
        self.assertTrue(check.ok, check.detail)

        payload = b"{}\n"
        server = self.serve(payload)
        try:
            check = bench.manifest_refused(server.server_address[1], attempts=1)
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(check.ok)
        self.assertIn("200", check.detail)

    def test_the_uki_boot_forwards_the_manifest_port(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_popen(command, **kwargs):  # noqa: ANN001 - subprocess API
            captured["command"] = command
            return mock.MagicMock()

        with mock.patch.object(bench.subprocess, "Popen", side_effect=fake_popen):
            bench.boot_qemu_uki(
                pathlib.Path("/var/tmp/x.raw"), pathlib.Path("/var/tmp/x.efi"), pathlib.Path("/var/tmp/OVMF.fd"),
                pathlib.Path("/var/tmp/boot.log"), pathlib.Path("/var/tmp/bench.log"), 19132, 2048, 2,
                manifest_port=19134,
            )
        netdev = captured["command"][captured["command"].index("-netdev") + 1]
        self.assertIn("hostfwd=tcp:127.0.0.1:19134-:8012", netdev)


class FixtureRebuildTests(unittest.TestCase):
    """`firstboot-fixture.py prepare --backup-password-file`: the bench's writer.

    The QEMU rebuild in the card's evidence runs `prepare` with the user's old
    recovery file; this holds the two properties the guest depends on: the file
    the service reads holds ciphertext under the token it just minted, and the
    real exchange serves it so `odile-firstboot` can open it.
    """

    INSTANCE = "45445445-0000-4000-8000-000000000699"
    SLUG = "odl-bench"
    PASSWORD = "test-rebuild-password-2026-0001"

    @staticmethod
    def certified_identity(directory: pathlib.Path, key: pathlib.Path) -> tuple[bytes, bytes]:
        """(seed public key PEM, a release key's certificate over it) for `redeem`.

        ODL-514: the seed service signs with its own key and carries the release
        key's certificate over that key. A direct `redeem` call has no service
        around it, so it passes the pair the service would have loaded.
        """
        public = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout"],
                                check=True, capture_output=True, timeout=60).stdout
        public_file = directory / "seed.pub"
        public_file.write_bytes(public)
        release = directory / "release.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(release)],
                       check=True, capture_output=True, timeout=60)
        os.chmod(release, 0o600)
        certificate = directory / "seed-key.crt"
        subprocess.run(["openssl", "dgst", "-sign", str(release), "-out", str(certificate),
                        str(public_file)], check=True, capture_output=True, timeout=60)
        return public, certificate.read_bytes()

    def test_prepare_cuts_the_certificate_the_guest_walks(self) -> None:
        """The fixture's seed key is certified by the release key's public half (ODL-514)."""
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl is missing")
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp) / "bench"
            seed_key = pathlib.Path(tmp) / "seed.pem"
            release_key = pathlib.Path(tmp) / "release.pem"
            for key in (seed_key, release_key):
                subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                               check=True, capture_output=True, timeout=60)
                os.chmod(key, 0o600)
            result = fixture.prepare(directory, self.INSTANCE, self.SLUG, str(seed_key),
                                     release_key=str(release_key))
            certificate = pathlib.Path(result["certificate"])
            public = pathlib.Path(result["seed_pubkey"])
            self.assertEqual(len(certificate.read_bytes()), 64)
            self.assertEqual(stat.S_IMODE(certificate.stat().st_mode), 0o644)
            release_pub = subprocess.run(["openssl", "pkey", "-in", str(release_key), "-pubout"],
                                         check=True, capture_output=True, timeout=60).stdout
            self.assertEqual(pathlib.Path(result["release_pubkey"]).read_bytes(), release_pub)
            verified = subprocess.run(
                ["openssl", "dgst", "-verify", result["release_pubkey"],
                 "-signature", str(certificate), str(public)],
                capture_output=True, timeout=30)
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_prepare_seals_the_old_password_under_the_token_it_mints(self) -> None:
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl is missing")
        seed_module = _load("odile_seed_fixture_rebuild", ROOT / "seed" / "seed.py")
        firstboot_module = _load("odile_firstboot_fixture_rebuild", IMAGE / "odile-firstboot.py")
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp) / "bench"
            key = pathlib.Path(tmp) / "k.pem"
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                           check=True, capture_output=True, timeout=60)
            os.chmod(key, 0o600)
            result = fixture.prepare(directory, self.INSTANCE, self.SLUG, str(key), self.PASSWORD)
            self.assertEqual(result["rebuild"], "sealed")
            rebuilds_file = directory / "seed-rebuilds.json"
            self.assertEqual(stat.S_IMODE(rebuilds_file.stat().st_mode), 0o600)
            self.assertNotIn(self.PASSWORD, rebuilds_file.read_text(encoding="utf-8"))
            rebuilds = seed_module.load_rebuilds(str(rebuilds_file))
            self.assertEqual(rebuilds[self.SLUG]["token_hash"],
                             hashlib.sha256(result["token"].encode()).hexdigest())
            con = seed_module.connect(str(directory / "seed.db"))
            try:
                config = json.loads((directory / "seed-config.json").read_text(encoding="utf-8"))
                served = seed_module.redeem(con, result["token"], self.INSTANCE,
                                            config, rebuilds,
                                            identity=self.certified_identity(directory, key),
                                            # Hermetic: without this the seed
                                            # service's default path is HQ's
                                            # live `/var/lib/odile/seed-backup-pairs.json`.
                                            backup_pairs={})
            finally:
                con.close()
            self.assertEqual(firstboot_module.unseal_rebuild(served["rebuild"], result["token"]),
                             self.PASSWORD)

    def test_prepare_rearms_the_token_an_image_already_carries(self) -> None:
        """A bench rerun: the measured line cannot change, so the token must not."""
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl is missing")
        seed_module = _load("odile_seed_fixture_rearm", ROOT / "seed" / "seed.py")
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp) / "bench"
            key = pathlib.Path(tmp) / "k.pem"
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                           check=True, capture_output=True, timeout=60)
            os.chmod(key, 0o600)
            first = fixture.prepare(directory, self.INSTANCE, self.SLUG, str(key), self.PASSWORD)
            before = seed_module.load_rebuilds(str(directory / "seed-rebuilds.json"))[self.SLUG]
            second = fixture.prepare(directory, self.INSTANCE, self.SLUG, str(key),
                                     self.PASSWORD, first["token"])
            self.assertEqual(second["token"], first["token"])
            # The same token seals to the same ciphertext; only created_at moves.
            after = seed_module.load_rebuilds(str(directory / "seed-rebuilds.json"))[self.SLUG]
            self.assertEqual((after["sealed"], after["tag"]), (before["sealed"], before["tag"]))
            con = seed_module.connect(str(directory / "seed.db"))
            try:
                rows = con.execute(
                    "SELECT token_hash, used_at FROM seed_tokens WHERE token_hash = ?",
                    (hashlib.sha256(first["token"].encode()).hexdigest(),)).fetchall()
            finally:
                con.close()
            self.assertEqual(len(rows), 1, "one row per token, never a duplicate")
            self.assertIsNone(rows[0]["used_at"], "re-armed means live again")

    def test_prepare_without_the_file_carries_no_rebuild(self) -> None:
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl is missing")
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp) / "bench"
            key = pathlib.Path(tmp) / "k.pem"
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                           check=True, capture_output=True, timeout=60)
            os.chmod(key, 0o600)
            result = fixture.prepare(directory, self.INSTANCE, self.SLUG, str(key))
            self.assertEqual(result["rebuild"], "none")
            self.assertFalse((directory / "seed-rebuilds.json").exists())


class FirstbootBenchTests(unittest.TestCase):
    """The harness that reads the unit's one line and the page's answer."""

    def test_the_installed_line_requires_the_page_status(self) -> None:
        self.assertEqual(
            bench.parse_guest_firstboot(
                "…\nodile-firstboot: installed odl-bench (instance x) · first-visit 200 · 3s\n"),
            ("installed", "installed odl-bench (instance x) · first-visit 200 · 3s"))
        self.assertEqual(bench.parse_guest_firstboot("noise\n"), None)

    def test_each_state_parses_and_the_last_line_wins(self) -> None:
        text = ("odile-firstboot: fail-closed: no setup token in the command line\n"
                "odile-firstboot: same\n")
        self.assertEqual(bench.parse_guest_firstboot(text), ("same", "same"))
        self.assertEqual(bench.parse_guest_firstboot("odile-firstboot: fail-closed: seed token used\n"),
                         ("fail-closed", "seed token used"))

    def test_wait_firstboot_matches_only_the_expected_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            serial = pathlib.Path(tmp) / "boot.log"
            serial.write_text("odile-firstboot: same\n")
            self.assertTrue(bench.wait_firstboot(serial, "same", 1)[0])
            self.assertFalse(bench.wait_firstboot(serial, "installed", 0.2)[0])
            self.assertFalse(bench.wait_firstboot(serial, "fail-closed", 0.2)[0])
            serial.write_text("odile-firstboot: installed odl-bench · first-visit 200 · 3s\n")
            self.assertTrue(bench.wait_firstboot(serial, "installed", 1)[0])
            serial.write_text("odile-firstboot: installed odl-bench · first-visit nope\n")
            ok, detail = bench.wait_firstboot(serial, "installed", 0.2)
            self.assertFalse(ok)
            self.assertIn("page", detail)

    def test_the_first_visit_page_check_reads_the_form(self) -> None:
        class Form(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib signature
                body = (b"<html><form method=\"post\" action=\"/first-visit\">"
                        b"<label>Set your sign-in</label></form></html>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Form)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            check = bench.first_visit_page(server.server_address[1], timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(check.ok, check.detail)
        self.assertIn("200", check.detail)

    def test_the_page_check_fails_when_the_page_is_absent(self) -> None:
        class Gone(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib signature
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gone)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            check = bench.first_visit_page(server.server_address[1], timeout=0.3)
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(check.ok)

    def test_the_uki_boot_forwards_the_first_visit_port_only_when_asked(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_popen(command, **kwargs):  # noqa: ANN001 - subprocess API
            captured["command"] = command
            return mock.MagicMock()

        with mock.patch.object(bench.subprocess, "Popen", side_effect=fake_popen):
            bench.boot_qemu_uki(
                pathlib.Path("/var/tmp/x.raw"), pathlib.Path("/var/tmp/x.efi"), pathlib.Path("/var/tmp/OVMF.fd"),
                pathlib.Path("/var/tmp/boot.log"), pathlib.Path("/var/tmp/bench.log"), 19132, 2048, 2,
            )
        netdev = captured["command"][captured["command"].index("-netdev") + 1]
        self.assertIn("hostfwd=tcp:127.0.0.1:19132-:9130", netdev)
        self.assertNotIn(":8091", netdev)

        with mock.patch.object(bench.subprocess, "Popen", side_effect=fake_popen):
            bench.boot_qemu_uki(
                pathlib.Path("/var/tmp/x.raw"), pathlib.Path("/var/tmp/x.efi"), pathlib.Path("/var/tmp/OVMF.fd"),
                pathlib.Path("/var/tmp/boot.log"), pathlib.Path("/var/tmp/bench.log"), 19132, 2048, 2,
                first_visit_port=19133,
            )
        netdev = captured["command"][captured["command"].index("-netdev") + 1]
        self.assertIn("hostfwd=tcp:127.0.0.1:19133-:8091", netdev)


class FirstbootWaitFirstVisitTests(unittest.TestCase):
    """The boot's readiness probe: the bare `/first-visit`, and the form.

    ODL-925 made the first visit a login, so `wait_first_visit` asks the page
    without a setup token; nothing else in the tree calls it, so this class is
    the only pin on the path it requests. A regression that puts the token back
    in the query (`/first-visit?t=…`) fails the page 404 below and the exact-path
    assertion beside it.
    """

    def _serve(self, handler: type) -> http.server.ThreadingHTTPServer:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_the_probe_asks_the_bare_path_and_passes_on_the_form(self) -> None:
        asked: list[str] = []

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib signature
                asked.append(self.path)
                if self.path == "/health":
                    body = b"ok"
                elif self.path == "/first-visit":
                    body = (b'<html><form method="post" action="/first-visit">'
                            b'<label for="password">Your sign-in</label></form></html>')
                else:
                    body = b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

        server = self._serve(Page)
        ok, detail = firstboot.wait_first_visit(server.server_address[1], seconds=2)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "200")
        self.assertIn("/first-visit", asked)
        self.assertEqual([path for path in asked if "?" in path], [])
        self.assertEqual([path for path in asked if path not in ("/health", "/first-visit")], [])

    def test_the_probe_fails_when_the_page_is_absent(self) -> None:
        class Gone(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib signature
                body = b"ok" if self.path == "/health" else b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

        server = self._serve(Gone)
        ok, detail = firstboot.wait_first_visit(server.server_address[1], seconds=0.3)
        self.assertFalse(ok)
        self.assertIn("status 404", detail)

    def test_the_probe_fails_when_the_service_is_not_healthy(self) -> None:
        class Sick(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib signature
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

        server = self._serve(Sick)
        ok, detail = firstboot.wait_first_visit(server.server_address[1], seconds=0.3)
        self.assertFalse(ok)
        self.assertIn("health status 503", detail)


class FirstbootScriptTests(unittest.TestCase):
    """The units and the installer's flags: the image's boot contract."""

    def test_the_firstboot_unit_runs_before_the_tenant_and_speaks_on_the_console(self) -> None:
        unit = (IMAGE / "odile-firstboot.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertIn("Before=odile-agent.service", unit)
        self.assertIn("After=network-online.target", unit)
        self.assertIn("StandardOutput=journal+console", unit)
        self.assertIn("StandardError=journal+console", unit)
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/lib/odile-image/odile-firstboot.py", unit)
        self.assertIn("ConditionPathExists=/usr/local/lib/odile-image/odile-firstboot.py", unit)

    def test_every_first_visit_placeholder_has_a_substitution(self) -> None:
        installer = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        for name in ("odile-first-visit.service", "odile-bench-proxy-firstvisit.service",
                     "odile-bench-proxy-firstvisit.socket"):
            text = (IMAGE / name).read_text(encoding="utf-8")
            for placeholder in sorted(set(re.findall(r"__[A-Z_]+__", text))):
                self.assertIn(f"s|{placeholder}|", installer,
                              f"{name}: {placeholder} has no substitution in install-stack.sh")

    def test_the_manifest_listener_can_traverse_the_release_installers_etc(self) -> None:
        """Round 7: 503 "manifest unavailable" on the finished tenant.

        The release installer sets /etc/odile to 0750 root:odile-manifest; the
        listener runs as a DynamicUser and could not read the pair without the
        group, so /manifest answered 503 while the installer's own check had
        passed. The unit must join the group, and the image must create it
        before the unit starts.
        """
        unit = (IMAGE / "odile-manifest.service").read_text(encoding="utf-8")
        self.assertIn("DynamicUser=yes", unit)
        self.assertIn("SupplementaryGroups=odile-manifest", unit)
        installer = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        self.assertIn("getent group odile-manifest", installer)
        self.assertIn("groupadd --system odile-manifest", installer)
        release_installer = (ROOT / "installer" / "odile_install.py").read_text(encoding="utf-8")
        self.assertIn('MANIFEST_GROUP = "odile-manifest"', release_installer)
        self.assertIn("ensure_dir(layout.etc, 0o750, 0, group_id(MANIFEST_GROUP)", release_installer)

    def test_the_manifest_listener_dynamic_user_does_not_collide_with_its_group(self) -> None:
        """ODL-521: a DynamicUser named after the unit cannot join a group of
        that name.

        systemd allocates a transient user *and group* named after the unit.
        Main's meter removal renamed the group this unit joins to
        `odile-manifest`, and the allocation failed on a real boot —
        `Failed to update dynamic user credentials: User or group with
        specified name already exists` — and the first boot stopped before the
        sign-in evidence could be taken. The unit must name a user that is not
        the group it joins.
        """
        unit = (IMAGE / "odile-manifest.service").read_text(encoding="utf-8")
        self.assertIn("DynamicUser=yes", unit)
        group = re.search(r"^SupplementaryGroups=(\S+)$", unit, re.M)
        user = re.search(r"^User=(\S+)$", unit, re.M)
        self.assertIsNotNone(group, "the unit joins no group; /etc/odile is 0750 root:<group>")
        self.assertIsNotNone(user, "DynamicUser names its transient user after the unit")
        self.assertNotEqual(user.group(1), group.group(1),
                            "DynamicUser's transient group would collide with the joined group")

    def test_the_image_names_the_gateway_unit_as_the_release_does(self) -> None:
        """Round 6: two units for one gateway, and a report for the wrong user.

        The release installer writes `odile-agent.service` and runs the
        access report with the wrapper's default user (`odile`). The image used
        `odile-tenant.service` and the account `tenant`: the release's second
        gateway would lose the port, and the report failed on
        /home/odile/.hermes/.env. The image must converge on the release's unit
        name and pin the tenant account in a drop-in the rewrite cannot remove.
        """
        release = (ROOT / "installer" / "odile_install.py").read_text(encoding="utf-8")
        self.assertIn('unit_name="odile-agent"', release)
        installer = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        self.assertIn("SERVICE_NAME=odile-agent", installer)
        self.assertIn('"s|__UNIT__|$SERVICE_NAME|g"', installer)
        dropin = (IMAGE / "odile-access-report-user.conf").read_text(encoding="utf-8")
        self.assertIn("Environment=ODILE_ACCESS_USER=tenant", dropin)
        self.assertIn("/etc/systemd/system/odile-access-report.service.d/10-tenant-user.conf",
                      installer)
        image = (IMAGE / "sealed-image.sh").read_text(encoding="utf-8")
        self.assertIn("odile-access-report-user.conf", image)
        self.assertNotIn("SERVICE_NAME=odile-tenant", installer)

    def test_the_first_visit_unit_restarts_the_tenants_own_unit(self) -> None:
        unit = (IMAGE / "odile-first-visit.service").read_text(encoding="utf-8")
        self.assertIn("--state /etc/odile/first-visit.json", unit)
        self.assertIn("--env-file __TENANT_HOME__/.hermes/.env", unit)
        self.assertIn("--reload-command 'systemctl restart __UNIT__'", unit)
        self.assertIn("ReadWritePaths=__TENANT_HOME__ /etc/odile", unit)

    def test_install_stack_names_the_seed_and_token_flags(self) -> None:
        result = subprocess.run(
            ["bash", str(IMAGE / "install-stack.sh"), "--help"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--seed FILE", result.stdout)
        self.assertIn("--first-visit-token TOKEN", result.stdout)
        self.assertIn("--first-visit-port", result.stdout)

    def test_install_stack_refuses_a_token_without_a_seed(self) -> None:
        result = subprocess.run(
            ["bash", str(IMAGE / "install-stack.sh"), "--first-visit-token", "ab" * 32],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--first-visit-token needs --seed", result.stderr)

    def test_sealed_image_dry_run_names_the_first_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["bash", str(IMAGE / "sealed-image.sh"), "--dry-run",
                 "--out", str(pathlib.Path(tmp) / "out")],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("odile-firstboot.service", result.stdout)
        self.assertIn("odile.token", result.stdout)

    def test_the_production_image_ships_a_discarded_signin(self) -> None:
        """ODL-521: no public password in the published bytes.

        A production build (`sealed-image.sh --production` writes
        /etc/odile/production before install-stack runs) writes a hash of a
        password generated and discarded, with the marker that says so; the
        bench build keeps its documented fixture password, which the QEMU smoke
        signs in with. The first boot of a published image leaves the file
        alone: the placeholder is already unusable and the session secret must
        not move. The release installer's --first-visit write is what replaces
        the hash on every run until the page sets the owner's.
        """
        installer = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        self.assertIn("discarded_scrypt_hash", installer)
        self.assertIn("# odile-signin-placeholder: 1", installer)
        self.assertIn("first_visit_signin_set", installer)
        self.assertIn("/etc/odile/first-visit.json", installer)
        self.assertIn("[ -f /etc/odile/production ]", installer)
        self.assertIn("# bench-password-sha256: $password_sha", installer)
        self.assertIn("bench_password_verifies", installer)
        # The Hermes bootstrap leaves its own .env.example copy in place: a
        # published image must replace a file with no scrypt hash, not keep it.
        self.assertIn('[ ! -f "$env_file" ]', installer)
        self.assertIn("PASSWORD_HASH=scrypt[$]", installer)
        # The bench image's old .env header is gone with the usable hash.
        self.assertNotIn("Odile image bench — generated by deploy/image/install-stack.sh",
                         installer)
        # The first boot of a published image must not rewrite the file: the
        # production branch prints `same` for a hash that rejects the fixture.
        production = installer.index("if [ -f /etc/odile/production ]; then")
        bench = installer.index("elif [ -f \"$env_file\" ] && grep -qx")
        branch = installer[production:bench]
        self.assertIn("say same", branch)
        self.assertIn("write_env", branch)
        self.assertLess(branch.index("bench_password_verifies"),
                        branch.index("write_env"),
                        "the hash must be checked before the file is rewritten")

    def test_the_build_removes_every_host_key_and_fails_if_one_survives(self) -> None:
        """ODL-523: debootstrap's host keys must not reach a tenant.

        The build branch (no --seed) removes /etc/ssh/ssh_host_* and then
        asserts none is left, exiting 1 with the named error; the seed branch
        (the first boot) runs ssh-keygen -A for the machine's own. Debian's
        sshd-keygen.service cannot do it on a cloned disk (ConditionFirstBoot=yes
        and the image ships a machine-id), so the image also carries a drop-in
        that clears the condition.
        """
        installer = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        # The drop-in: the path, and the cleared condition (not `no`).
        self.assertIn("/etc/systemd/system/sshd-keygen.service.d/10-odile-hostkeys.conf",
                      installer)
        self.assertIn("[Unit]\nConditionFirstBoot=\n", installer)
        self.assertNotIn("ConditionFirstBoot=no", installer)
        # The build branch: remove, then assert, then the named error.
        branch = installer.index('if [ -n "$SEED_FILE" ]; then\n    if [ -f /etc/ssh/ssh_host_ed25519_key ]')
        section = installer[branch:]
        removal = section.index('rm -f "$key"')
        assertion = section.index('compgen -G "/etc/ssh/ssh_host_*_key"')
        named = section.index("install-stack: the image still carries an SSH host private key")
        self.assertLess(removal, assertion, "the removal must come before the assertion")
        self.assertLess(assertion, named, "the assertion must name the failure")
        self.assertIn("exit 1", section[assertion:named + 200])
        # The first boot's half, in the same block.
        self.assertIn("ssh-keygen -A", section[:removal])

    def test_the_first_visit_write_replaces_the_hash_until_the_page_sets_it(self) -> None:
        """The installer half of ODL-521, read where it runs.

        `ensure_env(first_visit=True)` must not take the converged early return
        before `signin_set_at` exists: an image hash that looks valid (a scrypt
        hash of the public fixture) is exactly the exploit. The unit-level
        behaviour is in tests/test_first_visit.py; this binds the shipped
        installer to it.
        """
        installer = (ROOT / "installer" / "odile_install.py").read_text(encoding="utf-8")
        self.assertIn("def first_visit_signin_set(", installer)
        self.assertIn("signin_set_at", installer)
        self.assertIn("first_visit and not signin_set", installer)
        self.assertIn("placeholder until first visit", installer)

    def test_the_first_boot_generates_the_machines_own_secret(self) -> None:
        """ODL-522: the image's session secret never signs a tenant's cookie.

        The build writes a random secret into the image, so every disk built
        from one image shared it. The first boot (`--seed` is in the argv) of a
        published image must replace it with a secret generated on this
        machine, and must leave the file alone once the first-visit page has
        set the owner's sign-in (`signin_set_at`), so a reboot keeps the secret
        byte for byte. Both halves live in install-stack.sh's production
        branch; the boot behaviour is measured in docs/runbooks/image.md §13.
        """
        installer = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        self.assertIn("fresh_secret", installer)
        self.assertIn("first_visit_signin_set", installer)
        production = installer.index("if [ -f /etc/odile/production ]; then")
        bench = installer.index("elif [ -f \"$env_file\" ] && grep -qx")
        branch = installer[production:bench]
        # The first boot is the run that carries --seed; the build has none.
        self.assertIn('[ -n "$SEED_FILE" ]', branch)
        self.assertIn("this machine's own secret", branch)
        # Keep once the sign-in exists: that check must come before the
        # rewrite, or a reboot after the visit would rotate the secret and
        # invalidate every session cookie the owner holds.
        self.assertLess(branch.index("first_visit_signin_set"),
                        branch.index("this machine's own secret"),
                        "the first boot must keep the secret once signin_set_at exists")
        # And the rewrite must be a fresh value, never the image's.
        self.assertIn("$(fresh_secret)", branch)
        self.assertIn("$(discarded_scrypt_hash)", branch)

    def test_every_shell_script_parses(self) -> None:
        # The seal script has no .sh suffix (it is installed as a program), so
        # it is named here: a syntax error in it is a first boot that never
        # re-keys.
        scripts = sorted(IMAGE.glob("*.sh")) + [IMAGE / "bb-sealed-provision"]
        for path in scripts:
            result = subprocess.run(["bash", "-n", str(path)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")
        # And under the /bin/sh it declares, which on the image is dash.
        result = subprocess.run(["/bin/sh", "-n", str(IMAGE / "bb-sealed-provision")],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_pin_carries_a_well_formed_seed_channel_when_it_has_one(self) -> None:
        pin = json.loads(PIN.read_text())
        tokens = pin["cmdline"].split()
        token = [t for t in tokens if t.startswith("odile.token=")]
        if not token:
            self.skipTest("the pinned line predates ODL-456; rebuild to add the channel")
        self.assertRegex(token[0][len("odile.token="):], r"^[0-9a-f]{64}$")
        url = [t for t in tokens if t.startswith("odile.seed_url=")]
        self.assertEqual(len(url), 1, pin["cmdline"])
        self.assertRegex(url[0][len("odile.seed_url="):], r"^https?://")


class HookBootstrapHarnessTests(unittest.TestCase):
    """ODL-332: the first boot's bootstrap branch, driven by the hook itself.

    The hook is a shell script in an initramfs; reading its source proves
    nothing. This runs the spike's stub harness (fake mount/cryptsetup/
    snpreport, a fake /dev/sda2) and requires every assertion to pass,
    including the two the strict first run depends on: no blob yet, and a blob
    sealed to another host — both open with the bootstrap key and say so.
    """

    HARNESS = ROOT / "spike" / "sealed-unlock" / "initramfs" / "test-bb-sealed-unlock.sh"

    @unittest.skipUnless(HARNESS.is_file(), SPIKE_MISSING)
    def test_the_hook_opens_with_the_bootstrap_key_and_the_harness_passes(self) -> None:
        result = subprocess.run(["sh", str(self.HARNESS)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("harness: PASS", result.stdout)
        self.assertNotIn("\nnot ok ", "\n" + result.stdout)
        oks = [line for line in result.stdout.splitlines() if line.startswith("ok ")]
        self.assertGreaterEqual(len(oks), 52)
        self.assertIn("bootstrap_no_blob_state - state is opened-bootstrap", result.stdout)
        self.assertIn("bootstrap_unseal_state - state is opened-bootstrap", result.stdout)


class ProductionImageTests(unittest.TestCase):
    """ODL-332: the tenant image and its first-boot seal are wired together."""

    def dry_run(self, *extra: str) -> str:
        result = subprocess.run(
            ["bash", str(IMAGE / "sealed-image.sh"), "--dry-run", *extra],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_production_plan_names_the_bootstrap_key_and_the_seal_unit(self) -> None:
        output = self.dry_run("--production")
        self.assertIn("first-boot bootstrap key /odile/luks-bootstrap.key", output)
        self.assertIn("odile-seal-provision.service", output)
        self.assertIn("resolver: 1.1.1.1 9.9.9.9", output)
        self.assertNotIn("luks-bootstrap.key", self.dry_run(), "the bench must not write it")
        # ODL-454 follow-up: the bench rehearsal writes the same bootstrap key,
        # ships no blob, and runs the seal unit with the stand-in, so the first
        # boot's re-key is exercised without an AMD PSP.
        rehearsal = self.dry_run("--first-boot-seal")
        self.assertIn("bootstrap key /odile/luks-bootstrap.key and no blob", rehearsal)
        self.assertIn("bench stand-in as its snpreport", rehearsal)
        both = subprocess.run(
            ["bash", str(IMAGE / "sealed-image.sh"), "--dry-run", "--production",
             "--first-boot-seal"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(both.returncode, 2, both.stdout + both.stderr)
        self.assertIn("mutually exclusive", both.stderr)

    def test_the_seal_unit_runs_before_the_first_boot_and_needs_servurys_binary(self) -> None:
        text = (IMAGE / "odile-seal-provision.service").read_text()
        self.assertIn("Before=odile-firstboot.service", text)
        self.assertIn("ConditionPathExists=/usr/local/lib/odile-image/snpreport", text)
        self.assertIn("ExecStart=/usr/local/lib/odile-image/bb-sealed-provision", text)
        self.assertIn("EXISTING_KEY_FILE=/run/bb/boot/odile/luks-bootstrap.key", text)
        # The first boot re-encrypts 7.3 GiB; the 90 s default would SIGTERM it
        # mid-re-key and the disk would never be re-keyed.
        self.assertIn("TimeoutStartSec=infinity", text)

    def test_the_image_carries_udev_because_the_rekey_needs_it(self) -> None:
        """Without systemd-udevd the online re-key blocks in libdevmapper's wait.

        debootstrap's minbase leaves udev out. The first boot's `cryptsetup
        reencrypt` then never gets its dm completion event (measured on the
        ODL-454 follow-up bench: the unit sat in finalize while the guest was
        idle), and /dev/ttyS0 never becomes a device unit, so the console has
        no getty and there is no drill shell. The package list is the fix, so
        the list is what this test reads.
        """
        text = (IMAGE / "sealed-image.sh").read_text()
        include = next(line for line in text.splitlines() if line.startswith("INCLUDE="))
        self.assertIn("udev", include.split("=", 1)[1].split(","))

    def test_the_provision_script_commits_only_after_the_rekey_is_proved(self) -> None:
        text = (IMAGE / "bb-sealed-provision").read_text()
        command = "cryptsetup --batch-mode reencrypt --progress-frequency 30"
        self.assertIn(command, text)
        self.assertIn('--key-file "$KEY" --key-slot "$NEW_SLOT" "$ROOT_PART"', text)
        block = text[text.index(command):text.index("the volume re-key did not complete")]
        self.assertNotIn("--keep-key", block, "the volume key must be regenerated, not kept")
        self.assertLess(text.index(command), text.index("COMMITTED=1"))
        self.assertLess(text.index("MK_BEFORE"), text.index("COMMITTED=1"))
        self.assertIn('shred -u "$BOOT_MNT$SEAL_DIR/luks-bootstrap.key"', text)
        # The shred lives in finalize(); every success path calls it after the
        # re-key is proved -- the end of a first run, and the `already
        # provisioned` path that finishes a run killed after the commit.
        committed = text.index("COMMITTED=1")
        self.assertLess(committed, text.index("\nfinalize\n", committed))
        self.assertLess(text.index("already provisioned"),
                        text.index("finalize", text.index("already provisioned")))
        self.assertIn('"bootstrap_removed":%s', text)

    def test_the_bootstrap_key_is_written_before_the_volume_key_is_shredded(self) -> None:
        text = (IMAGE / "sealed-image.sh").read_text()
        self.assertLess(
            text.index('install -m 0600 "$VOLKEY" "$BOOT_MNT/odile/luks-bootstrap.key"'),
            text.rindex('shred -u "$VOLKEY"'),
        )

    def test_the_initrd_build_pins_servurys_release_snpreport(self) -> None:
        text = (IMAGE / "sealed-initrd.sh").read_text()
        self.assertIn("1e2a6c0782bebf471a6973a2bf1f0eee5fac86005a5ee56c5b8939ef40cacc77", text)
        self.assertIn("--snpreport", text)
        self.assertIn("is not Servury's release binary", text)

    def test_the_production_image_runs_the_release_installer_after_the_page(self) -> None:
        unit = (IMAGE / "odile-release-setup.service").read_text()
        self.assertIn("After=odile-firstboot.service", unit)
        self.assertIn("ConditionPathExists=/etc/odile/production", unit)
        self.assertIn("ConditionPathExists=/usr/local/bin/odile-install.sh", unit)
        script = (IMAGE / "odile-release-setup.sh").read_text()
        self.assertIn("--first-visit --first-visit-token", script)
        # Round 5: the seed loader reads kit["setup_token"], which only --token
        # sets; without it the installer exits 1 after the boot check passes.
        self.assertIn('--token "$TOKEN"', script)
        self.assertIn("the recovery kit has no 64-hex setup token", script)
        # t_d95b8c13 + ODL-340 follow-up: the public name is an override whose
        # next source is the measured odile.public_url= token and whose last
        # is the slug's production name, so a moved machine keeps its name and
        # a second environment needs no drop-in; the seed endpoint rides
        # through so the installer's site and heartbeat come from the one
        # environment that issued the seed.
        self.assertIn('PUBLIC_URL=${ODILE_RELEASE_PUBLIC_URL:-${PUBLIC_TOKEN:-https://$HOST.odile.fyi}}', script)
        self.assertIn('odile.public_url=*) PUBLIC_TOKEN=', script)
        self.assertIn('odile.seed_url=*) SEED_URL=', script)
        self.assertIn('--seed-url "$SEED_URL"', script)
        self.assertIn('--public-url "$PUBLIC_URL"', script)
        self.assertIn("--i-have-copied-it", script)
        self.assertIn("--user tenant --port 9130", script)
        self.assertLess(script.index("--instance-id"), script.index("--slug"))
        image = (IMAGE / "sealed-image.sh").read_text()
        self.assertIn("odile-release-setup.service", image)
        self.assertIn("systemctl enable odile-release-setup.service", image)


class SerialConsoleTests(unittest.TestCase):
    """ODL-914: the tenant console ends at a login: prompt; the bench keeps its shell.

    The Servury panel's "interactive console" is a shell on the machine's
    serial port, so an autologin in a production image hands the panel holder
    a root-capable session with no user credential — against
    docs/MVP-V0.1.md §6.8 ("console only, can watch"). The bench image is the
    drill bench and keeps the automatic login; the production image must not
    install the drop-in, while still enabling the getty that asks for one.
    """

    def dry_run(self, script: str, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(IMAGE / script), "--dry-run", *extra],
            capture_output=True, text=True, timeout=120)

    def test_a_production_build_plans_a_login_prompt_and_no_autologin(self) -> None:
        result = self.dry_run("sealed-image.sh", "--production")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("serial console: ttyS0 login: prompt (production: no autologin)",
                      result.stdout)
        self.assertNotIn("autologin tenant", result.stdout,
                         "a production image must not plan the drill shell")

    def test_the_sealed_bench_image_keeps_the_drill_shell(self) -> None:
        result = self.dry_run("sealed-image.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("serial console: ttyS0 autologin tenant (bench drill shell)",
                      result.stdout)
        # The bench rehearsal of a first boot is still a bench image.
        rehearsal = self.dry_run("sealed-image.sh", "--first-boot-seal")
        self.assertEqual(rehearsal.returncode, 0, rehearsal.stderr)
        self.assertIn("serial console: ttyS0 autologin tenant (bench drill shell)",
                      rehearsal.stdout)

    def test_a_production_disk_writes_the_drop_in_only_behind_the_production_guard(self) -> None:
        """The autologin file is written in the non-production branch only."""
        text = (IMAGE / "sealed-image.sh").read_text(encoding="utf-8")
        guard = text.index('if [ "$PRODUCTION" != 1 ]; then')
        drop_in = text.index("serial-getty@ttyS0.service.d/autologin.conf")
        agetty = text.index("--autologin tenant")
        self.assertLess(guard, drop_in, "the drop-in write must sit inside the guard")
        self.assertLess(drop_in, agetty, "the agetty line follows its own file")
        bench_branch_end = text.index("mount --bind /proc", drop_in)
        self.assertLess(agetty, bench_branch_end)
        # ... and the getty itself is enabled for both images, so a production
        # console still ends at `login:` rather than nowhere.
        enable = text.index("for unit in systemd-networkd.service")
        self.assertIn("serial-getty@ttyS0.service", text[enable:enable + 400])

    def test_the_bench_disk_builder_keeps_its_autologin(self) -> None:
        result = self.dry_run("mkdisk.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("serial console: ttyS0 autologin tenant (bench drill shell)",
                      result.stdout)

    def test_the_bench_disk_builder_has_no_tenant_mode_to_leak_the_autologin_into(self) -> None:
        result = self.dry_run("mkdisk.sh", "--production")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unknown argument: --production", result.stderr)


class FirstBootReKeyTests(unittest.TestCase):
    """ODL-454 follow-up: the first boot must replace the *volume* key.

    A keyslot dance (`luksAddKey` + `luksKillSlot`) leaves the LUKS master key
    alone, and the published image carries that master key inside its own
    keyslot -- so the image, plus the bootstrap key it publishes, opens the
    live disk. These tests run the real script against a real LUKS2 volume and
    assert what the holder of the image can do afterwards: the build key must
    not open the volume, the volume key must be different, and the guest's
    sealed key must open it with the filesystem intact.

    The fixture's build keyslot uses PBKDF2 so the gate does not spend a
    minute per test in Argon2id; the behaviour under test is the volume key,
    which no PBKDF touches.

    Every mapping carries this run's tag -- the pid plus four random bytes --
    because two `make check` runs share one /dev/mapper. A fixed name made the
    second gate red with exit 32 ("device or resource busy") on either the
    `luksOpen` or the `mount`; the tag, the `mapped()` context and the
    dead-owner sweep below are what make the class concurrent-safe.

    A mapping is not free to close while something it backs is mounted, so the
    class also tracks every mount point it creates: the SIGTERM/SIGINT handler
    unmounts before it closes, and the dead-owner sweep unmounts a killed
    run's mounts by their device source before closing its mapping. Without
    that, a run killed between `mount` and `umount` left both behind and no
    later run could recover the mapping (its opener count never fell).
    """

    VOLUME = 64 << 20
    REKEY_VOLUME = 2 << 30
    BOOT = 32 << 20
    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    CRYPT = "cryptsetup"
    RUN_TAG = ""
    _open: set[str] = set()
    _mounts: dict[str, str] = {}
    _seq = 0
    _handlers: dict = {}

    @staticmethod
    def tool_env() -> dict:
        env = dict(os.environ)
        env["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
        return env

    @classmethod
    def setUpClass(cls) -> None:
        for tool in ("cryptsetup", "losetup", "mkfs.ext4", "dmsetup", "gcc", "mount", "umount"):
            if not shutil.which(tool):
                raise unittest.SkipTest(f"{tool} is missing")
        if os.geteuid() != 0:
            raise unittest.SkipTest("LUKS, loop devices and mount need root")
        cls.CRYPT = shutil.which("cryptsetup") or "cryptsetup"
        cls.RUN_TAG = f"{os.getpid()}-{os.urandom(4).hex()}"
        cls._open = set()
        cls._mounts = {}
        cls._seq = 0
        cls._install_signal_cleanup()
        # A killed older run must not make this one fail: close what its dead
        # pid left, and never touch a mapping a live pid owns.
        cls.close_stale_mappers()
        cls.tmp = tempfile.TemporaryDirectory(prefix="odile-rekey-")
        cls.dir = pathlib.Path(cls.tmp.name)
        cls.bin = cls.dir / "snpreport-standin"
        subprocess.run(
            ["gcc", "-O2", "-static", "-s", "-o", str(cls.bin), str(SOURCE)],
            check=True, timeout=120,
        )
        cls.cmdline = cls.dir / "cmdline"
        cls.cmdline.write_text("root=/dev/mapper/root_crypt ro seal_id=rekey-test bb.iid=1\n")
        cls.standin = cls.dir / "snpreport"
        cls.standin.write_text(
            "#!/bin/sh\nexec %s \"$@\" --cmdline-file %s\n" % (cls.bin, cls.cmdline))
        cls.standin.chmod(0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.close_all()
            cls.close_stale_mappers()
        finally:
            cls._restore_signal_cleanup()
            cls.tmp.cleanup()

    # -- mapping bookkeeping ----------------------------------------------

    @classmethod
    def _install_signal_cleanup(cls) -> None:
        """Close the mappings before SIGINT/SIGTERM ends the process.

        unittest runs `addCleanup` for an exception but not for the
        KeyboardInterrupt a Ctrl-C raises, and SIGTERM runs nothing at all.
        Both left a mapping behind; this turns the signal into a clean close
        and then re-raises it with the default action, so the exit status is
        still the signal's.
        """
        if threading.current_thread() is not threading.main_thread():
            return
        cls._handlers = {sig: signal.getsignal(sig) for sig in cls.SIGNALS}
        for sig in cls.SIGNALS:
            signal.signal(sig, cls._signal_cleanup)

    @classmethod
    def _restore_signal_cleanup(cls) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig, previous in cls._handlers.items():
            signal.signal(sig, previous)
        cls._handlers = {}

    @classmethod
    def _signal_cleanup(cls, signum: int, frame: object) -> None:
        try:
            cls.close_all()
        finally:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    @classmethod
    def close_all(cls) -> None:
        """Unmount what this run mounted, then close what it opened."""
        for point in sorted(cls._mounts, key=len, reverse=True):
            cls._umount(point)
        for name in sorted(cls._open):
            cls._close(name)

    @classmethod
    def _umount(cls, point: str, retries: int = 20) -> bool:
        """Unmount `point`; True when it is no longer mounted.

        Safe in a signal handler: it never asserts. A plain unmount is tried
        first, then a lazy one, so the block device is released even when a
        file or a process still pins the mount -- otherwise `cryptsetup close`
        retries against a busy device and the run leaves both behind.
        """
        cls._mounts.pop(point, None)
        for _ in range(retries):
            if not os.path.ismount(point):
                return True
            try:
                result = subprocess.run(["umount", point], capture_output=True, text=True,
                                        timeout=60, env=cls.tool_env())
            except (OSError, subprocess.SubprocessError):
                return False
            if result.returncode == 0 or not os.path.ismount(point):
                return True
            time.sleep(0.1)
        try:
            subprocess.run(["umount", "-l", point], capture_output=True, text=True,
                           timeout=60, env=cls.tool_env())
        except (OSError, subprocess.SubprocessError):
            return False
        return not os.path.ismount(point)

    @staticmethod
    def _unescaped(field: str) -> str:
        """A mountinfo field with its octal escapes decoded."""
        for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            field = field.replace(code, char)
        return field

    @classmethod
    def _mounts_for(cls, name: str) -> list[str]:
        """Every mount point whose source is this mapping, from mountinfo.

        `mount` records the path it was given, so a mapper opened as
        `/dev/mapper/<name>` can appear as that path or as its `/dev/dm-N`
        node; both resolve to the same realpath.
        """
        points = []
        mapper = f"/dev/mapper/{name}"
        try:
            rows = pathlib.Path("/proc/self/mountinfo").read_text().splitlines()
        except OSError:
            return points
        for row in rows:
            fields = row.split(" ")
            try:
                separator = fields.index("-")
            except ValueError:
                continue
            if len(fields) < max(6, separator + 3):
                continue
            source = cls._unescaped(fields[separator + 2])
            if source not in (mapper, name):
                try:
                    if os.path.realpath(source) != os.path.realpath(mapper):
                        continue
                except OSError:
                    continue
            points.append(cls._unescaped(fields[4]))
        return points

    @classmethod
    def _close(cls, name: str) -> bool:
        """Close one mapping; True when it is no longer active.

        A mapping can still be mounted when the signal path or a stale-run
        sweep reaches it -- a test killed between `mount` and `umount`. The
        mount keeps the device busy, so unmount it first, by source, which
        also catches a mount this run did not create; then ask the mapper
        until it answers "not active" rather than trusting one exit code (the
        same race sealed-image.sh retries).
        """
        cls._open.discard(name)
        for point in cls._mounts_for(name):
            cls._umount(point)
        for _ in range(20):
            status = subprocess.run([cls.CRYPT, "status", name], capture_output=True,
                                    text=True, timeout=60, env=cls.tool_env())
            if status.returncode != 0:
                return True
            subprocess.run([cls.CRYPT, "close", name], capture_output=True, text=True,
                           timeout=120, env=cls.tool_env())
            time.sleep(0.1)
        return subprocess.run([cls.CRYPT, "status", name], capture_output=True, text=True,
                              timeout=60, env=cls.tool_env()).returncode != 0

    @classmethod
    def _mappings(cls) -> list[tuple[str, int]]:
        """Every device-mapper name and its opener count, or [] without dm."""
        result = subprocess.run([shutil.which("dmsetup") or "dmsetup", "info", "-c",
                                 "--noheadings", "-o", "name,open"],
                                capture_output=True, text=True, timeout=60,
                                env=cls.tool_env())
        if result.returncode != 0:
            return []
        rows = []
        for line in result.stdout.splitlines():
            # `dmsetup info -c` separates the columns with ':'.
            name, _, openers = line.rpartition(":")
            if name and openers.strip().isdigit():
                rows.append((name, int(openers)))
        return rows

    @staticmethod
    def _owner_pid(name: str) -> int | None:
        """The pid a name was created by, when the name carries one."""
        parts = name.split("-")
        if name.startswith("odile-sealed-") and parts[-1].isdigit():
            return int(parts[-1])
        # odile-rekey-<purpose>-<pid>-<rand>-<n>; the purpose may carry dashes.
        if name.startswith("odile-rekey-") and len(parts) >= 5 and parts[-3].isdigit():
            return int(parts[-3])
        return None

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def close_stale_mappers(cls, dead: set[int] | None = None) -> None:
        """Close what a killed run left; never what a live run owns.

        `odile-sealed-<pid>` is the image build's mapper and
        `odile-rekey-<purpose>-<pid>-...` is this class's. A live pid is a
        concurrent gate, so its mapping is left alone. A dead pid's mapping is
        recovered even when it still has an opener -- a mount that outlived
        the process, which is exactly what SIGKILL leaves -- because `_close`
        unmounts every mountinfo entry sourced from that mapping first. The
        old sweep skipped `openers>0` and so never recovered one: the mount
        kept the count up and every later run inherited the mapping.

        `dead` names pids the caller has proven dead. The tests that fabricate
        an orphan pass their own pid here (a real, live pid) instead of a
        numeric dead one: a sibling gate then sees a live owner and leaves the
        fabricated mapping alone, so two concurrent runs cannot sweep each
        other's test fixture.

        A name without a pid is the old fixed form (`odile-rekey`,
        `odile-rekey-check`): a run still on that code may hold it with zero
        openers for the instant between `luksOpen` and `mount`, so it is never
        swept. Those names cannot collide with the per-run names anyway.
        """
        declared = dead or set()
        for name, _openers in cls._mappings():
            if name in cls._open or not name.startswith(("odile-rekey-", "odile-sealed-")):
                continue
            owner = cls._owner_pid(name)
            if owner is None or (owner not in declared and cls._alive(owner)):
                continue
            cls._close(name)

    def mapper_name(self, purpose: str) -> str:
        """A name no other run, test or retry on this box can be holding."""
        type(self)._seq += 1
        return f"odile-rekey-{purpose}-{type(self).RUN_TAG}-{type(self)._seq}"

    def open_mapper(self, device: str, key: pathlib.Path, purpose: str) -> tuple[str, str]:
        """Open `device` under a per-run name and promise to close it.

        The cleanup is registered before the assertion: a `luksOpen` that
        fails halfway can still have created the mapping, and a test that
        only closes on its happy path is how the next run inherits a busy
        device.
        """
        name = self.mapper_name(purpose)
        result = self.crypt("--key-file", str(key), "luksOpen", device, name)
        type(self)._open.add(name)
        self.addCleanup(self.close_mapper, name)
        self.assertEqual(result.returncode, 0, result.stderr)
        return name, f"/dev/mapper/{name}"

    def close_mapper(self, name: str) -> None:
        type(self)._close(name)

    @contextlib.contextmanager
    def mapped(self, device: str, key: pathlib.Path, purpose: str):
        """`luksOpen` -> body -> `cryptsetup close`, on every path."""
        name, path = self.open_mapper(device, key, purpose)
        try:
            yield name, path
        finally:
            self.close_mapper(name)

    def mount(self, device: str, point: pathlib.Path, *options: str) -> None:
        """Mount `device` and remember the point for the signal path.

        Registered before the call: a SIGTERM delivered while `mount` is in
        flight runs this class's handler as soon as the subprocess returns,
        and by then the mount exists. A failed mount unregisters itself.
        """
        where = str(point)
        type(self)._mounts[where] = device
        result = subprocess.run(["mount", *options, str(device), where],
                                capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            type(self)._mounts.pop(where, None)
        self.assertEqual(result.returncode, 0, result.stderr)

    def umount(self, point: pathlib.Path, *, check: bool = True) -> None:
        """Unmount `point` and forget it; `check=False` for "may be absent"."""
        where = str(point)
        type(self)._mounts.pop(where, None)
        result = subprocess.run(["umount", where], capture_output=True, text=True, timeout=60)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)

    # -- fixture -----------------------------------------------------------

    def env(self) -> dict:
        return self.tool_env()

    def crypt(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["cryptsetup", *args], capture_output=True, text=True,
                              timeout=600, env=self.env())

    def fixture(self, volume: int = 0) -> dict:
        """A disk shaped like the published image's: a LUKS2 root whose only
        key is the build key, and a boot partition that carries that key as
        /odile/luks-bootstrap.key and no blob (the tenant's first-boot state).
        """
        volume = volume or self.VOLUME
        base = pathlib.Path(tempfile.mkdtemp(dir=self.dir))
        fx: dict = {"base": base, "root": base / "root.img", "boot": base / "boot.img",
                    "build": base / "build.key", "mnt": base / "boot.mnt",
                    "work": base / "work", "state": base / "seal.json",
                    "marker": base / "marker.sha"}
        with open(fx["root"], "wb") as handle:
            handle.truncate(volume)
        with open(fx["boot"], "wb") as handle:
            handle.truncate(self.BOOT)
        subprocess.run(["mkfs.ext4", "-q", "-F", str(fx["boot"])], check=True,
                       timeout=120, capture_output=True)
        fx["build"].write_bytes(os.urandom(64))
        fx["build"].chmod(0o600)
        made = self.crypt("--batch-mode", "--type", "luks2", "--pbkdf", "pbkdf2",
                          "--pbkdf-force-iterations", "1000",
                          "--key-file", str(fx["build"]), "luksFormat", str(fx["root"]))
        self.assertEqual(made.returncode, 0, made.stderr)

        root_loop = subprocess.run(["losetup", "-f", "--show", str(fx["root"])],
                                   check=True, capture_output=True, text=True).stdout.strip()
        self.addCleanup(subprocess.run, ["losetup", "-d", root_loop], capture_output=True)
        boot_loop = subprocess.run(["losetup", "-f", "--show", str(fx["boot"])],
                                   check=True, capture_output=True, text=True).stdout.strip()
        self.addCleanup(subprocess.run, ["losetup", "-d", boot_loop], capture_output=True)
        fx["root_loop"] = root_loop
        fx["boot_loop"] = boot_loop

        rootmnt = base / "root.mnt"
        rootmnt.mkdir()
        with self.mapped(root_loop, fx["build"], "format") as (_name, device):
            subprocess.run(["mkfs.ext4", "-q", device], check=True,
                           timeout=120, capture_output=True)
            self.mount(device, rootmnt)
            try:
                (rootmnt / "marker.txt").write_text("the tenant's own filesystem\n")
                payload = os.urandom(1 << 20)
                (rootmnt / "blob").write_bytes(payload)
                (rootmnt / "tail.txt").write_text("the far end of the filesystem\n")
                fx["marker"].write_text(hashlib.sha256(payload).hexdigest() + "  blob\n")
                subprocess.run(["sync"], check=True)
            finally:
                self.umount(rootmnt)

        fx["mnt"].mkdir()
        self.mount(boot_loop, fx["mnt"])
        (fx["mnt"] / "odile").mkdir()
        shutil.copyfile(fx["build"], fx["mnt"] / "odile" / "luks-bootstrap.key")
        os.chmod(fx["mnt"] / "odile" / "luks-bootstrap.key", 0o600)
        self.umount(fx["mnt"])
        return fx

    # -- helpers -----------------------------------------------------------

    def provision_env(self, fx: dict) -> dict:
        env = self.env()
        env.update({
            "ROOT_PART": fx["root_loop"], "BOOT_PART": fx["boot_loop"],
            "BOOT_MNT": str(fx["mnt"]), "SEAL_DIR": "/odile",
            "WORKDIR": str(fx["work"]), "SNPREPORT": str(self.standin),
            "STATE": str(fx["state"]),
            "EXISTING_KEY_FILE": str(fx["mnt"] / "odile" / "luks-bootstrap.key"),
        })
        return env

    def run_provision(self, fx: dict, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(IMAGE / "bb-sealed-provision"), *args],
                              capture_output=True, text=True, timeout=900,
                              env=self.provision_env(fx))

    def header(self, fx: dict) -> str:
        return self.crypt("luksDump", fx["root_loop"]).stdout

    def interrupt_rekey(self, fx: dict) -> str:
        """SIGTERM the run's process group the moment the header shows the
        re-encryption requirement -- the state `systemctl stop` leaves.

        The check and the signal are milliseconds apart while the data pass
        lasts seconds (REKEY_VOLUME), so the signal lands inside the pass and
        cryptsetup tears down with the requirement still set. The assertions
        after the wait pin that state: a test that let the re-key finish would
        exercise nothing.
        """
        log = fx["base"] / "interrupt.log"
        with open(log, "wb") as handle:
            proc = subprocess.Popen(
                ["sh", str(IMAGE / "bb-sealed-provision")],
                stdout=handle, stderr=subprocess.STDOUT,
                env=self.provision_env(fx), start_new_session=True)
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if "online-reencrypt" in self.header(fx):
                    break
                if proc.poll() is not None:
                    self.fail("the re-key finished before it could be interrupted:\n"
                              + log.read_text())
                time.sleep(0.02)
            else:
                self.fail("the re-encryption requirement never reached the header")
            # The requirement reaches the header before cryptsetup starts moving
            # data; a SIGTERM in that gap kills the process before its handler
            # prints "Reencryption interrupted." (measured in CI: two identical
            # jobs at one commit, and the check job's signal landed too early).
            # Settle inside the data pass, and fail clearly if it was over.
            time.sleep(1.0)
            self.assertIsNone(proc.poll(), "the re-key finished before it could be interrupted")
            os.killpg(proc.pid, signal.SIGTERM)
            self.assertNotEqual(proc.wait(timeout=300), 0, log.read_text())
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=60)
        output = log.read_text()
        self.assertIn("Reencryption interrupted.", output)
        self.assertIn("online-reencrypt", self.header(fx))
        return output

    def master_key(self, fx: dict, key: pathlib.Path) -> str:
        """SHA-256 of the 64-byte master key, read through `key`."""
        result = self.crypt("--batch-mode", "luksDump", "--dump-master-key",
                            "--key-file", str(key), fx["root_loop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        start = next(i for i, line in enumerate(lines) if "MK dump:" in line)
        hexed = "".join(line.replace("MK dump:", "") for line in lines[start:])
        return hashlib.sha256(bytes.fromhex("".join(hexed.split()))).hexdigest()

    def opens(self, fx: dict, key: pathlib.Path) -> bool:
        return self.crypt("luksOpen", "--test-passphrase", "--key-file", str(key),
                          fx["root_loop"]).returncode == 0

    def mount_boot(self, fx: dict) -> None:
        """Mount the boot partition even if a killed run left it mounted."""
        self.umount(fx["mnt"], check=False)
        self.mount(fx["boot_loop"], fx["mnt"])

    def unseal(self, fx: dict, out: pathlib.Path) -> pathlib.Path:
        blob = fx["mnt"] / "odile" / "luks-vol.key.sealed"
        self.mount_boot(fx)
        try:
            shutil.copyfile(blob, fx["base"] / "blob.copy")
        finally:
            self.umount(fx["mnt"])
        result = subprocess.run([str(self.standin), "--unseal",
                                 str(fx["base"] / "blob.copy"), str(out)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return out

    def boot_files(self, fx: dict) -> dict:
        files = {}
        self.mount_boot(fx)
        try:
            for name in ("luks-vol.key.sealed", "luks-vol.keyslot", "luks-bootstrap.key"):
                path = fx["mnt"] / "odile" / name
                files[name] = path.read_bytes() if path.exists() else None
        finally:
            self.umount(fx["mnt"])
        return files

    def keyslots(self, fx: dict) -> list[str]:
        result = self.crypt("luksDump", fx["root_loop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line.split(":")[0].strip() for line in result.stdout.splitlines()
                if line.startswith("  ") and line.rstrip().endswith(": luks2")]

    # -- the gate's own collision -----------------------------------------

    def test_two_opens_of_one_volume_take_distinct_names_and_both_close(self) -> None:
        """The bug this card fixes: a fixed name made a concurrent gate red.

        Two `make check` runs on one box both used `odile-rekey-check` -- on
        their own loop devices -- so the second `luksOpen` failed with exit 32
        ("device or resource busy"). These two mappings are open at once, each
        on the loop device a concurrent run would own, and both read the
        tenant's filesystem.
        """
        left = self.fixture()
        right = self.fixture()
        with self.mapped(left["root_loop"], left["build"], "check") as (first, first_dev):
            with self.mapped(right["root_loop"], right["build"], "check") as (second, second_dev):
                self.assertNotEqual(first, second)
                self.assertNotEqual(left["root_loop"], right["root_loop"])
                for device, fx in ((first_dev, left), (second_dev, right)):
                    mnt = pathlib.Path(tempfile.mkdtemp(dir=fx["base"]))
                    self.mount(device, mnt, "-o", "ro")
                    try:
                        self.assertEqual((mnt / "marker.txt").read_text(),
                                         "the tenant's own filesystem\n")
                    finally:
                        self.umount(mnt)
        for name in (first, second):
            self.assertNotEqual(self.crypt("status", name).returncode, 0,
                                f"{name} survived its context")

    def test_a_failed_body_does_not_leave_the_mapping_open(self) -> None:
        fx = self.fixture()
        name = ""
        with self.assertRaises(RuntimeError):
            with self.mapped(fx["root_loop"], fx["build"], "check") as (name, _device):
                raise RuntimeError("the body failed")
        self.assertNotEqual(self.crypt("status", name).returncode, 0,
                            f"{name} survived a failed test")

    def test_an_orphan_mapping_is_swept_and_a_live_run_is_left_alone(self) -> None:
        """A killed run's mapping is closed; a concurrent run's is not.

        `dmsetup create` makes the two mappings a run can leave: one named
        for this live pid and one the sweep is told is dead. Both carry this
        run's random tag and a live pid, so a sibling gate sees a live owner
        and leaves them alone; the sweep is the only thing that closes one.
        """
        tag = os.urandom(4).hex()
        live = f"odile-rekey-check-{os.getpid()}-{tag}-1"
        orphan = f"odile-rekey-check-{os.getppid()}-{tag}-2"
        for name in (live, orphan):
            made = subprocess.run(["dmsetup", "create", name, "--table", "0 1 zero"],
                                  capture_output=True, text=True, env=self.env())
            if made.returncode != 0:
                self.skipTest(f"dmsetup create is unavailable: {made.stderr.strip()}")
            self.addCleanup(subprocess.run, ["dmsetup", "remove", name],
                            capture_output=True, text=True, env=self.env())
        self.assertEqual(self.crypt("status", live).returncode, 0)
        self.assertEqual(self.crypt("status", orphan).returncode, 0)
        type(self).close_stale_mappers(dead={os.getppid()})
        self.assertEqual(self.crypt("status", live).returncode, 0,
                         "the sweep closed a live pid's mapping")
        self.assertNotEqual(self.crypt("status", orphan).returncode, 0,
                            "the sweep left a declared-dead pid's mapping")

    def test_a_mounted_orphans_mapping_is_unmounted_and_closed(self) -> None:
        """A SIGKILL leaves a mounted mapping; the sweep must recover it.

        The mount outlives the process but has no process behind it -- only
        the kernel holds the device -- so `openers` stays 1. The old sweep
        read that as "someone is using it" and skipped the orphan forever.
        This is the shape the verifier reproduced: killed while mounted. The
        name carries a live pid (the parent make) and this run's random tag,
        and the sweep is told that pid is dead, so a concurrent gate neither
        creates the same name nor sweeps it.
        """
        fx = self.fixture()
        tag = os.urandom(4).hex()
        name = f"odile-rekey-check-{os.getppid()}-{tag}-1"
        opened = self.crypt("--key-file", str(fx["build"]), "luksOpen",
                            fx["root_loop"], name)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.addCleanup(subprocess.run, ["dmsetup", "remove", name], capture_output=True)
        mnt = pathlib.Path(tempfile.mkdtemp(dir=fx["base"]))
        self.addCleanup(subprocess.run, ["umount", str(mnt)], capture_output=True)
        subprocess.run(["mount", "-o", "ro", f"/dev/mapper/{name}", str(mnt)],
                       check=True, timeout=60)
        self.assertTrue(mnt.is_mount())
        self.assertEqual(self.crypt("status", name).returncode, 0,
                         "the fixture's mapping did not open")
        type(self).close_stale_mappers(dead={os.getppid()})
        self.assertNotEqual(self.crypt("status", name).returncode, 0,
                            "the sweep left a dead run's mounted mapping")
        self.assertFalse(mnt.is_mount(), "the sweep left the dead run's mount")

    def _child_driver(self) -> pathlib.Path:
        """The program the SIGTERM test starts: this class's own code, held open."""
        path = self.dir / "held-mapping.py"
        path.write_text(
            "import json, pathlib, sys, time\n"
            f"sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
            "import test_sealed_image as suite\n"
            "T = suite.FirstBootReKeyTests\n"
            "T.setUpClass()\n"
            "case = T(methodName='test_two_opens_of_one_volume_take_distinct_names_and_both_close')\n"
            "fx = case.fixture()\n"
            "name, device = case.open_mapper(fx['root_loop'], fx['build'], 'probe')\n"
            "held = fx['base'] / 'held.mnt'\n"
            "held.mkdir()\n"
            "case.mount(device, held, '-o', 'ro')\n"
            "print(json.dumps({'name': name, 'device': device, 'mount': str(held),\n"
            "                  'loops': [fx['root_loop'], fx['boot_loop']]}), flush=True)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        return path

    def _child_ready(self, proc: subprocess.Popen) -> dict:
        """The first JSON line the child prints, or a failure with its stderr."""
        deadline = time.monotonic() + 180
        stdout = proc.stdout
        assert stdout is not None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([stdout], [], [], 0.2)
            if ready:
                line = stdout.readline()
                if line:
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            if proc.poll() is not None:
                break
        stderr = proc.stderr.read() if proc.poll() is not None and proc.stderr else ""
        self.fail(f"the child never mounted a mapping (rc={proc.poll()}):\n{stderr}")

    def _kill_child(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=60)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()

    def test_a_sigterm_held_across_a_mount_leaves_no_mapping(self) -> None:
        """DoD 2: a run interrupted with a mounted mapping leaves nothing.

        The old handler called `cryptsetup close` while the mount still held
        the device: twenty retries over ~2.5 s, then the process died and both
        the mapping and its mount outlived it. The next run's sweep skipped
        the mapping because `openers` stayed 1, so the leak was permanent.
        This holds the class's own fixture open in a child, sends SIGTERM, and
        asks the box: no mapping, no mount.
        """
        env = self.env()
        env["TMPDIR"] = str(self.dir)  # the child's temp dirs die with the class
        proc = subprocess.Popen(["/usr/bin/python3", "-u", str(self._child_driver())],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True)
        self.addCleanup(self._kill_child, proc)
        info = self._child_ready(proc)
        self.addCleanup(subprocess.run, ["losetup", "-d", *info["loops"]],
                        capture_output=True, text=True)
        self.addCleanup(subprocess.run, ["dmsetup", "remove", info["name"]],
                        capture_output=True, text=True)
        self.addCleanup(subprocess.run, ["umount", info["mount"]],
                        capture_output=True, text=True)
        self.assertTrue(pathlib.Path(info["mount"]).is_mount(),
                        "the child never mounted the mapping")
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=180), -signal.SIGTERM,
                         "the SIGTERM handler did not end the process")
        self.assertNotEqual(self.crypt("status", info["name"]).returncode, 0,
                            f"{info['name']} outlived its run")
        self.assertFalse(pathlib.Path(info["mount"]).is_mount(),
                         "the mount outlived its run")
        self.assertEqual([name for name, _openers in self._mappings()
                          if name.startswith("odile-rekey-")
                          and f"-{proc.pid}-" in name], [])

    # -- the behaviour -----------------------------------------------------

    def test_the_first_boot_replaces_the_build_volume_key(self) -> None:
        fx = self.fixture()
        before = self.master_key(fx, fx["build"])
        self.assertTrue(self.opens(fx, fx["build"]))
        result = self.run_provision(fx)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("re-keying", result.stdout)
        self.assertIn("re-keyed: volume key", result.stdout)
        self.assertIn("shredded the first-boot bootstrap key", result.stdout)
        # The definitive fact: the volume key is not the published image's.
        guest = self.unseal(fx, fx["base"] / "guest.key")
        self.assertNotEqual(before, self.master_key(fx, guest))
        # The image's own way in is gone, file and keyslot alike.
        self.assertFalse(self.opens(fx, fx["build"]))
        files = self.boot_files(fx)
        self.assertIsNone(files["luks-bootstrap.key"])
        self.assertIsNotNone(files["luks-vol.key.sealed"])
        self.assertEqual(len(self.keyslots(fx)), 1)
        # And the tenant's data came through the online re-encryption intact.
        self.assertTrue(self.opens(fx, guest))
        rootmnt = fx["base"] / "check.mnt"
        rootmnt.mkdir()
        with self.mapped(fx["root_loop"], guest, "check") as (_name, device):
            self.mount(device, rootmnt, "-o", "ro")
            try:
                self.assertEqual((rootmnt / "marker.txt").read_text(), "the tenant's own filesystem\n")
                self.assertEqual(hashlib.sha256((rootmnt / "blob").read_bytes()).hexdigest(),
                                 fx["marker"].read_text().split()[0])
            finally:
                self.umount(rootmnt)
        state = json.loads(fx["state"].read_text())
        self.assertTrue(state["sealed"])
        self.assertTrue(state["bootstrap_removed"])

    def test_an_interrupted_rekey_is_resumed_on_the_next_run(self) -> None:
        """A stop mid-re-key must be finished by the next run, not fatal.

        The header is left in `Requirements: online-reencrypt-*` with the
        volume key the pass generated already wrapped by the blob's keyslot, and
        the image's build key no longer opens it. The next run must unseal V,
        repair the metadata, resume, and then finalize: one keyslot, the
        bootstrap file shredded, seal.json written, the same volume key -- not a
        third one -- and the tenant's data intact.
        """
        fx = self.fixture(self.REKEY_VOLUME)
        before = self.master_key(fx, fx["build"])
        self.interrupt_rekey(fx)
        self.assertFalse(self.opens(fx, fx["build"]))
        guest = self.unseal(fx, fx["base"] / "guest.key")
        self.assertTrue(self.opens(fx, guest))
        self.assertNotEqual(before, self.master_key(fx, guest))
        files = self.boot_files(fx)
        self.assertIsNotNone(files["luks-vol.key.sealed"])
        self.assertIsNotNone(files["luks-bootstrap.key"])

        again = self.run_provision(fx)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("resuming the interrupted re-encryption", again.stdout)
        self.assertNotIn("re-keying ", again.stdout)
        self.assertIn("shredded the first-boot bootstrap key", again.stdout)
        self.assertEqual(self.master_key(fx, guest), self.master_key(
            fx, self.unseal(fx, fx["base"] / "guest2.key")))
        self.assertFalse(self.opens(fx, fx["build"]))
        self.assertEqual(len(self.keyslots(fx)), 1)
        self.assertIsNone(self.boot_files(fx)["luks-bootstrap.key"])
        state = json.loads(fx["state"].read_text())
        self.assertTrue(state["sealed"])
        self.assertTrue(state["bootstrap_removed"])
        # The data pass the interrupted run started is now whole: the filesystem
        # checks clean and the far end of the volume reads.
        rootmnt = fx["base"] / "resume.mnt"
        rootmnt.mkdir()
        with self.mapped(fx["root_loop"], guest, "resume") as (_name, device):
            if shutil.which("e2fsck"):
                fsck = subprocess.run(["e2fsck", "-fn", device],
                                      capture_output=True, text=True, timeout=300)
                self.assertEqual(fsck.returncode, 0, fsck.stdout + fsck.stderr)
            self.mount(device, rootmnt, "-o", "ro")
            try:
                self.assertEqual((rootmnt / "marker.txt").read_text(),
                                 "the tenant's own filesystem\n")
                self.assertEqual((rootmnt / "tail.txt").read_text(),
                                 "the far end of the filesystem\n")
                self.assertEqual(hashlib.sha256((rootmnt / "blob").read_bytes()).hexdigest(),
                                 fx["marker"].read_text().split()[0])
            finally:
                self.umount(rootmnt)

    def test_a_second_run_writes_nothing_and_a_rotate_keeps_the_guest_key(self) -> None:
        """The keyslot dance is the fallback: a new V, the same volume key."""
        fx = self.fixture()
        self.assertEqual(self.run_provision(fx).returncode, 0)
        keyslots = self.keyslots(fx)
        sealed = self.boot_files(fx)["luks-vol.key.sealed"]
        first = self.unseal(fx, fx["base"] / "guest.key")
        volume = self.master_key(fx, first)

        state_before = fx["state"].read_text()
        second = self.run_provision(fx)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already provisioned", second.stdout)
        self.assertIn("state already final", second.stdout)
        self.assertNotIn("re-keyed", second.stdout)
        self.assertEqual(self.keyslots(fx), keyslots)
        self.assertEqual(self.boot_files(fx)["luks-vol.key.sealed"], sealed)
        self.assertEqual(self.master_key(fx, fx["base"] / "guest.key"), volume)
        # "writes nothing" is literal: the state file keeps its bytes.
        self.assertEqual(fx["state"].read_text(), state_before)

        rotated = self.run_provision(fx, "--rotate")
        self.assertEqual(rotated.returncode, 0, rotated.stdout + rotated.stderr)
        self.assertNotIn("re-keyed", rotated.stdout)
        self.assertIn("killed the previous keyslot", rotated.stdout)
        replacement = self.unseal(fx, fx["base"] / "guest2.key")
        self.assertNotEqual(first.read_bytes(), replacement.read_bytes())
        self.assertFalse(self.opens(fx, first))
        self.assertTrue(self.opens(fx, replacement))
        self.assertEqual(self.master_key(fx, replacement), volume)
        self.assertEqual(len(self.keyslots(fx)), 1)

    def test_a_run_killed_after_the_rekey_is_finalized_on_the_next_run(self) -> None:
        """The re-key commits, then the machine dies before the shred.

        That disk has the blob, the guest key and no build keyslot -- but the
        bootstrap file is still on the boot partition and /verify has no state.
        `already provisioned` must still finalize: shred the file and write
        seal.json. The bench found this exact shape when the first boot hung in
        the re-key's finalize (ODL-454 follow-up, 2026-11-13).
        """
        fx = self.fixture()
        self.assertEqual(self.run_provision(fx).returncode, 0)
        self.mount(fx["boot_loop"], fx["mnt"])
        try:
            shutil.copyfile(fx["build"], fx["mnt"] / "odile" / "luks-bootstrap.key")
            os.chmod(fx["mnt"] / "odile" / "luks-bootstrap.key", 0o600)
        finally:
            self.umount(fx["mnt"])
        fx["state"].unlink()

        again = self.run_provision(fx)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("already provisioned", again.stdout)
        self.assertIn("shredded the first-boot bootstrap key", again.stdout)
        self.assertIsNone(self.boot_files(fx)["luks-bootstrap.key"])
        state = json.loads(fx["state"].read_text())
        self.assertTrue(state["sealed"])
        self.assertTrue(state["bootstrap_removed"])
        self.assertEqual(len(self.keyslots(fx)), 1)


class ReleasesRouteTests(unittest.TestCase):
    """ODL-332: /releases/* is spliced before the site's catch-all, once."""

    LIVE = (
        "{\n\tadmin off\n}\n\n"
        "odile.fyi {\n"
        "\thandle {\n\t\treverse_proxy 127.0.0.1:8080\n\t}\n"
        "\t# BEGIN odile-seed\n\thandle @seed {\n\t}\n\t# END odile-seed\n"
        "}\n"
    )

    def setUp(self) -> None:
        self.module = _load("odile_deploy_releases", ROOT / "deploy" / "caddy" / "deploy_releases.py")
        self.fragment = (ROOT / "deploy" / "caddy" / "releases.caddy").read_text()

    def test_the_block_lands_inside_the_site_before_the_catch_all(self) -> None:
        spliced = self.module.splice(self.LIVE, self.fragment)
        self.assertLess(spliced.index("handle_path /releases/*"), spliced.index("# BEGIN odile-seed"))
        self.assertLess(spliced.index("odile.fyi {"), spliced.index("handle_path /releases/*"))
        self.assertEqual(spliced.count("handle_path /releases/*"), 1)

    def test_a_second_splice_is_the_same_file(self) -> None:
        once = self.module.splice(self.LIVE, self.fragment)
        self.assertEqual(self.module.splice(once, self.fragment), once)

    def test_an_unbalanced_marker_is_an_error_not_a_guess(self) -> None:
        broken = self.LIVE.replace("# END odile-seed", "")
        broken = broken.replace("# BEGIN odile-seed", "# BEGIN odile-releases\nhandle_path /x/* {\n}\n")
        with self.assertRaises(ValueError):
            self.module.splice(broken, self.fragment)


class ProductionStandInTests(unittest.TestCase):
    """ODL-332: the production image is not the bench image.

    The QEMU stand-ins (the smoke unit, its proxy sockets and the boot-report
    stand-in) are gated on the production marker; the machine's own pin is
    placed in the disk after `uki.sh` has measured the signed UKI.
    """

    def test_a_production_build_takes_no_guest_smoke(self) -> None:
        text = (IMAGE / "sealed-image.sh").read_text()
        self.assertIn('[ "$GUEST_SMOKE" = 1 ] && [ "$PRODUCTION" != 1 ]', text)

    def test_the_bench_attest_stand_in_is_skipped_in_production(self) -> None:
        text = (IMAGE / "install-stack.sh").read_text()
        self.assertIn('[ ! -f /etc/odile/production ]', text)
        guard = text.index('if [ "$unit" = odile-bench-attest.service ]')
        enable = text.index('systemctl enable "$unit"')
        self.assertLess(guard, enable)
        self.assertIn("Servury's own boot report", text)

    def test_place_pin_opens_the_disk_with_the_bootstrap_key(self) -> None:
        place = IMAGE / "place-pin.sh"
        self.assertTrue(place.is_file())
        self.assertTrue(os.access(place, os.X_OK))
        text = place.read_text()
        self.assertIn("/odile/luks-bootstrap.key", text)
        self.assertIn("/etc/odile/tenant-image.pin", text)
        self.assertIn("unshare -m", text)
        self.assertIn("--production", (IMAGE / "sealed-image.sh").read_text())

    def test_place_pin_refuses_a_missing_pin(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".raw") as disk:
            result = subprocess.run(
                ["bash", str(IMAGE / "place-pin.sh"), "--disk", disk.name,
                 "--pin", "/nonexistent.pin"],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no pin at /nonexistent.pin", result.stderr)


class RecoverySeedTests(unittest.TestCase):
    """ODL-332 round 7: the done page offered no recovery file.

    The first-visit page is reachable as soon as Caddy answers; the release
    installer writes /etc/odile/recovery.txt only near the end of its run. A
    user who set the sign-in inside that window got a done page saying "The
    recovery file is not available on this machine" and no download (measured:
    page at 09:17:37.890Z, file at 09:17:37.994Z). The first-visit unit now
    seeds the file first. These tests run the program and compare its bytes
    with the installer's own renderer, so the two formats cannot drift.
    """

    SCRIPT = IMAGE / "recovery-seed.py"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.path = self.dir / "recovery.txt"
        self.cmdline = self.dir / "cmdline"
        self.cmdline.write_text(
            "root=/dev/mapper/root_crypt ro console=ttyS0 bb.release=2026.11.2"
            " bb.host=odl-fake01 bb.iid=0badc0de seal_id=0123456789abcdef\n",
            encoding="utf-8")
        self.installer = _load("odile_install_recovery_seed",
                               ROOT / "installer" / "odile_install.py")

    def run_seed(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), "--path", str(self.path),
             "--cmdline", str(self.cmdline), *extra],
            capture_output=True, text=True, timeout=60)

    def run_seed_stdin(self, password: str) -> subprocess.CompletedProcess:
        """A rebuild's call: the password on stdin, never on argv."""
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), "--path", str(self.path),
             "--tenant", "odl-fake01", "--instance", "0badc0de", "--password-stdin"],
            input=password, capture_output=True, text=True, timeout=60)

    def test_a_seeded_password_replaces_a_generated_one_and_keeps_written(self) -> None:
        first = self.run_seed()
        self.assertEqual(first.returncode, 0, first.stderr)
        generated = self.installer.parse_recovery(self.path.read_text(encoding="utf-8"))
        password = "test-rebuild-password-2026-0001"
        result = self.run_seed_stdin(password)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parsed = self.installer.parse_recovery(self.path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["backup_password"], password)
        self.assertNotEqual(parsed["backup_password"], generated["backup_password"])
        self.assertEqual(parsed["written"], generated["written"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertIn("the password from the seed", result.stdout)
        self.assertNotIn(password, result.stdout)
        self.assertNotIn(password, result.stderr)

    def test_the_stdin_reader_refuses_a_short_blank_or_spaced_value(self) -> None:
        self.assertEqual(self.run_seed().returncode, 0)
        before = self.path.read_bytes()
        for bad in ("short", "", "two words 0123456789abcdef"):
            result = self.run_seed_stdin(bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn("recovery-seed:", result.stderr)
            self.assertEqual(self.path.read_bytes(), before, bad)

    def test_the_seed_is_the_installers_own_recovery_file(self) -> None:
        result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        text = self.path.read_text(encoding="utf-8")
        parsed = self.installer.parse_recovery(text)
        self.assertEqual(len(parsed.get("backup_password", "")), 32)
        # `ensure_recovery` adopts the password and Written it finds and
        # rewrites from its own renderer; that renderer must produce exactly
        # these bytes from the same inputs.
        self.assertEqual(
            text,
            self.installer.render_recovery(
                "odl-fake01", "0badc0de", parsed["backup_password"], None,
                "odl-fake01.odile.fyi", written=parsed["written"]))
        self.assertIn("Certificate SHA-256: (pending", text)

    def test_a_second_run_leaves_the_file_byte_identical(self) -> None:
        first = self.run_seed()
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.path.read_bytes()
        stamp = self.path.stat().st_mtime_ns
        second = self.run_seed()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)

    def test_an_installers_recovery_file_is_not_replaced(self) -> None:
        canonical = self.installer.render_recovery(
            "odl-fake01", "0badc0de", "Z" * 32, "sha256:" + "a" * 64,
            "odl-fake01.odile.fyi", written="2026-11-12T09:17:37Z")
        self.path.write_text(canonical, encoding="utf-8")
        os.chmod(self.path, 0o600)
        result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_text(encoding="utf-8"), canonical)

    def test_the_seed_names_the_machine_from_the_command_line(self) -> None:
        result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = self.installer.parse_recovery(self.path.read_text(encoding="utf-8"))
        self.assertIn("odl-fake01", self.path.read_text(encoding="utf-8"))
        self.assertIn("0badc0de", self.path.read_text(encoding="utf-8"))
        self.assertIn("written", parsed)
        # A command line with no bb.host writes nothing rather than a file
        # naming the wrong machine.
        self.cmdline.write_text("root=/dev/mapper/root_crypt ro\n", encoding="utf-8")
        self.path.unlink()
        result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.path.exists())

    def test_the_first_visit_unit_seeds_before_it_serves(self) -> None:
        unit = (IMAGE / "odile-first-visit.service").read_text(encoding="utf-8")
        self.assertIn(
            "ExecStartPre=/usr/bin/python3 /usr/local/lib/odile/recovery-seed.py",
            unit)
        self.assertIn("--path /etc/odile/recovery.txt", unit)
        self.assertLess(unit.index("ExecStartPre="), unit.index("\nExecStart="))
        stack = (IMAGE / "install-stack.sh").read_text(encoding="utf-8")
        self.assertIn(
            'install_file "$HERE/recovery-seed.py" /usr/local/lib/odile/recovery-seed.py 0755',
            stack)
        image = (IMAGE / "sealed-image.sh").read_text(encoding="utf-8")
        self.assertIn("recovery-seed.py", image)


class BuildMirrorTests(unittest.TestCase):
    """t_84e94f4b: the disk is debootstrapped from the installer's snapshot.

    The first boot's release installer installs nine OS packages from
    `snapshot.debian.org/archive/debian/<OS_SNAPSHOT>` and refuses a different
    version rather than fall back. The disk must therefore carry exactly that
    set: built from `deb.debian.org`, which takes a point release every few
    weeks, the disk carried curl 8.14.1-2+deb13u7 and openssl 3.5.9-1+deb13u1
    against the pins +deb13u5 and 3.5.7-1~deb13u2, and the fresh boot ended
    `=== rc=1` at "the pinned OS packages are not in the snapshot" (measured
    2026-11-21, t_30da4de3; the same tree built from the snapshot converged).

    The default mirror is read out of `installer/odile_install.py`, not
    repeated in the script, so a bump of OS_SNAPSHOT moves the build with it.
    """

    SCRIPT = IMAGE / "sealed-image.sh"
    INSTALLER = ROOT / "installer" / "odile_install.py"

    @staticmethod
    def pinned(text: str) -> str:
        match = re.search(r'^OS_SNAPSHOT = "([0-9A-Za-z]+)"$', text, re.M)
        assert match is not None, "installer/odile_install.py has no OS_SNAPSHOT"
        return match.group(1)

    def dry_run(self, script: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run(
                ["bash", str(script), "--dry-run", "--out", str(pathlib.Path(tmp) / "out"), *args],
                capture_output=True, text=True, timeout=60,
            )

    def test_the_default_mirror_is_the_pinned_snapshot(self) -> None:
        snapshot = self.pinned(self.INSTALLER.read_text(encoding="utf-8"))
        result = self.dry_run(self.SCRIPT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"https://snapshot.debian.org/archive/debian/{snapshot}", result.stdout)
        self.assertNotIn("deb.debian.org", result.stdout)

    def test_a_bump_of_the_pin_moves_the_build_mirror(self) -> None:
        """One date, not two: the mirror follows the installer's OS_SNAPSHOT."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            (tree / "deploy" / "image").mkdir(parents=True)
            (tree / "installer").mkdir()
            script = tree / "deploy" / "image" / "sealed-image.sh"
            shutil.copy2(self.SCRIPT, script)
            text = self.INSTALLER.read_text(encoding="utf-8")
            moved = "20991231T235959Z"
            text, count = re.subn(r'^OS_SNAPSHOT = "[^"]+"$',
                                  f'OS_SNAPSHOT = "{moved}"', text, count=1, flags=re.M)
            self.assertEqual(count, 1)
            (tree / "installer" / "odile_install.py").write_text(text, encoding="utf-8")
            result = self.dry_run(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"https://snapshot.debian.org/archive/debian/{moved}", result.stdout)
        self.assertNotIn(self.pinned(self.INSTALLER.read_text(encoding="utf-8")), result.stdout)

    def test_an_explicit_mirror_still_overrides(self) -> None:
        result = self.dry_run(self.SCRIPT, "--mirror", "http://mirror.example.invalid/debian")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("http://mirror.example.invalid/debian", result.stdout)
        self.assertIn("not the release installer's snapshot", result.stdout)

    def test_the_help_states_the_rule_and_prints_the_date(self) -> None:
        result = subprocess.run(["bash", str(self.SCRIPT), "--help"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("snapshot.debian.org", result.stdout)
        self.assertIn(self.pinned(self.INSTALLER.read_text(encoding="utf-8")), result.stdout)
        self.assertIn("deb.debian.org", result.stdout)


if __name__ == "__main__":
    unittest.main()
