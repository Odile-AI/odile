#!/usr/bin/python3
"""Tests for ODL-208: the handoff audit.

Hermetic: every fixture is a machine in a temp directory (a cmdline, a passwd,
a shadow, an sshd configuration, a LUKS header dump and an sshd log), and the
script is exercised through its command line as well as its functions. No
tenant, no root, no network, no sleeps.

The card's four lines, one group of tests each:

* the audit passes on a fresh machine and fails when a second key is planted;
* the login list contains only the boot key's fingerprint;
* the output is the report `/verify` can quote (rows, commands, provenance);
* the script is inside the installer's published manifest (the installer
  embeds it, `--manifest` names its hash and `ensure_verify_surface` places it
  at `/usr/local/bin/odile-audit` — `InstallerManifestTest` below).

    /usr/bin/python3 -m unittest tests.test_audit -v
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "audit.py"

FIXED = datetime(2026, 11, 12, 18, 0, 0, tzinfo=timezone.utc)


def load_module():
    spec = importlib.util.spec_from_file_location("odile_audit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load_module()


class Fixture:
    """One machine root plus the fixture paths the script reads."""

    def __init__(self, temporary: str, plants: tuple[str, ...] = ()) -> None:
        self.root = Path(temporary)
        self.commands = audit.build_fixture(self.root, plants=plants)

    def machine(self, **overrides):
        values = dict(root=str(self.root), cmdline=str(self.commands["cmdline"]),
                      sshd_effective=str(self.commands["sshd"]),
                      luks_dump=str(self.commands["luks"]),
                      auth_log=str(self.commands["auth"]), user="tenant", now=FIXED)
        values.update(overrides)
        return audit.Machine(**values)

    def results(self, **overrides):
        return audit.audit(self.machine(**overrides))

    def failed(self, **overrides):
        return [result.name for result in self.results(**overrides) if not result.ok]

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")


class CleanMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name)

    def test_every_check_passes_and_the_rows_are_the_ten_named_checks(self):
        results = self.fixture.results()
        self.assertEqual([result.name for result in results], list(audit.CHECK_NAMES))
        self.assertTrue(all(result.ok for result in results),
                        [(result.name, result.detail) for result in results if not result.ok])

    def test_the_summary_counts_and_names_the_checks(self):
        summary = audit.summary(self.fixture.results())
        self.assertTrue(summary.startswith("PASS — 10 checks:"), summary)
        self.assertIn("one LUKS keyslot", summary)
        self.assertIn("login history", summary)

    def test_every_row_carries_the_command_that_produced_it(self):
        for result in self.fixture.results():
            self.assertTrue(result.command, "no command for %r" % result.name)

    def test_the_report_has_a_provenance_line_and_no_shell_prompt(self):
        body = audit.render(self.fixture.results(), stamp=FIXED)
        self.assertTrue(body.startswith("read from your machine, 2026-11-12T18:00:00Z\n"), body)
        self.assertNotIn("$ ", body)
        self.assertIn("odile-audit — the handoff audit", body)
        self.assertNotIn("\x1b[", body)

    def test_the_login_list_is_printed_with_its_fingerprint(self):
        body = audit.render(self.fixture.results(), stamp=FIXED)
        self.assertIn("login tenant 203.0.113.9 SHA256:I2VaMwa+", body)

    def test_no_passphrase_is_read_or_opened(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("cryptsetup open", source)
        self.assertNotIn("luksOpen", source)
        self.assertNotIn("luksAddKey", source)
        # The only cryptsetup invocation is the header dump.
        self.assertEqual(re.findall(r'"cryptsetup",\s*"([a-zA-Z]+)"', source), ["status", "luksDump"])

    def test_the_script_writes_nothing_except_the_optional_report(self):
        import inspect
        for function in audit.CHECKS + (audit.audit,):
            source = inspect.getsource(function)
            self.assertNotIn("write_text(", source, function.__name__)
            self.assertNotIn("open(", source, function.__name__)


class PlantedKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)

    def test_a_second_key_in_the_tenants_authorized_keys_fails_the_audit(self):
        fixture = Fixture(self._tmp.name, plants=("second-key",))
        self.assertEqual(fixture.failed(), ["authorized keys"])
        detail = [r.detail for r in fixture.results() if r.name == "authorized keys"][0]
        self.assertIn("not the measured boot key", detail)
        self.assertIn("SHA256:O1p0nW2V1kjA4bY5sNrJJOlnzxxLbi/T0ntYCi1q8tM", detail)

    def test_a_second_key_in_roots_authorized_keys2_fails_the_audit(self):
        # sshd's default AuthorizedKeysFile is .ssh/authorized_keys and
        # .ssh/authorized_keys2; a plant in the second file opens the machine.
        fixture = Fixture(self._tmp.name)
        plant = fixture.root / "root" / ".ssh" / "authorized_keys2"
        plant.write_text(audit.FIXTURE_OTHER_KEY + "\n", encoding="utf-8")
        os.chmod(plant, 0o600)
        self.assertEqual(fixture.failed(), ["authorized keys"])
        detail = [r.detail for r in fixture.results() if r.name == "authorized keys"][0]
        self.assertIn("/root/.ssh/authorized_keys2:1", detail)

    def test_a_second_key_in_the_tenants_authorized_keys2_fails_the_audit(self):
        fixture = Fixture(self._tmp.name)
        plant = fixture.root / "home" / "tenant" / ".ssh" / "authorized_keys2"
        plant.write_text(audit.FIXTURE_OTHER_KEY + "\n", encoding="utf-8")
        os.chmod(plant, 0o600)
        self.assertEqual(fixture.failed(), ["authorized keys"])
        detail = [r.detail for r in fixture.results() if r.name == "authorized keys"][0]
        self.assertIn("/home/tenant/.ssh/authorized_keys2:1", detail)

    def test_a_second_key_fails_even_when_only_the_tenant_home_has_it(self):
        fixture = Fixture(self._tmp.name)
        plant = audit.FIXTURE_OTHER_KEY + "\n"
        with (fixture.root / "home" / "tenant" / ".ssh" / "authorized_keys").open("a") as handle:
            handle.write(plant)
        self.assertEqual(fixture.failed(), ["authorized keys"])

    def test_a_forced_command_line_fails_the_operator_key_check(self):
        fixture = Fixture(self._tmp.name)
        line = ('command="/usr/local/bin/odile-support",no-pty %s\n'
                % audit.FIXTURE_BOOT_KEY)
        with (fixture.root / "root" / ".ssh" / "authorized_keys").open("a") as handle:
            handle.write(line)
        self.assertIn("operator key", fixture.failed())

    def test_a_key_whose_comment_names_support_fails_the_operator_key_check(self):
        fixture = Fixture(self._tmp.name)
        line = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU5Pru+2th/4Bdp7sk96CHqbhvX7uJ4D2WbNz"
                "GnL+Cj odile-support\n")
        with (fixture.root / "root" / ".ssh" / "authorized_keys").open("a") as handle:
            handle.write(line)
        self.assertIn("authorized keys", fixture.failed())
        self.assertIn("operator key", fixture.failed())

    def test_an_operator_account_fails(self):
        fixture = Fixture(self._tmp.name)
        withFixture = fixture.path("etc/passwd")
        withFixture.write_text(withFixture.read_text() + "operator:x:1001:1001::/home/operator:/bin/bash\n",
                               encoding="utf-8")
        self.assertIn("operator account", fixture.failed())

    def test_a_second_uid_zero_account_fails(self):
        fixture = Fixture(self._tmp.name)
        path = fixture.path("etc/passwd")
        path.write_text(path.read_text() + "toor:x:0:0::/root:/bin/bash\n", encoding="utf-8")
        self.assertIn("operator account", fixture.failed())

    def test_a_private_half_beside_a_matching_public_key_fails_the_boot_key(self):
        fixture = Fixture(self._tmp.name)
        directory = fixture.root / "root" / ".ssh"
        (directory / "id_ed25519.pub").write_text(audit.FIXTURE_BOOT_KEY + "\n", encoding="utf-8")
        (directory / "id_ed25519").write_text(
            "-----BEGIN OPENSSH " + "PRIVATE" + " " + "KEY-----\n"
            "not-a-real-key\n" * 20
            + "-----END OPENSSH " + "PRIVATE" + " " + "KEY-----\n", encoding="utf-8")
        self.assertEqual(fixture.failed(), ["boot key"])
        detail = [r.detail for r in fixture.results() if r.name == "boot key"][0]
        self.assertIn("private half is on the machine", detail)


class PrivateHalfTest(unittest.TestCase):
    """The boot key's private half is a sin wherever the strict bar looks:
    ``/root``, every ``/home``, ``/etc/odile`` and ``/var/lib/odile``, with or
    without a ``.pub`` sibling beside it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)

    def keypair(self, name: str = "id_ed25519") -> tuple[Path, str]:
        directory = Path(self._tmp.name) / "keys"
        directory.mkdir(exist_ok=True)
        private = directory / name
        done = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        public = (directory / (name + ".pub")).read_text(encoding="utf-8").strip()
        return private, public

    def use_key(self, fixture: Fixture, public: str) -> None:
        """Make ``public`` the measured boot key everywhere the fixture reads."""
        parsed = audit.parse_public_key(public)
        self.assertIsNotNone(parsed)
        cmdline = fixture.commands["cmdline"]
        token = base64.b64encode(public.encode()).decode()
        cmdline.write_text(re.sub(r"bb\.key=\S+", "bb.key=" + token, cmdline.read_text()),
                           encoding="utf-8")
        for home in ("root", "home/tenant"):
            (fixture.root / home / ".ssh" / "authorized_keys").write_text(
                public + "\n", encoding="utf-8")
        fixture.commands["auth"].write_text(
            "Accepted publickey for tenant from 203.0.113.9 port 51514 ssh2: ED25519 %s\n"
            % audit.fingerprint(parsed[1]), encoding="utf-8")

    def test_the_boot_private_key_at_root_boot_key_with_no_pub_fails(self):
        # The strict bar names exactly this path (verify_and_unlock.sh: /root/
        # boot-key included); the audit must not PASS it.
        private, public = self.keypair()
        fixture = Fixture(self._tmp.name)
        self.use_key(fixture, public)
        target = fixture.root / "root" / "boot-key"
        target.write_bytes(private.read_bytes())
        os.chmod(target, 0o600)
        self.assertEqual(fixture.failed(), ["boot key"])
        detail = [r.detail for r in fixture.results() if r.name == "boot key"][0]
        self.assertIn("private half is on the machine: /root/boot-key", detail)

    def test_an_unrelated_private_key_under_root_is_not_the_boot_key(self):
        private, _public = self.keypair("other-key")
        fixture = Fixture(self._tmp.name)
        target = fixture.root / "root" / "other-key"
        target.write_bytes(private.read_bytes())
        os.chmod(target, 0o600)
        self.assertEqual(fixture.failed(), [])

    def test_a_non_ssh_private_key_under_root_is_not_the_boot_key(self):
        # A TLS key is a private key, not the boot key's half.
        fixture = Fixture(self._tmp.name)
        target = fixture.root / "root" / "tls.key"
        target.write_bytes(b"-----BEGIN " + b"PRIVATE KEY-----\n" + b"x" * 200
                           + b"\n-----END " + b"PRIVATE KEY-----\n")
        self.assertEqual(fixture.failed(), [])


class WalkCostTest(unittest.TestCase):
    """The boot-key row's walk is find(1)'s, not Python's (t_87943990)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)

    def keypair(self, name: str = "id_ed25519") -> tuple[Path, str]:
        directory = Path(self._tmp.name) / "keys"
        directory.mkdir(exist_ok=True)
        private = directory / name
        done = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        public = (directory / (name + ".pub")).read_text(encoding="utf-8").strip()
        return private, public

    def boot_key_fixture(self, public: str) -> Fixture:
        """A fixture whose measured boot key is ``public`` everywhere it reads."""
        fixture = Fixture(self._tmp.name)
        parsed = audit.parse_public_key(public)
        self.assertIsNotNone(parsed)
        cmdline = fixture.commands["cmdline"]
        token = base64.b64encode(public.encode()).decode()
        cmdline.write_text(re.sub(r"bb\.key=\S+", "bb.key=" + token, cmdline.read_text()),
                           encoding="utf-8")
        for home in ("root", "home/tenant"):
            (fixture.root / home / ".ssh" / "authorized_keys").write_text(
                public + "\n", encoding="utf-8")
        fixture.commands["auth"].write_text(
            "Accepted publickey for tenant from 203.0.113.9 port 51514 ssh2: ED25519 %s\n"
            % audit.fingerprint(parsed[1]), encoding="utf-8")
        return fixture

    def test_find_enumerates_and_head_reads_the_candidates(self):
        """os.scandir is never reached and no regular file's head is read in
        Python -- head(1) reads them, one batch per process -- while the
        planted key is still found. On the demo tenant the Python walk was
        6.3 s of the /verify page's 6.8 s (151,548 candidates, 170k of them
        under one home)."""
        private, public = self.keypair()
        fixture = self.boot_key_fixture(public)
        target = fixture.root / "home" / "tenant" / ".hermes" / "cache" / "x" / "id_ed25519"
        target.parent.mkdir(parents=True)
        target.write_bytes(private.read_bytes())
        os.chmod(target, 0o600)
        python_heads = []
        real_head = audit._head
        real_scandir = os.scandir
        private = tuple(str(fixture.root / part)
                        for part in ("root", "home", "etc/odile", "var/lib/odile"))

        def guarded_scandir(path="."):
            if str(path).startswith(private):
                raise AssertionError("the Python walk ran")
            return real_scandir(path)

        with mock.patch.object(audit.os, "scandir", side_effect=guarded_scandir), \
                mock.patch.object(audit, "_head",
                                  side_effect=lambda path: python_heads.append(path) or real_head(path)):
            failed = fixture.failed()
        self.assertEqual(failed, ["boot key"])
        self.assertEqual(python_heads, [], "a regular file's head was read in Python")

    def test_the_python_walk_answers_when_find_cannot(self):
        """The fallback is the old walk, and it reaches the same verdict."""
        private, public = self.keypair()
        fixture = self.boot_key_fixture(public)
        target = fixture.root / "home" / "tenant" / "boot-key"
        target.write_bytes(private.read_bytes())
        os.chmod(target, 0o600)
        with mock.patch.object(audit, "_scan_candidates", return_value=None):
            self.assertEqual(fixture.failed(), ["boot key"])

    def test_a_symlink_to_the_private_half_is_still_the_private_half(self):
        private, public = self.keypair()
        fixture = self.boot_key_fixture(public)
        elsewhere = fixture.root / "outside" / "key"
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(private.read_bytes())
        os.chmod(elsewhere, 0o600)
        os.symlink(elsewhere, fixture.root / "home" / "tenant" / "linked-key")
        self.assertEqual(fixture.failed(), ["boot key"])

    def test_a_private_root_that_is_itself_a_symlink_is_scanned(self):
        """find(1) must follow a command-line symlink, as the walk does.

        Measured on t_87943990: ``/var/lib/odile`` replaced by a symlink to a
        directory produced a well-formed empty stream from ``find`` without
        ``-H``, so the row printed PASS over a planted key while
        ``_walk_candidates`` found it. The installer creates real directories,
        so no shipped shape is affected; this pins the candidate set the row
        claims for every shape a machine can be in.
        """
        private, public = self.keypair()
        fixture = self.boot_key_fixture(public)
        # The private root the row scans is a symlink to a directory elsewhere
        # in the machine: only a walk that follows the argument reaches it.
        real = fixture.root / "var" / "lib" / "odile-real"
        target = real / "sub" / "key"
        target.parent.mkdir(parents=True)
        target.write_bytes(private.read_bytes())
        os.chmod(target, 0o600)
        linked = fixture.root / "var" / "lib" / "odile"
        linked.symlink_to(real)
        answers = []
        real_scan = audit._scan_candidates

        def spy(roots):
            found = real_scan(roots)
            answers.append(found)
            return found

        with mock.patch.object(audit, "_scan_candidates", side_effect=spy):
            self.assertEqual(fixture.failed(), ["boot key"])
        self.assertEqual(len(answers), 1)
        self.assertIsNotNone(answers[0], "the fast path fell back to the Python walk")
        detail = [r.detail for r in fixture.results() if r.name == "boot key"][0]
        self.assertIn("private half is on the machine: /var/lib/odile/sub/key", detail)

    def test_a_hung_find_cannot_hold_the_read(self):
        """WALK_TIMEOUT bounds the read, not only ``wait()`` (t_87943990).

        The first cut timed out ``wait()`` after ``stdout.read()`` had already
        returned, so a ``find`` hung on an unresponsive mount still held the
        page. The timeout now covers ``communicate`` and kills the group.
        """
        class Hung:
            pid = 4242

            def communicate(self, timeout: float = 0.0):
                raise subprocess.TimeoutExpired("find", timeout)

            def kill(self):
                raise AssertionError("only the group kill should run")

        killed = []
        with mock.patch.object(audit, "_fast_path_tools", return_value=True), \
                mock.patch.object(audit.subprocess, "Popen", return_value=Hung()), \
                mock.patch.object(audit, "_kill_group", side_effect=killed.append):
            self.assertIsNone(audit._scan_candidates([Path(self._tmp.name)]))
        self.assertEqual(len(killed), 1, "a hung find was not killed")

    def test_the_size_window_is_200_to_65536_bytes(self):
        private, public = self.keypair()
        fixture = self.boot_key_fixture(public)
        body = private.read_bytes()
        below = fixture.root / "root" / "below"
        below.write_bytes((body + b"x" * 199)[:199])
        os.chmod(below, 0o600)
        (fixture.root / "root" / "below.pub").write_text(public + "\n", encoding="utf-8")
        self.assertEqual(fixture.failed(), [], "a 199-byte file is outside the window")
        below.unlink()
        (fixture.root / "root" / "below.pub").unlink()
        inside = fixture.root / "root" / "at-least"
        inside.write_bytes((body + b"x" * 200)[:200])
        os.chmod(inside, 0o600)
        (fixture.root / "root" / "at-least.pub").write_text(public + "\n", encoding="utf-8")
        self.assertEqual(fixture.failed(), ["boot key"], "a 200-byte file is inside the window")

    def test_a_pub_sibling_is_not_a_candidate_but_a_copy_is(self):
        private, public = self.keypair()
        fixture = self.boot_key_fixture(public)
        for name in ("id_ed25519.pub", "authorized_keys.bak", "known_hosts.old"):
            target = fixture.root / "root" / name
            target.write_bytes(private.read_bytes())
            os.chmod(target, 0o600)
        self.assertEqual(fixture.failed(), [])
        copy = fixture.root / "root" / "id_ed25519"
        copy.write_bytes(private.read_bytes())
        os.chmod(copy, 0o600)
        self.assertEqual(fixture.failed(), ["boot key"])


class CmdlineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name)

    def test_no_boot_key_fails(self):
        self.fixture.commands["cmdline"].write_text("root=/dev/mapper/root_crypt ro\n", encoding="utf-8")
        # Without a boot key there is nothing to compare against, so the key
        # rows and the login list fail closed as well.
        self.assertEqual(self.fixture.failed(),
                         ["boot key", "authorized keys", "operator key", "login history"])

    def test_two_boot_keys_fail(self):
        text = self.fixture.commands["cmdline"].read_text()
        extra = "bb.key=" + text.split("bb.key=")[1].split()[0]
        self.fixture.commands["cmdline"].write_text(text + " " + extra, encoding="utf-8")
        self.assertIn("boot key", self.fixture.failed())
        detail = [r.detail for r in self.fixture.results() if r.name == "boot key"][0]
        self.assertIn("2 bb.key= tokens", detail)

    def test_a_boot_key_that_is_not_base64_fails(self):
        text = re.sub(r"bb\.key=\S+", "bb.key=not-base64!", self.fixture.commands["cmdline"].read_text())
        self.fixture.commands["cmdline"].write_text(text, encoding="utf-8")
        detail = [r.detail for r in self.fixture.results() if r.name == "boot key"][0]
        self.assertIn("does not decode", detail)


class SystemRowsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name)

    def test_root_with_a_password_hash_fails(self):
        shadow = self.fixture.path("etc/shadow")
        shadow.write_text(shadow.read_text().replace("root:*:", "root:$6$abc$def:", 1),
                          encoding="utf-8")
        self.assertEqual(self.fixture.failed(), ["root password"])

    def test_password_authentication_on_fails(self):
        sshd = self.fixture.commands["sshd"]
        sshd.write_text(sshd.read_text().replace("PasswordAuthentication no",
                                                 "PasswordAuthentication yes"), encoding="utf-8")
        self.assertEqual(self.fixture.failed(), ["password auth"])

    def test_a_drop_in_password_authentication_yes_beats_the_main_file(self):
        # OpenSSH reads a top-of-file Include first: the drop-in wins.
        main = self.fixture.path("etc/ssh/sshd_config")
        main.write_text("Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication no\n",
                        encoding="utf-8")
        drop_in = self.fixture.path("etc/ssh/sshd_config.d/50-test.conf")
        drop_in.parent.mkdir(parents=True, exist_ok=True)
        drop_in.write_text("PasswordAuthentication yes\n", encoding="utf-8")
        machine = audit.Machine(root=str(self.fixture.root), cmdline=str(self.fixture.commands["cmdline"]),
                                luks_dump=str(self.fixture.commands["luks"]),
                                auth_log=str(self.fixture.commands["auth"]), user="tenant")
        parsed = [line for line in machine._parsed_sshd_config().splitlines()
                  if line.lower().startswith("passwordauthentication")]
        self.assertEqual(parsed[0], "PasswordAuthentication yes")

    def test_a_second_keyslot_fails(self):
        fixture = Fixture(self._tmp.name, plants=("second-keyslot",))
        self.assertEqual(fixture.failed(), ["keyslots"])
        detail = [r.detail for r in fixture.results() if r.name == "keyslots"][0]
        self.assertIn("2 keyslots", detail)

    def test_a_luks_token_fails(self):
        dump = self.fixture.commands["luks"]
        dump.write_text(dump.read_text().replace(
            "Tokens:\n", "Tokens:\n  0: systemd-tpm2\n\tKeyslot:  0\n"), encoding="utf-8")
        self.assertEqual(self.fixture.failed(), ["keyslots"])

    def test_a_plain_ext4_root_is_not_a_luks_volume(self):
        dump = self.fixture.commands["luks"]
        dump.write_text("LUKS header information\nVersion: 2\n", encoding="utf-8")
        detail = [r.detail for r in self.fixture.results() if r.name == "keyslots"][0]
        self.assertIn("no keyslot", detail)


class GrantAndSocketTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)

    def test_an_active_grant_fails(self):
        fixture = Fixture(self._tmp.name, plants=("support-grant",))
        self.assertEqual(fixture.failed(), ["support grant"])
        detail = [r.detail for r in fixture.results() if r.name == "support grant"][0]
        self.assertIn("active until 2099-01-01T00:00:00Z", detail)

    def test_an_expired_grant_passes(self):
        fixture = Fixture(self._tmp.name)
        grant = fixture.path("etc/odile/support-grant.json")
        grant.parent.mkdir(parents=True, exist_ok=True)
        grant.write_text('{"reason": "done", "expires_at": "2026-11-01T00:00:00Z"}\n',
                         encoding="utf-8")
        self.assertNotIn("support grant", fixture.failed())

    def test_a_grant_file_with_no_expiry_fails_closed(self):
        fixture = Fixture(self._tmp.name)
        grant = fixture.path("etc/odile/support-grant.json")
        grant.parent.mkdir(parents=True, exist_ok=True)
        grant.write_text('{"reason": "no expiry"}\n', encoding="utf-8")
        self.assertIn("support grant", fixture.failed())

    def test_an_agent_socket_fails(self):
        fixture = Fixture(self._tmp.name)
        socket = fixture.root / "tmp" / "ssh-abc" / "agent.42"
        socket.parent.mkdir(parents=True)
        socket.write_text("socket", encoding="utf-8")
        self.assertEqual(fixture.failed(), ["agent socket"])

    def test_an_empty_live_agent_socket_is_the_logins_own_agent(self):
        original = audit._agent_holds
        audit._agent_holds = lambda path: ("empty", "no key loaded")
        self.addCleanup(setattr, audit, "_agent_holds", original)
        result = audit._agent_result(["/run/user/0/openssh_agent"], [], "cmd")
        self.assertTrue(result.ok, result.detail)
        self.assertIn("no key loaded", result.detail)

    def test_a_live_agent_holding_a_key_fails(self):
        original = audit._agent_holds
        audit._agent_holds = lambda path: ("keys", "ED25519 SHA256:abc card")
        self.addCleanup(setattr, audit, "_agent_holds", original)
        result = audit._agent_result(["/run/user/0/openssh_agent"], [], "cmd")
        self.assertFalse(result.ok)
        self.assertIn("holds a key", result.detail)

    def test_an_exported_agent_fails_even_when_the_socket_is_empty(self):
        result = audit._agent_result([], ["sshd matches SSH_AUTH_SOCK"], "cmd")
        self.assertFalse(result.ok)
        self.assertIn("SSH_AUTH_SOCK", result.detail)


class LoginHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)

    def test_a_foreign_fingerprint_fails(self):
        fixture = Fixture(self._tmp.name, plants=("foreign-login",))
        self.assertEqual(fixture.failed(), ["login history"])
        detail = [r.detail for r in fixture.results() if r.name == "login history"][0]
        self.assertIn("SHA256:O1p0nW2V1kjA4bY5sNrJJOlnzxxLbi/T0ntYCi1q8tM", detail)

    def test_a_password_login_fails(self):
        fixture = Fixture(self._tmp.name)
        fixture.commands["auth"].write_text(
            "Accepted password for tenant from 203.0.113.9 port 51514 ssh2\n", encoding="utf-8")
        detail = [r.detail for r in fixture.results() if r.name == "login history"][0]
        self.assertIn("password login succeeded", detail)

    def test_an_empty_log_is_a_pass_with_an_empty_list(self):
        fixture = Fixture(self._tmp.name)
        fixture.commands["auth"].write_text("", encoding="utf-8")
        # An empty log file is a source that was read; no log at all is a FAIL.
        self.assertIn("no sshd journal", [r.detail for r in fixture.results()
                                          if r.name == "login history"][0])
        fixture.commands["auth"].write_text("sshd[1]: Server listening on 0.0.0.0 port 22.\n",
                                            encoding="utf-8")
        results = [r for r in fixture.results() if r.name == "login history"]
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].lines, [])


class CommandLineTest(unittest.TestCase):
    """The CLI contract: exit codes, --json, --save, --self-test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="odile-audit-test-")
        self.addCleanup(self._tmp.cleanup)
        self.fixture = Fixture(self._tmp.name)

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments],
                              capture_output=True, text=True, timeout=60)

    def arguments(self, *extra: str) -> list[str]:
        return ["--root", str(self.fixture.root),
                "--cmdline", str(self.fixture.commands["cmdline"]),
                "--sshd-effective", str(self.fixture.commands["sshd"]),
                "--luks-dump", str(self.fixture.commands["luks"]),
                "--auth-log", str(self.fixture.commands["auth"]),
                "--user", "tenant", *extra]

    def test_a_clean_machine_exits_zero_with_a_pass(self):
        done = self.run_script(*self.arguments())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("PASS — 10 checks", done.stdout)

    def test_a_planted_key_exits_one_with_a_fail(self):
        planted = tempfile.TemporaryDirectory(prefix="odile-audit-planted-")
        self.addCleanup(planted.cleanup)
        fixture = Fixture(planted.name, plants=("second-key",))
        done = self.run_script(
            "--root", str(fixture.root),
            "--cmdline", str(fixture.commands["cmdline"]),
            "--sshd-effective", str(fixture.commands["sshd"]),
            "--luks-dump", str(fixture.commands["luks"]),
            "--auth-log", str(fixture.commands["auth"]), "--user", "tenant")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("FAIL — 1 of 10 checks: authorized keys", done.stdout)

    def test_save_writes_the_report_at_0644_and_stdout_is_the_same_bytes(self):
        target = Path(self._tmp.name) / "handoff-audit.txt"
        done = self.run_script(*self.arguments("--save", str(target)))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(target.is_file())
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o644)
        self.assertEqual(target.read_text(), done.stdout)

    def test_json_is_the_same_facts_machine_readable(self):
        done = self.run_script(*self.arguments("--json"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        document = json.loads(done.stdout)
        self.assertTrue(document["ok"])
        self.assertEqual([check["name"] for check in document["checks"]], list(audit.CHECK_NAMES))
        self.assertEqual(document["stamp"], document["stamp"])
        self.assertIn("read from your machine", audit.render(
            self.fixture.results(), stamp=FIXED))

    def test_json_and_save_together_are_a_usage_error(self):
        done = self.run_script(*self.arguments("--json", "--save", "/tmp/x"))
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)

    def test_a_missing_fixture_file_is_exit_two(self):
        done = self.run_script("--cmdline", "/nonexistent/cmdline-208")
        self.assertEqual(done.returncode, 2)
        self.assertIn("does not exist", done.stderr)

    def test_a_missing_root_is_exit_two(self):
        done = self.run_script("--root", "/nonexistent/root-208")
        self.assertEqual(done.returncode, 2)

    def test_self_test_passes_and_names_both_directions(self):
        done = self.run_script("--self-test")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("a second key fails 'authorized keys'", done.stdout)
        self.assertIn("audit self-test: PASS", done.stdout)

    def test_version_prints_the_tool(self):
        done = self.run_script("--version")
        self.assertEqual(done.returncode, 0)
        self.assertIn("odile-audit", done.stdout)


class ContractTest(unittest.TestCase):
    """The card's quotes, checked against the script rather than the prose."""

    def test_the_report_rows_cover_every_sentence_of_the_card(self):
        source = SCRIPT.read_text(encoding="utf-8").lower()
        for sentence in ("authorized_keys", "bb.key", "passwordauthentication",
                         "operator", "keyslot", "ssh_auth_sock", "odile-support",
                         "accepted publickey"):
            self.assertIn(sentence, source, sentence)

    def test_the_report_is_the_shape_the_access_report_uses(self):
        # /verify renders the access report's lines; the audit keeps the same
        # one-line-per-check and provenance shape so the page can quote it.
        body = audit.render(Fixture(tempfile.mkdtemp(prefix="odile-audit-shape-")).results(),
                            stamp=FIXED)
        self.assertRegex(body, r"(?m)^  \S.*\s(PASS|FAIL)\s")
        self.assertRegex(body, r"(?m)^read from your machine, \d{4}-\d{2}-\d{2}T")

    def test_the_fingerprint_helper_matches_openssh(self):
        line = audit.FIXTURE_BOOT_KEY
        parsed = audit.parse_public_key(line)
        with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as handle:
            handle.write(line + "\n")
            path = handle.name
        self.addCleanup(os.unlink, path)
        done = subprocess.run(["ssh-keygen", "-lf", path], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(audit.fingerprint(parsed[1]), done.stdout)


class InstallerManifestTest(unittest.TestCase):
    """The card's fourth line: the installer ships the script and names it.

    The installer is a program rather than a library, so it is loaded the way
    `tests/test_installer_copy.py` loads it: registered in `sys.modules`
    before `exec_module` so the frozen dataclasses resolve their annotations.
    """

    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "installer" / "odile_install.py"
        spec = importlib.util.spec_from_file_location("odile_installer_audit", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["odile_installer_audit"] = module
        spec.loader.exec_module(module)
        cls.installer = module

    def test_the_embedded_copy_is_the_checkout_byte_for_byte(self):
        data = SCRIPT.read_bytes()
        self.assertEqual(self.installer.AUDIT_SCRIPT.encode("utf-8"), data)
        self.assertEqual(self.installer.AUDIT_SCRIPT_SHA256,
                         hashlib.sha256(data).hexdigest())

    def test_the_installer_manifest_names_the_audit_hash(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.installer.manifest()
        document = json.loads(output.getvalue())
        self.assertEqual(document["audit_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())

    def test_the_installer_places_it_at_0755_and_a_second_run_reads_same(self):
        with tempfile.TemporaryDirectory(prefix="odile-audit-install-") as temporary:
            layout = self.installer.Layout.scratch(
                root=Path(temporary), user="odileaudit", webapp_port=9160, public_url="http://odl-audit.test:9160")
            with contextlib.redirect_stdout(io.StringIO()):
                changed = self.installer.ensure_verify_surface(layout, False, "")
                again = self.installer.ensure_verify_surface(layout, False, "")
            target = Path(temporary) / "usr/local/bin/odile-audit"
            self.assertTrue(changed)
            self.assertEqual(target.read_bytes(), SCRIPT.read_bytes())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertFalse(again, "a converged re-run must write nothing")


if __name__ == "__main__":
    unittest.main()
